import copy
import os
import threading
import unittest
import uuid
from unittest.mock import patch

from core_agent.database import PostgresDatabase
from core_agent.errors import CoreError
from core_agent.postgres_tasks import PostgresTaskScheduler
from core_agent.tasks import TaskScheduler, REMOTE_TASK_PENDING, _remote_contract


class RemoteContextBindingContract:
    def setup_scheduler(self):
        self.tenant = str(uuid.uuid4())
        self.owner = str(uuid.uuid4())
        self.contract = {
            "version": 3, "tenant_id": self.tenant, "owner_id": self.owner,
            "peer_id": str(uuid.uuid4()), "peer_revision": 1, "peer_name": "peer",
            "url": "https://peer.example/a2a", "binding": "HTTP+JSON",
            "message_id": str(uuid.uuid4()), "task": "request", "timeout_seconds": 60,
            "poll_interval_seconds": 1, "attachment_limit_bytes": 1000, "outgoing_files": [],
            "caller_scope": {"owner_id": "person", "context_id": "local-chat",
                             "task_id": "local-task", "run_id": self.owner},
            "conversation": {"version": 1, "owner_id": "person", "context_id": "local-chat", "generation": 1},
        }
        self.sends, self.outcomes = [], {}
        self.tenants = {self.tenant}
        self.before_response = None
        self.scheduler.register("remote_a2a", self.execute)
        self.addCleanup(self.scheduler.close)

    def execute(self, claim, cancel):
        current = self.scheduler.read_remote_claim(claim)
        if not current["checkpoint"]["send_started"]:
            self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"],
                checkpoint={**current["checkpoint"], "send_started": True})
            current = self.scheduler.read_remote_claim(claim)
            self.sends.append((claim.task_id, current["checkpoint"]["remote_context_id"]))
        if self.before_response:
            self.before_response(claim, current)
        state = self.outcomes.get(current["contract"]["message_id"], "completed")
        if state == "working_no_context":
            return REMOTE_TASK_PENDING
        if state in {"unknown", "timeout", "cancel_unknown"}:
            self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"],
                checkpoint=current["checkpoint"], outcome=("failed", {"remote_outcome": "unknown"},
                    "REMOTE_OPERATION_TIMEOUT" if state == "timeout" else "SIDE_EFFECT_UNKNOWN"))
            return REMOTE_TASK_PENDING
        checkpoint = {**current["checkpoint"], "remote_task_id": "remote-" + claim.task_id,
                      "remote_context_id": current["checkpoint"]["remote_context_id"] or "context-" + claim.task_id,
                      "next_poll_at": current["now"]}
        self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"], checkpoint=checkpoint,
            outcome=None if state == "working" else (state, {"remote_state": "TASK_STATE_" + state.upper()},
                                                      "REMOTE_TASK_FAILED" if state == "failed" else None))
        return REMOTE_TASK_PENDING

    def settle(self):
        with self.scheduler._lock:
            threads = tuple(self.scheduler._threads)
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())

    def start(self, *, state="completed", contract=None, wait=True):
        contract = copy.deepcopy(self.contract if contract is None else contract)
        contract["message_id"] = str(uuid.uuid4())
        self.outcomes[contract["message_id"]] = state
        self.tenants.add(contract["tenant_id"])
        task = self.scheduler.start_remote(contract, owner_id=contract["owner_id"], tenant_id=contract["tenant_id"],
                                           task_id=str(uuid.uuid4()))
        if wait:
            self.settle()
        return task.id

    def recover(self):
        self.scheduler.recover()
        self.settle()

    def test_sequential_terminal_tasks_reuse_context_and_start_deadline_at_dispatch(self):
        first = self.start(state="failed")
        second = self.start()
        self.assertEqual(self.sends, [(first, None), (second, "context-" + first)])
        self.assertEqual(self.snapshot(second)["checkpoint"]["remote_context_id"], "context-" + first)

    def test_concurrent_first_operations_serialize_until_terminal(self):
        barrier, identifiers, failures = threading.Barrier(3), [], []

        def start():
            try:
                barrier.wait(5)
                identifiers.append(self.start(state="working", wait=False))
            except BaseException as error:
                failures.append(error)

        threads = [threading.Thread(target=start) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(5)
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.settle()
        self.assertEqual(len(self.sends), 1)
        active = self.sends[0][0]
        queued = next(identifier for identifier in identifiers if identifier != active)
        self.assertEqual(self.snapshot(queued)["state"], "submitted")
        self.assertIsNone(self.snapshot(queued)["checkpoint"]["deadline"])
        self.outcomes[self.snapshot(active)["contract"]["message_id"]] = "completed"
        self.recover()
        self.recover()
        self.assertEqual(self.sends, [(active, None), (queued, "context-" + active)])

    def test_unknown_outcome_blocks_queued_and_new_operations_without_dispatch(self):
        first = self.start(state="working")
        queued = self.start()
        self.outcomes[self.snapshot(first)["contract"]["message_id"]] = "unknown"
        self.recover()
        self.recover()
        fresh = self.start()
        for identifier in (queued, fresh):
            row = self.snapshot(identifier)
            self.assertEqual((row["state"], row["error_code"]), ("failed", "REMOTE_CONTEXT_UNRESOLVED"))
            self.assertFalse(row["checkpoint"]["send_started"])
            self.assertIsNone(row["checkpoint"]["deadline"])
        self.assertEqual(len(self.sends), 1)

    def test_timeout_and_unconfirmed_cancel_block_following_send(self):
        for state in ("timeout", "cancel_unknown"):
            with self.subTest(state=state):
                contract = copy.deepcopy(self.contract)
                contract["conversation"]["context_id"] = contract["caller_scope"]["context_id"] = state
                self.start(state=state, contract=contract)
                second = self.start(contract=contract)
                self.assertEqual(self.snapshot(second)["error_code"], "REMOTE_CONTEXT_UNRESOLVED")
                self.assertFalse(self.snapshot(second)["checkpoint"]["send_started"])

    def test_confirmed_cancellation_releases_context(self):
        first = self.start(state="canceled")
        second = self.start()
        self.assertEqual(self.sends[-1], (second, "context-" + first))

    def test_scope_peer_generation_and_binding_do_not_share_context(self):
        self.start()
        variants = []
        for field, value in (("tenant_id", str(uuid.uuid4())), ("peer_id", str(uuid.uuid4())), ("binding", "JSONRPC")):
            variants.append({**copy.deepcopy(self.contract), field: value})
        for field in ("owner_id", "context_id"):
            contract = copy.deepcopy(self.contract)
            contract["conversation"][field] = contract["caller_scope"][field] = "different"
            variants.append(contract)
        contract = copy.deepcopy(self.contract)
        contract["peer_revision"] = contract["conversation"]["generation"] = 2
        variants.append(contract)
        for contract in variants:
            with self.subTest(contract=contract):
                identifier = self.start(contract=contract)
                self.assertIn((identifier, None), self.sends)

    def test_failed_cas_does_not_publish_context(self):
        def stale(claim, current):
            with self.assertRaises(CoreError) as error:
                self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"] - 1,
                    checkpoint={**current["checkpoint"], "remote_context_id": "incorrect"})
            self.assertEqual(error.exception.code, "SESSION_CONFLICT")

        self.before_response = stale
        first = self.start()
        self.before_response = None
        second = self.start()
        self.assertEqual(self.sends[-1], (second, "context-" + first))

    def test_existing_context_cannot_be_replaced(self):
        first = self.start()

        def replacement(claim, current):
            with self.assertRaises(CoreError) as error:
                self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"],
                    checkpoint={**current["checkpoint"], "remote_context_id": "different-context"})
            self.assertEqual(error.exception.code, "CHECKPOINT_INVALID")

        self.before_response = replacement
        second = self.start()
        self.assertEqual(self.snapshot(second)["state"], "completed")
        self.assertEqual(self.sends[-1], (second, "context-" + first))

    def test_legacy_completed_checkpoint_bootstraps_context_without_mutation(self):
        legacy = copy.deepcopy(self.contract)
        legacy.pop("conversation")
        first = self.start(contract=legacy)
        before = self.snapshot(first)
        second = self.start()
        self.assertEqual(self.sends[-1], (second, "context-" + first))
        self.assertEqual(self.snapshot(first), before)

    def test_legacy_active_checkpoint_reserves_context_until_terminal(self):
        legacy = copy.deepcopy(self.contract)
        legacy.pop("conversation")
        first = self.start(state="working", contract=legacy)
        second = self.start()
        self.assertEqual(self.snapshot(second)["state"], "submitted")
        self.assertIsNone(self.snapshot(second)["checkpoint"]["deadline"])
        self.outcomes[self.snapshot(first)["contract"]["message_id"]] = "completed"
        self.recover()
        self.recover()
        self.assertEqual(self.sends[-1], (second, "context-" + first))

    def test_legacy_unknown_checkpoint_blocks_bootstrapped_context(self):
        legacy = copy.deepcopy(self.contract)
        legacy.pop("conversation")
        first = self.start(state="working", contract=legacy)
        self.outcomes[self.snapshot(first)["contract"]["message_id"]] = "unknown"
        self.recover()
        second = self.start()
        self.assertEqual(self.snapshot(second)["error_code"], "REMOTE_CONTEXT_UNRESOLVED")
        self.assertEqual(len(self.sends), 1)

    def test_legacy_active_without_context_holds_queue_then_unknown_blocks(self):
        legacy = copy.deepcopy(self.contract)
        legacy.pop("conversation")
        first = self.start(state="working_no_context", contract=legacy)
        second = self.start()
        self.assertEqual(self.snapshot(second)["state"], "submitted")
        self.assertFalse(self.snapshot(second)["checkpoint"]["send_started"])
        self.outcomes[self.snapshot(first)["contract"]["message_id"]] = "unknown"
        self.recover()
        self.recover()
        self.assertEqual(self.snapshot(second)["error_code"], "REMOTE_CONTEXT_UNRESOLVED")
        self.assertEqual(len(self.sends), 1)

    def test_legacy_unknown_without_context_blocks_new_initial_send(self):
        legacy = copy.deepcopy(self.contract)
        legacy.pop("conversation")
        self.start(state="unknown", contract=legacy)
        second = self.start()
        self.assertEqual(self.snapshot(second)["error_code"], "REMOTE_CONTEXT_UNRESOLVED")
        self.assertEqual(len(self.sends), 1)

    def test_legacy_active_without_context_later_publishes_context_to_waiter(self):
        legacy = copy.deepcopy(self.contract)
        legacy.pop("conversation")
        first = self.start(state="working_no_context", contract=legacy)
        second = self.start()
        self.assertEqual(self.snapshot(second)["state"], "submitted")
        self.outcomes[self.snapshot(first)["contract"]["message_id"]] = "completed"
        self.recover()
        self.recover()
        self.assertEqual(self.sends, [(first, None), (second, "context-" + first)])

    def test_conversation_validation_rejects_foreign_or_malformed_scope(self):
        for changes in ({"version": True}, {"generation": 0}, {"generation": 2},
                        {"owner_id": "foreign"}, {"context_id": "foreign"}, {"extra": "value"}):
            with self.subTest(changes=changes):
                contract = copy.deepcopy(self.contract)
                contract["conversation"].update(changes)
                with self.assertRaises(CoreError) as error:
                    _remote_contract(contract, self.tenant, self.owner)
                self.assertEqual(error.exception.code, "CHECKPOINT_INVALID")


