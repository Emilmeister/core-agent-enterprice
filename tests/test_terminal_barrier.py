import copy
import os
import sys
import threading
import unittest
import uuid
from dataclasses import replace
from unittest.mock import Mock, patch

from core_agent.errors import CoreError
from core_agent.database import PostgresDatabase
from core_agent.model import ModelResponse
from core_agent.workflow import InMemoryWorkflowStore, PostgresWorkflowStore, SuspendedRun, WorkflowRecord
from tests import test_tool_approvals as approvals


class TerminalBarrierTests(unittest.TestCase):
    setUp = approvals.ToolApprovalTests.setUp
    app = approvals.ToolApprovalTests.app
    call = staticmethod(approvals.ToolApprovalTests.call)
    start = staticmethod(approvals.ToolApprovalTests.start)
    policy = staticmethod(approvals.ToolApprovalTests.policy)

    def test_root_completion_stops_nonrequired_background_process(self):
        agent, model = self.app(self.call("core_task_start", {
            "tool": "core_terminal_exec", "arguments": {
                "argv": [sys.executable, "-c", "import time; time.sleep(60)"]},
            "required": False,
        }), ModelResponse(message="root done"))
        # The canonical ownership requirement also applies without owner HITL.
        agent.interaction_store = None
        manager = agent.tool_runtime.environment_manager
        generated = model.generate
        live = threading.Event()
        original_start = manager.backend.launcher.start
        classify = agent.guardrail_classifier.classify
        classifier_lock = threading.Lock()

        def available_classifier(*args, **kwargs):
            # This cleanup test requires a launched process. Serialize the clear
            # detector so concurrent parent/child reviews cannot instead require
            # an owner decision for detector_busy; keep the actual review path.
            with classifier_lock:
                return classify(*args, **kwargs)

        def launched(*args, **kwargs):
            handle = original_start(*args, **kwargs)
            live.set()
            return handle

        def generate(*args, **kwargs):
            if model.calls:
                self.assertTrue(live.wait(5), "background tool did not start")
            return generated(*args, **kwargs)

        with patch.object(manager.backend.launcher, "start", side_effect=launched), \
                patch.object(model, "generate", side_effect=generate), \
                patch.object(agent.guardrail_classifier, "classify", side_effect=available_classifier):
            result = self.start(agent)
        self.assertEqual(result.message, "root done")
        self.assertFalse(any(not handle._process_done.is_set() for session in manager._environments.values()
                             for handle in session._processes.values()))

    def test_cleanup_failure_keeps_terminal_intent_nonterminal(self):
        agent, _ = self.app(ModelResponse(message="done"))
        manager = agent.tool_runtime.environment_manager
        with patch.object(manager, "close_execution_tree", side_effect=CoreError("EXECUTION_ENVIRONMENT_UNAVAILABLE")):
            result = self.start(agent)
        self.assertIsInstance(result, SuspendedRun)
        record = agent.workflow_store.lookup_task("task")
        self.assertNotEqual(record.state, "COMPLETED")
        self.assertEqual(record.snapshot["terminal_intent"]["state"], "COMPLETED")
        self.assertEqual(agent.resume_task("task").message, "done")

    def test_followup_during_cleanup_uses_new_generation_and_model_turn(self):
        agent, model = self.app(ModelResponse(message="old answer"), ModelResponse(message="updated answer"))
        manager = agent.tool_runtime.environment_manager
        close = manager.close_execution_tree
        generations = []

        def close_with_input(run_id, intent_id, **kwargs):
            record = agent.workflow_store.lookup_task("task")
            generations.append(record.snapshot["execution_owner"]["generation"])
            if len(generations) == 1:
                agent.enqueue_message({"prompt": "additional requirement"}, task_id="task", message_id="followup",
                                      identity="owner", session_id="chat", tenant_id="company")
            return close(run_id, intent_id, **kwargs)

        with patch.object(manager, "close_execution_tree", side_effect=close_with_input):
            result = self.start(agent)
        self.assertEqual(result.message, "updated answer")
        self.assertEqual(len(set(generations)), 2)
        self.assertEqual(result.usage.model_turns, 2)
        self.assertIn("additional requirement", model.calls[-1].context)

    def test_foreign_receipt_blocks_restart_and_terminal_publication(self):
        agent, model = self.app(ModelResponse(message="done"))
        with patch.object(agent.tool_runtime.environment_manager, "close_execution_tree", side_effect=CoreError("EXECUTION_ENVIRONMENT_UNAVAILABLE")):
            sleeping = self.start(agent)
        store = agent.workflow_store
        record = store.lookup_task("task")
        owner = {**record.snapshot["execution_owner"], "instance_id": "previous-server", "cleanup_confirmed": False, "local_execution_pending": True}
        store._records[record.run_id] = replace(record, snapshot={**record.snapshot, "execution_owner": owner})
        again = agent.resume_task("task")
        self.assertIsInstance(again, SuspendedRun)
        self.assertEqual(again.run_id, sleeping.run_id)
        self.assertEqual(len(model.calls), 1)
        self.assertNotEqual(store.lookup_task("task").state, "COMPLETED")
        self.assertFalse(store.lookup_task("task").snapshot["execution_owner"]["cleanup_confirmed"])

    def test_foreign_model_only_receipt_does_not_block_restart(self):
        agent, model = self.app(ModelResponse(message="recovered"))
        record, *_ = agent._new_workflow({"prompt": "model only"}, task_id="task", identity="owner", session_id="chat", tenant_id="company")
        store = agent.workflow_store
        token = store.acquire_lease(record.run_id, tenant_id="company", owner_id="owner", worker_id="old", ttl=1)
        store.register_execution(record, instance_id="previous-server", worker_id="old", generation=token, lease_token=token)
        self.now[0] += 2
        result = agent.resume_task("task")
        self.assertEqual(result.message, "recovered")
        self.assertEqual(len(model.calls), 1)

    def test_foreign_safe_wait_without_finally_ack_resumes(self):
        agent, model = self.app(self.call("core_wait_until", {"until": "2027-10-01T00:00:00Z"}), ModelResponse(message="awake"))
        self.policy(agent, "allow", "core_wait_until")
        with patch.object(agent.workflow_store, "confirm_execution", side_effect=lambda record, owner: record):
            sleeping = self.start(agent)
        record = agent.workflow_store.lookup_task("task")
        self.assertFalse(record.snapshot["execution_owner"]["cleanup_confirmed"])
        agent.tool_runtime.environment_manager.instance_id = "new-server"
        agent.workflow_store.resolve_wait(sleeping.wait_id, tenant_id="company", outcome={"reason": "time"})
        result = agent.resume_task("task")
        self.assertEqual(result.message, "awake")
        self.assertEqual(len(model.calls), 2)

    def test_recovery_of_legacy_local_dispatch_does_not_assume_process_death(self):
        agent, model = self.app()
        record, *_ = agent._new_workflow({"prompt": "legacy"}, task_id="task", identity="owner", session_id="chat", tenant_id="company")
        snapshot = {**record.snapshot, "pending_call": {"id": "legacy", "name": "core_terminal_exec", "arguments": {"argv": ["true"]}}, "pending_mutating": True}
        agent.workflow_store.transition(record.run_id, tenant_id="company", owner_id="owner", expected_version=record.version,
                                        state="EXECUTING", snapshot=snapshot, event_kind="tool.intent")
        self.assertIsInstance(agent.resume_task("task"), SuspendedRun)
        self.assertEqual(agent.workflow_store.lookup_task("task").state, "EXECUTING")
        self.assertFalse(model.calls)

    def test_generation_cleanup_failure_suspends_without_acknowledgement(self):
        agent, _ = self.app(self.call("core_wait_until", {"until": "2027-10-01T00:00:00Z"}))
        self.policy(agent, "allow", "core_wait_until")
        with patch.object(agent.tool_runtime.environment_manager, "destroy_execution", side_effect=CoreError("EXECUTION_ENVIRONMENT_UNAVAILABLE")):
            result = self.start(agent)
        self.assertIsInstance(result, SuspendedRun)
        record = agent.workflow_store.lookup_task("task")
        self.assertFalse(record.snapshot["execution_owner"]["cleanup_confirmed"])

    def test_stale_attempt_finally_cannot_acknowledge_or_close_new_generation(self):
        agent, _ = self.app()
        record, *_ = agent._new_workflow({"prompt": "lease race"}, task_id="task", identity="owner", session_id="chat", tenant_id="company")
        old_started, new_started = threading.Event(), threading.Event()
        release_old, release_new = threading.Event(), threading.Event()
        errors = []
        generations = {}

        def attempt(label, started, release):
            def owned(current, token):
                generations[label] = token
                if label == "new":
                    agent._runtime_cache[current.run_id] = ("new-runtime", {}, None)
                    agent._run_mcp_connectors[current.run_id] = connector
                started.set()
                self.assertTrue(release.wait(5))
                if label == "old":
                    agent._drop_run_runtime(current.run_id)
            try:
                agent._run_execution_attempt(record, owned)
            except Exception as error:
                errors.append(error)

        first = threading.Thread(target=attempt, args=("old", old_started, release_old))
        second = threading.Thread(target=attempt, args=("new", new_started, release_new))
        connector = Mock()
        first.start()
        try:
            self.assertTrue(old_started.wait(5))
            self.now[0] += 3600
            second.start()
            self.assertTrue(new_started.wait(5))
            release_old.set()
            first.join(5)
            current = agent.workflow_store.lookup_task("task")
            self.assertEqual(current.snapshot["execution_owner"]["generation"], generations["new"])
            self.assertFalse(current.snapshot["execution_owner"]["cleanup_confirmed"])
            key = (record.run_id, agent._worker_id, generations["new"])
            self.assertNotIn(key, agent.tool_runtime.environment_manager._retired_executions)
            self.assertEqual(agent._runtime_cache[record.run_id][0], "new-runtime")
            connector.close.assert_not_called()
        finally:
            release_old.set()
            release_new.set()
            first.join(5)
            if second.ident is not None:
                second.join(5)
        self.assertFalse(errors)

    def test_stale_terminal_cleanup_cannot_retire_reopened_generation(self):
        agent, _ = self.app()
        record, *_ = agent._new_workflow({"prompt": "lease race"}, task_id="task", identity="owner", session_id="chat", tenant_id="company")
        store = agent.workflow_store
        manager = agent.tool_runtime.environment_manager
        token = store.acquire_lease(record.run_id, tenant_id="company", owner_id="owner", worker_id=agent._worker_id, ttl=60)
        record = store.register_execution(record, instance_id=manager.instance_id, worker_id=agent._worker_id, generation=token, lease_token=token)
        record = store.begin_terminal(record, {"id": "old-intent", "state": "COMPLETED", "snapshot": record.snapshot,
                                               "event_kind": "task.completed"}, lease_token=token)
        started, release = threading.Event(), threading.Event()
        errors = []
        close = manager.close_execution_tree

        def paused(*args, **kwargs):
            if threading.current_thread() is old:
                started.set()
                if not release.wait(5):
                    raise AssertionError("old terminal cleanup not released")
            return close(*args, **kwargs)

        def finish():
            try:
                agent._finish_terminal(record, lease_token=token)
            except Exception as error:
                errors.append(error)

        old = threading.Thread(target=finish)
        with patch.object(manager, "close_execution_tree", side_effect=paused):
            old.start()
            try:
                self.assertTrue(started.wait(5))
                agent.enqueue_message({"prompt": "new input"}, task_id="task", message_id="followup",
                                      identity="owner", session_id="chat", tenant_id="company")
                self.now[0] += 61
                fresh = store.acquire_lease(record.run_id, tenant_id="company", owner_id="owner", worker_id=agent._worker_id, ttl=60)
                with self.assertRaises(CoreError) as reopened:
                    agent._finish_terminal(store.lookup_task("task"), lease_token=fresh)
                self.assertEqual(reopened.exception.code, "EXECUTION_REOPENED")
                current = store.register_execution(store.lookup_task("task"), instance_id=manager.instance_id,
                                                   worker_id=agent._worker_id, generation=fresh, lease_token=fresh)
                key = (record.run_id, agent._worker_id, fresh)
                with manager.execution_scope(*key):
                    release.set()
                    old.join(5)
                    self.assertFalse(old.is_alive())
                    self.assertNotIn(key, manager._retired_executions)
                    self.assertEqual(manager._execution_key(record.run_id), key)
                    self.assertNotIn(record.run_id, manager._execution_seals)
                    self.assertEqual(store.lookup_task("task").snapshot["execution_owner"], current.snapshot["execution_owner"])
            finally:
                release.set()
                old.join(5)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], CoreError)
        self.assertEqual(errors[0].code, "LEASE_LOST")

    def test_child_admitted_before_root_terminal_cannot_execute_later(self):
        agent, _ = self.app(self.call("core_task_start", {
            "tool": "core_terminal_exec", "arguments": {"argv": ["true"]}, "required": False,
        }), ModelResponse(message="done"))
        for name in ("core_task_start", "core_terminal_exec"):
            self.policy(agent, "allow", name)
        target_calls = []
        agent.tool_runtime.handlers["core_terminal_exec"] = lambda *_: target_calls.append(True)
        original = agent._recover_background_tool

        def delayed(contract, _cancel):
            child = agent.workflow_store.lookup_task(contract["task_id"])
            return SuspendedRun(child.run_id, child.task_id, "", child.version)

        with patch.object(agent, "_recover_background_tool", side_effect=delayed):
            result = self.start(agent)
            approvals.ToolApprovalTests.settle_workers(agent)
        task, = agent.task_scheduler.list(owner_id=result.run_id, tenant_id="company")
        contract = agent.task_scheduler._recovery[task.id][1]
        with self.assertRaises(CoreError) as raised:
            original(contract, threading.Event())
        self.assertEqual(raised.exception.code, "TASK_CANCELLED")
        self.assertEqual(agent.workflow_store.lookup_task(task.id).state, "CANCELLED")
        self.assertFalse(target_calls)


class TerminalBarrierStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryWorkflowStore()
        self.create_root()

    def create_root(self):
        self.root = self.store.create(WorkflowRecord(str(uuid.uuid4()), str(uuid.uuid4()), "chat", "tenant", "owner", None,
                                                    "RUNNING", 1, {"prompt": "test"}, {}))
        self.token = self.store.acquire_lease(self.root.run_id, tenant_id="tenant", owner_id="owner", worker_id="worker", ttl=100)

    def test_intent_fences_zero_charge_dispatch_and_child_admission(self):
        self.root = self.store.begin_terminal(self.root, {"id": "intent", "state": "COMPLETED"}, lease_token=self.token)
        with self.assertRaises(CoreError) as raised:
            self.store.create(replace(self.root, run_id=str(uuid.uuid4()), task_id=str(uuid.uuid4()), parent_run_id=self.root.run_id, version=1, snapshot={}))
        self.assertEqual(raised.exception.code, "EXECUTION_CLOSING")
        with self.assertRaises(CoreError) as raised:
            self.store.transition(self.root.run_id, tenant_id="tenant", owner_id="owner", expected_version=self.root.version,
                                  state="EXECUTING", snapshot=self.root.snapshot, event_kind="tool.intent", lease_token=self.token)
        self.assertEqual(raised.exception.code, "EXECUTION_CLOSING")

    def test_unconfirmed_receipt_cannot_be_overwritten_and_stale_ack_is_inert(self):
        owner = dict(instance_id="instance", worker_id="worker", generation=self.token, cleanup_confirmed=False)
        self.root = self.store.register_execution(self.root, **{k: owner[k] for k in ("instance_id", "worker_id", "generation")}, lease_token=self.token)
        with self.assertRaises(CoreError):
            self.store.register_execution(self.root, instance_id="new", worker_id="new", generation="new", lease_token=self.token)
        self.store.confirm_execution(self.root, owner)
        fresh = self.store.register_execution(self.root, instance_id="instance", worker_id="worker", generation="new", lease_token=self.token)
        self.store.confirm_execution(self.root, owner)
        self.assertEqual(self.store.get(self.root.run_id, tenant_id="tenant").snapshot["execution_owner"], fresh.snapshot["execution_owner"])
        stale = copy.deepcopy(self.root.snapshot)
        changed = self.store.transition(self.root.run_id, tenant_id="tenant", owner_id="owner", expected_version=self.root.version,
                                        state="RUNNING", snapshot=stale, event_kind="test", lease_token=self.token)
        self.assertEqual(changed.snapshot["execution_owner"], fresh.snapshot["execution_owner"])

    def test_child_receipt_blocks_parent_commit_until_exact_cleanup_ack(self):
        child = self.store.create(replace(self.root, run_id=str(uuid.uuid4()), task_id=str(uuid.uuid4()), parent_run_id=self.root.run_id, snapshot={}))
        token = self.store.acquire_lease(child.run_id, tenant_id="tenant", owner_id="owner", worker_id="child", ttl=100)
        child = self.store.register_execution(child, instance_id="server", worker_id="child", generation=token, lease_token=token)
        self.root = self.store.begin_terminal(self.root, {"id": "intent", "state": "COMPLETED"}, lease_token=self.token)
        with self.assertRaises(CoreError) as raised:
            self.store.transition(self.root.run_id, tenant_id="tenant", owner_id="owner", expected_version=self.root.version,
                                  state="COMPLETED", snapshot=self.root.snapshot, event_kind="task.completed", lease_token=self.token)
        self.assertEqual(raised.exception.code, "EXECUTION_CLEANUP_PENDING")
        with self.assertRaises(CoreError) as raised:
            self.store.transition(child.run_id, tenant_id="tenant", owner_id="owner", expected_version=child.version,
                                  state="EXECUTING", snapshot=child.snapshot, event_kind="tool.intent", lease_token=token)
        self.assertEqual(raised.exception.code, "EXECUTION_CLOSING")
        self.store.confirm_execution(child, child.snapshot["execution_owner"])
        finished = self.store.transition(self.root.run_id, tenant_id="tenant", owner_id="owner", expected_version=self.root.version,
                                         state="COMPLETED", snapshot=self.root.snapshot, event_kind="task.completed", lease_token=self.token)
        self.assertEqual(finished.state, "COMPLETED")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "set TEST_DATABASE_URL for PostgreSQL terminal barriers")
