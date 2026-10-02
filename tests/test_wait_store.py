import os
import threading
import time
import unittest
import uuid
from contextlib import nullcontext
from unittest.mock import Mock

from a2a.types import a2a_pb2
from a2a.server.context import ServerCallContext

from core_agent.auth import ScopeUser
from core_agent.a2a_sdk import ScopedMemoryTaskStore
from core_agent.database import (
    PostgresDatabase,
    PostgresEventStore,
    PostgresTaskStore,
    reconcile_workflow_task,
)
from core_agent.errors import CoreError
from core_agent.workflow import (
    InMemoryWorkflowStore,
    PostgresWorkflowStore,
    WorkflowRecord,
)


class LegacyTaskProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_identity_reconciles_without_expanding_task_access(self):
        workflows = InMemoryWorkflowStore()
        tasks = ScopedMemoryTaskStore(workflow_store=workflows, legacy_identity="configured-user")
        context = ServerCallContext()
        task = a2a_pb2.Task(id="legacy-task", context_id="legacy-chat")
        task.status.state = a2a_pb2.TASK_STATE_WORKING
        await tasks.save(task, context)
        workflows.create(WorkflowRecord(
            "legacy-run", task.id, task.context_id, "default", "configured-user",
            None, "COMPLETED", 2, {"prompt": "work"}, {}, result={"message": "done"},
        ))
        projected = await tasks.get(task.id, context)
        self.assertEqual(projected.status.state, a2a_pb2.TASK_STATE_COMPLETED)
        self.assertEqual(projected.artifacts[0].parts[0].text, "done")
        self.assertIsNone(await tasks.get(task.id, ServerCallContext(user=ScopeUser("stranger"))))
        self.assertIsNone(await tasks.get(task.id, ServerCallContext(tenant="other")))
        await tasks.save(task, context)
        self.assertEqual((await tasks.get(task.id, context)).status.state, a2a_pb2.TASK_STATE_COMPLETED)


class WaitStoreTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.store = InMemoryWorkflowStore(clock=lambda: self.now)
        self.record = self.store.create(
            WorkflowRecord(
                str(uuid.uuid4()),
                str(uuid.uuid4()),
                "context",
                "tenant",
                "owner",
                None,
                "RUNNING",
                1,
                {"prompt": "wait"},
                {"turns": 1},
            ),
            budget_limits=(5, 5),
            reserve_model_turns=1,
        )
        self.token = self.lease()

    def lease(self):
        return self.store.acquire_lease(
            self.record.run_id,
            tenant_id="tenant",
            owner_id="owner",
            worker_id="worker",
            ttl=100,
        )

    def current(self):
        return self.store.get(self.record.run_id, tenant_id="tenant", owner_id="owner")

    def wait(self, *, kind="timer", source_id="call-1", deadline=200.0):
        return self.store.enter_wait(
            self.record,
            kind=kind,
            source_id=source_id,
            subject={"until": "private"} if kind == "timer" else {"task_id": "child"},
            continuation={"version": 1, "phase": "tool_wait", "call_id": source_id},
            deadline=deadline,
            snapshot=self.record.snapshot,
            lease_token=self.token,
        )

    def append(self, message_id):
        return self.store.append_inbound(
            self.record.task_id,
            tenant_id="tenant",
            owner_id="owner",
            message_id=message_id,
            context_id="context",
            content="follow up",
            provenance={},
        )

    def apply(self, *, state="RUNNING"):
        record = self.current()
        snapshot = dict(record.snapshot)
        snapshot.pop("wait_id", None)
        self.record = self.store.transition(
            record.run_id,
            tenant_id="tenant",
            owner_id="owner",
            expected_version=record.version,
            state=state,
            snapshot=snapshot,
            event_kind="wait.applied",
            lease_token=self.token,
        )
        return self.record

    def test_cancel_fences_dispatch_but_allows_recording_known_results(self):
        self.store.request_cancel(self.record.run_id, tenant_id="tenant", owner_id="owner")
        record = self.current()
        for state, event in (("EXECUTING", "tool.intent"), ("EXECUTING", "tool.nested.intent"),
                             ("MODEL_RESPONDED", "tool.intent")):
            with self.subTest(state=state, event=event):
                with self.assertRaises(CoreError) as caught:
                    self.store.transition(
                        record.run_id, tenant_id="tenant", owner_id="owner", expected_version=record.version,
                        state=state, snapshot=record.snapshot, event_kind=event,
                        lease_token=self.token, consume_tool_calls=1,
                    )
                self.assertEqual(caught.exception.code, "CANCEL_REQUESTED")
                self.assertEqual(self.current().version, record.version)
        known = self.store.transition(
            record.run_id, tenant_id="tenant", owner_id="owner", expected_version=record.version,
            state="EXECUTING", snapshot=record.snapshot, event_kind="tool.nested.completed", lease_token=self.token,
        )
        self.assertTrue(known.cancel_requested)

    def test_wait_releases_lease_deduplicates_and_resolves_once_without_charge(self):
        wait = self.wait()
        self.assertEqual(self.wait(), wait)
        self.assertEqual(self.current().state, "WAITING_TASK")
        self.assertFalse(self.store.recoverable())
        self.assertEqual(self.store.pending_waits(kind="timer"), (wait,))
        first = self.store.resolve_wait(
            wait.wait_id,
            tenant_id="tenant",
            outcome={"reason": "message", "message_id": "m1"},
        )
        version = self.current().version
        again = self.store.resolve_wait(
            wait.wait_id, tenant_id="tenant", outcome={"reason": "time"}
        )
        self.assertEqual(first.outcome, again.outcome)
        self.assertEqual(self.current().version, version)
        self.assertEqual(self.current().state, "WAITING_TASK")
        self.assertTrue(self.current().snapshot["wait_ready"])
        self.assertEqual(len(self.store.recoverable(states={"WAITING_TASK"})), 1)
        self.token = self.lease()
        applied = self.apply()
        self.assertEqual(applied.snapshot["wait_generation"], 1)
        self.assertNotIn("wait_ready", applied.snapshot)
        self.assertIsNotNone(
            self.store.get_wait(wait.wait_id, tenant_id="tenant").applied_at
        )
        self.assertEqual(self.wait(source_id="call-2").generation, 2)
        if not self.store.atomic:
            self.assertEqual(self.store._budgets[self.record.run_id][2:], [1, 0])

    def test_inbox_wakes_timer_before_or_after_commit_but_duplicate_does_not(self):
        self.append("early")
        wait = self.wait()
        self.assertEqual(wait.outcome["message_id"], "early")
        self.token = self.lease()
        current = self.current()
        snapshot = dict(current.snapshot)
        snapshot.pop("wait_id")
        self.record = self.store.consume_inbound(
            current,
            expected_version=current.version,
            snapshot=snapshot,
            sequences=[1],
            lease_token=self.token,
        )
        next_wait = self.wait(source_id="call-2")
        self.assertIsNone(next_wait.outcome)
        self.assertFalse(self.append("early")[1])
        self.assertIsNone(
            self.store.get_wait(next_wait.wait_id, tenant_id="tenant").outcome
        )
        self.append("late")
        self.assertEqual(
            self.store.get_wait(next_wait.wait_id, tenant_id="tenant").outcome[
                "message_id"
            ],
            "late",
        )
        self.assertEqual(len(self.store.pending_inbound(self.current())), 1)

    def test_followup_does_not_resolve_task_wait_and_deadline_wins(self):
        wait = self.wait(kind="task")
        self.append("message")
        self.assertIsNone(self.store.get_wait(wait.wait_id, tenant_id="tenant").outcome)
        self.now = 201
        resolved = self.store.resolve_wait(
            wait.wait_id,
            tenant_id="tenant",
            outcome={"reason": "task", "result": {"state": "completed"}},
        )
        self.assertEqual(resolved.outcome, {"reason": "timeout", "woke_at": 201})

    def test_expiry_is_bounded_and_timer_uses_time(self):
        wait = self.wait(deadline=150)
        self.assertFalse(self.store.expire_waits())
        self.now = 151
        self.assertFalse(self.store.expire_waits(limit=0))
        self.assertEqual(
            self.store.expire_waits(limit=1)[0].outcome,
            {"reason": "time", "woke_at": 151},
        )
        self.assertFalse(self.store.expire_waits())
        self.assertIsNone(
            self.store.get_wait(wait.wait_id, tenant_id="tenant").applied_at
        )

    def test_company_scope_precedes_wait_batches_and_cancel_recovery(self):
        store = InMemoryWorkflowStore(clock=lambda: self.now)
        waits, records = [], []
        for tenant in ("foreign", "foreign", "tenant"):
            self.now += 1
            record = store.create(WorkflowRecord(
                str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()), tenant,
                "owner", None, "RUNNING", 1, {"prompt": "wait"}, {},
            ))
            token = store.acquire_lease(record.run_id, tenant_id=tenant, owner_id="owner",
                                        worker_id="worker", ttl=100)
            waits.append(store.enter_wait(record, kind="timer", source_id="timer",
                subject={"until": "private"}, continuation={"version": 1, "phase": "tool_wait", "call_id": "timer"}, deadline=self.now + 10,
                snapshot=record.snapshot, lease_token=token))
            records.append(record)
        store.request_cancel(records[0].run_id, tenant_id="foreign", owner_id="owner")
        self.assertFalse(store.recoverable(tenant_id="tenant", limit=1))
        self.assertEqual(store.pending_waits(tenant_id="tenant", limit=1), (waits[2],))
        self.assertEqual(store.pending_waits(tenant_id="tenant", limit=1,
            after=(waits[1].created_at, waits[1].wait_id)), (waits[2],))
        self.now += 20
        self.assertEqual(store.expire_waits(tenant_id="tenant", limit=1)[0].wait_id, waits[2].wait_id)
        self.assertIsNone(store.get_wait(waits[0].wait_id, tenant_id="foreign").outcome)
        self.assertIsNone(store.get_wait(waits[1].wait_id, tenant_id="foreign").outcome)
        self.assertEqual([r.run_id for r in store.recoverable(tenant_id="tenant", limit=1)],
                         [records[2].run_id])
        self.assertEqual(len(store.expire_waits()), 2)
        self.assertEqual(len(store.recoverable()), 3)

    def test_unresolved_wait_cannot_be_applied_and_terminal_transition_closes_it(self):
        wait = self.wait()
        self.token = self.lease()
        with self.assertRaises(CoreError) as caught:
            self.apply()
        self.assertEqual(caught.exception.code, "INVALID_TASK_STATE")
        self.store.request_cancel(
            self.record.run_id, tenant_id="tenant", owner_id="owner"
        )
        self.store.release_lease(
            self.record.run_id, tenant_id="tenant", worker_id="worker", token=self.token
        )
        self.assertEqual(len(self.store.recoverable()), 1)
        self.token = self.lease()
        self.apply(state="CANCELLED")
        closed = self.store.get_wait(wait.wait_id, tenant_id="tenant")
        self.assertEqual(closed.outcome["reason"], "cancelled")
        self.assertIsNotNone(closed.applied_at)
        self.assertFalse(self.store.pending_waits())

    def test_scope_lease_and_cancellation_are_checked_before_wait(self):
        original_token = self.token
        self.token = "stale"
        with self.assertRaises(CoreError) as caught:
            self.wait()
        self.assertEqual(caught.exception.code, "LEASE_LOST")
        self.assertFalse(self.store.pending_waits())
        self.token = original_token
        wait = self.wait()
        for scope in (
            {"tenant_id": "other"},
            {"tenant_id": "tenant", "owner_id": "other"},
        ):
            with self.assertRaises(CoreError) as caught:
                self.store.get_wait(wait.wait_id, **scope)
            self.assertEqual(caught.exception.code, "TASK_NOT_FOUND")
        with self.assertRaises(CoreError):
            self.store.resolve_wait(
                wait.wait_id, tenant_id="other", outcome={"reason": "time"}
            )

    def test_cancel_before_admission_does_not_create_wait(self):
        self.store.request_cancel(
            self.record.run_id, tenant_id="tenant", owner_id="owner"
        )
        with self.assertRaises(CoreError) as caught:
            self.wait()
        self.assertEqual(caught.exception.code, "CANCEL_REQUESTED")
        self.assertFalse(self.store.pending_waits())
        self.assertEqual(self.current().version, 1)

    def test_public_projection_keeps_wait_private_and_fences_stale_revision(self):
        task = a2a_pb2.Task(id="task", context_id="context")
        task.status.state = a2a_pb2.TASK_STATE_SUBMITTED
        self.assertTrue(
            reconcile_workflow_task(task, run_id="run", state="WAITING_TASK", version=3)
        )
        self.assertEqual(task.status.state, a2a_pb2.TASK_STATE_WORKING)
        self.assertEqual(dict(task.metadata)["core_agent_workflow_version"], 3)
        self.assertFalse(
            reconcile_workflow_task(task, run_id="run", state="FAILED", version=2)
        )
        self.assertEqual(task.status.state, a2a_pb2.TASK_STATE_WORKING)
        self.assertTrue(
            reconcile_workflow_task(
                task, run_id="run", state="WAITING_INPUT", version=4
            )
        )
        self.assertEqual(task.status.state, a2a_pb2.TASK_STATE_WORKING)
        self.assertFalse(task.status.HasField("message"))
        self.assertTrue(
            reconcile_workflow_task(task, run_id="run", state="CANCELLED", version=5)
        )
        self.assertFalse(
            reconcile_workflow_task(task, run_id="run", state="WAITING_TASK", version=6)
        )

    def test_sdk_save_cannot_restore_stale_terminal_or_input_required_state(self):
        for incoming_version in (1, 2):
            for stale_state in (a2a_pb2.TASK_STATE_FAILED, a2a_pb2.TASK_STATE_INPUT_REQUIRED):
                with self.subTest(version=incoming_version, state=stale_state):
                    stored = a2a_pb2.Task(id="task", context_id="chat")
                    stored.status.state = a2a_pb2.TASK_STATE_WORKING
                    stored.metadata["core_agent_workflow_version"] = 2
                    incoming = a2a_pb2.Task(id="task", context_id="chat")
                    incoming.status.state = stale_state
                    incoming.status.message.parts.add(text="obsolete private reason")
                    incoming.metadata["core_agent_workflow_version"] = incoming_version
                    incoming.history.add(message_id="follow-up", role=a2a_pb2.ROLE_USER).parts.add(text="new detail")
                    saved = []

                    def execute(sql, values):
                        if "SELECT payload" in sql:
                            return Mock(fetchone=lambda: {"payload": stored.SerializeToString()})
                        if "FROM core_runs" in sql:
                            return Mock(fetchone=lambda: {"run_id": "run", "state": "RUNNING", "version": 3, "result": None, "error_code": None})
                        if "INSERT INTO core_a2a_tasks" in sql:
                            saved.append(a2a_pb2.Task.FromString(values[6]))
                        return Mock()

                    connection = Mock(execute=execute)
                    database = Mock(transaction=lambda: nullcontext(connection))
                    context = ServerCallContext(user=ScopeUser("external"), tenant="tenant")
                    PostgresTaskStore(database)._save(incoming, context)
                    self.assertEqual(saved[0].status.state, a2a_pb2.TASK_STATE_WORKING)
                    self.assertFalse(saved[0].status.HasField("message"))
                    self.assertEqual(saved[0].history[0].message_id, "follow-up")
                    self.assertEqual(dict(saved[0].metadata)["core_agent_workflow_version"], 3)
                    self.assertEqual(incoming.status.state, stale_state)

    def test_authoritative_terminal_projection_removes_obsolete_private_message(self):
        for state in ("COMPLETED", "CANCELLED"):
            with self.subTest(state=state):
                task = a2a_pb2.Task(id="task", context_id="context")
                task.status.state = a2a_pb2.TASK_STATE_FAILED
                task.status.message.parts.add(text="obsolete private reason")
                reconcile_workflow_task(task, run_id="run", state=state, version=3, authoritative=True)
                self.assertFalse(task.status.HasField("message"))

    def test_two_resolvers_share_first_outcome_and_old_generation_cannot_wake_next(
        self,
    ):
        wait = self.wait()
        barrier = threading.Barrier(3)
        outcomes = []

        def resolve(reason):
            barrier.wait()
            outcomes.append(
                self.store.resolve_wait(
                    wait.wait_id, tenant_id="tenant", outcome={"reason": reason}
                )
            )

        threads = [
            threading.Thread(target=resolve, args=(reason,))
            for reason in ("message", "time")
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=1)
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(outcomes[0].outcome, outcomes[1].outcome)
        outcomes[0].outcome["reason"] = "changed"
        self.assertNotEqual(
            self.store.get_wait(wait.wait_id, tenant_id="tenant").outcome["reason"],
            "changed",
        )
        self.token = self.lease()
        self.apply()
        next_wait = self.wait(source_id="call-2")
        self.store.resolve_wait(
            wait.wait_id, tenant_id="tenant", outcome={"reason": "time"}
        )
        self.assertIsNone(
            self.store.get_wait(next_wait.wait_id, tenant_id="tenant").outcome
        )


