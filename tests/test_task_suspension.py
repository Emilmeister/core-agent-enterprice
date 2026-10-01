import os
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core_agent.errors import CoreError
from core_agent.tasks import TaskScheduler
from core_agent.workflow import SuspendedRun


class PostgresAdmissionClaimTests(unittest.TestCase):
    def test_launch_failure_releases_admission_claim_for_immediate_recovery(self):
        from core_agent.postgres_tasks import PostgresTaskScheduler

        database = SimpleNamespace(transaction=lambda: nullcontext(Mock()))
        scheduler = PostgresTaskScheduler(database)
        self.addCleanup(scheduler.close)
        initial_claim = ("token", {"claimed_from_state": "submitted"})
        with patch.object(scheduler, "_claim", return_value=initial_claim), patch.object(
            scheduler, "_launch", side_effect=RuntimeError("thread unavailable")
        ), patch.object(scheduler, "_release_claim") as release:
            with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
                scheduler.start(
                    lambda: None, task_id="task", tenant_id="tenant",
                    owner_id="owner", kind="command", contract={},
                )
            release.assert_called_once_with("task", "tenant", "token", state="submitted")

    def test_initial_claim_is_reserved_in_admission_transaction(self):
        from core_agent.postgres_tasks import PostgresTaskScheduler

        events = []
        connection = Mock()

        @contextmanager
        def transaction():
            events.append("begin")
            yield connection
            events.append("commit")

        scheduler = PostgresTaskScheduler(SimpleNamespace(transaction=transaction))
        self.addCleanup(scheduler.close)
        initial_claim = ("token", {"claimed_from_state": "submitted"})

        def claim(*args, **kwargs):
            self.assertIs(kwargs.get("connection"), connection)
            self.assertEqual(events, ["begin", "admission"])
            events.append("claim")
            return initial_claim

        def launch(*args, **kwargs):
            self.assertEqual(events, ["begin", "admission", "claim", "commit"])
            self.assertEqual(kwargs.get("initial_claim"), initial_claim)

        with patch.object(scheduler, "_claim", side_effect=claim), patch.object(
            scheduler, "_launch", side_effect=launch
        ):
            scheduler.start(
                lambda: None, owner_id="owner", kind="command", contract={},
                admission=lambda _: events.append("admission"),
            )

    def test_worker_does_not_dispatch_after_claim_expires(self):
        from core_agent.postgres_tasks import PostgresTaskScheduler

        scheduler = PostgresTaskScheduler(None)
        self.addCleanup(scheduler.close)
        calls = []
        row = {"cancel_requested": False, "claimed_from_state": "submitted"}
        with patch.object(scheduler, "_claim", return_value=("token", row)), patch.object(
            scheduler, "_renew_claim", side_effect=CoreError("LEASE_LOST")
        ), patch.object(scheduler, "_finish") as finish:
            scheduler._launch("task", "tenant", lambda: calls.append("dispatched"),
                              accepts_cancel_event=False)
            scheduler.close()
            self.assertEqual(calls, [])
            finish.assert_not_called()


