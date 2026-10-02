"""Durable Python control-flow tests; fake process teardown is not isolation proof."""
import copy
import json
import shutil
import socket
import threading
import unittest
from contextvars import copy_context
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from core_agent.errors import CoreError
from core_agent.execution import ExecutionResult
from core_agent.model import ModelResponse
from core_agent.workflow import SuspendedRun
from tests import test_tool_approvals as approvals


class PythonWaitTests(unittest.TestCase):
    setUp = approvals.ToolApprovalTests.setUp
    app = approvals.ToolApprovalTests.app
    call = staticmethod(approvals.ToolApprovalTests.call)
    start = staticmethod(approvals.ToolApprovalTests.start)
    policy = staticmethod(approvals.ToolApprovalTests.policy)
    resolve = staticmethod(approvals.ToolApprovalTests.resolve)
    until = approvals.ToolApprovalTests.until

    def python(self, agent, *, name="core_task_list", arguments=None, prior=False,
               stop_error=False, before_stop=None, early=False):
        self.prefix = self.remainder = self.stops = 0
        self.thread_errors = []
        self.stopping = []
        self.observed_early_request = False

        def execute(_manager, **kwargs):
            self.prefix += 1

            def cancel(_process_id):
                current = agent.workflow_store.lookup_task("task")
                self.stopping.append(copy.deepcopy(current))
                self.stops += 1
                if before_stop:
                    before_stop()
                if stop_error:
                    raise CoreError("SIDE_EFFECT_UNKNOWN", "test cleanup failed")

            session = SimpleNamespace(cancel=cancel)
            result = ExecutionResult(-9, "prefix output\n", "", (), (), status="failed", cleanup="sandbox_terminated")
            session.wait = lambda *_: result
            started = threading.Event()

            def broker():
                started.set()
                try:
                    if prior:
                        kwargs["dispatch"]("core_task_list", {})
                    try:
                        kwargs["dispatch"](name, arguments or {}, "stable-request")
                    except Exception:
                        pass  # User Python may catch every ordinary exception.
                    self.remainder += 1
                except BaseException as error:
                    self.thread_errors.append(error)

            context = copy_context()
            thread = threading.Thread(target=context.run, args=(broker,))
            if early:
                thread.start()
                started.wait(1)
                # The runtime admission hook is the synchronization boundary;
                # wait for its stopping checkpoint before registration arrives.
                import time
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    if agent.workflow_store.lookup_task("task").snapshot.get("python_execution", {}).get("phase") == "stopping":
                        self.observed_early_request = True
                        break
                    threading.Event().wait(.001)
            if kwargs.get("on_start"):
                kwargs["on_start"](session, SimpleNamespace(id="process"))
            if not early:
                thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            return result

        return patch("core_agent.runtime.execute_python", side_effect=execute)

    def setup_run(self):
        agent, model = self.app(self.call("core_python_exec", {"code": "prefix; tools.call(); remainder"}), ModelResponse(message="done"))
        self.policy(agent, "allow", "core_python_exec")
        return agent, model

    def test_approval_stops_before_safe_wait_then_only_nested_call_resumes(self):
        agent, model = self.setup_run()
        with self.python(agent, early=True):
            sleeping = self.start(agent)
        self.assertIsInstance(sleeping, SuspendedRun)
        self.assertEqual((self.prefix, self.remainder, self.stops), (1, 0, 1))
        self.assertTrue(self.observed_early_request)
        self.assertEqual(self.stopping[0].state, "EXECUTING")
        self.assertEqual(self.stopping[0].snapshot["python_execution"]["phase"], "stopping")
        wait = agent.workflow_store.get_wait(sleeping.wait_id, tenant_id="company")
        self.assertEqual(wait.continuation["phase"], "python_nested")
        record = agent.workflow_store.lookup_task("task")
        self.assertEqual(record.snapshot["tool_calls"], 2)
        self.assertEqual(record.snapshot["python_execution"]["phase"], "stopped")
        self.assertNotIn(record.run_id, agent.workflow_store._leases)
        self.assertEqual(self.executed, [])
        self.resolve(agent, sleeping, "allowed")
        self.resolve(agent, sleeping, "allowed")
        agent._runtime_cache.clear()
        with patch("core_agent.runtime.execute_python", side_effect=AssertionError("Python replay")):
            result = agent.resume_task("task")
            self.assertEqual(agent.resume_task("task").message, "done")
        self.assertEqual((result.usage.tool_calls, result.shared_budget["used"]["tool_calls"]), (2, 2))
        self.assertEqual(len(self.executed), 1)
        self.assertIn("PYTHON_CONTINUATION_INTERRUPTED", model.calls[-1].context)
        self.assertIn("prefix output", model.calls[-1].context)
        results = [json.loads(item["content"]) for item in model.calls[-1].messages if item["role"] == "tool"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["tool_call_id"], "call")

    def test_rejection_timeout_and_deny_never_dispatch_nested(self):
        for reason, expected in (("rejected", "OWNER_APPROVAL_REJECTED"), ("timeout", "OWNER_APPROVAL_TIMEOUT"), ("deny", "POLICY_DENIED")):
            with self.subTest(reason=reason):
                agent, model = self.setup_run()
                with self.python(agent):
                    sleeping = self.start(agent)
                self.assertIsInstance(sleeping, SuspendedRun)
                if reason == "deny":
                    self.policy(agent, "deny")
                elif reason == "timeout":
                    self.now[0] += 86401
                    agent.workflow_store.expire_waits()
                else:
                    self.resolve(agent, sleeping, reason)
                self.assertEqual(agent.resume_task("task").message, "done")
                self.assertEqual(self.executed, [])
                self.assertIn(expected, model.calls[-1].context)

    def test_allow_during_stop_preserves_original_approval_requirement(self):
        agent, _ = self.setup_run()
        with self.python(agent, before_stop=lambda: self.policy(agent, "allow")):
            sleeping = self.start(agent)
        self.assertIsInstance(sleeping, SuspendedRun)
        self.assertEqual(self.executed, [])
        self.assertEqual(agent.resume_task("task").wait_id, sleeping.wait_id)

    def test_changed_schema_or_deny_after_approval_prevents_nested_dispatch(self):
        for deny in (False, True):
            with self.subTest(deny=deny):
                agent, model = self.setup_run()
                with self.python(agent):
                    sleeping = self.start(agent)
                self.assertIsInstance(sleeping, SuspendedRun)
                self.resolve(agent, sleeping, "allowed")
                if deny:
                    self.policy(agent, "deny")
                else:
                    definition = agent.tool_runtime.registry.get("core_task_list")
                    agent.tool_runtime.registry._tools[definition.name] = replace(definition, input_schema={**definition.input_schema, "required": ["added"]})
                self.assertEqual(agent.resume_task("task").message, "done")
                self.assertEqual(self.executed, [])
                self.assertIn("POLICY_DENIED" if deny else "TOOL_APPROVAL_STALE", model.calls[-1].context)

    def test_failed_stop_never_becomes_a_safe_wait(self):
        agent, _ = self.setup_run()
        with self.python(agent, stop_error=True), self.assertRaises(CoreError) as caught:
            self.start(agent)
        self.assertEqual(caught.exception.code, "SIDE_EFFECT_UNKNOWN")
        record = agent.workflow_store.lookup_task("task")
        self.assertEqual(record.state, "ABORTED")
        self.assertFalse(record.snapshot.get("wait_id"))
        self.assertEqual(self.remainder, 0)

    def test_nested_timer_reuses_durable_wait_without_python_replay(self):
        agent, model = self.setup_run()
        self.policy(agent, "allow", "core_wait_until")
        with self.python(agent, name="core_wait_until", arguments={"until": self.until()}):
            sleeping = self.start(agent)
        self.assertIsInstance(sleeping, SuspendedRun)
        self.assertEqual(agent.workflow_store.get_wait(sleeping.wait_id, tenant_id="company").kind, "timer")
        self.now[0] += 61
        agent.workflow_store.expire_waits()
        self.assertEqual(agent.resume_task("task").message, "done")
        self.assertIn('"reason": "time"', model.calls[-1].context)
        self.assertEqual((self.prefix, self.remainder), (1, 0))


    def test_prior_outcomes_and_owner_question_are_preserved_without_replay(self):
        from core_agent.config import AgentConfig
        agent, model = self.app(self.call("core_python_exec", {"code": "question"}), ModelResponse(message="answered"))
        # Explicit unit deployment enables owner questions without HTTP auth fixtures.
        agent.platform_config = replace(agent.platform_config,
            allowed_builtin_tools=agent.platform_config.allowed_builtin_tools | {"core_ask_owner"})
        raw = agent.agent_config.to_dict()
        raw["features"]["human_input"] = True
        raw["tools"]["builtins"]["allow"].append("core_ask_owner")
        agent.agent_config = AgentConfig.from_dict(raw)
        for name in ("core_python_exec", "core_task_list", "core_ask_owner"):
            self.policy(agent, "allow", name)
        executed = []
        agent.tool_runtime.handlers["core_task_list"] = lambda *_: executed.append(1) or {"prior": "known"}
        with self.python(agent, name="core_ask_owner", arguments={"question": "Which one?"}, prior=True):
            sleeping = self.start(agent)
        self.assertIsInstance(sleeping, SuspendedRun)
        record = agent.workflow_store.lookup_task("task")
        frame = record.snapshot["python_execution"]
        self.assertEqual(frame["request_id"], "stable-request")
        self.assertEqual(frame["completed"][0]["output"], {"prior": "known"})
        agent.enqueue_message({"prompt": "extra detail"}, task_id="task", message_id="followup", identity="owner", session_id="chat", tenant_id="company")
        self.assertEqual(agent.resume_task("task").wait_id, sleeping.wait_id)
        self.assertEqual(len(agent.workflow_store.pending_inbound(agent.workflow_store.lookup_task("task"))), 1)
        agent.workflow_store.resolve_wait(sleeping.wait_id, tenant_id="company", outcome={"reason": "answer", "answer": "second"}, actor_id="owner-actor")
        self.assertEqual(agent.resume_task("task").message, "answered")
        self.assertEqual(executed, [1])
        self.assertIn('"answer": "second"', model.calls[-1].context)
        self.assertIn("extra detail", model.calls[-1].context)

    def test_cancel_during_stop_never_enters_wait_or_executes_nested(self):
        agent, _ = self.setup_run()
        def cancel():
            record = agent.workflow_store.lookup_task("task")
            agent.workflow_store.request_cancel(record.run_id, tenant_id=record.tenant_id, owner_id=record.owner_id)
        with self.python(agent, before_stop=cancel), self.assertRaises(CoreError) as caught:
            self.start(agent)
        self.assertEqual(caught.exception.code, "TASK_CANCELLED")
        record = agent.workflow_store.lookup_task("task")
        self.assertEqual(record.state, "CANCELLED")
        self.assertFalse(record.snapshot.get("wait_id"))
        self.assertEqual(self.executed, [])

    def test_stopped_checkpoint_recovers_frozen_call_without_restarting_python(self):
        agent, model = self.setup_run()
        original = agent._tool_dispatch_intent
        def crash(record, snapshot, call, *args, **kwargs):
            if snapshot.get("python_execution", {}).get("phase") == "stopped":
                raise SystemExit("worker lost after confirmed stop")
            return original(record, snapshot, call, *args, **kwargs)
        with self.python(agent), patch.object(agent, "_tool_dispatch_intent", side_effect=crash), self.assertRaises(SystemExit):
            self.start(agent)
        checkpoint = agent.workflow_store.lookup_task("task")
        self.assertEqual(checkpoint.state, "MODEL_RESPONDED")
        self.assertEqual(checkpoint.snapshot["python_execution"]["phase"], "stopped")
        agent._runtime_cache.clear()
        sleeping = agent.resume_task("task")
        self.assertIsInstance(sleeping, SuspendedRun)
        self.resolve(agent, sleeping, "allowed")
        self.assertEqual(agent.resume_task("task").message, "done")
        self.assertEqual(len(self.executed), 1)
        self.assertEqual((self.prefix, self.remainder), (1, 0))
        self.assertIn("PYTHON_CONTINUATION_INTERRUPTED", model.calls[-1].context)

    def test_nested_task_wait_holds_followups_and_does_not_cancel_on_timeout(self):
        from core_agent.config import AgentConfig
        agent, model = self.setup_run()
        agent.platform_config = replace(agent.platform_config,
            allowed_builtin_tools=agent.platform_config.allowed_builtin_tools | {"core_task_wait"})
        raw = agent.agent_config.to_dict()
        raw["tools"]["builtins"]["allow"].append("core_task_wait")
        agent.agent_config = AgentConfig.from_dict(raw)
        self.policy(agent, "allow", "core_task_wait")
        record, *_ = agent._new_workflow({"prompt": "wait"}, task_id="task", identity="owner", session_id="chat", tenant_id="company")
        release = threading.Event()
        self.addCleanup(release.set)
        task = agent.task_scheduler.start(lambda: release.wait(5), task_id="child", owner_id=record.run_id, tenant_id="company")
        with self.python(agent, name="core_task_wait", arguments={"task_id": "child", "timeout": 30}):
            sleeping = agent.resume_task("task")
        self.assertIsInstance(sleeping, SuspendedRun)
        wait = agent.workflow_store.get_wait(sleeping.wait_id, tenant_id="company")
        self.assertEqual(wait.kind, "task")
        agent.enqueue_message({"prompt": "extra detail"}, task_id="task", message_id="followup", identity="owner", session_id="chat", tenant_id="company")
        self.assertEqual(agent.resume_task("task").wait_id, sleeping.wait_id)
        self.now[0] += 31
        agent.workflow_store.expire_waits()
        self.assertFalse(task.cancel_event.is_set())
        release.set()
        agent.task_scheduler.wait(task.id, timeout=2, owner_id=record.run_id, tenant_id="company")
        self.assertEqual(agent.resume_task("task").message, "done")
        self.assertIn("extra detail", model.calls[-1].context)
        self.assertIn("PYTHON_CONTINUATION_INTERRUPTED", model.calls[-1].context)
        self.assertEqual((self.prefix, self.remainder), (1, 0))

    def test_joined_nested_delegate_resumes_outer_call_after_child_result(self):
        agent, model = self.app(self.call("core_python_exec", {"code": "nested delegate"}),
                                ModelResponse(message="child done"), ModelResponse(message="parent done"))
        self.policy(agent, "allow", "core_python_exec")
        self.policy(agent, "allow", "core_delegate")
        arguments = {"instruction": "child", "tools": [], "skills": [],
                     "budget": {"turns": 2, "tool_calls": 1}}
        with self.python(agent, name="core_delegate", arguments=arguments):
            sleeping = self.start(agent)
        record = agent.workflow_store.lookup_task("task")
        wait = agent.workflow_store.get_wait(sleeping.wait_id, tenant_id="company")
        child = agent.task_scheduler.wait(wait.subject["task_id"], timeout=2,
                                         owner_id=record.run_id, tenant_id="company")
        agent.workflow_store.resolve_wait(wait.wait_id, tenant_id="company",
                                         outcome={"reason": "task", "result": agent._task_snapshot(child)})
        self.assertEqual(agent.resume_task("task").message, "parent done")
        self.assertIn("child done", model.calls[-1].context)
        self.assertIn("PYTHON_CONTINUATION_INTERRUPTED", model.calls[-1].context)
        self.assertEqual((self.prefix, self.remainder, self.stops), (1, 0, 1))

    def test_new_approval_required_during_stop_gets_finite_timeout(self):
        agent, _model = self.setup_run()
        self.policy(agent, "allow", "core_wait_until")
        with self.python(agent, name="core_wait_until", arguments={"until": self.until()},
                         before_stop=lambda: self.policy(agent, "require_hitl", "core_wait_until")):
            sleeping = self.start(agent)
        wait = agent.workflow_store.get_wait(sleeping.wait_id, tenant_id="company")
        self.assertEqual(wait.kind, "tool_approval")
        self.assertEqual(wait.deadline, self.now[0] + 86400)
        self.now[0] += 86401
        agent.workflow_store.expire_waits()
        wait = agent.workflow_store.get_wait(sleeping.wait_id, tenant_id="company")
        self.assertEqual(wait.outcome["reason"], "timeout")

    def test_pending_approval_of_outer_python_is_not_approval_of_nested_call(self):
        agent, _ = self.setup_run()
        self.policy(agent, "require_hitl", "core_python_exec")
        outer = self.start(agent)
        self.resolve(agent, outer, "allowed")
        with self.python(agent):
            inner = agent.resume_task("task")
        self.assertIsInstance(inner, SuspendedRun)
        self.assertNotEqual(inner.wait_id, outer.wait_id)
        self.assertEqual(agent.workflow_store.get_wait(inner.wait_id, tenant_id="company").subject["tool_name"], "core_task_list")
        self.assertEqual(self.executed, [])

    def test_expired_timer_returns_in_python_without_interrupting(self):
        agent, model = self.setup_run()
        self.policy(agent, "allow", "core_wait_until")
        with self.python(agent, name="core_wait_until", arguments={"until": self.until(-1)}):
            self.assertEqual(self.start(agent).message, "done")
        self.assertEqual((self.prefix, self.remainder, self.stops), (1, 1, 0))
        self.assertNotIn("PYTHON_CONTINUATION_INTERRUPTED", model.calls[-1].context)

    def test_nested_timer_without_owner_settings_uses_same_stop_and_budget_path(self):
        agent, model = self.setup_run()
        agent.interaction_store = None
        with self.python(agent, name="core_wait_until", arguments={"until": self.until()}):
            sleeping = self.start(agent)
        self.assertIsInstance(sleeping, SuspendedRun)
        self.assertEqual(agent.workflow_store.lookup_task("task").snapshot["tool_calls"], 2)
        self.now[0] += 61
        agent.workflow_store.expire_waits()
        self.assertEqual(agent.resume_task("task").usage.tool_calls, 2)
        self.assertIn("PYTHON_CONTINUATION_INTERRUPTED", model.calls[-1].context)


