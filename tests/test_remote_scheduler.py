import copy
import os
import threading
import time
import unittest
import uuid
from dataclasses import replace

from core_agent.database import PostgresDatabase
from core_agent.errors import CoreError
from core_agent.postgres_tasks import PostgresTaskScheduler
from core_agent.tasks import REMOTE_TASK_PENDING, TaskScheduler, remote_timeout_result


class RemoteSchedulerContract:
    def contract(self, **changes):
        return {"version": 1, "tenant_id": self.tenant, "owner_id": self.owner,
                "peer_id": "peer", "peer_revision": 1, "peer_name": "delivery",
                "url": "https://peer.example/a2a", "binding": "HTTP+JSON",
                "message_id": str(uuid.uuid4()), "task": "Deliver the task",
                "timeout_seconds": 86400, "poll_interval_seconds": 300, **changes}

    def settle(self, scheduler=None):
        scheduler = scheduler or self.scheduler
        with scheduler._lock:
            threads = tuple(scheduler._threads)
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())

    def start(self, handler, **contract):
        self.scheduler.register("remote_a2a", handler)
        task = self.scheduler.start_remote(self.contract(**contract), owner_id=self.owner,
            tenant_id=self.tenant, task_id=str(uuid.uuid4()), required=True)
        self.settle()
        return self.scheduler.get(task.id, owner_id=self.owner, tenant_id=self.tenant)

    def test_send_checkpoint_due_recovery_and_one_terminal_notification(self):
        seen = []

        def handler(claim, cancel):
            current = self.scheduler.read_remote_claim(claim)
            seen.append(current)
            checkpoint = current["checkpoint"]
            if not checkpoint["send_started"]:
                checkpoint["send_started"] = True
                self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"], checkpoint=checkpoint)
                current = self.scheduler.read_remote_claim(claim)
                checkpoint = current["checkpoint"]
                self.assertAlmostEqual(checkpoint["deadline"] - current["now"], 86400, delta=2)
                checkpoint.update(remote_task_id="remote", remote_context_id="context", next_poll_at=current["now"] + 300)
                self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"], checkpoint=checkpoint)
            else:
                self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"], checkpoint=checkpoint,
                    outcome=("completed", {"answer": "done"}, None))
            return REMOTE_TASK_PENDING

        task = self.start(handler)
        self.assertEqual((task.state, task.revision), ("working", 2))
        self.assertTrue(self.scheduler.is_remote(task.id, owner_id=self.owner, tenant_id=self.tenant))
        self.assertEqual(self.scheduler.mailbox(self.owner, self.tenant).poll(), ())
        self.assertEqual(self.scheduler.recover(ready=lambda *_: True), 0)
        self.advance(task.id, 301)
        self.scheduler.recover(ready=lambda *_: True)
        self.settle()
        completed = self.scheduler.get(task.id, owner_id=self.owner, tenant_id=self.tenant)
        self.assertEqual((completed.state, completed.result, completed.revision), ("completed", {"answer": "done"}, 3))
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(self.scheduler.mailbox(self.owner, self.tenant).poll()), 1)
        self.assertEqual(self.scheduler.mailbox(self.owner, self.tenant + "other").poll(), ())
        self.assertEqual(self.scheduler.recover(ready=lambda *_: True), 0)

    def test_live_request_expiry_wins_over_late_result_and_cancel(self):
        entered, release = threading.Event(), threading.Event()
        late = []

        def handler(claim, cancel):
            current = self.scheduler.read_remote_claim(claim)
            checkpoint = {**current["checkpoint"], "send_started": True}
            self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"], checkpoint=checkpoint)
            current = self.scheduler.read_remote_claim(claim)
            entered.set()
            release.wait(3)
            late.append(self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"], checkpoint=current["checkpoint"],
                outcome=("completed", {"answer": "too late"}, None)))
            return REMOTE_TASK_PENDING

        self.scheduler.register("remote_a2a", handler)
        task = self.scheduler.start_remote(self.contract(timeout_seconds=1), owner_id=self.owner,
            tenant_id=self.tenant, task_id=str(uuid.uuid4()))
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(3))
        self.advance(task.id, 2)
        self.assertEqual(self.scheduler.expire_remote(limit=1), 1)
        release.set()
        self.settle()
        result = self.scheduler.get(task.id, owner_id=self.owner, tenant_id=self.tenant)
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error.code, "REMOTE_OPERATION_TIMEOUT")
        self.assertEqual(result.result, remote_timeout_result(self.contract()))
        self.assertEqual(late[0].result, result.result)
        self.assertEqual(self.scheduler.cancel(task.id, owner_id=self.owner, tenant_id=self.tenant).result, result.result)
        self.assertEqual(self.scheduler.expire_remote(), 0)
        self.assertEqual(len(self.scheduler.mailbox(self.owner, self.tenant).poll()), 1)

    def test_claim_scope_cas_checkpoint_immutability_and_copies(self):
        errors = []

        def handler(claim, cancel):
            current = self.scheduler.read_remote_claim(claim)
            detached = self.scheduler.read_remote_claim(claim)
            detached["contract"]["task"] = "mutated"
            detached["checkpoint"]["send_started"] = True
            fresh = self.scheduler.read_remote_claim(claim)
            self.assertEqual({k: v for k, v in fresh.items() if k != "now"},
                             {k: v for k, v in current.items() if k != "now"})
            for forged in (replace(claim, tenant_id="other"), replace(claim, owner_id="other"), replace(claim, token="wrong")):
                with self.assertRaises(CoreError):
                    self.scheduler.read_remote_claim(forged)
            for changes in ({"version": 2}, {"send_started": 1}, {"next_poll_at": float("nan")},
                            {"remote_task_id": "\x00"}, {"remote_context_id": "\ud800"}, {"extra": True},
                            {"send_started": True, "deadline": 12}, {"next_poll_at": 10**500}):
                with self.assertRaises(CoreError) as error:
                    self.scheduler.commit_remote_claim(claim, expected_revision=0, checkpoint={**current["checkpoint"], **changes})
                errors.append(error.exception.code)
            checkpoint = {**current["checkpoint"], "send_started": True}
            self.scheduler.commit_remote_claim(claim, expected_revision=0, checkpoint=checkpoint)
            with self.assertRaises(CoreError):
                self.scheduler.commit_remote_claim(claim, expected_revision=0, checkpoint=checkpoint)
            current = self.scheduler.read_remote_claim(claim)
            for changes in ({"send_started": False}, {"deadline": current["checkpoint"]["deadline"] + 1}):
                with self.assertRaises(CoreError):
                    self.scheduler.commit_remote_claim(claim, expected_revision=1, checkpoint={**current["checkpoint"], **changes})
            checkpoint = {**current["checkpoint"], "remote_task_id": "remote", "remote_context_id": "context"}
            self.scheduler.commit_remote_claim(claim, expected_revision=1, checkpoint=checkpoint)
            with self.assertRaises(CoreError):
                self.scheduler.commit_remote_claim(claim, expected_revision=2, checkpoint={**checkpoint, "remote_task_id": "replaced"})
            return REMOTE_TASK_PENDING

        task = self.start(handler)
        self.assertEqual(len(errors), 8)
        for kwargs in ({"owner_id": self.owner, "tenant_id": "other"}, {"owner_id": "other", "tenant_id": self.tenant},
                       {"owner_id": None, "tenant_id": self.tenant}):
            for method in (self.scheduler.get, self.scheduler.is_remote, self.scheduler.cancel):
                with self.assertRaises(CoreError) as error:
                    method(task.id, **kwargs)
                self.assertEqual(error.exception.code, "TASK_NOT_FOUND")
        self.assertEqual(self.scheduler.list(owner_id=self.owner, tenant_id="other"), ())

    def test_contract_validation_prevents_admission(self):
        self.scheduler.register("remote_a2a", lambda *_: REMOTE_TASK_PENDING)
        for changes in ({"version": 2}, {"tenant_id": "other"}, {"owner_id": "other"}, {"headers": {}},
                        {"timeout_seconds": True}, {"poll_interval_seconds": 0}, {"binding": "unknown"},
                        {"message_id": "\ud800"}, {"peer_id": "\x00"}, {"task": ""}, {"binding": []},
                        {"_trace_parent": {"authorization": "private"}}):
            with self.subTest(changes=changes), self.assertRaises(CoreError):
                self.scheduler.start_remote(self.contract(**changes), owner_id=self.owner,
                    tenant_id=self.tenant, task_id=str(uuid.uuid4()))
        self.assertEqual(self.scheduler.list(owner_id=self.owner, tenant_id=self.tenant), ())

    def test_released_claim_cannot_commit_and_recovery_changes_token(self):
        claims = []
        def handler(claim, cancel):
            claims.append(claim)
            return REMOTE_TASK_PENDING
        task = self.start(handler)
        for forged in (claims[0], replace(claims[0], token=None)):
            with self.assertRaises(CoreError) as error:
                self.scheduler.read_remote_claim(forged)
            self.assertEqual(error.exception.code, "LEASE_LOST")
        self.scheduler.recover()
        self.settle()
        self.assertEqual(len(claims), 2)
        self.assertNotEqual(claims[0].token, claims[1].token)
        self.assertEqual(self.scheduler.get(task.id, owner_id=self.owner, tenant_id=self.tenant).state, "working")

    def test_local_and_remote_notifications_share_parent_view(self):
        local = self.scheduler.start(lambda: "local", owner_id=self.owner, tenant_id=self.tenant,
                                     kind="local", contract={})
        self.settle()
        def handler(claim, cancel):
            current = self.scheduler.read_remote_claim(claim)
            self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"], checkpoint=current["checkpoint"],
                                               outcome=("completed", "remote", None))
            return REMOTE_TASK_PENDING
        remote = self.start(handler)
        mailbox = self.scheduler.mailbox(self.owner, self.tenant)
        self.assertEqual({event.task_id for event in mailbox.poll()}, {local.id, remote.id})
        for event in mailbox.poll():
            mailbox.ack(event.id)
        self.assertEqual(mailbox.poll(), ())

    def test_cancel_before_send_prevents_intent_and_preserves_reconciliation(self):
        observed = []
        def handler(claim, cancel):
            current = self.scheduler.read_remote_claim(claim)
            if not current["cancel_requested"]:
                return REMOTE_TASK_PENDING
            with self.assertRaises(CoreError) as error:
                self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"],
                    checkpoint={**current["checkpoint"], "send_started": True})
            observed.append(error.exception.code)
            self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"], checkpoint=current["checkpoint"],
                outcome=("failed", {"remote_outcome": "unknown"}, "SIDE_EFFECT_UNKNOWN"))
            return REMOTE_TASK_PENDING
        task = self.start(handler)
        self.scheduler.cancel(task.id, owner_id=self.owner, tenant_id=self.tenant)
        self.scheduler.recover()
        self.settle()
        self.assertEqual(observed, ["CANCEL_REQUESTED"])
        task = self.scheduler.get(task.id, owner_id=self.owner, tenant_id=self.tenant)
        self.assertEqual((task.state, task.error.code), ("failed", "SIDE_EFFECT_UNKNOWN"))
        self.assertEqual(self.scheduler.cancel(task.id, owner_id=self.owner, tenant_id=self.tenant).error.code, "SIDE_EFFECT_UNKNOWN")

    def test_close_fences_remote_claim_without_cancel_or_terminal_result(self):
        entered, release = threading.Event(), threading.Event()
        claims, rejected = [], []
        def handler(claim, cancel):
            claims.append(claim)
            current = self.scheduler.read_remote_claim(claim)
            entered.set()
            release.wait(3)
            try:
                self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"],
                    checkpoint=current["checkpoint"], outcome=("completed", "late", None))
            except CoreError as error:
                rejected.append(error.code)
            return REMOTE_TASK_PENDING
        self.scheduler.register("remote_a2a", handler)
        task = self.scheduler.start_remote(self.contract(), owner_id=self.owner, tenant_id=self.tenant, task_id=str(uuid.uuid4()))
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(3))
        # Close runs separately because the durable scheduler joins its worker.
        closed = threading.Thread(target=self.scheduler.close)
        closed.start()
        for _ in range(100):
            if self.scheduler._closed:
                break
            time.sleep(.001)
        release.set()
        closed.join(3)
        self.settle()
        self.assertEqual(rejected, ["LEASE_LOST"])
        self.assertEqual(self.scheduler.get(task.id, owner_id=self.owner, tenant_id=self.tenant).state, "working")
        self.assertEqual(self.scheduler.mailbox(self.owner, self.tenant).poll(), ())

    def test_send_intent_survives_worker_stop_and_recovery_without_duplicate_send(self):
        sent, observed = [], []
        def handler(claim, cancel):
            current = self.scheduler.read_remote_claim(claim)
            if not current["checkpoint"]["send_started"]:
                self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"],
                    checkpoint={**current["checkpoint"], "send_started": True})
                sent.append("physical request")
                raise CoreError("WORKER_STOPPED")
            observed.append(current)
            self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"], checkpoint=current["checkpoint"],
                outcome=("failed", None, "SIDE_EFFECT_UNKNOWN"))
            return REMOTE_TASK_PENDING
        task = self.start(handler)
        self.assertEqual(task.state, "working")
        self.restart(handler)
        self.scheduler.recover()
        self.settle()
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["revision"], 1)
        self.assertIsNone(observed[0]["checkpoint"]["remote_task_id"])
        self.assertEqual(self.scheduler.get(task.id, owner_id=self.owner, tenant_id=self.tenant).error.code, "SIDE_EFFECT_UNKNOWN")

    def test_competing_recovery_workers_claim_once_and_recheck_due(self):
        observed = []
        entered, release = threading.Event(), threading.Event()
        def handler(claim, cancel, scheduler=None):
            scheduler = scheduler or self.scheduler
            current = scheduler.read_remote_claim(claim)
            observed.append(claim.token)
            if len(observed) > 1:
                entered.set()
                release.wait(3)
            if not current["checkpoint"]["send_started"]:
                scheduler.commit_remote_claim(claim, expected_revision=current["revision"],
                    checkpoint={**current["checkpoint"], "send_started": True})
                current = scheduler.read_remote_claim(claim)
            scheduler.commit_remote_claim(claim, expected_revision=current["revision"],
                checkpoint={**current["checkpoint"], "remote_task_id": "known", "next_poll_at": current["now"] + 300})
            return REMOTE_TASK_PENDING
        task = self.start(handler)
        self.advance(task.id, 301)
        self.addCleanup(release.set)
        other = self.competitor(lambda claim, cancel: handler(claim, cancel, other))
        calls = [threading.Thread(target=scheduler.recover) for scheduler in (self.scheduler, other)]
        for thread in calls:
            thread.start()
        self.assertTrue(entered.wait(3))
        for thread in calls:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        release.set()
        self.settle()
        self.settle(other)
        self.assertEqual(len(observed), 2)
        self.assertEqual(self.scheduler.recover(), 0)

    def test_read_expires_without_dispatch(self):
        failures = []
        def handler(claim, cancel):
            current = self.scheduler.read_remote_claim(claim)
            self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"],
                checkpoint={**current["checkpoint"], "send_started": True})
            self.advance(claim.task_id, 2)
            try:
                self.scheduler.read_remote_claim(claim)
            except CoreError as error:
                failures.append(error.code)
            return REMOTE_TASK_PENDING
        task = self.start(handler, timeout_seconds=1)
        self.assertEqual(failures, ["LEASE_LOST"])
        self.assertEqual(task.error.code, "REMOTE_OPERATION_TIMEOUT")
        self.assertEqual(self.scheduler.expire_remote(), 0)

    def test_timeout_sweep_is_bounded_and_does_not_repeat_notifications(self):
        def handler(claim, cancel):
            current = self.scheduler.read_remote_claim(claim)
            self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"],
                checkpoint={**current["checkpoint"], "send_started": True})
            return REMOTE_TASK_PENDING
        first = self.start(handler, timeout_seconds=1)
        second = self.scheduler.start_remote(self.contract(timeout_seconds=1), owner_id=self.owner,
            tenant_id=self.tenant, task_id=str(uuid.uuid4()))
        self.settle()
        self.advance(first.id, 2)
        self.advance(second.id, 2)
        self.assertEqual(self.scheduler.expire_remote(limit=1), 1)
        self.assertEqual(self.scheduler.expire_remote(limit=1), 1)
        self.assertEqual(self.scheduler.expire_remote(limit=1), 0)
        self.assertEqual(len(self.scheduler.mailbox(self.owner, self.tenant).poll()), 2)

    def test_worker_cancel_event_does_not_authorize_remote_cancel(self):
        observed = []
        def handler(claim, cancel):
            cancel.set()
            observed.append(self.scheduler.read_remote_claim(claim)["cancel_requested"])
            return REMOTE_TASK_PENDING
        task = self.start(handler)
        self.assertEqual(observed, [False])
        self.assertEqual(task.state, "working")

    def test_failed_admission_leaves_no_task_or_worker(self):
        calls = []
        self.scheduler.register("remote_a2a", lambda *_: calls.append("executed"))
        def reject(connection):
            raise CoreError("SESSION_CONFLICT")
        with self.assertRaises(CoreError):
            self.scheduler.start_remote(self.contract(), owner_id=self.owner, tenant_id=self.tenant,
                task_id=str(uuid.uuid4()), admission=reject)
        self.settle()
        self.assertEqual(calls, [])
        self.assertEqual(self.scheduler.list(owner_id=self.owner, tenant_id=self.tenant), ())

    def test_progress_is_atomic_scoped_metadata_without_terminal_notification(self):
        expected = {"agent_name": "delivery", "remote_state": "TASK_STATE_INPUT_REQUIRED"}
        def handler(claim, cancel):
            current = self.scheduler.read_remote_claim(claim)
            self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"],
                                              checkpoint={**current["checkpoint"], "send_started": True})
            current = self.scheduler.read_remote_claim(claim)
            with self.assertRaises(CoreError):
                self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"],
                                                  checkpoint=current["checkpoint"], progress=expected)
            checkpoint = {**current["checkpoint"], "remote_task_id": "remote", "remote_context_id": "context",
                          "next_poll_at": current["now"] + 300}
            self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"], checkpoint=checkpoint,
                                              progress=expected)
            current = self.scheduler.read_remote_claim(claim)
            for progress in ({**expected, "text": "untrusted"}, {**expected, "agent_name": "other"},
                             {**expected, "remote_state": "TASK_STATE_COMPLETED"}, {**expected, "remote_state": 1},
                             {"remote_state": "TASK_STATE_WORKING"}):
                with self.assertRaises(CoreError):
                    self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"],
                        checkpoint={**checkpoint, "next_poll_at": current["now"] + 600}, progress=progress)
                fresh = self.scheduler.read_remote_claim(claim)
                self.assertEqual((fresh["revision"], fresh["checkpoint"]), (current["revision"], checkpoint))
            with self.assertRaises(CoreError):
                self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"], checkpoint=checkpoint,
                    progress=expected, outcome=("completed", "done", None))
            return REMOTE_TASK_PENDING
        task = self.start(handler)
        self.assertEqual((task.state, task.revision, task.result), ("working", 2, expected))
        self.assertEqual(self.scheduler.mailbox(self.owner, self.tenant).poll(), ())
        self.assertEqual(self.scheduler.list(owner_id=self.owner, tenant_id="other"), ())

    def test_internal_progress_read_is_scoped_detached_and_excludes_expired_rows(self):
        def handler(claim, cancel):
            current = self.scheduler.read_remote_claim(claim)
            self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"],
                                              checkpoint={**current["checkpoint"], "send_started": True})
            current = self.scheduler.read_remote_claim(claim)
            self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"],
                checkpoint={**current["checkpoint"], "remote_task_id": "remote", "remote_context_id": "context"},
                progress={"agent_name": "delivery", "remote_state": "TASK_STATE_INPUT_REQUIRED"})
            return REMOTE_TASK_PENDING
        task = self.start(handler, timeout_seconds=10)
        expected = {"task_id": task.id, "revision": 2, "agent_name": "delivery", "remote_state": "TASK_STATE_INPUT_REQUIRED"}
        self.assertEqual(self.scheduler.remote_progress(owner_id=self.owner, tenant_id=self.tenant), (expected,))
        self.assertEqual(self.scheduler.remote_progress(owner_id="other", tenant_id=self.tenant), ())
        self.assertEqual(self.scheduler.remote_progress(owner_id=self.owner, tenant_id="other"), ())
        detached = self.scheduler.remote_progress(owner_id=self.owner, tenant_id=self.tenant)
        detached[0]["remote_state"] = "forged"
        self.assertEqual(self.scheduler.remote_progress(owner_id=self.owner, tenant_id=self.tenant), (expected,))
        self.advance(task.id, 11)
        self.assertEqual(self.scheduler.remote_progress(owner_id=self.owner, tenant_id=self.tenant), ())

    def test_timeout_ignores_late_progress_and_preserves_one_terminal_notification(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        late = []
        def handler(claim, cancel):
            current = self.scheduler.read_remote_claim(claim)
            self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"],
                                              checkpoint={**current["checkpoint"], "send_started": True})
            current = self.scheduler.read_remote_claim(claim)
            checkpoint = {**current["checkpoint"], "remote_task_id": "remote", "remote_context_id": "context"}
            self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"], checkpoint=checkpoint,
                progress={"agent_name": "delivery", "remote_state": "TASK_STATE_WORKING"})
            current = self.scheduler.read_remote_claim(claim)
            entered.set()
            release.wait(3)
            late.append(self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"],
                checkpoint=current["checkpoint"], progress={"agent_name": "delivery", "remote_state": "TASK_STATE_INPUT_REQUIRED"}))
            return REMOTE_TASK_PENDING
        self.scheduler.register("remote_a2a", handler)
        task = self.scheduler.start_remote(self.contract(timeout_seconds=1), owner_id=self.owner,
                                          tenant_id=self.tenant, task_id=str(uuid.uuid4()))
        self.assertTrue(entered.wait(3))
        self.advance(task.id, 2)
        self.assertEqual(self.scheduler.expire_remote(), 1)
        release.set()
        self.settle()
        outcome = self.scheduler.get(task.id, owner_id=self.owner, tenant_id=self.tenant)
        self.assertEqual(outcome.result, remote_timeout_result(self.contract()))
        self.assertEqual(late[0].result, outcome.result)
        self.assertEqual(len(self.scheduler.mailbox(self.owner, self.tenant).poll()), 1)