class TaskSuspensionTests(unittest.TestCase):
    def setUp(self):
        self.scheduler = TaskScheduler()
        self.addCleanup(self.scheduler.close)
        self.tenant = "tenant-" + str(uuid.uuid4())
        self.owner = "parent-" + str(uuid.uuid4())

    def start_suspended(self, *, on_cancel=None):
        task_id = str(uuid.uuid4())
        task = self.scheduler.start(
            lambda: SuspendedRun("run", task_id, "wait", 2),
            task_id=task_id,
            owner_id=self.owner,
            tenant_id=self.tenant,
            kind="subagent",
            contract={"instruction": "resume"},
            recoverable=True,
            required=True,
            mutating=False,
            on_cancel=on_cancel,
        )
        self.settle_workers()
        return task

    def settle_workers(self):
        with self.scheduler._lock:
            workers = tuple(self.scheduler._threads)
        for worker in workers:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())

    def get(self, task):
        return self.scheduler.get(task.id, tenant_id=self.tenant)

    def mailbox(self):
        return self.scheduler.mailbox(self.owner, self.tenant).poll()

    def workflow_agent(self, scheduler=None):
        from core_agent.model import ScriptedModel
        from core_agent.workflow import InMemoryWorkflowStore, PostgresWorkflowStore
        from tests.test_runtime_observability import make_agent

        if not hasattr(self, "workflows"):
            self.workflows = (
                PostgresWorkflowStore(self.database)
                if hasattr(self, "database") else InMemoryWorkflowStore()
            )
        return make_agent(
            ScriptedModel([]), task_scheduler=scheduler or self.scheduler,
            workflow_store=self.workflows,
        )

    def workflow_task(self, kind="subagent", *, tenant_id=None):
        from core_agent.workflow import WorkflowRecord

        task_id = str(uuid.uuid4())
        tenant_id = tenant_id or self.tenant
        record = self.workflows.create(WorkflowRecord(
            str(uuid.uuid4()), task_id, "chat", tenant_id, "identity", None,
            "RUNNING", 1, {"prompt": "child"}, {"background_tool": kind == "background_tool"},
        ))
        scope = {"task_id": task_id, "tenant_id": tenant_id, "identity": "identity"}
        contract = {"scope": scope} if kind == "subagent" else {
            **scope, "workflow_version": 1, "run_id": record.run_id,
        }
        return record, contract

    def commit_workflow(self, record, state):
        result = {"output": {"answer": "saved"}} if record.snapshot["background_tool"] else {
            "message": "saved partial", "usage": {"model_turns": 3, "tool_calls": 2},
            "complete": False, "completion_reason": "budget_exhausted",
            "exhausted_dimension": "tool_calls", "shared_budget": {"tool_calls": 2},
            "pending_tasks": ["pending-child"],
        }
        return self.workflows.transition(
            record.run_id, tenant_id=record.tenant_id, owner_id=record.owner_id,
            expected_version=record.version, state=state, snapshot=record.snapshot,
            event_kind="task." + state.lower(), result=result if state == "COMPLETED" else None,
            error_code="ORIGINAL_FAILURE" if state == "FAILED" else None,
        )

    def assert_workflow_outcome(self, task, record):
        current = self.get(task)
        expected_state = {"COMPLETED": "completed", "FAILED": "failed", "CANCELLED": "canceled"}[record.state]
        self.assertEqual(current.state, expected_state)
        if record.state == "COMPLETED":
            expected = record.result["output"] if record.snapshot["background_tool"] else {
                "run_id": record.run_id, "terminal_state": "completed", **record.result,
            }
            value = current.result.to_dict() if hasattr(current.result, "to_dict") else current.result
            self.assertEqual(value, expected)
        else:
            self.assertIsNone(current.result)
            self.assertEqual(getattr(current.error, "code", None), record.error_code)
        event = next(event for event in self.mailbox() if event.task_id == task.id)
        self.assertEqual(event.kind, "task." + expected_state)
        self.assertEqual(event.payload["result"], current.result)
        self.assertEqual(event.payload.get("error_code"), record.error_code)

    def test_committed_workflow_outcome_survives_late_cancel(self):
        self.workflow_agent()
        for kind in ("subagent", "background_tool"):
            for state in ("COMPLETED", "FAILED"):
                with self.subTest(kind=kind, state=state):
                    record, contract = self.workflow_task(kind)
                    committed, release = threading.Event(), threading.Event()
                    saved = []

                    def execute():
                        saved.append(self.commit_workflow(record, state))
                        committed.set()
                        if not release.wait(3):
                            raise TimeoutError("worker not released")
                        if state == "FAILED":
                            raise CoreError("HANDLER_FAILURE")
                        return "stale result"

                    task = self.scheduler.start(
                        execute, task_id=record.task_id, owner_id=self.owner,
                        tenant_id=self.tenant, kind=kind, contract=contract,
                        recoverable=True, mutating=False,
                    )
                    try:
                        self.assertTrue(committed.wait(2))
                        self.scheduler.cancel(task.id, tenant_id=self.tenant)
                    finally:
                        release.set()
                        self.settle_workers()
                    self.assert_workflow_outcome(task, saved[0])

    def test_nonterminal_workflow_is_not_finalized_by_scheduler(self):
        self.workflow_agent()
        record, contract = self.workflow_task()
        task = self.scheduler.start(
            lambda: "not committed", task_id=record.task_id, owner_id=self.owner,
            tenant_id=self.tenant, kind="subagent", contract=contract, recoverable=True,
        )
        self.settle_workers()
        self.assertEqual(self.get(task).state, "working")
        self.assertEqual(self.mailbox(), ())

    def test_cancel_committed_before_outcome_wins(self):
        self.workflow_agent()
        record, contract = self.workflow_task()
        entered, release = threading.Event(), threading.Event()
        saved = []

        def execute():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("worker not released")
            current = self.workflows.get(record.run_id, tenant_id=self.tenant)
            with self.assertRaises(CoreError) as caught:
                self.commit_workflow(current, "COMPLETED")
            self.assertEqual(caught.exception.code, "CANCEL_REQUESTED")
            saved.append(self.commit_workflow(current, "CANCELLED"))
            raise CoreError("TASK_CANCELLED")

        task = self.scheduler.start(
            execute, task_id=record.task_id, owner_id=self.owner, tenant_id=self.tenant,
            kind="subagent", contract=contract, recoverable=True,
            on_cancel=lambda: self.workflows.request_cancel(
                record.run_id, tenant_id=self.tenant, owner_id=record.owner_id,
            ),
        )
        try:
            self.assertTrue(entered.wait(2))
            self.scheduler.cancel(task.id, tenant_id=self.tenant)
        finally:
            release.set()
            self.settle_workers()
        self.assert_workflow_outcome(task, saved[0])

    def test_workflow_projection_does_not_read_another_tenant(self):
        self.workflow_agent()
        foreign = self.tenant + "-foreign"
        record, contract = self.workflow_task(tenant_id=foreign)
        self.commit_workflow(record, "COMPLETED")
        task = self.scheduler.start(
            lambda: "local result", task_id=record.task_id, owner_id=self.owner,
            tenant_id=self.tenant, kind="subagent", contract=contract, recoverable=True,
        )
        self.settle_workers()
        self.assertEqual(self.get(task).result, "local result")

    def restart_scheduler(self):
        # The memory adapter retains suspended continuations in this process.
        return self.scheduler

    def test_recovery_projects_committed_outcome_without_redispatch(self):
        self.workflow_agent()
        for kind in ("subagent", "background_tool"):
            for state in ("COMPLETED", "FAILED"):
                with self.subTest(kind=kind, state=state):
                    record, contract = self.workflow_task(kind)
                    task = self.scheduler.start(
                        lambda: SuspendedRun(record.run_id, record.task_id, "wait", 1),
                        task_id=record.task_id, owner_id=self.owner, tenant_id=self.tenant,
                        kind=kind, contract=contract, recoverable=True, mutating=False,
                    )
                    self.settle_workers()
                    saved = self.commit_workflow(record, state)
                    self.scheduler.cancel(task.id, tenant_id=self.tenant)
                    restarted = self.restart_scheduler()
                    dispatch = Mock(side_effect=AssertionError("terminal workflow was replayed"))
                    with patch.dict(restarted._handlers, {kind: dispatch}):
                        self.assertEqual(restarted.recover(ready=lambda *_: False), 0)
                        self.settle_workers()
                    dispatch.assert_not_called()
                    self.assert_workflow_outcome(task, saved)
                    self.assertEqual(restarted.recover(ready=lambda *_: True), 0)
                    self.assertEqual(sum(event.task_id == task.id for event in self.mailbox()), 1)

    def test_suspend_keeps_working_without_result_or_terminal_notification(self):
        task = self.start_suspended()
        current = self.get(task)
        self.assertEqual(current.state, "working")
        self.assertIsNone(current.result)
        self.assertEqual(current.revision, 0)
        self.assertEqual(self.mailbox(), ())
        self.assertEqual(
            self.scheduler.count(owner_id=self.owner, active_only=True, tenant_id=self.tenant),
            1,
        )
        with self.assertRaises(TimeoutError):
            self.scheduler.wait(task.id, timeout=0, tenant_id=self.tenant)

    def test_ready_recovery_runs_once_and_delivers_final_notification(self):
        calls = []
        self.scheduler.register("subagent", lambda contract, cancel: calls.append(contract) or "done")
        task = self.start_suspended()
        for _ in range(3):
            self.assertEqual(self.scheduler.recover(ready=lambda *_: False), 0)
        self.assertEqual(calls, [])
        seen = []
        self.assertEqual(self.scheduler.recover(ready=lambda *scope: seen.append(scope) or True), 1)
        self.settle_workers()
        self.assertEqual(seen, [(task.id, self.tenant)])
        self.assertEqual(self.get(task).result, "done")
        self.assertEqual(calls, [{"instruction": "resume"}])
        self.assertEqual(self.scheduler.recover(ready=lambda *_: True), 0)
        self.assertEqual(len(self.mailbox()), 1)
        self.assertEqual(self.mailbox()[0].kind, "task.completed")

    def test_concurrent_recovery_claims_single_worker(self):
        gate = threading.Event()
        entered = threading.Event()
        calls = []

        def resume(contract, cancel):
            calls.append(contract)
            entered.set()
            gate.wait(2)
            return "done"

        self.scheduler.register("subagent", resume)
        task = self.start_suspended()
        try:
            with ThreadPoolExecutor(max_workers=6) as workers:
                recovered = list(workers.map(lambda _: self.scheduler.recover(ready=lambda *_: True), range(6)))
            self.assertTrue(entered.wait(1))
            self.assertEqual(sum(recovered), 1)
            self.assertEqual(len(calls), 1)
        finally:
            gate.set()
            self.settle_workers()
        self.assertEqual(self.get(task).state, "completed")
        self.assertEqual(len(self.mailbox()), 1)

    def test_cancel_after_suspension_preserves_callback_and_bypasses_readiness(self):
        callbacks = []
        canceled = []

        def resume(contract, cancel):
            canceled.append(cancel.is_set())
            return "canceled result"

        self.scheduler.register("subagent", resume)
        task = self.start_suspended(on_cancel=lambda: callbacks.append("cleanup"))
        self.scheduler.cancel(task.id, owner_id=self.owner, tenant_id=self.tenant)
        self.scheduler.recover(ready=lambda *_: False)
        self.settle_workers()
        self.assertEqual(callbacks, ["cleanup"])
        self.assertEqual(canceled, [True])
        self.assertEqual(self.get(task).state, "canceled")
        self.assertEqual(self.get(task).result, "canceled result")
        self.assertEqual(len(self.mailbox()), 1)
        self.assertEqual(self.mailbox()[0].kind, "task.canceled")

    def test_recovery_thread_start_failure_retains_suspension(self):
        self.scheduler.register("subagent", lambda *_: "done")
        task = self.start_suspended()
        with patch("threading.Thread.start", side_effect=RuntimeError("thread unavailable")):
            with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
                self.scheduler.recover(ready=lambda *_: True)
        self.assertEqual(self.get(task).state, "working")
        self.assertEqual(self.mailbox(), ())
        self.assertEqual(self.scheduler.recover(ready=lambda *_: True), 1)
        self.settle_workers()
        self.assertEqual(self.get(task).result, "done")


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "set TEST_DATABASE_URL for PostgreSQL suspension tests")
class PostgresTaskSuspensionTests(TaskSuspensionTests):
    def setUp(self):
        from core_agent.database import PostgresDatabase
        from core_agent.postgres_tasks import PostgresTaskScheduler

        self.database = PostgresDatabase(os.environ["TEST_DATABASE_URL"], min_size=0, max_size=8)
        self.database.migrate()
        self.addCleanup(self.database.close)
        self.scheduler = PostgresTaskScheduler(self.database)
        self.tenant = "tenant-" + str(uuid.uuid4())
        self.owner = "parent-" + str(uuid.uuid4())
        self.addCleanup(self.cleanup_tasks)
        self.addCleanup(self.scheduler.close)

    def cleanup_tasks(self):
        with self.database.transaction() as connection:
            for table in (
                "core_waits", "core_runs", "core_budget_ledgers", "core_notifications",
                "core_background_tasks", "core_outbox", "core_events", "core_checkpoints",
            ):
                connection.execute(
                    f"DELETE FROM {table} WHERE tenant_id IN (%s, %s)",
                    (self.tenant, self.tenant + "-foreign"),
                )

    def restart_scheduler(self):
        from core_agent.postgres_tasks import PostgresTaskScheduler

        self.scheduler.close()
        self.scheduler = PostgresTaskScheduler(self.database)
        self.addCleanup(self.scheduler.close)
        self.workflow_agent()
        return self.scheduler

    def test_cancel_before_initial_dispatch_reconciles_nonterminal_workflow(self):
        self.workflow_agent()
        record, contract = self.workflow_task("background_tool")
        original_launch = self.scheduler._launch
        saved, seen = [], []

        def cancel_before_launch(*args, **kwargs):
            self.scheduler.cancel(record.task_id, tenant_id=self.tenant)
            return original_launch(*args, **kwargs)

        def execute(cancel):
            seen.append(cancel.is_set())
            current = self.workflows.get(record.run_id, tenant_id=self.tenant)
            saved.append(self.commit_workflow(current, "CANCELLED"))
            raise CoreError("TASK_CANCELLED")

        with patch.object(self.scheduler, "_launch", side_effect=cancel_before_launch):
            task = self.scheduler.start(
                execute, task_id=record.task_id, owner_id=self.owner,
                tenant_id=self.tenant, kind="background_tool", contract=contract,
                accepts_cancel_event=True, recoverable=True, mutating=False,
                on_cancel=lambda: self.workflows.request_cancel(
                    record.run_id, tenant_id=self.tenant, owner_id=record.owner_id,
                ),
            )
        self.settle_workers()
        self.assertEqual(seen, [True])
        self.assert_workflow_outcome(task, saved[0])

    def test_cancel_between_workflow_read_and_scheduler_commit_preserves_outcome(self):
        self.workflow_agent()
        record, contract = self.workflow_task()
        original = self.scheduler._canonical_outcome
        saved = []

        def cancel_after_read(task_id, tenant_id, row=None):
            outcome = original(task_id, tenant_id, row)
            if outcome is not None and not isinstance(outcome, SuspendedRun):
                self.scheduler.cancel(task_id, tenant_id=tenant_id)
            return outcome

        def execute():
            saved.append(self.commit_workflow(record, "COMPLETED"))
            return "stale result"

        with patch.object(self.scheduler, "_canonical_outcome", side_effect=cancel_after_read):
            task = self.scheduler.start(
                execute, task_id=record.task_id, owner_id=self.owner,
                tenant_id=self.tenant, kind="subagent", contract=contract, recoverable=True,
            )
            self.settle_workers()
        self.assert_workflow_outcome(task, saved[0])

    def test_admitted_command_cannot_be_stolen_before_initial_launch(self):
        self.check_initial_launch_race(expire_claim=False)

    def test_expired_initial_launcher_cannot_dispatch_after_recovery(self):
        self.check_initial_launch_race(expire_claim=True)

    def check_initial_launch_race(self, *, expire_claim):
        from core_agent.postgres_tasks import PostgresTaskScheduler

        observer = PostgresTaskScheduler(self.database)
        self.addCleanup(observer.close)
        committed = threading.Event()
        release = threading.Event()
        task_id = str(uuid.uuid4())
        calls = []
        original_launch = self.scheduler._launch

        def paused_launch(*args, **kwargs):
            committed.set()
            if not release.wait(5):
                raise TimeoutError("initial launch was not released")
            return original_launch(*args, **kwargs)

        with patch.object(self.scheduler, "_launch", side_effect=paused_launch):
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    self.scheduler.start, lambda: calls.append("executed") or "done",
                    task_id=task_id, owner_id=self.owner, tenant_id=self.tenant,
                    kind="mutating_command", contract={}, recoverable=False,
                    mutating=True,
                )
                try:
                    self.assertTrue(committed.wait(2))
                    self.assertEqual(observer.get(task_id, tenant_id=self.tenant).state, "working")
                    if expire_claim:
                        with self.database.transaction() as connection:
                            connection.execute(
                                "UPDATE core_background_tasks SET claim_expires_at = 0 WHERE id = %s",
                                (task_id,),
                            )
                    self.assertEqual(observer.recover(), 0)
                    if not expire_claim:
                        self.assertEqual(self.mailbox(), ())
                finally:
                    release.set()
                task = future.result(timeout=2)
        self.settle_workers()
        final = self.get(task)
        self.assertEqual(calls, [] if expire_claim else ["executed"])
        self.assertEqual(final.state, "failed" if expire_claim else "completed")
        if expire_claim:
            self.assertEqual(final.error.code, "SIDE_EFFECT_UNKNOWN")
        else:
            self.assertEqual(final.result, "done")
        self.assertEqual(len(self.mailbox()), 1)

    def enter_wait(self, task):
        from core_agent.workflow import PostgresWorkflowStore, WorkflowRecord

        store = PostgresWorkflowStore(self.database)
        record = store.create(WorkflowRecord(
            str(uuid.uuid4()), task.id, "chat", self.tenant, self.owner,
            None, "RUNNING", 1, {"prompt": "wait"}, {},
        ))
        token = store.acquire_lease(
            record.run_id, tenant_id=self.tenant, owner_id=self.owner,
            worker_id="worker", ttl=30,
        )
        wait = store.enter_wait(
            record, kind="timer", source_id="timer", subject={},
            continuation={"version": 1, "phase": "tool_wait", "call_id": "timer"},
            deadline=None, snapshot=record.snapshot, lease_token=token,
        )
        return store, wait

    def test_restart_skips_unresolved_wait_then_resumes_after_resolution(self):
        from core_agent.postgres_tasks import PostgresTaskScheduler

        task = self.start_suspended()
        store, wait = self.enter_wait(task)
        restarted = PostgresTaskScheduler(self.database)
        self.addCleanup(restarted.close)
        calls = []
        restarted.register("subagent", lambda *_: calls.append("resumed") or "done")
        for _ in range(3):
            self.assertEqual(restarted.recover(), 0)
        token, claimed = restarted._claim(task.id, self.tenant, allow_working=True)
        self.assertIsNone(token)
        self.assertIsNone(claimed)
        self.assertEqual(calls, [])
        store.resolve_wait(wait.wait_id, tenant_id=self.tenant, outcome={"reason": "time"})
        self.assertEqual(restarted.recover(), 1)
        final = restarted.wait(task.id, timeout=2, tenant_id=self.tenant)
        self.assertEqual(final.result, "done")
        self.assertEqual(restarted.recover(), 0)
        self.assertEqual(calls, ["resumed"])
        self.assertEqual(len(self.mailbox()), 1)

    def test_cancel_bypasses_unresolved_canonical_wait(self):
        task = self.start_suspended()
        self.enter_wait(task)
        seen = []
        self.scheduler.register("subagent", lambda _, cancel: seen.append(cancel.is_set()))
        self.scheduler.cancel(task.id, tenant_id=self.tenant)
        self.scheduler.recover(ready=lambda *_: False)
        self.settle_workers()
        self.assertEqual(self.get(task).state, "canceled")
        self.assertEqual(seen, [True])

    def test_suspension_releases_database_claim(self):
        task = self.start_suspended()
        with self.database.pool.connection() as connection:
            row = connection.execute(
                "SELECT claim_owner, claim_token, claim_expires_at FROM core_background_tasks WHERE id = %s",
                (task.id,),
            ).fetchone()
        self.assertEqual(row, {"claim_owner": None, "claim_token": None, "claim_expires_at": None})
        with self.assertRaises(CoreError) as caught:
            self.scheduler.assert_can_complete_parent(self.owner, tenant_id=self.tenant)
        self.assertEqual(caught.exception.code, "REQUIRED_TASK_PENDING")