class WaitProjectionSDKTests(unittest.IsolatedAsyncioTestCase):
    async def test_canonical_projection_does_not_mutate_sdk_chunk_aggregate(self):
        from a2a.server.tasks import TaskManager
        from core_agent.a2a_sdk import ScopedMemoryTaskStore

        workflows = InMemoryWorkflowStore()
        record = workflows.create(WorkflowRecord("run", "task", "context", "tenant", "owner", None, "RUNNING", 1, {"prompt": "x"}, {}))
        store = ScopedMemoryTaskStore(workflow_store=workflows)
        context = ServerCallContext(user=ScopeUser("owner"), tenant="tenant")
        manager = TaskManager(store, context, "task", "context", None)
        task = a2a_pb2.Task(id="task", context_id="context")
        task.status.state = a2a_pb2.TASK_STATE_WORKING
        await manager.save_task_event(task)
        workflows.transition(record.run_id, tenant_id="tenant", owner_id="owner", expected_version=1,
                             state="COMPLETED", snapshot={}, result={"message": "abcdef"})
        for append, text in ((False, "abc"), (True, "def")):
            event = a2a_pb2.TaskArtifactUpdateEvent(task_id="task", context_id="context", append=append)
            # SQL artifacts may have a provenance-qualified ID rather than the text digest.
            event.artifact.artifact_id = "live-artifact"
            event.artifact.parts.add(text=text)
            aggregate = await manager.save_task_event(event)
        self.assertEqual([part.text for part in aggregate.artifacts[0].parts], ["abc", "def"])
        persisted = await store.get("task", context)
        self.assertEqual(persisted.status.state, a2a_pb2.TASK_STATE_COMPLETED)
        self.assertEqual(len(persisted.artifacts), 1)
        self.assertEqual([part.text for part in persisted.artifacts[0].parts], ["abcdef"])


