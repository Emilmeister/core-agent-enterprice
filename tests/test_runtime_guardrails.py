"""Actual runtime material boundaries; deterministic detector and owned workflow."""
import hashlib
import json
import os
import unittest
import uuid
from contextlib import nullcontext
from unittest.mock import Mock, patch

from core_agent.agent_settings import AgentSettingsStore
from core_agent.guardrails import GuardrailClassifier
from core_agent.material_reviews import MemoryMaterialReviewStore
from core_agent.model import ModelResponse
from core_agent.observability import FailingExporter, RecordingExporter, Telemetry
from core_agent.workflow import SuspendedRun
from tests import test_tool_approvals as approvals
from tests import test_python_waits as python_waits


class RuntimeGuardrailTests(unittest.TestCase):
    setUp = approvals.ToolApprovalTests.setUp
    app = approvals.ToolApprovalTests.app
    call = staticmethod(approvals.ToolApprovalTests.call)
    start = staticmethod(approvals.ToolApprovalTests.start)
    policy = staticmethod(approvals.ToolApprovalTests.policy)
    resolve = staticmethod(approvals.ToolApprovalTests.resolve)
    python = python_waits.PythonWaitTests.python
    background_app = approvals.ToolApprovalTests.background_app
    start_background_with_open_parent = approvals.ToolApprovalTests.start_background_with_open_parent
    settle_workers = staticmethod(approvals.ToolApprovalTests.settle_workers)

    def guard(self, agent, *verdicts):
        detector = Mock()
        detector.context_window = 128000
        detector.max_tokens = 512
        detector.count_tokens.side_effect = lambda text: max(1, len(text) // 4)
        detector.generate.side_effect = [ModelResponse(message=json.dumps({"verdict": v}), finish_reason="stop") for v in verdicts]
        agent.guardrail_classifier = GuardrailClassifier(detector, clock=lambda: self.now[0])
        agent.material_review_store = MemoryMaterialReviewStore(agent.workflow_store)
        return detector

    def test_legacy_argument_review_keeps_decision_deadline_and_dispatch_once(self):
        for reason in ("allowed", "rejected", "timeout"):
            with self.subTest(reason=reason):
                agent, model = self.app(self.call(), ModelResponse(message="done"))
                self.policy(agent, "allow")
                detector = self.guard(agent, "clear", "uncertain", "clear")
                guard = agent._guard_material

                def seed_legacy_review(record, snapshot, **kwargs):
                    if kwargs["source_kind"] == "tool_arguments":
                        payload = kwargs["payload"]
                        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False,
                            sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
                        review = agent.material_review_store.create(record,
                            source_id=kwargs["source_id"], source_kind="tool_arguments", payload=payload,
                            deadline=self.now[0] + 60, max_calls=32, max_input_tokens=100000,
                            completed_result_ref={"material_digest": digest}, lease_token=kwargs["lease_token"])
                        snapshot.setdefault("material_reviews", {})[kwargs["source_id"] + ":" + digest] = review["review_id"]
                    return guard(record, snapshot, **kwargs)

                # Recreate the review already saved by an older runtime before its
                # first classification. Subsequent resumes use unpatched new code.
                with patch.object(agent, "_guard_material", side_effect=seed_legacy_review):
                    sleeping = self.start(agent)
                self.assertIsInstance(sleeping, SuspendedRun)
                wait = agent.workflow_store.get_wait(sleeping.wait_id, tenant_id="company")
                self.assertEqual(wait.subject["source_kind"], "tool_arguments")
                self.assertEqual(self.executed, [])
                agent._runtime_cache.clear()
                self.assertEqual(agent.resume_task("task").wait_id, sleeping.wait_id)
                self.assertEqual(agent.workflow_store.get_wait(sleeping.wait_id, tenant_id="company").deadline, wait.deadline)
                self.assertEqual(detector.generate.call_count, 2)
                self.resolve(agent, sleeping, reason)
                self.assertEqual(agent.resume_task("task").message, "done")
                self.assertEqual(len(self.executed), 1 if reason == "allowed" else 0)
                self.assertEqual(detector.generate.call_count, 3 if reason == "allowed" else 2)
                if reason != "allowed":
                    self.assertIn("MATERIAL_", model.calls[-1].context)
                agent.resume_task("task")
                self.assertEqual(len(self.executed), 1 if reason == "allowed" else 0)

    def test_committed_guardrail_spans_correlate_wait_recovery_without_private_content(self):
        agent, model = self.app(ModelResponse(message="done"))
        exporter = RecordingExporter()
        agent.telemetry = Telemetry(exporter)
        detector = self.guard(agent, "suspicious")
        sleeping = self.start(agent)
        spans = [span for span in exporter.spans if span.name.startswith("core_agent.guardrail.")]
        self.assertEqual([span.name for span in spans], ["core_agent.guardrail.classified", "core_agent.guardrail.wait.entered"])
        self.assertEqual(spans[0].attributes["core_agent.guardrail.verdict"], "suspicious")
        self.assertEqual(spans[0].attributes["core_agent.guardrail.reason"], "suspicious")
        self.assertEqual(spans[1].attributes["core_agent.guardrail.wait_id"], sleeping.wait_id)
        self.assertEqual(spans[1].attributes["core_agent.guardrail.owner_deadline"] - self.now[0], 86400)
        self.assertEqual(spans[0].attributes["core_agent.guardrail.detector_deadline"] - self.now[0], 60)
        self.assertGreaterEqual(spans[0].attributes["core_agent.guardrail.elapsed_seconds"], 0)
        self.assertEqual(model.calls, ())
        self.resolve(agent, sleeping, "allowed")
        agent._runtime_cache.clear()
        self.assertEqual(agent.resume_task("task").message, "done")
        spans = [span for span in exporter.spans if span.name.startswith("core_agent.guardrail.")]
        self.assertEqual([span.name for span in spans].count("core_agent.guardrail.wait.resolved"), 1)
        self.assertEqual([span.name for span in spans].count("core_agent.guardrail.wait.applied"), 1)
        self.assertEqual(detector.generate.call_count, 1)
        self.assertEqual(len({span.attributes["core_agent.diagnostic.event_id"] for span in spans}), len(spans))
        encoded = json.dumps([span.attributes for span in spans])
        for private in ("perform task", "content_digest", "material_digest", "payload", "answer"):
            self.assertNotIn(private, encoded)
        agent.resume_task("task")
        self.assertEqual(len([span for span in exporter.spans if span.name.startswith("core_agent.guardrail.")]), len(spans))

    def test_guardrail_export_failure_does_not_undo_task_and_terminal_read_retries(self):
        agent, model = self.app(ModelResponse(message="done"))
        agent.telemetry = Telemetry(FailingExporter())
        self.guard(agent, "clear")
        result = self.start(agent)
        self.assertEqual(result.message, "done")
        self.assertEqual(len(model.calls), 1)
        exporter = RecordingExporter()
        agent.telemetry = Telemetry(exporter)
        self.now[0] += 2
        self.assertEqual(agent.resume_task("task").message, "done")
        spans = [span for span in exporter.spans if span.name.startswith("core_agent.guardrail.")]
        self.assertEqual([span.name for span in spans], ["core_agent.guardrail.classified"])

    def test_terminal_diagnostic_recovery_is_company_scoped_and_does_not_call_model(self):
        agent, model = self.app(ModelResponse(message="done"))
        agent.telemetry = Telemetry(FailingExporter())
        self.guard(agent, "clear")
        self.start(agent)
        agent.material_review_store = MemoryMaterialReviewStore(agent.workflow_store)
        agent._runtime_cache.clear()
        exporter = RecordingExporter()
        agent.telemetry = Telemetry(exporter)
        self.now[0] += 2
        agent.recovery_tenant_id = "foreign"
        agent._recover_workflows_once()
        self.assertFalse(any(span.name.startswith("core_agent.guardrail.") for span in exporter.spans))
        agent.recovery_tenant_id = "company"
        agent._recover_workflows_once()
        agent._recover_workflows_once()
        spans = [span for span in exporter.spans if span.name.startswith("core_agent.guardrail.")]
        self.assertEqual([span.name for span in spans], ["core_agent.guardrail.classified"])
        self.assertEqual(len(model.calls), 1)

    def test_initial_material_is_private_until_owner_resolution(self):
        agent, model = self.app(ModelResponse(message="done"))
        detector = self.guard(agent, "suspicious")
        sleeping = self.start(agent)
        self.assertIsInstance(sleeping, SuspendedRun)
        record = agent.workflow_store.lookup_task("task")
        self.assertTrue(record.snapshot["initializing"])
        self.assertNotIn("perform task", json.dumps(record.snapshot["context"]))
        self.assertEqual(model.calls, ())
        self.resolve(agent, sleeping, "allowed")
        agent._runtime_cache.clear()
        self.assertEqual(agent.resume_task("task").message, "done")
        self.assertIn("perform task", model.calls[-1].context)
        self.assertEqual(detector.generate.call_count, 1)

    def test_initial_rejection_and_timeout_continue_without_source(self):
        for reason in ("rejected", "timeout"):
            with self.subTest(reason=reason):
                agent, model = self.app(ModelResponse(message="please clarify"))
                self.guard(agent, "suspicious")
                sleeping = self.start(agent)
                self.assertIsInstance(sleeping, SuspendedRun)
                self.resolve(agent, sleeping, reason)
                self.assertEqual(agent.resume_task("task").message, "please clarify")
                self.assertNotIn("perform task", model.calls[-1].context)
                self.assertIn("MATERIAL_", model.calls[-1].context)

    def test_disabled_guardrails_skip_new_inputs_and_tool_results(self):
        for nested in (False, True):
            with self.subTest(nested=nested):
                request = self.call("core_python_exec", {"code": "tools.call(); remainder"}) if nested else self.call()
                agent, model = self.app(request, ModelResponse(message="done"))
                agent.agent_settings_store = AgentSettingsStore(agent.workflow_store)
                agent.agent_settings_store.update("company", {"guardrails_enabled": False}, expected_revision=0, actor_id="owner")
                for name in ("core_task_list", "core_python_exec"):
                    self.policy(agent, "allow", name)
                detector = self.guard(agent)
                with self.python(agent) if nested else nullcontext():
                    result = self.start(agent)
                self.assertEqual(result.message, "done")
                self.assertEqual(len(self.executed), 1)
                self.assertFalse(agent.workflow_store.lookup_task("task").snapshot["guardrails_enabled"])
                self.assertIn("perform task", model.calls[-1].context)
                if not nested:
                    self.assertIn('"count": 17', model.calls[-1].context)
                detector.generate.assert_not_called()

    def test_guardrail_setting_is_pinned_for_recovery_and_children(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                agent, _ = self.app(ModelResponse(message="done"))
                settings = agent.agent_settings_store = AgentSettingsStore(agent.workflow_store)
                settings.update("company", {"guardrails_enabled": enabled}, expected_revision=0, actor_id="owner")
                root, *_ = agent._new_workflow({"prompt": "perform task"}, task_id="task", identity="owner",
                    tenant_id="company", session_id="chat", defer_initialization=True)
                settings.update("company", {"guardrails_enabled": not enabled}, expected_revision=1, actor_id="owner")
                child, *_ = agent._new_workflow({"prompt": "child"}, task_id="child", identity="owner",
                    tenant_id="company", session_id="chat", parent_run_id=root.run_id, defer_initialization=True)
                later, *_ = agent._new_workflow({"prompt": "later"}, task_id="later", identity="owner",
                    tenant_id="company", session_id="other", defer_initialization=True)
                self.assertEqual(root.snapshot["guardrails_enabled"], enabled)
                self.assertEqual(child.snapshot["guardrails_enabled"], enabled)
                self.assertEqual(later.snapshot["guardrails_enabled"], not enabled)
                detector = self.guard(agent, "clear")
                agent._runtime_cache.clear()
                self.assertEqual(agent.resume_task("task").message, "done")
                self.assertEqual(detector.generate.call_count, int(enabled))

    def test_background_inherits_guardrail_setting_pinned_before_owner_change(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                agent, _ = self.background_app(self.call("core_task_start", {
                    "tool": "core_terminal_exec", "arguments": {"argv": ["true"]}}), ModelResponse(message="submitted"))
                settings = agent.agent_settings_store = AgentSettingsStore(agent.workflow_store)
                settings.update("company", {"guardrails_enabled": enabled}, expected_revision=0, actor_id="owner")
                agent._new_workflow({"prompt": "perform task"}, task_id="task", identity="owner",
                    tenant_id="company", session_id="chat", defer_initialization=True)
                settings.update("company", {"guardrails_enabled": not enabled}, expected_revision=1, actor_id="owner")
                detector = self.guard(agent, "clear", "clear")
                parent = self.start_background_with_open_parent(agent)
                self.settle_workers(agent)
                child_task, = agent.task_scheduler.list(owner_id=parent.run_id, tenant_id="company")
                child = agent.workflow_store.lookup_task(child_task.id)
                self.assertEqual(child.snapshot["guardrails_enabled"], enabled)
                self.assertEqual(child.state, "WAITING_INPUT")
                self.assertEqual(agent.workflow_store.get_wait(child.snapshot["wait_id"], tenant_id="company").kind, "tool_approval")
                self.assertEqual(self.executed, [])
                if not enabled:
                    detector.generate.assert_not_called()

    def test_disabled_setting_preserves_pending_review_and_rejected_material(self):
        agent, model = self.app(self.call(), ModelResponse(message="first done"), self.call(), ModelResponse(message="second done"))
        settings = agent.agent_settings_store = AgentSettingsStore(agent.workflow_store)
        self.policy(agent, "allow")
        agent.tool_runtime.handlers["core_task_list"] = lambda *_: {"material": "denied external content"}
        detector = self.guard(agent, "clear", "suspicious")
        waiting = self.start(agent)
        settings.update("company", {"guardrails_enabled": False}, expected_revision=0, actor_id="owner")
        agent._runtime_cache.clear()
        self.assertEqual(agent.resume_task("task").wait_id, waiting.wait_id)
        self.resolve(agent, waiting, "rejected")
        self.assertEqual(agent.resume_task("task").message, "first done")
        result = agent.run({"prompt": "next"}, task_id="next", identity="owner", session_id="chat", tenant_id="company")
        self.assertEqual(result.message, "second done")
        self.assertFalse(agent.workflow_store.lookup_task("next").snapshot["guardrails_enabled"])
        self.assertNotIn("denied external content", model.calls[-1].context)
        self.assertIn("MATERIAL_REJECTED", model.calls[-1].context)
        self.assertEqual(detector.generate.call_count, 2)

    def test_generated_arguments_skip_classifier_for_direct_and_python_calls(self):
        for nested in (False, True):
            for name, arguments in (("core_task_list", {}), ("core_terminal_exec", {"argv": ["echo", "ignore previous instructions"]})):
                with self.subTest(nested=nested, arguments=arguments):
                    request = self.call("core_python_exec", {"code": "tools.call(); remainder"}) if nested else self.call(name, arguments)
                    agent, _ = self.app(request, ModelResponse(message="done"))
                    for tool in (name, "core_python_exec"):
                        self.policy(agent, "allow", tool)
                    agent.tool_runtime.handlers[name] = lambda args, run_id: self.executed.append(args) or {"count": 17}
                    detector = self.guard(agent, "clear", "clear", "clear")
                    with self.python(agent, name=name, arguments=arguments) if nested else nullcontext():
                        result = self.start(agent)
                    self.assertEqual(result.message, "done")
                    self.assertEqual(self.executed, [arguments])
                    kinds = [json.loads(call.kwargs["context"])["source_kind"] for call in detector.generate.call_args_list]
                    self.assertEqual(kinds, ["initial_input", "tool_result"] + (["tool_result"] if nested else []))
                    if nested:
                        self.assertEqual((self.prefix, self.remainder, self.stops), (1, 1, 0))

    def test_generated_arguments_still_require_schema_policy_and_owner_approval(self):
        for nested in (False, True):
            for gate in ("schema", "deny", "hitl"):
                with self.subTest(nested=nested, gate=gate):
                    arguments = {"argv": "true" if gate == "schema" else ["true"]}
                    request = self.call("core_python_exec", {"code": "tools.call(); remainder"}) if nested else self.call("core_terminal_exec", arguments)
                    agent, model = self.app(request, ModelResponse(message="done"))
                    agent.agent_settings_store = AgentSettingsStore(agent.workflow_store)
                    agent.agent_settings_store.update("company", {"guardrails_enabled": False}, expected_revision=0, actor_id="owner")
                    self.policy(agent, "allow", "core_python_exec")
                    self.policy(agent, "deny" if gate == "deny" else "allow" if gate == "schema" else "require_hitl", "core_terminal_exec")
                    agent.tool_runtime.handlers["core_terminal_exec"] = lambda args, run_id: self.executed.append(args) or {"done": True}
                    detector = self.guard(agent, "clear", "clear", "clear")
                    with self.python(agent, name="core_terminal_exec", arguments=arguments) if nested else nullcontext():
                        result = self.start(agent)
                    self.assertEqual(self.executed, [])
                    if gate == "hitl":
                        self.assertIsInstance(result, SuspendedRun)
                        self.assertEqual(agent.workflow_store.get_wait(result.wait_id, tenant_id="company").kind, "tool_approval")
                        self.assertEqual(detector.generate.call_count, 0)
                        self.resolve(agent, result, "allowed")
                        agent._runtime_cache.clear()
                        result = agent.resume_task("task")
                        self.assertEqual(self.executed, [arguments])
                    self.assertEqual(result.message, "done")
                    if not nested and gate != "hitl":
                        self.assertIn("TOOL_ARGUMENT_INVALID" if gate == "schema" else "POLICY_DENIED", model.calls[-1].context)
                    detector.generate.assert_not_called()

    def test_completed_tool_result_suspends_and_replays_without_dispatch(self):
        for verdict in ("suspicious", "uncertain"):
            with self.subTest(verdict=verdict):
                agent, model = self.app(self.call(), ModelResponse(message="done"))
                self.policy(agent, "allow")
                self.guard(agent, "clear", verdict)
                agent.tool_runtime.handlers["core_task_list"] = lambda args, run_id: self.executed.append(run_id) or {"secret": "untrusted payload"}
                stream = Mock()
                agent._task_streams["task"] = stream
                sleeping = self.start(agent)
                self.assertIsInstance(sleeping, SuspendedRun)
                self.assertEqual(len(self.executed), 1)
                self.assertEqual(stream.tool_result.call_count, 0)
                self.assertNotIn("untrusted payload", json.dumps(agent.workflow_store.lookup_task("task").snapshot["context"]))
                self.resolve(agent, sleeping, "allowed")
                agent._runtime_cache.clear()
                result = agent.resume_task("task")
                self.assertEqual(result.message, "done")
                self.assertEqual(len(self.executed), 1)
                self.assertEqual(result.usage.tool_calls, 1)
                self.assertIn("untrusted payload", model.calls[-1].context)

    def test_policy_denial_prevents_dispatch_without_argument_review(self):
        agent, model = self.app(self.call(), ModelResponse(message="done"))
        self.policy(agent, "deny")
        detector = self.guard(agent, "clear")
        self.assertEqual(self.start(agent).message, "done")
        self.assertEqual(detector.generate.call_count, 1)
        self.assertEqual(self.executed, [])
        self.assertIn("POLICY_DENIED", model.calls[-1].context)

    def test_python_result_wait_stops_and_lifts_without_replaying_effect(self):
        for verdict in ("suspicious", "uncertain"):
            with self.subTest(verdict=verdict):
                agent, model = self.app(self.call("core_python_exec", {"code": "prefix; tools.call(); remainder"}), ModelResponse(message="done"))
                self.policy(agent, "allow", "core_python_exec")
                self.policy(agent, "allow")
                detector = self.guard(agent, "clear", verdict, "clear")
                with self.python(agent):
                    sleeping = self.start(agent)
                self.assertIsInstance(sleeping, SuspendedRun)
                self.assertEqual((self.prefix, self.remainder, self.stops), (1, 0, 1))
                wait = agent.workflow_store.get_wait(sleeping.wait_id, tenant_id="company")
                self.assertEqual(wait.continuation["stage"], "tool_result")
                self.assertEqual(len(self.executed), 1)
                self.resolve(agent, sleeping, "allowed")
                agent._runtime_cache.clear()
                with patch("core_agent.runtime.execute_python", side_effect=AssertionError("Python replay")):
                    result = agent.resume_task("task")
                self.assertEqual(result.message, "done")
                self.assertEqual(result.usage.tool_calls, 2)
                self.assertEqual(len(self.executed), 1)
                self.assertIn("PYTHON_CONTINUATION_INTERRUPTED", model.calls[-1].context)
                self.assertEqual(detector.generate.call_count, 3)

    def test_python_clear_material_continues_interpreter(self):
        agent, model = self.app(self.call("core_python_exec", {"code": "prefix; tools.call(); remainder"}), ModelResponse(message="done"))
        self.policy(agent, "allow", "core_python_exec")
        self.policy(agent, "allow")
        self.guard(agent, "clear", "clear", "clear")
        with self.python(agent):
            result = self.start(agent)
        self.assertEqual(result.message, "done")
        self.assertEqual((self.prefix, self.remainder, self.stops), (1, 1, 0))

    def test_exemption_change_does_not_release_existing_material(self):
        agent, model = self.app(self.call(), ModelResponse(message="done"))
        self.policy(agent, "allow")
        detector = self.guard(agent, "clear", "suspicious")
        sleeping = self.start(agent)
        self.assertIsInstance(sleeping, SuspendedRun)
        current = agent.interaction_store.get_policy("company", "core_task_list", "builtin:core_task_list")
        agent.interaction_store.update_policy("company", current.canonical_name, current.origin, mode="allow", guardrails_exempt=True,
                                             expected_revision=current.revision, actor_id="owner")
        self.assertEqual(agent.resume_task("task").wait_id, sleeping.wait_id)
        self.resolve(agent, sleeping, "rejected")
        self.assertEqual(agent.resume_task("task").message, "done")
        self.assertEqual(len(self.executed), 1)
        self.assertEqual(detector.generate.call_count, 2)

    def test_owner_answer_is_checked_even_when_tool_exempt(self):
        from core_agent.config import AgentConfig
        from dataclasses import replace
        agent, model = self.app(self.call("core_ask_owner", {"question": "private question"}), ModelResponse(message="done"))
        raw = agent.agent_config.to_dict()
        raw["features"]["human_input"] = True
        raw["tools"]["builtins"]["allow"].append("core_ask_owner")
        agent.agent_config = AgentConfig.from_dict(raw)
        agent.platform_config = replace(agent.platform_config,
            allowed_builtin_tools=agent.platform_config.allowed_builtin_tools | {"core_ask_owner"})
        policy = agent.interaction_store.get_policy("company", "core_ask_owner", "builtin:core_ask_owner")
        agent.interaction_store.update_policy("company", policy.canonical_name, policy.origin, mode="allow", guardrails_exempt=True,
                                             expected_revision=0, actor_id="owner")
        self.guard(agent, "clear", "suspicious")
        sleeping = self.start(agent)
        agent.workflow_store.resolve_wait(sleeping.wait_id, tenant_id="company", outcome={"reason": "answer", "answer": "private source"}, actor_id="owner")
        reviewed = agent.resume_task("task")
        self.assertIsInstance(reviewed, SuspendedRun)
        self.assertNotEqual(reviewed.wait_id, sleeping.wait_id)
        self.assertNotIn("private source", json.dumps(agent.workflow_store.lookup_task("task").snapshot["context"]))
        self.resolve(agent, reviewed, "rejected")
        self.assertEqual(agent.resume_task("task").message, "done")
        self.assertNotIn("private source", model.calls[-1].context)

    def test_follow_up_review_is_ordered_and_private(self):
        agent, model = self.app(self.call(), ModelResponse(message="done"))
        self.guard(agent, "clear", "suspicious", "clear")
        approval = self.start(agent)
        agent.enqueue_message({"prompt": "private follow up"}, task_id="task", message_id="follow", identity="owner", session_id="chat", tenant_id="company")
        self.resolve(agent, approval, "allowed")
        sleeping = agent.resume_task("task")
        self.assertIsInstance(sleeping, SuspendedRun)
        self.assertNotIn("private follow up", json.dumps(agent.workflow_store.lookup_task("task").snapshot["context"]))
        self.resolve(agent, sleeping, "rejected")
        self.assertEqual(agent.resume_task("task").message, "done")
        self.assertNotIn("private follow up", model.calls[-1].context)

    def test_completed_mutation_checkpoint_survives_detector_interruption(self):
        agent, model = self.app(self.call("core_terminal_exec", {"argv": ["true"]}), ModelResponse(message="done"))
        self.policy(agent, "allow", "core_terminal_exec")
        self.guard(agent, "clear", "clear", "clear")
        agent.tool_runtime.handlers["core_terminal_exec"] = lambda *_: self.executed.append(1) or {"receipt": "private receipt"}
        classify = agent.material_review_store.classify
        def crash(record, review_id, *args, **kwargs):
            if agent.material_review_store.get(record, review_id)["source_kind"] == "tool_result":
                raise SystemExit("worker lost after durable known result")
            return classify(record, review_id, *args, **kwargs)
        with patch.object(agent.material_review_store, "classify", side_effect=crash), self.assertRaises(SystemExit):
            self.start(agent)
        record = agent.workflow_store.lookup_task("task")
        self.assertEqual(record.state, "MODEL_RESPONDED")
        self.assertNotIn("private receipt", json.dumps(record.snapshot["context"]))
        agent._runtime_cache.clear()
        self.assertEqual(agent.resume_task("task").message, "done")
        self.assertEqual(self.executed, [1])
        self.assertIn("private receipt", model.calls[-1].context)

    def test_background_result_wait_recovers_saved_outcome(self):
        agent, _ = self.background_app(self.call("core_task_start", {"tool": "core_terminal_exec", "arguments": {"argv": ["true"]}}), ModelResponse(message="handle returned"))
        self.policy(agent, "allow", "core_terminal_exec")
        policy = agent.interaction_store.get_policy("company", "core_task_start", "builtin:core_task_start")
        agent.interaction_store.update_policy("company", policy.canonical_name, policy.origin, mode="allow", guardrails_exempt=True,
                                             expected_revision=policy.revision, actor_id="owner")
        self.guard(agent, "clear", "suspicious")
        parent = self.start_background_with_open_parent(agent)
        self.settle_workers(agent)
        task = agent.task_scheduler.list(owner_id=parent.run_id, tenant_id="company")[0]
        child = agent.workflow_store.lookup_task(task.id)
        self.assertEqual(child.state, "WAITING_INPUT")
        self.assertEqual(len(self.executed), 1)
        agent.workflow_store.resolve_wait(child.snapshot["wait_id"], tenant_id="company", outcome={"reason": "allowed"}, actor_id="owner")
        self.assertEqual(agent.recover_durable_tasks(), 1)
        task = agent.task_scheduler.wait(task.id, timeout=5, owner_id=parent.run_id, tenant_id="company")
        self.assertEqual(task.state, "completed", task.error)
        self.assertEqual(task.result, {"background": "done"})
        self.assertEqual(len(self.executed), 1)

    def test_cancel_dispositions_unread_input_without_detector_or_new_wait(self):
        agent, model = self.app(ModelResponse(message="must not run"))
        detector = self.guard(agent, "suspicious")
        sleeping = self.start(agent)
        agent.enqueue_message({"prompt": "unprocessed follow up"}, task_id="task", message_id="follow", identity="owner", session_id="chat", tenant_id="company")
        agent.cancel_task("task")
        record = agent.workflow_store.lookup_task("task")
        self.assertEqual(record.state, "CANCELLED")
        self.assertEqual(detector.generate.call_count, 1)
        self.assertFalse(model.calls)
        self.assertFalse(agent.workflow_store.pending_inbound(record))
        self.assertEqual(agent.workflow_store.get_wait(sleeping.wait_id, tenant_id="company").outcome["reason"], "cancelled")

    def test_tool_calls_and_results_never_publish_private_content(self):
        agent, _ = self.app(self.call(), ModelResponse(message="done"))
        self.policy(agent, "allow")
        self.guard(agent, "clear", "clear", "clear")
        stream = Mock(enabled=True)
        agent._task_streams["task"] = stream
        self.assertEqual(self.start(agent).message, "done")
        stream.tool_call.assert_not_called()
        stream.tool_result.assert_not_called()

    def test_denied_exact_result_cannot_reopen_under_new_call_tool_or_exemption(self):
        for reason in ("rejected", "timeout"):
            with self.subTest(reason=reason):
                agent, model = self.app(self.call(), ModelResponse(message="first done"),
                    self.call("core_terminal_exec", {"argv": ["true"]}, "different-call"), ModelResponse(message="second done"),
                    self.call("core_terminal_exec", {"argv": ["true"]}, "changed-version"), ModelResponse(message="third done"))
                self.policy(agent, "allow")
                self.policy(agent, "allow", "core_terminal_exec")
                agent.tool_runtime.handlers["core_task_list"] = lambda *_: {"material": "immutable denied source"}
                agent.tool_runtime.handlers["core_terminal_exec"] = lambda *_: {"material": "immutable denied source"}
                detector = self.guard(agent, "clear", "suspicious", "clear", "clear")
                sleeping = self.start(agent)
                self.resolve(agent, sleeping, reason)
                self.assertEqual(agent.resume_task("task").message, "first done")
                policy = agent.interaction_store.get_policy("company", "core_terminal_exec", "builtin:core_terminal_exec")
                agent.interaction_store.update_policy("company", policy.canonical_name, policy.origin, mode="allow", guardrails_exempt=True,
                                                     expected_revision=policy.revision, actor_id="owner")
                second = agent.run({"prompt": "next"}, task_id="second", identity="owner", session_id="chat", tenant_id="company")
                self.assertEqual(second.message, "second done")
                self.assertNotIn("immutable denied source", model.calls[-1].context)
                self.assertIn("MATERIAL_", model.calls[-1].context)
                self.assertEqual(detector.generate.call_count, 3)
                agent.tool_runtime.handlers["core_terminal_exec"] = lambda *_: {"material": "changed version"}
                third = agent.run({"prompt": "changed"}, task_id="third", identity="owner", session_id="chat", tenant_id="company")
                self.assertEqual(third.message, "third done")
                self.assertIn("changed version", model.calls[-1].context)

    def test_result_rejection_does_not_cross_chat(self):
        agent, model = self.app(self.call(), ModelResponse(message="first done"),
            self.call("core_task_list", {}, "next"), ModelResponse(message="second done"))
        self.policy(agent, "allow")
        agent.tool_runtime.handlers["core_task_list"] = lambda *_: "same source"
        self.guard(agent, "clear", "suspicious", "clear", "clear")
        sleeping = self.start(agent)
        self.resolve(agent, sleeping, "rejected")
        agent.resume_task("task")
        result = agent.run({"prompt": "another chat"}, task_id="other", identity="owner", session_id="other-chat", tenant_id="company")
        self.assertEqual(result.message, "second done")
        self.assertIn("same source", model.calls[-1].context)

    def test_python_completed_result_survives_crash_before_review(self):
        agent, model = self.app(self.call("core_python_exec", {"code": "prefix; tools.call(); remainder"}), ModelResponse(message="done"))
        for name in ("core_python_exec", "core_task_list"):
            self.policy(agent, "allow", name)
        self.guard(agent, "clear", "clear", "clear")
        classify = agent.material_review_store.classify
        def crash(record, review_id, *args, **kwargs):
            if agent.material_review_store.get(record, review_id)["source_kind"] == "tool_result":
                raise SystemExit("worker crash")
            return classify(record, review_id, *args, **kwargs)
        with self.python(agent) as execute, patch.object(agent.material_review_store, "classify", side_effect=crash):
            fake_execute = execute.side_effect
            def lose_worker(*args, **kwargs):
                fake_execute(*args, **kwargs)
                raise SystemExit("whole worker stopped")
            execute.side_effect = lose_worker
            with self.assertRaises(SystemExit):
                self.start(agent)
        record = agent.workflow_store.lookup_task("task")
        self.assertEqual(record.state, "EXECUTING")
        self.assertIn("pending_completed_result", record.snapshot)
        self.assertEqual(len(self.executed), 1)
        agent._runtime_cache.clear()
        with patch("core_agent.runtime.execute_python", side_effect=AssertionError("Python replay")):
            result = agent.resume_task("task")
        self.assertEqual(result.message, "done")
        self.assertEqual(len(self.executed), 1)
        self.assertIn('"output_capture_incomplete": true', model.calls[-1].context)

    def test_python_clear_verdict_expiring_before_commit_still_stops(self):
        agent, model = self.app(self.call("core_python_exec", {"code": "prefix; tools.call(); remainder"}), ModelResponse(message="done"))
        for name in ("core_python_exec", "core_task_list"):
            self.policy(agent, "allow", name)
        self.guard(agent, "clear", "clear", "clear")
        finish = agent.material_review_store.finish
        advanced = []
        def expire(record, review_id, result, **kwargs):
            review = agent.material_review_store.get(record, review_id)
            if review["source_kind"] == "tool_result" and kwargs.get("before_pending") and not advanced:
                advanced.append(True)
                self.now[0] += 61
            return finish(record, review_id, result, **kwargs)
        with self.python(agent), patch.object(agent.material_review_store, "finish", side_effect=expire):
            sleeping = self.start(agent)
        self.assertIsInstance(sleeping, SuspendedRun)
        self.assertEqual((self.prefix, self.remainder, self.stops), (1, 0, 1))
        wait = agent.workflow_store.get_wait(sleeping.wait_id, tenant_id="company")
        self.assertEqual(wait.subject["reason"], "timeout")
        self.resolve(agent, sleeping, "allowed")
        self.assertEqual(agent.resume_task("task").message, "done")
        self.assertEqual(len(self.executed), 1)

    def test_python_cannot_catch_guard_lease_or_storage_failure_and_continue(self):
        from core_agent.errors import CoreError
        agent, model = self.app(self.call("core_python_exec", {"code": "prefix; tools.call(); remainder"}), ModelResponse(message="must not run"))
        for name in ("core_python_exec", "core_task_list"):
            self.policy(agent, "allow", name)
        self.guard(agent, "clear", "clear", "clear", "clear")
        classify = agent.material_review_store.classify
        def failure(record, review_id, *args, **kwargs):
            row = agent.material_review_store.get(record, review_id)
            if row["source_kind"] == "tool_result":
                raise CoreError("LEASE_LOST")
            return classify(record, review_id, *args, **kwargs)
        with self.python(agent), patch.object(agent.material_review_store, "classify", side_effect=failure):
            with self.assertRaises(CoreError) as stopped:
                self.start(agent)
        self.assertEqual(stopped.exception.code, "LEASE_LOST")
        self.assertEqual((self.prefix, self.remainder, self.stops), (1, 0, 1))
        self.assertEqual(len(model.calls), 1)
        self.assertNotEqual(agent.workflow_store.lookup_task("task").state, "COMPLETED")


class MaterialReuseProof:
    def new_record(self, **scope):
        from core_agent.workflow import WorkflowRecord
        record = self.workflow.create(WorkflowRecord(str(uuid.uuid4()), str(uuid.uuid4()),
            scope.get("context", self.context), scope.get("tenant", self.tenant),
            scope.get("owner", "owner"), None, "RUNNING", 1, {"prompt": "test"}, {}))
        token = self.workflow.acquire_lease(record.run_id, tenant_id=record.tenant_id,
            owner_id=record.owner_id, worker_id="worker", ttl=100)
        return record, token

    def test_immutable_negative_is_derived_from_authoritative_wait_across_runs(self):
        from core_agent.errors import CoreError
        from tests.test_material_reviews import Model
        digest = "a" * 64
        first, token = self.new_record()
        row = self.store.create(first, source_id="first-call", source_kind="tool_result", payload="private",
            completed_result_ref={"material_digest": digest}, deadline=self.workflow.current_time() + 60, lease_token=token)
        pending = self.store.classify(first, row["review_id"], GuardrailClassifier(Model("suspicious"), clock=self.workflow.current_time),
            lease_token=token, snapshot=first.snapshot, continuation={"version": 1, "phase": "tool_result", "call_id": "first-call"})
        self.workflow.resolve_wait(pending["wait_id"], tenant_id=first.tenant_id, outcome={"reason": "rejected"}, actor_id="owner")
        # Do not refresh the old review: lazy state still says pending.
        second, token = self.new_record()
        self.assertEqual(self.store.denied_material(second, digest, lease_token=token), "rejected")
        fresh = self.store.create(second, source_id="other-tool-call", source_kind="tool_result", payload="another envelope",
            completed_result_ref={"material_digest": digest}, deadline=self.workflow.current_time() + 60, lease_token=token)
        self.store.classify(second, fresh["review_id"], GuardrailClassifier(Model(), clock=self.workflow.current_time),
            lease_token=token, snapshot=second.snapshot, continuation={"version": 1, "phase": "tool_result", "call_id": "other-tool-call"})
        with self.assertRaises(CoreError) as denied:
            self.store.read_payload(second, fresh["review_id"], lease_token=token)
        self.assertEqual(denied.exception.code, "MATERIAL_REVIEW_REQUIRED")
        for scope in ({"tenant": str(uuid.uuid4())}, {"owner": "different-owner"}, {"context": str(uuid.uuid4())}):
            with self.subTest(scope=scope):
                isolated, isolated_token = self.new_record(**scope)
                self.assertIsNone(self.store.denied_material(isolated, digest, lease_token=isolated_token))


    def test_typed_file_text_aliases_share_actual_denial_without_hash_namespace_collision(self):
        from core_agent.errors import CoreError
        from tests.test_material_reviews import Model
        raw_digest, text_digest = "a" * 64, "b" * 64
        for first_is_file in (True, False):
            with self.subTest(first_is_file=first_is_file):
                context = str(uuid.uuid4())
                first, token = self.new_record(context=context)
                reference = ({"material_digest": raw_digest, "material_kind": "file_sha256", "text_digest": text_digest}
                             if first_is_file else {"material_digest": text_digest})
                row = self.store.create(first, source_id="first", source_kind="file_attachment" if first_is_file else "initial_input",
                    payload="private", completed_result_ref=reference, deadline=self.workflow.current_time() + 60, lease_token=token)
                pending = self.store.classify(first, row["review_id"],
                    GuardrailClassifier(Model("suspicious"), clock=self.workflow.current_time), lease_token=token,
                    snapshot=first.snapshot, continuation={"version": 1, "phase": "input", "sequence": 0})
                self.workflow.resolve_wait(pending["wait_id"], tenant_id=first.tenant_id,
                    outcome={"reason": "rejected"}, actor_id="owner")
                second, token = self.new_record(context=context)
                lookup = ({"material_digest": text_digest} if first_is_file else
                          {"material_digest": raw_digest, "material_kind": "file_sha256", "text_digest": text_digest})
                negative = self.store.negative_decision(second, lease_token=token, **lookup)
                self.assertEqual(negative, {"state": "rejected", "review_id": row["review_id"]})
                self.assertIsNone(self.store.negative_decision(second, raw_digest, lease_token=token))
                fresh = self.store.create(second, source_id="second", source_kind="tool_result", payload="changed envelope",
                    completed_result_ref=lookup, deadline=self.workflow.current_time() + 60, lease_token=token)
                self.store.classify(second, fresh["review_id"], GuardrailClassifier(Model(), clock=self.workflow.current_time),
                    lease_token=token, snapshot=second.snapshot, continuation={"version": 1, "phase": "input", "sequence": 0})
                with self.assertRaises(CoreError) as denied:
                    self.store.read_payload(second, fresh["review_id"], lease_token=token)
                self.assertEqual(denied.exception.code, "MATERIAL_REVIEW_REQUIRED")


class MemoryMaterialReuseTests(MaterialReuseProof, unittest.TestCase):
    def setUp(self):
        from core_agent.workflow import InMemoryWorkflowStore
        self.tenant, self.context = str(uuid.uuid4()), str(uuid.uuid4())
        self.workflow = InMemoryWorkflowStore()
        self.store = MemoryMaterialReviewStore(self.workflow)


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "set TEST_DATABASE_URL for material reuse persistence proof")
class PostgresMaterialReuseTests(MaterialReuseProof, unittest.TestCase):
    def setUp(self):
        from core_agent.database import PostgresDatabase
        from core_agent.material_reviews import PostgresMaterialReviewStore
        from core_agent.workflow import PostgresWorkflowStore
        self.tenant, self.context = str(uuid.uuid4()), str(uuid.uuid4())
        database = PostgresDatabase(os.environ["TEST_DATABASE_URL"], min_size=0, max_size=5)
        self.addCleanup(database.close)
        database.migrate()
        self.workflow = PostgresWorkflowStore(database)
        self.store = PostgresMaterialReviewStore(database, self.workflow)