class MemoryRemoteContextBindingTests(RemoteContextBindingContract, unittest.TestCase):
    def setUp(self):
        self.scheduler = TaskScheduler()
        self.setup_scheduler()

    def snapshot(self, task_id):
        row = self.scheduler._remote[task_id]
        task = self.scheduler._tasks[task_id]
        return {"contract": copy.deepcopy(row["contract"]), "checkpoint": copy.deepcopy(row["checkpoint"]),
                "state": task.state, "error_code": getattr(task.error, "code", None)}


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "TEST_DATABASE_URL required for remote context durability")
class PostgresRemoteContextBindingTests(RemoteContextBindingContract, unittest.TestCase):
    def setUp(self):
        self.database = PostgresDatabase(os.environ["TEST_DATABASE_URL"], min_size=0, max_size=4)
        self.database.migrate()
        self.addCleanup(self.database.close)
        self.scheduler = PostgresTaskScheduler(self.database)
        self.setup_scheduler()
        self.addCleanup(self.cleanup_rows)

    def cleanup_rows(self):
        self.scheduler.close()
        with self.database.transaction() as connection:
            for tenant in self.tenants:
                connection.execute("DELETE FROM core_notifications WHERE tenant_id=%s", (tenant,))
                connection.execute("DELETE FROM core_outbox WHERE tenant_id=%s", (tenant,))
                connection.execute("DELETE FROM core_background_tasks WHERE tenant_id=%s", (tenant,))
                connection.execute("DELETE FROM core_remote_context_bindings WHERE tenant_id=%s", (tenant,))

    def snapshot(self, task_id):
        with self.database.pool.connection() as connection:
            return dict(connection.execute("""SELECT contract,checkpoint,state,error_code FROM core_background_tasks
                WHERE id=%s""", (task_id,)).fetchone())

    def test_binding_survives_scheduler_restart(self):
        first = self.start()
        self.scheduler.close()
        self.scheduler = PostgresTaskScheduler(self.database)
        self.scheduler.register("remote_a2a", self.execute)
        self.addCleanup(self.scheduler.close)
        second = self.start()
        self.assertEqual(self.sends[-1], (second, "context-" + first))