@unittest.skipUnless(
    os.getenv("TEST_DATABASE_URL"), "set TEST_DATABASE_URL to run PostgreSQL wait tests"
)
class PostgresWaitStoreTests(unittest.TestCase):
    current = WaitStoreTests.current
    test_cancel_fences_dispatch_but_allows_recording_known_results = WaitStoreTests.test_cancel_fences_dispatch_but_allows_recording_known_results

    def setUp(self):
        self.database = PostgresDatabase(
            os.environ["TEST_DATABASE_URL"], min_size=0, max_size=4
        )
        self.addCleanup(self.database.close)
        self.database.migrate()
        self.store = PostgresWorkflowStore(self.database, clock=lambda: 1)
        self.record = self.store.create(
            WorkflowRecord(
                str(uuid.uuid4()),
                str(uuid.uuid4()),
                "context",
                "tenant",
                "owner",
                None,
                "RUNNING",
                1,
                {"prompt": "wait"},
                {"turns": 1},
            ),
            budget_limits=(5, 5),
            reserve_model_turns=1,
        )
        self.token = self.store.acquire_lease(
            self.record.run_id,
            tenant_id="tenant",
            owner_id="owner",
            worker_id="worker",
            ttl=100,
        )

    def wait(self, deadline):
        return self.store.enter_wait(
            self.record,
            kind="timer",
            source_id="call-1",
            subject={"until": "private"},
            continuation={"version": 1, "phase": "tool_wait", "call_id": "call-1"},
            deadline=deadline,
            snapshot=self.record.snapshot,
            lease_token=self.token,
        )

    def test_resolved_wait_survives_restart_with_one_atomic_checkpoint_and_event(self):
        wait = self.wait(time.time() + 100)
        self.assertEqual(self.wait(wait.deadline), wait)
        self.assertNotIn(
            self.record.run_id, {item.run_id for item in self.store.recoverable()}
        )
        self.store.append_inbound(
            self.record.task_id,
            tenant_id="tenant",
            owner_id="owner",
            message_id="wake",
            context_id="context",
            content="private",
            provenance={},
        )
        restarted = PostgresWorkflowStore(self.database)
        resolved = restarted.get_wait(wait.wait_id, tenant_id="tenant")
        self.assertEqual(resolved.outcome["reason"], "message")
        self.assertIsNone(resolved.applied_at)
        record = restarted.get(self.record.run_id, tenant_id="tenant")
        self.assertTrue(record.snapshot["wait_ready"])
        events = PostgresEventStore(self.database).events(
            record.run_id, tenant_id="tenant"
        )
        self.assertEqual(sum(event.kind == "wait.resolved" for event in events), 1)
        with self.database.pool.connection() as connection:
            checkpoint = connection.execute(
                "SELECT state FROM core_checkpoints WHERE run_id = %s", (record.run_id,)
            ).fetchone()["state"]
            budget = connection.execute(
                "SELECT used_model_turns, used_tool_calls FROM core_budget_ledgers WHERE root_run_id = %s",
                (record.run_id,),
            ).fetchone()
            outbox = connection.execute(
                "SELECT payload FROM core_outbox WHERE aggregate_id = %s",
                (record.run_id,),
            ).fetchall()
        self.assertTrue(checkpoint["wait_ready"])
        self.assertEqual(budget, {"used_model_turns": 1, "used_tool_calls": 0})
        self.assertNotIn("private", str(outbox))

    def test_deadline_is_checked_after_blocking_run_lock(self):
        wait = self.wait(time.time() + 100)
        outcomes = []
        errors = []

        def resolve():
            try:
                outcomes.append(
                    self.store.resolve_wait(
                        wait.wait_id,
                        tenant_id="tenant",
                        outcome={"reason": "message", "message_id": "racing"},
                    )
                )
            except Exception as error:
                errors.append(error)

        with self.database.transaction() as connection:
            connection.execute(
                "SELECT run_id FROM core_runs WHERE run_id = %s FOR UPDATE",
                (self.record.run_id,),
            )
            thread = threading.Thread(target=resolve)
            thread.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                with self.database.pool.connection() as probe:
                    blocked = probe.execute(
                        "SELECT 1 FROM pg_stat_activity WHERE pid <> pg_backend_pid() AND query LIKE 'SELECT * FROM core_runs%' AND cardinality(pg_blocking_pids(pid)) > 0 LIMIT 1"
                    ).fetchone()
                if blocked:
                    break
                time.sleep(0.01)
            else:
                self.fail("resolve did not block on the run lock")
            connection.execute(
                "UPDATE core_waits SET deadline = EXTRACT(EPOCH FROM clock_timestamp()) - 1 WHERE wait_id = %s",
                (wait.wait_id,),
            )
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertEqual(outcomes[0].outcome["reason"], "time")
        self.assertGreater(outcomes[0].resolved_at, 1)

    def test_two_workers_resolve_once_and_only_one_acquires_continuation_lease(self):
        wait = self.wait(time.time() + 100)
        barrier = threading.Barrier(3)
        outcomes = []
        errors = []

        def resolve(reason):
            try:
                barrier.wait(timeout=5)
                outcomes.append(
                    self.store.resolve_wait(
                        wait.wait_id, tenant_id="tenant", outcome={"reason": reason}
                    )
                )
            except Exception as error:
                errors.append(error)

        threads = [
            threading.Thread(target=resolve, args=(reason,))
            for reason in ("message", "time")
        ]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(errors)
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(outcomes[0], outcomes[1])
        token = self.store.acquire_lease(
            self.record.run_id,
            tenant_id="tenant",
            owner_id="owner",
            worker_id="winner",
            ttl=10,
        )
        with self.assertRaises(CoreError) as caught:
            self.store.acquire_lease(
                self.record.run_id,
                tenant_id="tenant",
                owner_id="owner",
                worker_id="loser",
                ttl=10,
            )
        self.assertEqual(caught.exception.code, "LEASE_LOST")
        record = self.store.get(self.record.run_id, tenant_id="tenant")
        snapshot = dict(record.snapshot)
        snapshot.pop("wait_id")
        applied = self.store.transition(
            record.run_id,
            tenant_id="tenant",
            owner_id="owner",
            expected_version=record.version,
            state="RUNNING",
            snapshot=snapshot,
            event_kind="wait.applied",
            lease_token=token,
        )
        self.assertNotIn("wait_ready", applied.snapshot)
        self.assertIsNotNone(
            self.store.get_wait(wait.wait_id, tenant_id="tenant").applied_at
        )
        self.assertEqual(
            PostgresEventStore(self.database).count(
                record.run_id, kind="wait.resolved", tenant_id="tenant"
            ),
            1,
        )

    def test_supplied_transaction_rolls_back_wait_checkpoint_and_lease_release(self):
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.database.transaction() as connection:
                wait = self.store.enter_wait(
                    self.record,
                    kind="task",
                    source_id="joined-child",
                    subject={"task_id": "child"},
                    continuation={
                        "version": 1,
                        "phase": "tool_wait",
                        "call_id": "joined-child",
                    },
                    deadline=None,
                    snapshot=self.record.snapshot,
                    lease_token=self.token,
                    connection=connection,
                )
                raise RuntimeError("rollback")
        with self.assertRaises(CoreError):
            self.store.get_wait(wait.wait_id, tenant_id="tenant")
        self.assertEqual(
            self.store.get(self.record.run_id, tenant_id="tenant").version, 1
        )
        self.wait(time.time() + 100)

    def test_task_projection_keeps_followup_history_and_cannot_downgrade_wait(self):
        context = ServerCallContext(user=ScopeUser("owner"), tenant="tenant")
        tasks = PostgresTaskStore(self.database)
        task = a2a_pb2.Task(id=self.record.task_id, context_id="context")
        task.status.state = a2a_pb2.TASK_STATE_SUBMITTED
        tasks._save(task, context)
        self.wait(time.time() + 100)
        tasks.reconcile_from_workflows()
        projected = tasks._get(task.id, context)
        self.assertEqual(projected.status.state, a2a_pb2.TASK_STATE_WORKING)
        self.assertEqual(dict(projected.metadata)["core_agent_workflow_version"], 2)
        task.history.add(message_id="followup", task_id=task.id, role=a2a_pb2.ROLE_USER)
        tasks._save(task, context)
        saved = tasks._get(task.id, context)
        self.assertEqual(saved.status.state, a2a_pb2.TASK_STATE_WORKING)
        self.assertEqual(saved.history[-1].message_id, "followup")
        self.assertEqual(dict(saved.metadata)["core_agent_workflow_version"], 2)