class PythonBrokerWaitTests(unittest.TestCase):
    def test_broker_setup_failure_is_proven_before_process_dispatch(self):
        from unittest.mock import Mock
        from core_agent.errors import ExecutionNotStarted
        from core_agent.python_exec import execute_python

        for stage in ("tempfile.mkdtemp", "socket.socket"):
            with self.subTest(stage=stage), patch("core_agent.python_exec." + stage, side_effect=OSError("unavailable")):
                manager = Mock()
                with self.assertRaises(ExecutionNotStarted) as caught:
                    execute_python(manager, run_id="run", code="pass", tool_names=(), dispatch=None)
                self.assertEqual(caught.exception.code, "TOOL_START_FAILED")
                manager.execute_transient.assert_not_called()

    def broker(self, dispatch):
        from core_agent.python_exec import PythonToolBroker
        broker = PythonToolBroker("pass", ("protected",), dispatch)
        self.addCleanup(shutil.rmtree, broker.directory, True)
        server, client = socket.socketpair()
        stream = client.makefile("rwb")
        self.addCleanup(client.close)
        self.addCleanup(stream.close)
        self.addCleanup(server.close)
        done = threading.Event()
        def serve():
            try:
                broker._handle(server)
            finally:
                done.set()
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        broker._send(stream, {"token": broker.token})
        self.assertEqual(broker._receive(stream)["tools"], ["protected"])
        return broker, client, stream, done

    def test_stop_signal_is_never_sent_as_catchable_reply_and_request_id_survives(self):
        from core_agent.python_exec import PythonContinuationStopped
        requested = threading.Event()
        release = threading.Event()
        calls = []
        def dispatch(name, arguments, request_id):
            calls.append((name, arguments, request_id))
            requested.set()
            release.wait(3)
            raise PythonContinuationStopped()
        broker, client, stream, done = self.broker(dispatch)
        broker._send(stream, {"id": "stable", "name": "protected", "arguments": {"value": 1}})
        self.assertTrue(requested.wait(1))
        client.settimeout(.03)
        with self.assertRaises(TimeoutError):
            client.recv(1)
        release.set()
        self.assertTrue(done.wait(1))
        self.assertEqual(calls, [("protected", {"value": 1}, "stable")])

    def test_unconfirmed_stop_holds_connection_without_error_eof_or_dispatch(self):
        from core_agent.python_exec import PythonContinuationStopped
        requested = threading.Event()
        calls = []
        def dispatch(*args):
            calls.append(args)
            requested.set()
            raise PythonContinuationStopped(cleanup_failed=True)
        broker, client, stream, done = self.broker(dispatch)
        broker._send(stream, {"id": "first", "name": "protected", "arguments": {}})
        self.assertTrue(requested.wait(1))
        broker._send(stream, {"id": "second", "name": "protected", "arguments": {}})
        client.settimeout(.03)
        with self.assertRaises(TimeoutError):
            client.recv(1)
        self.assertFalse(done.is_set())
        self.assertEqual(len(calls), 1)
        client.shutdown(socket.SHUT_RDWR)
        self.assertTrue(done.wait(1))

    def test_duplicate_broker_request_is_never_dispatched_twice(self):
        calls = []
        broker, client, stream, done = self.broker(lambda *args: calls.append(args) or {"done": True})
        request = {"id": "repeat", "name": "protected", "arguments": {}}
        broker._send(stream, request)
        self.assertTrue(broker._receive(stream)["ok"])
        broker._send(stream, request)
        self.assertFalse(broker._receive(stream)["ok"])
        self.assertEqual(len(calls), 1)
        client.shutdown(socket.SHUT_RDWR)
        self.assertTrue(done.wait(1))
