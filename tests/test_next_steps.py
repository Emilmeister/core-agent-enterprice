import copy
import json
from dataclasses import replace
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

from core_agent.model import CompatibleHttpModel, ModelResponse, ScriptedModel, ToolRequest
from core_agent.config import AgentConfig, compile_effective_config
from core_agent.errors import CoreError
from core_agent.interactions import InMemoryInteractionStore
from core_agent.workflow import SuspendedRun
from tests.app_support import create_app
from tests import test_tool_approvals as approvals
from tests import test_python_waits as python_waits

NEXT_STEPS = "core_next_steps"
PLAN = {"previous_steps": [], "deal": "none", "stage": "", "current_step": "list tasks"}
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


def next_steps(call_id="plan"):
    return ModelResponse(tool_requests=(ToolRequest(call_id, NEXT_STEPS, dict(PLAN)),))


def task_list(call_id):
    return ModelResponse(tool_requests=(ToolRequest(call_id, "core_task_list", {}),))


class NextStepsRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict(os.environ, {
            "CORE_AGENT_ENVIRONMENT": "test", "CORE_AGENT_MEMORY": "disabled",
            "LOCAL_WORKSPACE_ROOT": self.temp.name + "/scratch",
            "CHAT_WORKSPACE_ROOT": self.temp.name + "/chats",
            "CORE_AGENT_ALLOWED_BUILTIN_TOOLS": "core_task_list,core_task_wait,core_delegate,core_next_steps",
            "CORE_AGENT_NEXT_STEPS": "true",
            "LOG_LEVEL": "ERROR",
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def app(self, *responses, model=None, **environment):
        model = model or ScriptedModel(responses)
        model.model = "next-steps-test"
        with patch.dict(os.environ, environment), \
                patch("core_agent.runtime.CoreAgent.recover_workflows", return_value=()):
            app = create_app(model=model)
        self.addCleanup(app.state.close)
        return app.state.core_agent, model

    def choices(self, model):
        return [call.tool_choice for call in model.calls]

    def test_disabled_never_offers_or_forces_next_steps(self):
        agent, model = self.app(ModelResponse(message="done"), CORE_AGENT_NEXT_STEPS="false")
        self.assertEqual(approvals.ToolApprovalTests.start(agent).message, "done")
        self.assertIsNone(model.calls[0].tool_choice)
        self.assertNotIn(NEXT_STEPS, model.calls[0].tools)
        record = agent.workflow_store.lookup_task("task")
        _, config, platform, declarations, _ = agent._admission_inputs(record)
        effective = compile_effective_config(platform, config, declarations, {})
        self.assertNotIn(NEXT_STEPS, effective.builtin_tools)
        with self.assertRaises(CoreError) as caught:
            effective.require_tool(NEXT_STEPS)
        self.assertEqual(caught.exception.code, "CAPABILITY_DISABLED")

    def test_owner_can_enable_planning_from_disabled_deployment_default(self):
        from core_agent.agent_settings import AgentSettingsStore

        agent, model = self.app(next_steps("p1"), task_list("t1"), task_list("t2"),
                                next_steps("p2"), ModelResponse(message="done"),
                                CORE_AGENT_NEXT_STEPS="false", CORE_AGENT_NEXT_STEPS_EVERY="9")
        agent.agent_settings_store = AgentSettingsStore(agent.workflow_store)
        agent.agent_settings_store.update("default", {"next_steps": {"enabled": True, "every": 2}},
                                         expected_revision=0, actor_id="owner")
        self.assertEqual(agent.run({"prompt": "hi"}).message, "done")
        self.assertEqual(self.choices(model), [NEXT_STEPS, None, None, NEXT_STEPS, None])
        self.assertIsNone(agent.next_steps_every)

    def test_owner_disabled_setting_and_deployment_deny_do_not_activate_planning(self):
        from core_agent.agent_settings import AgentSettingsStore

        for available, enabled in ((True, False), (False, True)):
            with self.subTest(available=available, enabled=enabled):
                agent, model = self.app(ModelResponse(message="done"),
                    CORE_AGENT_ALLOWED_BUILTIN_TOOLS="core_task_list,core_next_steps" if available else "core_task_list")
                agent.agent_settings_store = AgentSettingsStore(agent.workflow_store)
                agent.agent_settings_store.update("default", {"next_steps": {"enabled": enabled, "every": 2}},
                                                 expected_revision=0, actor_id="owner")
                self.assertEqual(agent.run({"prompt": "hi"}).message, "done")
                self.assertEqual(self.choices(model), [None])
                self.assertNotIn(NEXT_STEPS, model.calls[0].tools)

    def test_first_turn_is_forced_and_next_turn_is_not(self):
        agent, model = self.app(next_steps(), ModelResponse(message="done"))
        self.assertEqual(agent.run({"prompt": "hi"}).message, "done")
        self.assertEqual(self.choices(model), [NEXT_STEPS, None])
        self.assertIn(NEXT_STEPS, model.calls[0].tools)
        self.assertNotIn(NEXT_STEPS, model.calls[1].tools)
        self.assertIn(json.dumps("Принято. Выполни current_step.")[1:-1], model.calls[1].context)
        replayed = [call["function"]["name"] for message in model.calls[1].messages
                    for call in message.get("tool_calls", ())]
        self.assertIn(NEXT_STEPS, replayed)

    def test_forced_again_after_five_tool_calls_not_after_four(self):
        agent, model = self.app(next_steps("p1"), *(task_list(f"t{index}") for index in range(5)),
                                next_steps("p2"), ModelResponse(message="done"))
        self.assertEqual(agent.run({"prompt": "hi"}).message, "done")
        self.assertEqual(self.choices(model), [NEXT_STEPS, None, None, None, None, None, NEXT_STEPS, None])

    def test_every_is_configurable(self):
        agent, model = self.app(next_steps("p1"), task_list("t1"), task_list("t2"), next_steps("p2"),
                                ModelResponse(message="done"), CORE_AGENT_NEXT_STEPS_EVERY="2")
        self.assertEqual(agent.run({"prompt": "hi"}).message, "done")
        self.assertEqual(self.choices(model), [NEXT_STEPS, None, None, NEXT_STEPS, None])

    def test_forced_after_task_wait_result(self):
        wait = ModelResponse(tool_requests=(ToolRequest("wait", "core_task_wait", {"task_id": "missing"}),))
        agent, model = self.app(next_steps("p1"), wait, next_steps("p2"), ModelResponse(message="done"))
        self.assertEqual(agent.run({"prompt": "hi"}).message, "done")
        self.assertEqual(self.choices(model), [NEXT_STEPS, None, NEXT_STEPS, None])

    def test_owner_answer_and_peer_message_mark_next_steps_due(self):
        agent, _model = self.app(ModelResponse(message="unused"))
        snapshot = {"next_steps_every": 5, "next_steps_due": False, "next_steps_calls_since": 0}
        agent._note_next_steps(snapshot, "core_ask_owner", True)
        self.assertTrue(snapshot["next_steps_due"])
        agent._note_next_steps(snapshot, NEXT_STEPS, False)
        self.assertTrue(snapshot["next_steps_due"])
        agent._note_next_steps(snapshot, NEXT_STEPS, True)
        self.assertEqual(snapshot, {"next_steps_every": 5, "next_steps_due": False, "next_steps_calls_since": 0, "next_steps_failures": 0})

    def test_null_stage_is_rejected_and_two_invalid_calls_stop_the_requirement(self):
        invalid = {key: value for key, value in PLAN.items() if key != "stage"}
        broken = [ModelResponse(tool_requests=(ToolRequest(f"bad{index}", NEXT_STEPS, dict(invalid)),))
                  for index in range(2)]
        agent, model = self.app(*broken, ModelResponse(message="done"))
        with patch.object(agent, "_log", wraps=agent._log) as log:
            self.assertEqual(agent.run({"prompt": "hi"}).message, "done")
        self.assertEqual(self.choices(model), [NEXT_STEPS, NEXT_STEPS, None])
        self.assertIn("TOOL_ARGUMENT_INVALID", model.calls[1].context)
        self.assertEqual([call.args[0] for call in log.call_args_list].count("next_steps.invalid"), 1)

    def test_provider_null_stage_is_dropped_before_validation(self):
        self.assertEqual(CompatibleHttpModel._arguments('{"stage": null, "deal": "none"}'), {"deal": "none"})

    def test_ignored_force_is_executed_and_logged(self):
        agent, model = self.app(task_list("t1"), next_steps(), ModelResponse(message="done"))
        with patch.object(agent, "_log", wraps=agent._log) as log:
            self.assertEqual(agent.run({"prompt": "hi"}).message, "done")
        ignored = [call for call in log.call_args_list if call.args[0] == "next_steps.ignored"]
        self.assertEqual(len(ignored), 1)
        self.assertEqual(ignored[0].kwargs["tool_calls"], ["core_task_list"])
        self.assertEqual(self.choices(model), [NEXT_STEPS, NEXT_STEPS, None])

    def test_budget_finalization_is_not_forced(self):
        agent, model = self.app(task_list("t1"), ModelResponse(message="partial"), RUNTIME_MAX_LLM_CALLS="2")
        result = agent.run({"prompt": "hi"})
        self.assertFalse(result.complete)
        self.assertEqual(self.choices(model), [NEXT_STEPS, None])
        self.assertEqual(model.calls[1].tools, frozenset())

    def test_subagent_is_not_forced(self):
        delegate = ModelResponse(tool_requests=(ToolRequest("delegate", "core_delegate", {
            "instruction": "answer", "tools": ["core_task_list"], "skills": [],
            "budget": {"turns": 3, "tool_calls": 1}}),))
        agent, model = self.app(next_steps(), delegate, ModelResponse(message="child done"),
                                ModelResponse(message="parent done"))
        sleeping = agent.run({"prompt": "hi"}, task_id="task", identity="owner", session_id="chat",
                             tenant_id="company")
        parent = agent.workflow_store.lookup_task("task")
        child_id = agent.workflow_store.get_wait(sleeping.wait_id, tenant_id="company").subject["task_id"]
        agent.task_scheduler.wait(child_id, timeout=5, owner_id=parent.run_id, tenant_id="company")
        with patch.object(agent, "_launch_recovery", return_value=False):
            agent._recover_workflows_once()
        self.assertEqual(agent.resume_task("task").message, "parent done")
        self.assertEqual(self.choices(model), [NEXT_STEPS, None, None, None])
        self.assertNotIn(NEXT_STEPS, model.calls[2].tools)

    def test_hitl_wait_and_live_deny_do_not_bypass_policy_or_charge_twice(self):
        for deny in (False, True):
            with self.subTest(deny=deny):
                agent, model = self.app(next_steps(), ModelResponse(message="done"))
                agent.interaction_store = InMemoryInteractionStore(agent.workflow_store)
                approvals.ToolApprovalTests.policy(agent, "require_hitl", NEXT_STEPS)
                handler = agent.tool_runtime.handlers[NEXT_STEPS]
                with patch.dict(agent.tool_runtime.handlers, {NEXT_STEPS: unittest.mock.Mock(wraps=handler)}):
                    dispatch = agent.tool_runtime.handlers[NEXT_STEPS]
                    sleeping = approvals.ToolApprovalTests.start(agent)
                    self.assertIsInstance(sleeping, SuspendedRun)
                    dispatch.assert_not_called()
                    self.assertEqual(agent.resume_task("task").wait_id, sleeping.wait_id)
                    approvals.ToolApprovalTests.resolve(agent, sleeping, "allowed")
                    if deny:
                        approvals.ToolApprovalTests.policy(agent, "deny", NEXT_STEPS)
                    agent._runtime_cache.clear()
                    result = agent.resume_task("task")
                    self.assertEqual(result.message, "done")
                    self.assertEqual(result.usage.tool_calls, 1)
                    self.assertEqual(dispatch.call_count, 0 if deny else 1)
                    if deny:
                        self.assertIn("POLICY_DENIED", model.calls[-1].context)
                    self.assertIsNone(model.calls[-1].tool_choice)

    def test_admitted_cadence_and_kernel_survive_changed_runtime_settings(self):
        agent, model = self.app(next_steps("p1"), task_list("t1"), task_list("t2"),
                                next_steps("p2"), ModelResponse(message="done"),
                                CORE_AGENT_NEXT_STEPS_EVERY="2")
        record = agent._new_workflow({"prompt": "list tasks"}, task_id="pinned", identity="owner",
            session_id="chat", tenant_id="company", defer_initialization=True)[0]
        self.assertEqual(record.snapshot.get("next_steps_every"), 2)
        kernel = copy.deepcopy(record.snapshot["kernel_instructions"])
        agent.next_steps_every = 100
        agent.kernel_compiler.base_kernel = "NEW KERNEL MUST NOT REPLACE ADMITTED KERNEL"
        self.assertEqual(agent.resume_task("pinned").message, "done")
        self.assertEqual(self.choices(model), [NEXT_STEPS, None, None, NEXT_STEPS, None])
        saved = agent.workflow_store.lookup_task("pinned").snapshot
        self.assertEqual(saved["kernel_instructions"], kernel)
        self.assertEqual(saved["next_steps_every"], 2)
        self.assertTrue(all("NEW KERNEL" not in call.instructions for call in model.calls))
        self.assertTrue(all("core_response_begin" not in call.tools for call in model.calls))

    def test_legacy_snapshot_does_not_enable_planning_on_recovery(self):
        agent, model = self.app(ModelResponse(message="done"))
        record = agent._new_workflow({"prompt": "hi"}, task_id="legacy", identity="owner",
            session_id="chat", tenant_id="company", defer_initialization=True)[0]
        snapshot = copy.deepcopy(record.snapshot)
        snapshot.pop("next_steps_every", None)
        agent.workflow_store._records[record.run_id] = replace(record, snapshot=snapshot)
        self.assertEqual(agent.resume_task("legacy").message, "done")
        self.assertEqual(self.choices(model), [None])
        self.assertNotIn(NEXT_STEPS, model.calls[0].tools)

    def test_invalid_persisted_cadence_is_rejected_before_model(self):
        for interval in (True, 0, -1, "5"):
            with self.subTest(interval=interval):
                agent, model = self.app(ModelResponse(message="must not run"))
                record = agent._new_workflow({"prompt": "hi"}, task_id="invalid", identity="owner",
                    session_id="chat", tenant_id="company", defer_initialization=True)[0]
                snapshot = {**record.snapshot, "next_steps_every": interval}
                agent.workflow_store._records[record.run_id] = replace(record, snapshot=snapshot)
                with self.assertRaises(CoreError) as caught:
                    agent.resume_task("invalid")
                self.assertEqual(caught.exception.code, "CHECKPOINT_INVALID")
                self.assertEqual(model.calls, ())

    def test_inbound_message_rearms_planning_before_next_turn(self):
        agent, model = self.app(next_steps("p1"), task_list("t1"), next_steps("p2"),
                                ModelResponse(message="done"))
        handler = agent.tool_runtime.handlers["core_task_list"]

        def deliver(arguments, run_id):
            agent.enqueue_message({"prompt": "new instruction"}, task_id="task", message_id="steer",
                identity="owner", session_id="chat", tenant_id="company")
            return handler(arguments, run_id)

        with patch.dict(agent.tool_runtime.handlers, {"core_task_list": deliver}):
            self.assertEqual(approvals.ToolApprovalTests.start(agent).message, "done")
        self.assertEqual(self.choices(model), [NEXT_STEPS, None, NEXT_STEPS, None])
        self.assertIn("new instruction", model.calls[2].context)

    def test_python_nested_outcomes_count_toward_cadence_and_failed_wait_triggers(self):
        cases = (
            ('for _ in range(5):\n    tools.call("core_task_list", {})', 6),
            ('try:\n    tools.call("core_task_wait", {"task_id": "missing"})\nexcept Exception:\n    pass', 2),
        )
        for code, completed in cases:
            with self.subTest(code=code):
                python = ModelResponse(tool_requests=(ToolRequest("python", "core_python_exec", {"code": code}),))
                agent, model = self.app(next_steps("p1"), python, next_steps("p2"), ModelResponse(message="done"),
                    CORE_AGENT_ALLOWED_BUILTIN_TOOLS="core_next_steps,core_python_exec,core_task_list,core_task_wait")
                transition = agent._record_transition
                receipts = []

                def record_completion(*args, **kwargs):
                    record = transition(*args, **kwargs)
                    if kwargs.get("event_kind") == "tool.completed" and kwargs.get("event_data", {}).get("tool_call_id") == "python":
                        receipts.append(copy.deepcopy(record.snapshot))
                    return record

                with patch.object(agent, "_record_transition", side_effect=record_completion):
                    result = approvals.ToolApprovalTests.start(agent)
                self.assertEqual(result.message, "done")
                self.assertEqual(self.choices(model), [NEXT_STEPS, None, NEXT_STEPS, None])
                self.assertEqual(result.usage.tool_calls, completed + 2)
                self.assertEqual(receipts[0]["next_steps_calls_since"], completed)
                self.assertTrue(receipts[0]["next_steps_due"])

    def test_lifted_python_owner_answer_rearms_once_without_replaying_python(self):
        python = ModelResponse(tool_requests=(ToolRequest("python", "core_python_exec", {"code": "ask owner"}),))
        agent, model = self.app(next_steps("p1"), python, next_steps("p2"), ModelResponse(message="done"),
            CORE_AGENT_ALLOWED_BUILTIN_TOOLS="core_next_steps,core_python_exec,core_task_list",
            CORE_AGENT_NEXT_STEPS_EVERY="50")
        agent.interaction_store = InMemoryInteractionStore(agent.workflow_store)
        agent.platform_config = replace(agent.platform_config,
            allowed_builtin_tools=agent.platform_config.allowed_builtin_tools | {"core_ask_owner"})
        raw = agent.agent_config.to_dict()
        raw["features"]["human_input"] = True
        raw["tools"]["builtins"]["allow"].append("core_ask_owner")
        agent.agent_config = AgentConfig.from_dict(raw)
        for name in (NEXT_STEPS, "core_python_exec", "core_task_list", "core_ask_owner"):
            approvals.ToolApprovalTests.policy(agent, "allow", name)
        with python_waits.PythonWaitTests.python(self, agent, name="core_ask_owner",
                arguments={"question": "Which one?"}, prior=True):
            sleeping = approvals.ToolApprovalTests.start(agent)
        self.assertIsInstance(sleeping, SuspendedRun)
        waiting = agent.workflow_store.lookup_task("task").snapshot
        self.assertEqual(waiting["next_steps_calls_since"], 1)
        self.assertFalse(waiting["next_steps_due"])
        self.assertEqual(agent.resume_task("task").wait_id, sleeping.wait_id)
        agent.workflow_store.resolve_wait(sleeping.wait_id, tenant_id="company",
            outcome={"reason": "answer", "answer": "first"}, actor_id="owner-actor")
        agent._runtime_cache.clear()
        transition = agent._record_transition
        receipts = []

        def record_completion(*args, **kwargs):
            record = transition(*args, **kwargs)
            if kwargs.get("event_kind") == "tool.failed" and kwargs.get("event_data", {}).get("tool_call_id") == "python":
                receipts.append(copy.deepcopy(record.snapshot))
            return record

        detector = agent.guardrail_classifier.model
        classify = detector.generate

        def review_outer_result(**call):
            if "PYTHON_CONTINUATION_INTERRUPTED" in call["context"]:
                return ModelResponse(message='{"verdict":"uncertain"}', finish_reason="stop")
            return classify(**call)

        with patch("core_agent.runtime.execute_python", side_effect=AssertionError("Python replay")), \
                patch.object(agent, "_record_transition", side_effect=record_completion), \
                patch.object(detector, "generate", side_effect=review_outer_result):
            material_wait = agent.resume_task("task")
            self.assertIsInstance(material_wait, SuspendedRun)
            pending = agent.workflow_store.lookup_task("task").snapshot
            self.assertEqual(pending["next_steps_calls_since"], 2)
            self.assertTrue(pending["next_steps_due"])
            self.assertNotIn("python_execution", pending)
            self.assertEqual(pending["pending_completed_result"]["call"]["name"], "core_python_exec")
            self.assertEqual(agent.resume_task("task").wait_id, material_wait.wait_id)
            self.assertEqual(agent.workflow_store.lookup_task("task").snapshot["next_steps_calls_since"], 2)
            approvals.ToolApprovalTests.resolve(agent, material_wait, "allowed")
            result = agent.resume_task("task")
            self.assertEqual(agent.resume_task("task"), result)
        self.assertEqual(self.choices(model), [NEXT_STEPS, None, NEXT_STEPS, None])
        self.assertEqual(result.usage.tool_calls, 5)
        self.assertEqual(receipts[0]["next_steps_calls_since"], 3)
        self.assertTrue(receipts[0]["next_steps_due"])
        self.assertEqual((self.prefix, self.remainder, self.stops), (1, 0, 1))

    def test_committed_counters_survive_recovery_without_repeating_planning(self):
        class Crash(BaseException):
            pass

        agent, model = self.app(next_steps("p1"), task_list("t1"), task_list("t2"),
                                next_steps("p2"), ModelResponse(message="done"),
                                CORE_AGENT_NEXT_STEPS_EVERY="2")
        transition = agent._record_transition

        def crash_after_result(*args, **kwargs):
            record = transition(*args, **kwargs)
            if kwargs.get("event_kind") == "tool.completed" and kwargs.get("event_data", {}).get("tool_call_id") == "t1":
                raise Crash()
            return record

        with patch.object(agent, "_record_transition", side_effect=crash_after_result), self.assertRaises(Crash):
            approvals.ToolApprovalTests.start(agent)
        saved = agent.workflow_store.lookup_task("task").snapshot
        self.assertEqual(saved["next_steps_calls_since"], 1)
        self.assertFalse(saved["next_steps_due"])
        agent.next_steps_every = 100
        agent._runtime_cache.clear()
        result = agent.resume_task("task")
        self.assertEqual(result.message, "done")
        self.assertEqual(result.usage.tool_calls, 4)
        self.assertEqual(self.choices(model), [NEXT_STEPS, None, None, NEXT_STEPS, None])

    def test_compaction_preserves_cadence_and_uses_existing_progress_summary(self):
        summaries = []

        class SummaryModel(ScriptedModel):
            def generate(inner, **call):
                if call["instructions"].startswith("SEMANTIC CONTEXT SUMMARY"):
                    summaries.append(call)
                    records = json.loads(call["context"])["records"]
                    sources = list(dict.fromkeys(source for item in records for source in item["sources"]))
                    payload = {key: [] for key in ("Goal", "Constraints", "Decisions", "Completed", "Artifacts", "Pending", "Failures")}
                    payload["Pending"] = [{"text": "Continue listing tasks", "basis": "fact", "sources": sources}]
                    return ModelResponse(message=json.dumps(payload), finish_reason="stop")
                return super().generate(**call)

        model = SummaryModel([next_steps("p1"), task_list("t1"), task_list("t2"),
                              next_steps("p2"), ModelResponse(message="done")])
        agent, model = self.app(model=model, CORE_AGENT_NEXT_STEPS_EVERY="2")
        raw = agent.agent_config.to_dict()
        raw["context"]["compaction_interval"] = 1
        agent.agent_config = AgentConfig.from_dict(raw)
        self.assertEqual(approvals.ToolApprovalTests.start(agent).message, "done")
        self.assertTrue(summaries)
        self.assertTrue(all(not call.get("tool_choice") for call in summaries))
        self.assertEqual(self.choices(model), [NEXT_STEPS, None, None, NEXT_STEPS, None])
        self.assertIn("Summary of earlier turns in this same task", model.calls[-1].context)
        record = agent.workflow_store.lookup_task("task")
        self.assertEqual(record.snapshot["next_steps_every"], 2)
        self.assertTrue(any(NEXT_STEPS in item["content"] for item in record.snapshot["context"]["transcript"]))

    @unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL is required")
    def test_postgres_reopen_preserves_cadence_counters_kernel_and_charges(self):
        import psycopg
        from psycopg.conninfo import make_conninfo
        from psycopg.sql import SQL, Identifier
        from core_agent.database import PostgresDatabase
        from core_agent.agent_settings import AgentSettingsStore

        schema = "next_steps_" + uuid.uuid4().hex
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as connection:
            connection.execute(SQL("CREATE SCHEMA {}").format(Identifier(schema)))

        def drop_schema():
            with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as connection:
                connection.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(schema)))

        self.addCleanup(drop_schema)
        dsn = make_conninfo(TEST_DATABASE_URL, options="-c search_path=" + schema)

        def postgres_app(interval, *responses):
            database = PostgresDatabase(dsn, min_size=0, max_size=8)
            self.addCleanup(database.close)
            model = ScriptedModel(responses)
            model.model = "next-steps-test"
            with patch.dict(os.environ, {"CORE_AGENT_NEXT_STEPS_EVERY": str(interval),
                    "CORE_AGENT_NEXT_STEPS": "false",
                    "DURABLE_STORAGE_ROOT": self.temp.name + "/durable"}), \
                    patch("core_agent.runtime.CoreAgent.recover_workflows", return_value=()):
                app = create_app(model=model, database=database)
            self.addCleanup(app.state.close)
            app.state.core_agent.agent_settings_store = AgentSettingsStore(app.state.core_agent.workflow_store,
                                                                           database=database)
            return app, app.state.core_agent, model

        class Crash(BaseException):
            pass

        first, agent, before_model = postgres_app(2, next_steps("p1"), task_list("t1"))
        agent.agent_settings_store.update("company", {"next_steps": {"enabled": True, "every": 2}},
                                         expected_revision=0, actor_id="owner")
        agent.kernel_compiler.base_kernel = "KERNEL PLANNING V1"
        transition = agent._record_transition

        def crash_after_commit(*args, **kwargs):
            record = transition(*args, **kwargs)
            if kwargs.get("event_kind") == "tool.completed" and kwargs.get("event_data", {}).get("tool_call_id") == "t1":
                raise Crash()
            return record

        with patch.object(agent, "_record_transition", side_effect=crash_after_commit), self.assertRaises(Crash):
            approvals.ToolApprovalTests.start(agent)
        admitted_kernel = copy.deepcopy(agent.workflow_store.lookup_task("task").snapshot["kernel_instructions"])
        self.assertEqual(self.choices(before_model), [NEXT_STEPS, None])
        agent.agent_settings_store.update("company", {"next_steps": {"enabled": False, "every": 9}},
                                         expected_revision=1, actor_id="owner")
        first.state.close()

        _, reopened, model = postgres_app(100, task_list("t2"), next_steps("p2"), ModelResponse(message="done"))
        reopened.kernel_compiler.base_kernel = "KERNEL PLANNING V2"
        self.assertEqual(reopened.agent_settings_store.get("company")["config"]["next_steps"],
                         {"enabled": False, "every": 9})
        persisted = reopened.workflow_store.lookup_task("task").snapshot
        self.assertEqual(persisted["next_steps_every"], 2)
        self.assertEqual(persisted["next_steps_calls_since"], 1)
        self.assertFalse(persisted["next_steps_due"])
        self.assertEqual((persisted["turns"], persisted["tool_calls"]), (2, 2))
        result = reopened.resume_task("task")
        self.assertEqual(result.message, "done")
        self.assertTrue(result.complete)
        self.assertEqual((result.usage.model_turns, result.usage.tool_calls), (5, 4))
        self.assertEqual(self.choices(model), [None, NEXT_STEPS, None])
        self.assertTrue(all("KERNEL PLANNING V1" in call.instructions for call in model.calls))
        self.assertTrue(all("KERNEL PLANNING V2" not in call.instructions for call in model.calls))
        self.assertTrue(all("core_response_begin" not in call.tools for call in model.calls))
        self.assertEqual(reopened.workflow_store.lookup_task("task").snapshot["kernel_instructions"], admitted_kernel)
        self.assertEqual(reopened.resume_task("task"), result)
        self.assertEqual(len(model.calls), 3)