class MemoryRemoteSchedulerTests(RemoteSchedulerContract, unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.tenant, self.owner = str(uuid.uuid4()), str(uuid.uuid4())
        self.scheduler = TaskScheduler(clock=lambda: self.now)
        self.addCleanup(self.scheduler.close)

    def advance(self, task_id, seconds):
        self.now += seconds

    def restart(self, handler):
        # Memory adapter has no process persistence; relinquished claims model recovery.
        pass

    def competitor(self, handler):
        return self.scheduler

    def test_concurrent_same_id_admission_cannot_reset_send_intent(self):
        admitted, release = threading.Event(), threading.Event()
        accepted, errors, sent = [], [], []
        identifier = str(uuid.uuid4())
        def handler(claim, cancel):
            current = self.scheduler.read_remote_claim(claim)
            if not current["checkpoint"]["send_started"]:
                self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"],
                    checkpoint={**current["checkpoint"], "send_started": True})
                sent.append(claim.token)
            return REMOTE_TASK_PENDING
        self.scheduler.register("remote_a2a", handler)
        def admission(connection):
            admitted.set()
            release.wait(3)
        def start(callback=None):
            try:
                accepted.append(self.scheduler.start_remote(self.contract(), owner_id=self.owner,
                    tenant_id=self.tenant, task_id=identifier, admission=callback))
            except CoreError as error:
                errors.append(error.code)
        first = threading.Thread(target=start, args=(admission,))
        first.start()
        self.addCleanup(release.set)
        self.assertTrue(admitted.wait(3))
        second = threading.Thread(target=start)
        second.start()
        second.join(3)
        callback_unlocked = not second.is_alive()
        release.set()
        first.join(3)
        second.join(3)
        self.settle()
        self.assertTrue(callback_unlocked, "admission must not hold the scheduler lock")
        self.assertEqual(len(accepted), 1)
        self.assertEqual(errors, ["SESSION_CONFLICT"])
        self.assertEqual(len(sent), 1)

    def test_failed_admission_releases_id_reservation_for_retry(self):
        identifier = str(uuid.uuid4())
        calls = []
        self.scheduler.register("remote_a2a", lambda *_: calls.append("executed") or REMOTE_TASK_PENDING)
        def reject(connection):
            raise CoreError("SESSION_CONFLICT")
        with self.assertRaises(CoreError):
            self.scheduler.start_remote(self.contract(), owner_id=self.owner, tenant_id=self.tenant,
                task_id=identifier, admission=reject)
        self.scheduler.start_remote(self.contract(), owner_id=self.owner, tenant_id=self.tenant, task_id=identifier)
        self.settle()
        self.assertEqual(calls, ["executed"])


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "set TEST_DATABASE_URL to run PostgreSQL remote scheduler tests")
class PostgresRemoteSchedulerTests(RemoteSchedulerContract, unittest.TestCase):
    def setUp(self):
        self.tenant, self.owner = str(uuid.uuid4()), str(uuid.uuid4())
        self.database = PostgresDatabase(os.environ["TEST_DATABASE_URL"], min_size=0, max_size=5)
        self.database.migrate()
        self.addCleanup(self.database.close)
        self.scheduler = PostgresTaskScheduler(self.database)
        self.addCleanup(self.cleanup_tasks)
        self.addCleanup(self.scheduler.close)

    def cleanup_tasks(self):
        # Recovery scans all tenants; completed fixtures must leave no live rows.
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM core_notifications WHERE tenant_id=%s", (self.tenant,))
            connection.execute("DELETE FROM core_background_tasks WHERE tenant_id=%s", (self.tenant,))

    def advance(self, task_id, seconds):
        from psycopg.types.json import Jsonb
        with self.database.transaction() as connection:
            row = connection.execute("SELECT checkpoint FROM core_background_tasks WHERE id = %s", (task_id,)).fetchone()
            checkpoint = copy.deepcopy(row["checkpoint"])
            for field in ("deadline", "next_poll_at"):
                if checkpoint[field] is not None:
                    checkpoint[field] -= seconds
            connection.execute("UPDATE core_background_tasks SET checkpoint = %s WHERE id = %s", (Jsonb(checkpoint), task_id))

    def competitor(self, handler):
        scheduler = PostgresTaskScheduler(self.database)
        scheduler.register("remote_a2a", handler)
        self.addCleanup(scheduler.close)
        return scheduler

    def restart(self, handler):
        self.scheduler.close()
        self.scheduler = self.competitor(handler)