class PostgresTerminalBarrierTests(TerminalBarrierStoreTests):
    def setUp(self):
        self.database = PostgresDatabase(os.environ["TEST_DATABASE_URL"], min_size=0, max_size=4)
        self.addCleanup(self.database.close)
        self.database.migrate()
        self.store = PostgresWorkflowStore(self.database)
        self.create_root()

    def test_root_intent_serializes_concurrent_child_admission_and_dispatch(self):
        child = self.store.create(replace(self.root, run_id=str(uuid.uuid4()), task_id=str(uuid.uuid4()), parent_run_id=self.root.run_id, snapshot={}))
        prepared, release = threading.Event(), threading.Event()
        outcomes = {}
        original = self.store._execution_snapshot

        def paused(current, snapshot, state, event_kind, *args, **kwargs):
            result = original(current, snapshot, state, event_kind, *args, **kwargs)
            if event_kind == "execution.terminal_prepared":
                prepared.set()
                if not release.wait(5):
                    raise AssertionError("terminal transaction not released")
            return result

        def execute(name, action):
            try:
                outcomes[name] = action()
            except Exception as error:
                outcomes[name] = error

        terminal = threading.Thread(target=execute, args=("terminal", lambda: self.store.begin_terminal(
            self.root, {"id": "intent", "state": "COMPLETED"}, lease_token=self.token)))
        admission = threading.Thread(target=execute, args=("admission", lambda: self.store.create(replace(
            child, run_id=str(uuid.uuid4()), task_id=str(uuid.uuid4())))))
        dispatch = threading.Thread(target=execute, args=("dispatch", lambda: self.store.transition(
            child.run_id, tenant_id="tenant", owner_id="owner", expected_version=child.version,
            state="EXECUTING", snapshot=child.snapshot, event_kind="tool.intent")))
        with patch.object(self.store, "_execution_snapshot", side_effect=paused):
            terminal.start()
            try:
                self.assertTrue(prepared.wait(5))
                admission.start()
                dispatch.start()
            finally:
                release.set()
                for worker in (terminal, admission, dispatch):
                    if worker.ident is not None:
                        worker.join(5)
                        self.assertFalse(worker.is_alive())
        self.assertIsInstance(outcomes["terminal"], WorkflowRecord)
        for name in ("admission", "dispatch"):
            self.assertIsInstance(outcomes[name], CoreError)
            self.assertEqual(outcomes[name].code, "EXECUTION_CLOSING")

    def test_cleanup_runs_after_prepared_transaction_releases_run_lock(self):
        from core_agent.model import ScriptedModel
        from tests.test_runtime_observability import make_agent

        agent = make_agent(ScriptedModel([ModelResponse(message="done")]), memory="disabled", workflow_store=self.store)
        self.addCleanup(agent.close)
        cleaned = []

        def cleanup(run_id, _intent_id, **_kwargs):
            with self.database.transaction() as connection:
                row = connection.execute("SELECT snapshot FROM core_runs WHERE run_id = %s FOR UPDATE NOWAIT", (run_id,)).fetchone()
                self.assertEqual(row["snapshot"]["terminal_intent"]["state"], "COMPLETED")
                cleaned.append(run_id)

        with patch.object(agent.tool_runtime.environment_manager, "close_execution_tree", side_effect=cleanup, create=True):
            result = agent.run({"prompt": "finish"}, task_id=str(uuid.uuid4()))
        self.assertEqual(result.message, "done")
        self.assertEqual(cleaned, [result.run_id])
