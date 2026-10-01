import os
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from core_agent.errors import CoreError, ExecutionNotStarted
from core_agent.execution import ExecutionResult
from core_agent.interactions import InMemoryInteractionStore, tool_origin
from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
from core_agent.mcp import InMemoryMcpConnector
from core_agent.remote_agents import RemoteEvent
from core_agent.workflow import SuspendedRun
from tests import test_durable_waits as durable_waits
from tests.test_runtime_observability import DOCS_MCP, make_agent


class ToolApprovalTests(unittest.TestCase):
    setUp = durable_waits.DurableTimerTests.setUp
    until = durable_waits.DurableTimerTests.until

    def app(self, *responses):
        with patch.dict(os.environ, {"CORE_AGENT_ALLOWED_BUILTIN_TOOLS": "core_task_list,core_wait_until,core_delegate,core_terminal_exec,core_task_start,core_python_exec"}):
            agent, model = durable_waits.DurableTimerTests.app(self, *responses)
        agent.interaction_store = InMemoryInteractionStore(agent.workflow_store)
        self.executed = []
        agent.tool_runtime.handlers["core_task_list"] = lambda args, run_id: self.executed.append((args, run_id)) or {"count": 17}
        return agent, model

    @staticmethod
    def call(name="core_task_list", arguments=None, call_id="call"):
        return ModelResponse(tool_requests=(ToolRequest(call_id, name, arguments or {}),))

    @staticmethod
    def start(agent, **scope):
        return agent.run({"prompt": "perform task"}, task_id="task", identity="owner", session_id="chat", tenant_id="company", **scope)

    @staticmethod
    def policy(agent, mode, name="core_task_list"):
        store = agent.interaction_store
        current = store.get_policy("company", name, tool_origin(name))
        return store.update_policy("company", name, current.origin, mode=mode, guardrails_exempt=False, expected_revision=current.revision, actor_id="owner-actor")

    @staticmethod
    def resolve(agent, sleeping, reason):
        return agent.workflow_store.resolve_wait(sleeping.wait_id, tenant_id="company", outcome={"reason": reason}, actor_id="owner-actor")

    def test_approval_freezes_call_and_resumes_once_without_charging_twice(self):
        agent, model = self.app(self.call(), ModelResponse(message="done"))
        sleeping = self.start(agent)
        self.assertIsInstance(sleeping, SuspendedRun)
        self.assertEqual(self.executed, [])
        wait = agent.workflow_store.get_wait(sleeping.wait_id, tenant_id="company")
        self.assertEqual(wait.kind, "tool_approval")
        self.assertEqual(wait.subject["arguments"], {})
        self.assertEqual(wait.subject["origin"], "builtin:core_task_list")
        self.assertEqual(wait.deadline, self.now[0] + 86400)
        self.assertEqual(wait.continuation["phase"], "tool_gate")
        self.assertEqual(agent.resume_task("task").wait_id, sleeping.wait_id)
        self.assertEqual(len(model.calls), 1)
        self.resolve(agent, sleeping, "allowed")
        agent._runtime_cache.clear()
        result = agent.resume_task("task")
        self.assertEqual(result.message, "done")
        self.assertEqual(result.usage.tool_calls, 1)
        self.assertEqual(len(self.executed), 1)
        self.assertIn('"count": 17', model.calls[-1].context)
        self.assertEqual(agent.resume_task("task").message, "done")
        self.assertEqual(len(self.executed), 1)

    def test_proven_prestart_error_continues_but_unknown_execution_does_not(self):
        for exception_type in (ExecutionNotStarted, CoreError):
            with self.subTest(exception_type=exception_type.__name__):
                agent, model = self.app(
                    self.call("core_terminal_exec", {"argv": ["true"]}),
                    ModelResponse(message="continued after unavailable execution"),
                )
                self.policy(agent, "allow", "core_terminal_exec")
                def unavailable(arguments, run_id):
                    raise exception_type("EXECUTION_ENVIRONMENT_UNAVAILABLE")
                agent.tool_runtime.handlers["core_terminal_exec"] = unavailable
                if exception_type is ExecutionNotStarted:
                    result = self.start(agent)
                    self.assertEqual(result.message, "continued after unavailable execution")
                    self.assertIn("EXECUTION_ENVIRONMENT_UNAVAILABLE", model.calls[-1].context)
                else:
                    with self.assertRaises(CoreError) as caught:
                        self.start(agent)
                    self.assertEqual(caught.exception.code, "SIDE_EFFECT_UNKNOWN")
                    self.assertEqual(len(model.calls), 1)

    def test_reject_timeout_and_policy_deny_return_tool_result_without_dispatch(self):
        for outcome, expected in (("rejected", "OWNER_APPROVAL_REJECTED"), ("timeout", "OWNER_APPROVAL_TIMEOUT"), ("deny", "POLICY_DENIED")):
            with self.subTest(outcome=outcome):
                agent, model = self.app(self.call(), ModelResponse(message="continued"))
                sleeping = self.start(agent)
                self.assertIsInstance(sleeping, SuspendedRun)
                if outcome == "timeout":
                    self.now[0] += 86400
                    agent.workflow_store.expire_waits()
                elif outcome == "deny":
                    self.policy(agent, "deny")
                else:
                    self.resolve(agent, sleeping, outcome)
                result = agent.resume_task("task")
                self.assertEqual(result.message, "continued")
                self.assertEqual(self.executed, [])
                self.assertIn(expected, model.calls[-1].context)
                self.assertEqual(result.usage.tool_calls, 1)

    def test_switch_to_allow_does_not_release_existing_wait(self):
        agent, model = self.app(self.call(), ModelResponse(message="done"))
        sleeping = self.start(agent)
        self.assertIsInstance(sleeping, SuspendedRun)
        self.policy(agent, "allow")
        self.assertEqual(agent.resume_task("task").wait_id, sleeping.wait_id)
        self.assertEqual(len(model.calls), 1)
        self.resolve(agent, sleeping, "allowed")
        self.assertEqual(agent.resume_task("task").message, "done")

    def test_denied_tool_is_hidden_and_stale_call_is_structured_denial(self):
        agent, model = self.app(self.call(), ModelResponse(message="continued"))
        self.policy(agent, "deny")
        self.assertEqual(self.start(agent).message, "continued")
        self.assertTrue(all("core_task_list" not in call.tools for call in model.calls))
        self.assertIn("POLICY_DENIED", model.calls[-1].context)
        self.assertEqual(self.executed, [])

    def test_deny_after_approval_wins_before_dispatch(self):
        agent, model = self.app(self.call(), ModelResponse(message="continued"))
        sleeping = self.start(agent)
        self.assertIsInstance(sleeping, SuspendedRun)
        self.resolve(agent, sleeping, "allowed")
        self.policy(agent, "deny")
        self.assertEqual(agent.resume_task("task").message, "continued")
        self.assertEqual(self.executed, [])
        self.assertIn("POLICY_DENIED", model.calls[-1].context)

    def test_changed_schema_cannot_reuse_approval(self):
        agent, model = self.app(self.call(), ModelResponse(message="continued"))
        sleeping = self.start(agent)
        self.assertIsInstance(sleeping, SuspendedRun)
        self.resolve(agent, sleeping, "allowed")
        definition = agent.tool_runtime.registry.get("core_task_list")
        agent.tool_runtime.registry._tools[definition.name] = replace(definition, input_schema={**definition.input_schema, "required": ["new_argument"]})
        self.assertEqual(agent.resume_task("task").message, "continued")
        self.assertEqual(self.executed, [])
        self.assertIn("TOOL_APPROVAL_STALE", model.calls[-1].context)

    def test_timer_first_waits_for_approval_then_its_own_deadline(self):
        agent, model = self.app(self.call("core_wait_until", {"until": self.until()}), ModelResponse(message="awake"))
        approval = self.start(agent)
        self.assertIsInstance(approval, SuspendedRun)
        self.assertEqual(agent.workflow_store.get_wait(approval.wait_id, tenant_id="company").kind, "tool_approval")
        self.resolve(agent, approval, "allowed")
        timer = agent.resume_task("task")
        self.assertIsInstance(timer, SuspendedRun)
        self.assertNotEqual(timer.wait_id, approval.wait_id)
        self.assertEqual(agent.workflow_store.get_wait(timer.wait_id, tenant_id="company").kind, "timer")
        self.now[0] += 61
        agent.workflow_store.expire_waits()
        self.assertEqual(agent.resume_task("task").message, "awake")
        self.assertEqual(len(model.calls), 2)

    def test_text_accompanying_tool_call_cannot_become_final_answer(self):
        agent, model = self.app(
            ModelResponse(message="private interim", tool_requests=(ToolRequest("call", "core_task_list", {}),)),
            ModelResponse(message="verified final"),
        )
        self.policy(agent, "allow")
        self.assertEqual(self.start(agent).message, "verified final")
        self.assertIn('"count": 17', model.calls[-1].context)

    def test_policy_refresh_removes_tool_on_next_model_turn(self):
        agent, model = self.app(self.call(), ModelResponse(message="done"))
        self.policy(agent, "allow")
        agent.tool_runtime.handlers["core_task_list"] = lambda *_: self.policy(agent, "deny")
        self.assertEqual(self.start(agent).message, "done")
        self.assertIn("core_task_list", model.calls[0].tools)
        self.assertNotIn("core_task_list", model.calls[1].tools)

    def test_child_uses_same_owner_policy_and_can_suspend_parent(self):
        delegate = self.call("core_delegate", {"instruction": "list tasks", "tools": ["core_task_list"], "skills": [], "budget": {"turns": 3, "tool_calls": 1}})
        agent, model = self.app(delegate, self.call(), ModelResponse(message="child done"), ModelResponse(message="parent done"))
        self.policy(agent, "allow", "core_delegate")
        parent_wait = self.start(agent)
        self.assertIsInstance(parent_wait, SuspendedRun)
        with agent.task_scheduler._lock:
            workers = tuple(agent.task_scheduler._threads)
        for worker in workers:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        wait = agent.workflow_store.get_wait(parent_wait.wait_id, tenant_id="company")
        child = agent.workflow_store.lookup_task(wait.subject["task_id"])
        child_wait = agent.workflow_store.get_wait(child.snapshot["wait_id"], tenant_id="company")
        self.assertEqual(child_wait.kind, "tool_approval")
        self.assertEqual(len(model.calls), 2)
        agent.workflow_store.resolve_wait(child_wait.wait_id, tenant_id="company", outcome={"reason": "allowed"}, actor_id="owner")
        self.assertEqual(agent.recover_durable_tasks(), 1)
        task = agent.task_scheduler.wait(child.task_id, timeout=5, owner_id=child.parent_run_id, tenant_id="company")
        self.assertEqual(task.state, "completed", task.error)
        with patch.object(agent, "_launch_recovery", return_value=False):
            agent._recover_workflows_once()
        self.assertEqual(agent.resume_task("task").message, "parent done")

    def test_nested_python_call_cannot_bypass_require_hitl_or_deny(self):
        agent, _ = self.app()
        record, _, discovered, effective = agent._new_workflow({"prompt": "python"}, task_id="task", identity="owner", session_id="chat", tenant_id="company")
        for mode, expected in (("require_hitl", "OWNER_APPROVAL_REQUIRED"), ("deny", "POLICY_DENIED")):
            with self.subTest(mode=mode):
                self.policy(agent, mode)
                with self.assertRaises(CoreError) as caught:
                    agent._python_tool_call("core_task_list", {}, run_id=record.run_id, discovered=discovered, effective=effective, parent_context=None)
                self.assertEqual(caught.exception.code, expected)
        self.assertEqual(self.executed, [])

    def test_background_target_cannot_bypass_require_hitl_or_deny(self):
        agent, _ = self.app()
        record, *_ = agent._new_workflow({"prompt": "background"}, task_id="task", identity="owner", session_id="chat", tenant_id="company")
        for mode, expected in (("require_hitl", "OWNER_APPROVAL_REQUIRED"), ("deny", "POLICY_DENIED")):
            with self.subTest(mode=mode):
                self.policy(agent, mode, "core_terminal_exec")
                with self.assertRaises(CoreError) as caught:
                    agent._task_start({"tool": "core_terminal_exec", "arguments": {"argv": ["true"]}}, record.run_id)
                self.assertEqual(caught.exception.code, expected)
        self.assertEqual(agent.task_scheduler.count(owner_id=record.run_id, tenant_id="company"), 0)

    def test_remote_agent_intermediate_parts_stay_private_for_external_task(self):
        agent, _ = self.app()
        connection = Mock(supports_streaming=True)
        secret_parts = ({"text": "private-remote-material-459"},)
        connection.stream_message.return_value = (RemoteEvent("artifact", "completed", "private remote answer", True, secret_parts),)
        agent.remote_agents = {"trusted": connection}
        for identity, published in (("external-client", False), ("owner", False)):
            with self.subTest(identity=identity):
                publisher = Mock(enabled=True)
                agent._run_scopes["remote-run"] = {"identity": identity, "task_id": "remote-task", "tenant_id": "company"}
                agent._task_streams["remote-task"] = publisher
                result = agent._send_message({"task": "question"}, "remote-run")
                self.assertEqual(result["result"], "private remote answer")
                self.assertEqual(publisher.relay.called, published)

    def test_mcp_policy_uses_trusted_origin_and_executes_only_approved_call(self):
        model = ScriptedModel((self.call("docs_search"), ModelResponse(message="done")))
        connector = InMemoryMcpConnector(catalogs={"docs": {"search": {"type": "object", "additionalProperties": False}}}, results={"docs.search": {"found": 42}})
        agent = make_agent(model, memory="disabled", connector=connector, platform_mcp=(DOCS_MCP,))
        self.addCleanup(agent.close)
        agent.interaction_store = InMemoryInteractionStore(agent.workflow_store)
        with patch.object(connector, "call", wraps=connector.call) as remote_call:
            sleeping = self.start(agent)
            self.assertIsInstance(sleeping, SuspendedRun)
            remote_call.assert_not_called()
            wait = agent.workflow_store.get_wait(sleeping.wait_id, tenant_id="company")
            self.assertEqual(wait.subject["origin"], tool_origin("docs_search", ("docs", "search")))
            self.resolve(agent, sleeping, "allowed")
            self.assertEqual(agent.resume_task("task").message, "done")
            remote_call.assert_called_once_with("docs", "search", {})
        self.assertIn('"found": 42', model.calls[-1].context)

    def test_reconnected_mcp_schema_change_or_removal_cannot_reuse_approval(self):
        for current_catalog in ({"search": {"type": "object", "required": ["new_argument"]}}, {}):
            with self.subTest(catalog=current_catalog):
                model = ScriptedModel((self.call("docs_search"), ModelResponse(message="continued")))
                connector = InMemoryMcpConnector(catalogs={"docs": {"search": {"type": "object", "additionalProperties": False}}})
                agent = make_agent(model, memory="disabled", connector=connector, platform_mcp=(DOCS_MCP,))
                self.addCleanup(agent.close)
                agent.interaction_store = InMemoryInteractionStore(agent.workflow_store)
                with patch.object(connector, "call", wraps=connector.call) as remote_call:
                    sleeping = self.start(agent)
                    original = agent.workflow_store.lookup_task("task")
                    connector.update_catalog("docs", current_catalog)
                    self.resolve(agent, sleeping, "allowed")
                    self.assertEqual(agent.resume_task("task").message, "continued")
                    remote_call.assert_not_called()
                    result = agent.workflow_store.lookup_task("task")
                    self.assertEqual(result.snapshot["effective_config_digest"], original.snapshot["effective_config_digest"])
                    self.assertEqual(result.snapshot["mcp_catalogs"], original.snapshot["mcp_catalogs"])
                self.assertIn("TOOL_APPROVAL_STALE", model.calls[-1].context)
                self.assertEqual("docs_search" in model.calls[-1].tools, "search" in current_catalog)

    def test_python_nested_dispatch_persists_fenced_exact_intent_and_usage(self):
        agent, _ = self.app(self.call("core_python_exec", {"code": "tools.call(...)"}), ModelResponse(message="done"))
        self.policy(agent, "allow", "core_python_exec")
        self.policy(agent, "allow")
        observed = []
        original_audit = agent.audit_log.append

        def audit(run_id, kind, data, **scope):
            if kind == "tool.execution.started" and data.get("source") == "core_python_exec":
                record = agent.workflow_store.get(run_id, tenant_id="company")
                observed.append(record.snapshot.get("nested_dispatch"))
                # This change commits after durable handoff; it affects the next call.
                self.policy(agent, "deny")
            return original_audit(run_id, kind, data, **scope)

        def python_handler(_arguments, run_id):
            _, discovered, effective = agent._runtime_cache[run_id]
            result = agent._python_tool_call("core_task_list", {}, run_id=run_id, discovered=discovered, effective=effective, parent_context=None)
            with self.assertRaises(CoreError) as caught:
                agent._python_tool_call("core_task_list", {}, run_id=run_id, discovered=discovered, effective=effective, parent_context=None)
            self.assertEqual(caught.exception.code, "POLICY_DENIED")
            return result

        agent.tool_runtime.handlers["core_python_exec"] = python_handler
        with patch.object(agent.audit_log, "append", side_effect=audit):
            result = self.start(agent)
        self.assertEqual(result.message, "done")
        self.assertEqual(len(self.executed), 1)
        self.assertEqual(len(observed), 1)
        self.assertIsNotNone(observed[0])
        self.assertEqual(observed[0]["subject"]["origin"], "builtin:core_task_list")
        self.assertEqual(observed[0]["subject"]["arguments"], {})
        self.assertEqual(observed[0]["state"], "executing")
        self.assertEqual(result.usage.tool_calls, 3)
        self.assertEqual(result.shared_budget["used"]["tool_calls"], 3)

    def background_app(self, *responses):
        agent, model = self.app(*responses)
        agent.tool_runtime.handlers["core_terminal_exec"] = lambda args, run_id: self.executed.append((args, run_id)) or {"background": "done"}
        self.policy(agent, "allow", "core_task_start")
        return agent, model

    def start_background_with_open_parent(self, agent):
        transition = agent._record_transition
        def stop_before_parent_completion(record, **change):
            if record.task_id == "task" and change["state"] == "COMPLETED":
                raise CoreError("WORKER_STOPPED")
            return transition(record, **change)
        with patch.object(agent, "_record_transition", side_effect=stop_before_parent_completion):
            with self.assertRaises(CoreError) as stopped:
                self.start(agent)
        self.assertEqual(stopped.exception.code, "WORKER_STOPPED")
        parent = agent.workflow_store.lookup_task("task")
        self.assertEqual(parent.state, "MODEL_RESPONDED")
        self.assertNotIn("terminal_intent", parent.snapshot)
        return parent

    def test_python_cannot_hide_unknown_nested_mutation_by_catching_error(self):
        agent, model = self.app(self.call("core_python_exec", {"code": "test"}), ModelResponse(message="must not finish"))
        for name in ("core_python_exec", "core_terminal_exec"):
            self.policy(agent, "allow", name)

        def mutation(_arguments, run_id):
            self.executed.append(run_id)
            raise RuntimeError("connection lost after dispatch")

        caught = []

        def python_handler(_arguments, run_id):
            _, discovered, effective = agent._runtime_cache[run_id]
            for _ in range(2):
                try:
                    agent._python_tool_call("core_terminal_exec", {"argv": ["true"]}, run_id=run_id,
                                            discovered=discovered, effective=effective, parent_context=None)
                except Exception as error:
                    caught.append(getattr(error, "code", type(error).__name__))
            return {"caught": True}

        agent.tool_runtime.handlers.update(core_python_exec=python_handler, core_terminal_exec=mutation)
        with self.assertRaises(CoreError) as failed:
            self.start(agent)
        self.assertEqual(failed.exception.code, "SIDE_EFFECT_UNKNOWN")
        record = agent.workflow_store.lookup_task("task")
        self.assertEqual((record.state, record.error_code), ("ABORTED", "SIDE_EFFECT_UNKNOWN"))
        self.assertEqual(caught, ["SIDE_EFFECT_UNKNOWN", "SIDE_EFFECT_UNKNOWN"])
        self.assertEqual(len(self.executed), 1)
        self.assertEqual(len(model.calls), 1)
        with self.assertRaises(CoreError) as resumed:
            agent.resume_task("task")
        self.assertEqual(resumed.exception.code, "SIDE_EFFECT_UNKNOWN")
        self.assertEqual(len(self.executed), 1)

    def test_python_cannot_hide_nested_budget_exhaustion_by_catching_error(self):
        for shared in (False, True):
            with self.subTest(shared=shared):
                with patch.dict(os.environ, {"CORE_AGENT_MAX_TOOL_CALLS": "5" if shared else "1"}):
                    agent, model = self.app(self.call("core_python_exec", {"code": "test"}), ModelResponse(message="partial"))
                for name in ("core_python_exec", "core_task_list"):
                    self.policy(agent, "allow", name)

                def python_handler(_arguments, run_id):
                    _, discovered, effective = agent._runtime_cache[run_id]
                    if shared:
                        record = agent.workflow_store.get(run_id, tenant_id="company")
                        agent.workflow_store.consume_budget(record, tool_calls=4)
                    with self.assertRaises(CoreError) as caught:
                        agent._python_tool_call("core_task_list", {}, run_id=run_id, discovered=discovered,
                                                effective=effective, parent_context=None)
                    self.assertEqual(caught.exception.code, "BUDGET_EXCEEDED")
                    return "caught"

                agent.tool_runtime.handlers["core_python_exec"] = python_handler
                result = self.start(agent)
                self.assertFalse(result.complete)
                self.assertEqual(result.completion_reason, "budget_exhausted")
                self.assertEqual(result.usage.tool_calls, 1)
                self.assertFalse(model.calls[-1].tools)
                self.assertEqual(self.executed, [])

    def test_known_failed_nested_command_remains_a_catchable_result(self):
        agent, _ = self.app(self.call("core_python_exec", {"code": "test"}), ModelResponse(message="explained"))
        for name in ("core_python_exec", "core_terminal_exec"):
            self.policy(agent, "allow", name)
        agent.tool_runtime.handlers["core_terminal_exec"] = lambda *_: ExecutionResult(1, "", "failed", (), (), status="failed")

        def python_handler(_arguments, run_id):
            _, discovered, effective = agent._runtime_cache[run_id]
            with self.assertRaises(CoreError) as caught:
                agent._python_tool_call("core_terminal_exec", {"argv": ["false"]}, run_id=run_id,
                                        discovered=discovered, effective=effective, parent_context=None)
            self.assertEqual(caught.exception.code, "TOOL_RETURNED_FAILED")
            return "explained"

        agent.tool_runtime.handlers["core_python_exec"] = python_handler
        self.assertTrue(self.start(agent).complete)

    def test_rejected_background_budget_does_not_persist_uncommitted_charge(self):
        with patch.dict(os.environ, {"CORE_AGENT_MAX_TOOL_CALLS": "1"}):
            agent, _ = self.background_app(self.call("core_task_start", {"tool": "core_terminal_exec", "arguments": {"argv": ["true"]}}), ModelResponse(message="started"))
        result = self.start_background_with_open_parent(agent)
        self.settle_workers(agent)
        task, = agent.task_scheduler.list(owner_id=result.run_id, tenant_id="company")
        child = agent.workflow_store.lookup_task(task.id)
        self.assertEqual((child.state, child.error_code), ("FAILED", "BUDGET_EXCEEDED"))
        self.assertEqual(child.snapshot["tool_calls"], 0)
        self.assertFalse(child.snapshot["background_charged"])
        self.assertEqual(agent.workflow_store._budgets[result.run_id][-1], 1)
        self.assertEqual(self.executed, [])

    def test_background_recovers_known_budget_failure_without_charging_again(self):
        with patch.dict(os.environ, {"CORE_AGENT_MAX_TOOL_CALLS": "1"}):
            agent, _ = self.background_app(self.call("core_task_start", {"tool": "core_terminal_exec", "arguments": {"argv": ["true"]}}), ModelResponse(message="started"))
        transition = agent._record_transition

        def stop_before_terminal(record, **kwargs):
            if record.snapshot.get("background_tool") and kwargs["state"] == "FAILED":
                raise CoreError("WORKER_STOPPED")
            return transition(record, **kwargs)

        with patch.object(agent, "_record_transition", side_effect=stop_before_terminal):
            result = self.start_background_with_open_parent(agent)
            self.settle_workers(agent)
        task, = agent.task_scheduler.list(owner_id=result.run_id, tenant_id="company")
        child = agent.workflow_store.lookup_task(task.id)
        self.assertIsNone(child.snapshot["pending_call"])
        self.assertFalse(child.snapshot["background_charged"])
        self.assertEqual(agent.recover_durable_tasks(), 1)
        self.settle_workers(agent)
        child = agent.workflow_store.lookup_task(task.id)
        self.assertEqual((child.state, child.error_code), ("FAILED", "BUDGET_EXCEEDED"))
        self.assertEqual(child.snapshot["tool_calls"], 0)
        self.assertEqual(self.executed, [])

    @staticmethod
    def settle_workers(agent):
        with agent.task_scheduler._lock:
            workers = tuple(agent.task_scheduler._threads)
        for worker in workers:
            worker.join(timeout=5)
            if worker.is_alive():
                raise AssertionError("background worker did not suspend or finish")

    def test_background_target_has_own_durable_approval_and_completed_recovery(self):
        agent, _ = self.background_app(self.call("core_task_start", {"tool": "core_terminal_exec", "arguments": {"argv": ["true"]}}), ModelResponse(message="handle returned"))
        result = self.start_background_with_open_parent(agent)
        self.settle_workers(agent)
        tasks = agent.task_scheduler.list(owner_id=result.run_id, tenant_id="company")
        self.assertEqual(len(tasks), 1)
        child = agent.workflow_store.lookup_task(tasks[0].id)
        self.assertEqual(child.state, "WAITING_INPUT")
        self.assertEqual(self.executed, [])
        self.assertEqual(child.snapshot["tool_calls"], 1)
        self.assertFalse(child.snapshot["finalization_turn_reserved"])
        self.assertEqual(child.snapshot["admission"]["agent_config"]["tools"]["builtins"]["allow"], ["core_terminal_exec"])
        wait = agent.workflow_store.get_wait(child.snapshot["wait_id"], tenant_id="company")
        self.assertEqual(wait.subject["tool_name"], "core_terminal_exec")
        agent.workflow_store.resolve_wait(wait.wait_id, tenant_id="company", outcome={"reason": "allowed"}, actor_id="owner")
        with self.assertRaises(CoreError) as direct_resume:
            agent.resume_task(tasks[0].id)
        self.assertEqual(direct_resume.exception.code, "INVALID_TASK_STATE")
        contract = agent.task_scheduler._recovery[tasks[0].id][1]
        self.assertEqual(agent.recover_durable_tasks(), 1)
        task = agent.task_scheduler.wait(tasks[0].id, timeout=5, owner_id=result.run_id, tenant_id="company")
        self.assertEqual(task.state, "completed", task.error)
        self.assertEqual(task.result, {"background": "done"})
        self.assertEqual(len(self.executed), 1)
        self.assertEqual(agent._recover_background_tool(contract, task.cancel_event), task.result)
        self.assertEqual(len(self.executed), 1)
        self.assertTrue(result.snapshot["finalization_turn_reserved"])
        self.assertEqual(agent.workflow_store._budgets[result.run_id][2:], [3, 2])

    def test_background_parent_admission_restart_reuses_single_child(self):
        agent, model = self.background_app(self.call("core_task_start", {"tool": "core_terminal_exec", "arguments": {"argv": ["true"]}}), ModelResponse(message="recovered"))
        record_outcome = agent._record_tool_outcome

        def crash_after_admission(record, snapshot, call, *args, **kwargs):
            if call.name == "core_task_start":
                raise CoreError("WORKER_STOPPED")
            return record_outcome(record, snapshot, call, *args, **kwargs)

        with patch.object(agent, "_record_tool_outcome", side_effect=crash_after_admission):
            with self.assertRaises(CoreError) as stopped:
                self.start(agent)
        self.assertEqual(stopped.exception.code, "WORKER_STOPPED")
        self.settle_workers(agent)
        parent = agent.workflow_store.lookup_task("task")
        self.assertEqual(parent.state, "MODEL_RESPONDED")
        self.assertEqual(agent.resume_task("task").message, "recovered")
        self.assertEqual(agent.task_scheduler.count(owner_id=parent.run_id, tenant_id="company"), 1)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(self.executed, [])

    def test_background_rejection_timeout_and_cancel_never_execute_target(self):
        for reason in ("rejected", "timeout", "cancelled"):
            with self.subTest(reason=reason):
                agent, _ = self.background_app(self.call("core_task_start", {"tool": "core_terminal_exec", "arguments": {"argv": ["true"]}}), ModelResponse(message="submitted"))
                result = self.start_background_with_open_parent(agent)
                self.settle_workers(agent)
                task = agent.task_scheduler.list(owner_id=result.run_id, tenant_id="company")[0]
                child = agent.workflow_store.lookup_task(task.id)
                if reason == "cancelled":
                    agent.task_scheduler.cancel(task.id, owner_id=result.run_id, tenant_id="company")
                elif reason == "timeout":
                    self.now[0] += 86400
                    agent.workflow_store.expire_waits()
                else:
                    agent.workflow_store.resolve_wait(child.snapshot["wait_id"], tenant_id="company", outcome={"reason": reason}, actor_id="owner")
                agent.recover_durable_tasks()
                task = agent.task_scheduler.wait(task.id, timeout=5, owner_id=result.run_id, tenant_id="company")
                self.assertEqual(task.state, "canceled" if reason == "cancelled" else "failed")
                child = agent.workflow_store.lookup_task(task.id)
                self.assertEqual(child.state, "CANCELLED" if reason == "cancelled" else "FAILED")
                self.assertEqual(self.executed, [])

    def test_background_known_result_survives_restart_before_terminal_commit(self):
        agent, _ = self.background_app(self.call("core_task_start", {"tool": "core_terminal_exec", "arguments": {"argv": ["true"]}}), ModelResponse(message="submitted"))
        result = self.start_background_with_open_parent(agent)
        self.settle_workers(agent)
        task = agent.task_scheduler.list(owner_id=result.run_id, tenant_id="company")[0]
        child = agent.workflow_store.lookup_task(task.id)
        contract = agent.task_scheduler._recovery[task.id][1]
        agent.workflow_store.resolve_wait(child.snapshot["wait_id"], tenant_id="company", outcome={"reason": "allowed"}, actor_id="owner")
        transition = agent._record_transition

        def stop_before_completion(record, **change):
            if record.run_id == child.run_id and change["state"] == "COMPLETED":
                raise CoreError("WORKER_STOPPED")
            return transition(record, **change)

        with patch.object(agent, "_record_transition", side_effect=stop_before_completion):
            suspended = agent._recover_background_tool(contract, task.cancel_event)
        self.assertIsInstance(suspended, SuspendedRun)
        self.assertEqual(len(self.executed), 1)
        self.assertEqual(agent._recover_background_tool(contract, task.cancel_event), {"background": "done"})
        self.assertEqual(len(self.executed), 1)

    def test_policy_deny_before_nested_intent_prevents_execution(self):
        agent, _ = self.app(self.call("core_python_exec", {"code": "tools.call(...)"}), ModelResponse(message="continued"))
        self.policy(agent, "allow", "core_python_exec")
        self.policy(agent, "allow")
        original_log = agent._log

        def log(event, **data):
            if event == "tool.requested" and data.get("source") == "core_python_exec":
                self.policy(agent, "deny")
            return original_log(event, **data)

        def python_handler(_arguments, run_id):
            _, discovered, effective = agent._runtime_cache[run_id]
            with self.assertRaises(CoreError) as caught:
                agent._python_tool_call("core_task_list", {}, run_id=run_id, discovered=discovered, effective=effective, parent_context=None)
            self.assertEqual(caught.exception.code, "POLICY_DENIED")
            return {"denied": True}

        agent.tool_runtime.handlers["core_python_exec"] = python_handler
        with patch.object(agent, "_log", side_effect=log):
            result = self.start(agent)
        self.assertEqual(result.usage.tool_calls, 2)
        self.assertEqual(self.executed, [])

    def test_background_rechecks_policy_after_worker_preparation(self):
        agent, _ = self.background_app(self.call("core_task_start", {"tool": "core_terminal_exec", "arguments": {"argv": ["true"]}}), ModelResponse(message="submitted"))
        self.policy(agent, "allow", "core_terminal_exec")
        bind = agent._bind_workspace

        def prepare(run_id, *scope):
            bind(run_id, *scope)
            if "-background-" in run_id:
                self.policy(agent, "deny", "core_terminal_exec")

        with patch.object(agent, "_bind_workspace", side_effect=prepare):
            result = self.start_background_with_open_parent(agent)
            self.settle_workers(agent)
        task = agent.task_scheduler.list(owner_id=result.run_id, tenant_id="company")[0]
        self.assertEqual(task.state, "failed")
        self.assertEqual(task.error.code, "POLICY_DENIED")
        self.assertEqual(agent.workflow_store.lookup_task(task.id).state, "FAILED")
        self.assertEqual(self.executed, [])

    def test_external_task_suppresses_private_intermediate_stream(self):
        agent, model = self.app(ModelResponse(message="public final"))
        publisher = Mock(enabled=True)
        record, *_ = agent._new_workflow({"prompt": "external"}, task_id="external", identity="external-client", session_id="chat", tenant_id="company")
        agent._task_streams[record.task_id] = publisher
        self.assertFalse(agent._stream(record).enabled)


if __name__ == "__main__":
    unittest.main()
