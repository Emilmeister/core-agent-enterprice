"""Actual runtime material boundaries; deterministic detector and owned workflow."""
import json
import os
import unittest
import uuid
from unittest.mock import Mock, patch

from core_agent.guardrails import GuardrailClassifier
from core_agent.material_reviews import MemoryMaterialReviewStore
from core_agent.model import ModelResponse
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

    def test_completed_tool_result_suspends_and_replays_without_dispatch(self):
        agent, model = self.app(self.call(), ModelResponse(message="done"))
        self.policy(agent, "allow")
        self.guard(agent, "clear", "clear", "suspicious")
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

    def test_policy_denial_precedes_argument_review(self):
        agent, model = self.app(self.call(), ModelResponse(message="done"))
        self.policy(agent, "deny")
        detector = self.guard(agent, "clear")
        self.assertEqual(self.start(agent).message, "done")
        self.assertEqual(detector.generate.call_count, 1)
        self.assertEqual(self.executed, [])
        self.assertIn("POLICY_DENIED", model.calls[-1].context)

    def test_python_result_wait_stops_and_lifts_without_replaying_effect(self):
        agent, model = self.app(self.call("core_python_exec", {"code": "prefix; tools.call(); remainder"}), ModelResponse(message="done"))
        self.policy(agent, "allow", "core_python_exec")
        self.policy(agent, "allow")
        detector = self.guard(agent, "clear", "clear", "clear", "suspicious", "clear")
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
        self.assertEqual(detector.generate.call_count, 5)

    def test_python_clear_material_continues_interpreter(self):
        agent, model = self.app(self.call("core_python_exec", {"code": "prefix; tools.call(); remainder"}), ModelResponse(message="done"))
        self.policy(agent, "allow", "core_python_exec")
        self.policy(agent, "allow")
        self.guard(agent, "clear", "clear", "clear", "clear", "clear")
        with self.python(agent):
            result = self.start(agent)
        self.assertEqual(result.message, "done")
        self.assertEqual((self.prefix, self.remainder, self.stops), (1, 1, 0))

    def test_python_arguments_wait_prevents_nested_dispatch(self):
        agent, model = self.app(self.call("core_python_exec", {"code": "prefix; tools.call(); remainder"}), ModelResponse(message="done"))
        self.policy(agent, "allow", "core_python_exec")
        self.policy(agent, "allow")
        self.guard(agent, "clear", "clear", "suspicious", "clear", "clear")
        with self.python(agent):
            sleeping = self.start(agent)
        self.assertIsInstance(sleeping, SuspendedRun)
        self.assertEqual(self.executed, [])
        self.assertEqual((self.prefix, self.remainder, self.stops), (1, 0, 1))
        self.resolve(agent, sleeping, "allowed")
        result = agent.resume_task("task")
        self.assertEqual(result.message, "done")
        self.assertEqual(len(self.executed), 1)

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
        self.assertEqual(self.executed, [])
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
        self.guard(agent, "clear", "suspicious", "clear", "clear")
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
        self.guard(agent, "clear", "clear", "suspicious")
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
                detector = self.guard(agent, "clear", "clear", "suspicious", "clear", "clear")
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
                self.assertEqual(detector.generate.call_count, 4)
                agent.tool_runtime.handlers["core_terminal_exec"] = lambda *_: {"material": "changed version"}
                third = agent.run({"prompt": "changed"}, task_id="third", identity="owner", session_id="chat", tenant_id="company")
                self.assertEqual(third.message, "third done")
                self.assertIn("changed version", model.calls[-1].context)

    def test_result_rejection_does_not_cross_chat(self):
        agent, model = self.app(self.call(), ModelResponse(message="first done"),
            self.call("core_task_list", {}, "next"), ModelResponse(message="second done"))
        self.policy(agent, "allow")
        agent.tool_runtime.handlers["core_task_list"] = lambda *_: "same source"
        self.guard(agent, "clear", "clear", "suspicious", "clear", "clear", "clear")
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
        self.guard(agent, "clear", "clear", "clear", "clear", "clear")
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
        self.guard(agent, "clear", "clear", "clear", "clear", "clear")
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
        for stage in ("tool_arguments", "tool_result"):
            with self.subTest(stage=stage):
                agent, model = self.app(self.call("core_python_exec", {"code": "prefix; tools.call(); remainder"}), ModelResponse(message="must not run"))
                for name in ("core_python_exec", "core_task_list"):
                    self.policy(agent, "allow", name)
                self.guard(agent, "clear", "clear", "clear", "clear")
                classify = agent.material_review_store.classify
                def failure(record, review_id, *args, **kwargs):
                    row = agent.material_review_store.get(record, review_id)
                    payload = agent.material_review_store.owner_read_payload(record, review_id)["payload"]
                    if row["source_kind"] == stage and (stage == "tool_result" or payload == {}):
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
