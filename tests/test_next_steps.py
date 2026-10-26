import json
import os
import tempfile
import unittest
from unittest.mock import patch

from core_agent.model import CompatibleHttpModel, ModelResponse, ScriptedModel, ToolRequest
from tests.app_support import create_app

NEXT_STEPS = "core_next_steps"
PLAN = {"previous_steps": [], "deal": "none", "stage": None, "current_step": "list tasks"}


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

    def app(self, *responses, **environment):
        model = ScriptedModel(responses)
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
        self.assertEqual(agent.run({"prompt": "hi"}).message, "done")
        self.assertIsNone(model.calls[0].tool_choice)
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
        snapshot = {"next_steps_due": False, "next_steps_calls_since": 0}
        agent._note_next_steps(snapshot, "core_ask_owner", True)
        self.assertTrue(snapshot["next_steps_due"])
        agent._note_next_steps(snapshot, NEXT_STEPS, False)
        self.assertTrue(snapshot["next_steps_due"])
        agent._note_next_steps(snapshot, NEXT_STEPS, True)
        self.assertEqual(snapshot, {"next_steps_due": False, "next_steps_calls_since": 0})

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


class NextStepsWireTests(unittest.TestCase):
    CATALOG = {NEXT_STEPS: {"description": "plan", "input_schema": {"type": "object"}},
               "erp.order": {"description": "order", "input_schema": {"type": "object"}}}

    def model(self, api_format):
        return CompatibleHttpModel(api_format=api_format, model="m", base_url="http://model.test/v1",
                                   extra_body={"tool_choice": "required"})

    def test_provider_tool_choice_is_dropped_and_runtime_choice_uses_wire_name(self):
        model = self.model("openai")
        body, _headers, _reverse = model._request("ctx", "sys", self.CATALOG)
        self.assertNotIn("tool_choice", body)
        body, _headers, _reverse = model._request("ctx", "sys", self.CATALOG, tool_choice="erp.order")
        self.assertEqual(body["tool_choice"], {"type": "function", "function": {"name": "erp_order"}})
        body, _headers, _reverse = self.model("anthropic")._request("ctx", "sys", self.CATALOG, tool_choice=NEXT_STEPS)
        self.assertEqual(body["tool_choice"], {"type": "tool", "name": NEXT_STEPS})

    def test_choice_outside_catalog_is_not_sent_and_history_still_replays(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "plan", "type": "function", "function": {"name": NEXT_STEPS, "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "plan", "name": NEXT_STEPS, "content": "ok"},
        ]
        catalog = {"erp.order": self.CATALOG["erp.order"]}
        body, _headers, _reverse = self.model("openai")._request("ctx", "sys", catalog, messages=messages,
                                                                 tool_choice=NEXT_STEPS)
        self.assertNotIn("tool_choice", body)
        self.assertEqual([tool["function"]["name"] for tool in body["tools"]], ["erp_order"])
        replayed = [call["function"]["name"] for message in body["messages"] for call in message.get("tool_calls", ())]
        self.assertEqual(replayed, [NEXT_STEPS])


if __name__ == "__main__":
    unittest.main()