class RemoteContextFileCommitTests(unittest.TestCase):
    def check_file_rollback(self, fixture_type):
        fixture = fixture_type("test_bind_failure_after_acceptance_rolls_back_and_leaves_no_terminal_notification")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        conversation = {"version": 1, "owner_id": fixture.person, "context_id": fixture.context, "generation": 1}
        task, claim, current, prepared, release = fixture.hold_claim(conversation=conversation)
        current["checkpoint"]["remote_context_id"] = "response-context"

        def saved_binding():
            if hasattr(fixture, "database"):
                with fixture.database.pool.connection() as connection:
                    return connection.execute("SELECT remote_context_id,active_operation_id,blocked FROM core_remote_context_bindings WHERE tenant_id=%s",
                        (fixture.tenant,)).fetchone()
            return next(iter(fixture.scheduler._remote_contexts.values()))

        method = "_remote_finish_locked" if hasattr(fixture, "database") else "_finish_remote_locked"
        original = getattr(fixture.scheduler, method)

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise CoreError("INJECTED_NOTIFICATION_FAILURE")

        with patch.object(fixture.scheduler, method, side_effect=fail), self.assertRaises(CoreError):
            fixture.commit_files(claim, current, prepared)
        self.assertEqual(saved_binding(), {"remote_context_id": None, "active_operation_id": task.id, "blocked": False})
        self.assertIsNone(fixture.scheduler.read_remote_claim(claim)["checkpoint"]["remote_context_id"])
        self.assertEqual(fixture.store.get(prepared["batch_id"], fixture.tenant)["state"], "staging")
        fixture.commit_files(claim, current, prepared)
        self.assertEqual(saved_binding(), {"remote_context_id": "response-context", "active_operation_id": None, "blocked": False})
        self.assertEqual(fixture.store.get(prepared["batch_id"], fixture.tenant)["state"], "accepted_quarantine")
        release.set()
        fixture.settle()

    def test_memory_file_notification_failure_rolls_back_context_and_reservation(self):
        from tests.test_remote_inbound_files import MemoryRemoteInboundTests
        self.check_file_rollback(MemoryRemoteInboundTests)

    @unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "TEST_DATABASE_URL required for remote context durability")
    def test_postgres_file_notification_failure_rolls_back_context_and_reservation(self):
        from tests.test_remote_inbound_files import PostgresRemoteInboundTests
        self.check_file_rollback(PostgresRemoteInboundTests)
