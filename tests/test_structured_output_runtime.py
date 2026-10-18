"""Run-pinned structured reasoning across runtime consumers."""

import copy
import json
import threading
import unittest
from unittest.mock import patch

from core_agent.agent_settings import AgentSettingsStore
from core_agent.app import _guardrail_classifier
from core_agent.config import RunRequest
from core_agent.errors import CoreError
from core_agent.material_reviews import MemoryMaterialReviewStore
from core_agent.model import CompatibleHttpModel, ModelResponse, ScriptedModel
from tests.test_runtime_observability import make_agent


OFF = {"reasoning": "off", "compatibility": "chat_template"}
INHERIT = {"reasoning": "inherit", "compatibility": "auto"}


class StructuredOutputRuntimeTests(unittest.TestCase):
    def agent(self, *responses):
        agent = make_agent(ScriptedModel(responses), mcp=False, platform_mcp=())
        self.addCleanup(agent.close)
        agent.agent_settings_store = AgentSettingsStore(agent.workflow_store)
        agent.agent_settings_store.update("company", {"structured_output": OFF},
                                          expected_revision=0, actor_id="owner")
        return agent

    def admit(self, agent, task_id, **kwargs):
        return agent._new_workflow(RunRequest("Hello"), task_id=task_id,
                                  identity="owner", tenant_id="company", session_id="chat",
                                  defer_initialization=True, **kwargs)[0]

    def test_root_child_and_legacy_policy_survive_settings_changes(self):
        agent = self.agent()
        root = self.admit(agent, "root")
        self.assertEqual(root.snapshot["structured_output"], OFF)
        agent.agent_settings_store.update("company", {"structured_output": INHERIT},
                                          expected_revision=1, actor_id="owner")
        child = self.admit(agent, "child", parent_run_id=root.run_id)
        self.assertEqual(child.snapshot["structured_output"], OFF)
        self.assertEqual(self.admit(agent, "next").snapshot["structured_output"], INHERIT)
        restored = json.loads(json.dumps(root.snapshot))
        self.assertEqual(agent._structured_output_policy(restored), OFF)
        restored.pop("structured_output")
        self.assertIsNone(agent._structured_output_policy(restored))
        restored["structured_output"] = {"reasoning": "off"}
        with self.assertRaises(CoreError) as raised:
            agent._structured_output_policy(restored)
        self.assertEqual(raised.exception.code, "CHECKPOINT_INVALID")

    def test_guardrail_wire_uses_admitted_policy_after_settings_change(self):
        agent = self.agent(ModelResponse(message="done"))
        detector = CompatibleHttpModel(api_format="openai", model="guardrail", reasoning_effort="low",
                                       extra_body={"chat_template_kwargs": {"enable_thinking": True}})
        agent.guardrail_classifier = _guardrail_classifier(detector)
        agent.material_review_store = MemoryMaterialReviewStore(agent.workflow_store)
        record = self.admit(agent, "recover-policy")
        agent.agent_settings_store.update("company", {"structured_output": INHERIT},
                                          expected_revision=1, actor_id="owner")
        sent = []
        def respond(_model, body, _headers):
            sent.append(copy.deepcopy(body))
            return {"choices": [{"message": {"content": '{"verdict":"clear"}'}, "finish_reason": "stop"}]}
        with patch.object(CompatibleHttpModel, "_post", respond):
            result = agent.resume_task(record.task_id)
        self.assertEqual(result.message, "done")
        self.assertTrue(sent)
        for body in sent:
            self.assertNotIn("reasoning_effort", body)
            self.assertFalse(body["chat_template_kwargs"]["enable_thinking"])
            self.assertTrue(body["response_format"]["json_schema"]["strict"])
            self.assertEqual(body["model"], "guardrail")
        self.assertEqual(agent.guardrail_classifier.model.reasoning_effort, "low")
        self.assertTrue(detector.extra_body["chat_template_kwargs"]["enable_thinking"])

    def test_guardrail_copies_keep_one_five_request_limit(self):
        agent = self.agent()
        detector = CompatibleHttpModel(api_format="openai", model="guardrail")
        agent.guardrail_classifier = _guardrail_classifier(detector)
        copies = [agent._guardrail_for_snapshot({"structured_output": policy})
                  for policy in (OFF, INHERIT, OFF, INHERIT, OFF, INHERIT)]
        self.assertTrue(all(item._slot is agent.guardrail_classifier._slot for item in copies))
        release, entered = threading.Event(), threading.Semaphore(0)
        results = []
        def respond(_model, body, _headers):
            entered.release()
            if not release.wait(5):
                raise AssertionError("test provider was not released")
            return {"choices": [{"message": {"content": '{"verdict":"clear"}'}, "finish_reason": "stop"}]}
        def classify(classifier):
            results.append(classifier.classify(["material"], source_kind="tool_arguments",
                                               record_attempt=lambda _cost: None))
        threads = [threading.Thread(target=classify, args=(item,)) for item in copies[:5]]
        with patch.object(CompatibleHttpModel, "_post", respond):
            try:
                for thread in threads:
                    thread.start()
                for _ in threads:
                    self.assertTrue(entered.acquire(timeout=3))
                charged = []
                last = copies[5].classify(["material"], source_kind="tool_arguments", record_attempt=charged.append)
                self.assertEqual(last.reason, "detector_busy")
                self.assertEqual(charged, [])
            finally:
                release.set()
                for thread in threads:
                    thread.join(5)
        self.assertEqual([item.verdict for item in results], ["clear"] * 5)
