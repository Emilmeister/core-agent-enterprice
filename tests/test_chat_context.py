"""Cross-task history uses immutable scoped originals, never provider replay."""
import copy
import hashlib
import json
import os
import unittest
import uuid
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from core_agent.context import ContextItem, ContextState
from core_agent.errors import CoreError
from core_agent.config import AgentConfig
from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
from tests import test_runtime_guardrails as guardrails
from tests.test_runtime_observability import make_agent, locked_skill_declaration


class SkillChatContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "chat-skill"
        self.path.mkdir()
        self.header = "---\nname: chat-skill\ndescription: Verify chat work.\n---\n"
        self.body = "PINNED_CHAT_SKILL_BODY\n"
        (self.path / "SKILL.md").write_text(self.header + self.body, encoding="utf-8")
        (self.path / "reference.md").write_text("Original reference.\n", encoding="utf-8")
        self.model = ScriptedModel([
            ModelResponse(tool_requests=(ToolRequest("activate", "core_skill_activate", {"names": ["chat-skill"]}),)),
            ModelResponse(message="verified first task"),
        ])
        self.agent = make_agent(self.model, memory="disabled", mcp=False,
            declared_skills=(locked_skill_declaration(self.path, "chat-skill"),))
        self.addCleanup(self.agent.close)
        raw = self.agent.agent_config.to_dict()
        raw["features"]["skills"] = True
        raw["skills"]["allow"] = ["chat-skill"]
        self.agent.agent_config = AgentConfig.from_dict(raw)
        self.agent.platform_config.allowed_skills.add("chat-skill")
        self.agent.platform_config.supported_features.add("skills")
        first = self.agent.run({"prompt": "Verify work"}, task_id="activated",
            identity="owner", session_id="chat", tenant_id="company")
        self.first = self.agent.workflow_store.get(first.run_id, tenant_id="company", owner_id="owner")

    def admit(self, task="next", previous=None, **scope):
        return self.agent._new_workflow({"prompt": "Continue verification"}, task_id=task,
            identity=scope.get("owner", "owner"), session_id=scope.get("chat", "chat"),
            tenant_id=scope.get("tenant", "company"), parent_run_id=scope.get("parent"),
            previous_root_run_id=self.first.run_id if previous is None else previous,
            defer_initialization=True)[0]

    def continue_task(self, record):
        self.agent.model = self.model = ScriptedModel([ModelResponse(message="verified next task")])
        self.agent.resume_task(record.task_id)
        return self.agent.workflow_store.get(record.run_id, tenant_id=record.tenant_id, owner_id=record.owner_id)

    def seed_empty_root(self, task, previous, *, initializing=False):
        record = self.admit(task, previous)
        snapshot = copy.deepcopy(record.snapshot)
        snapshot["initializing"] = initializing
        saved = replace(record, state="FAILED" if initializing else "COMPLETED",
            snapshot=snapshot, result={"message": "Historical task", "complete": not initializing})
        self.agent.workflow_store._records[record.run_id] = saved
        return saved

    def test_rollout_restores_activation_before_initialized_empty_roots(self):
        previous = self.first.run_id
        for index in range(3):
            previous = self.seed_empty_root(f"legacy-{index}", previous).run_id
        saved = self.continue_task(self.admit(previous=previous))
        self.assertEqual(saved.snapshot["skills"], self.first.snapshot["skills"])
        self.assertIn(self.body, self.model.calls[0].instructions)

    def test_failed_initialization_does_not_clear_chat_activation(self):
        failed = self.seed_empty_root("initialization-failed", self.first.run_id, initializing=True)
        saved = self.continue_task(self.admit(previous=failed.run_id))
        self.assertEqual(saved.snapshot["skills"], self.first.snapshot["skills"])

    def test_temporary_deny_preserves_activation_for_later_reenable(self):
        self.agent.platform_config.allowed_skills.clear()
        denied = self.continue_task(self.admit("denied"))
        self.assertEqual(denied.snapshot["skills"], [])
        self.assertNotIn(self.body, self.model.calls[0].instructions)
        self.assertNotIn("core_skill_read_resource", self.model.calls[0].tools)
        self.agent.platform_config.allowed_skills.add("chat-skill")
        restored = self.continue_task(self.admit("reenabled", denied.run_id))
        self.assertEqual(restored.snapshot["skills"], self.first.snapshot["skills"])
        self.assertIn(self.body, self.model.calls[0].instructions)

    def test_new_root_refreshes_body_and_resources_from_current_pin(self):
        updated = "CURRENT_DEPLOYMENT_SKILL_BODY\n"
        (self.path / "SKILL.md").write_text(self.header + updated, encoding="utf-8")
        (self.path / "reference.md").unlink()
        (self.path / "updated.md").write_text("Current reference.\n", encoding="utf-8")
        declaration = locked_skill_declaration(self.path, "chat-skill")
        self.agent.declared_skills = (declaration,)
        saved = self.continue_task(self.admit())
        skill = saved.snapshot["skills"][0]
        self.assertEqual(skill["instructions"], updated)
        self.assertEqual(skill["digest"], declaration["digest"].removeprefix("sha256:"))
        self.assertEqual(skill["resources"], ["updated.md"])
        self.assertIn(updated, self.model.calls[0].instructions)
        self.assertNotIn(self.body, self.model.calls[0].instructions)
        self.assertEqual(self.first.snapshot["skills"][0]["instructions"], self.body)

    def test_unpinned_legacy_activation_fails_before_model(self):
        snapshot = copy.deepcopy(self.first.snapshot)
        snapshot["skills"][0].pop("digest")
        self.agent.workflow_store._records[self.first.run_id] = replace(self.first, snapshot=snapshot)
        current = self.admit()
        self.agent.model = self.model = ScriptedModel([ModelResponse(message="must not run")])
        with self.assertRaises(CoreError) as caught:
            self.agent.resume_task(current.task_id)
        self.assertEqual(caught.exception.code, "SKILL_INVALID")
        self.assertEqual(self.model.calls, ())

    def test_other_chat_and_subagent_do_not_inherit_without_lineage(self):
        parent = self.agent._new_workflow({"prompt": "Parent work"}, task_id="parent-open",
            identity="owner", session_id="parent-chat", tenant_id="company", defer_initialization=True)[0]
        for task, scope in (("other-chat", {"chat": "other"}), ("child", {"parent": parent.run_id})):
            with self.subTest(task=task):
                current = self.agent._new_workflow({"prompt": "Separate work"}, task_id=task,
                    identity="owner", session_id=scope.get("chat", "chat"), tenant_id="company",
                    parent_run_id=scope.get("parent"), defer_initialization=True)[0]
                saved = self.continue_task(current)
                self.assertEqual(saved.snapshot["skills"], [])
                self.assertNotIn(self.body, self.model.calls[0].instructions)

    def test_foreign_lineage_and_cycle_fail_before_model(self):
        for index, scope in enumerate(({"chat": "other"}, {"owner": "other"}, {"tenant": "other"}, {})):
            with self.subTest(scope=scope):
                current = self.admit(f"invalid-{index}", **scope)
                if not scope:
                    snapshot = copy.deepcopy(current.snapshot)
                    snapshot["previous_root_run_id"] = current.run_id
                    self.agent.workflow_store._records[current.run_id] = replace(current, snapshot=snapshot)
                self.agent.model = self.model = ScriptedModel([ModelResponse(message="must not run")])
                with self.assertRaises(CoreError) as caught:
                    self.agent.resume_task(current.task_id)
                self.assertEqual(caught.exception.code, "CHECKPOINT_INVALID")
                self.assertEqual(self.model.calls, ())

    def test_malformed_baseline_cannot_override_verified_activation_source(self):
        for index, baseline in enumerate((
            {"version": 2, "sources": {}},
            {"version": 1, "sources": []},
            {"version": 1, "sources": {}},
            {"version": 1, "sources": {"chat-skill": "foreign-source"}},
        )):
            with self.subTest(baseline=baseline):
                snapshot = copy.deepcopy(self.first.snapshot)
                snapshot["skill_activation_sources"] = baseline
                self.agent.workflow_store._records[self.first.run_id] = replace(self.first, snapshot=snapshot)
                current = self.admit(f"invalid-baseline-{index}")
                self.agent.model = self.model = ScriptedModel([ModelResponse(message="must not run")])
                with self.assertRaises(CoreError) as caught:
                    self.agent.resume_task(current.task_id)
                self.assertEqual(caught.exception.code, "CHECKPOINT_INVALID")
                self.assertEqual(self.model.calls, ())

    def test_inherited_skill_body_counts_toward_context_window(self):
        (self.path / "SKILL.md").write_text(self.header + "X" * 200_000 + "\n", encoding="utf-8")
        self.agent.declared_skills = (locked_skill_declaration(self.path, "chat-skill"),)
        self.agent.context_window = 50_000
        self.agent.output_reserve = 1_000
        current = self.admit()
        self.agent.model = self.model = ScriptedModel([ModelResponse(message="must not run")])
        with self.assertRaises(CoreError) as caught:
            self.agent.resume_task(current.task_id)
        self.assertEqual(caught.exception.code, "CONTEXT_UNRECOVERABLE")
        self.assertEqual(self.model.calls, ())


class ChatContextTests(unittest.TestCase):
    setUp = guardrails.RuntimeGuardrailTests.setUp
    app = guardrails.RuntimeGuardrailTests.app
    guard = guardrails.RuntimeGuardrailTests.guard

    def admit(self, agent, task, previous=None, prompt="Continue with current goal", **scope):
        return agent._new_workflow({"prompt": prompt}, task_id=task,
            identity=scope.get("owner", "owner"), session_id=scope.get("chat", "chat"),
            tenant_id=scope.get("tenant", "company"),
            previous_root_run_id=previous, defer_initialization=True)[0]

    def seed(self, agent, task="old", *, result=None, state="COMPLETED", items=(), **scope):
        record = self.admit(agent, task, **scope)
        snapshot = copy.deepcopy(record.snapshot)
        if items:
            snapshot["context"] = agent._context_to_dict(ContextState(tuple(items), tuple(items), (1, len(items))))
        record = replace(record, state=state, snapshot=snapshot, result=result, parent_run_id=scope.get("parent"),
            error_code="MODEL_UNAVAILABLE" if state == "FAILED" else None)
        agent.workflow_store._records[record.run_id] = record
        return record

    def test_three_runs_preserve_history_once_and_current_prompt_is_only_pin(self):
        agent, model = self.app(ModelResponse(message="Decision B agreed"),
            ModelResponse(message="B implemented"), ModelResponse(message="C follows B"))
        first = agent.run({"prompt": "Use decision B"}, task_id="one", identity="owner", session_id="chat", tenant_id="company")
        second = self.admit(agent, "two", first.run_id)
        agent.resume_task("two")
        third = self.admit(agent, "three", second.run_id, prompt="Now do C")
        agent.resume_task("three")
        for phrase in ("Use decision B", "Decision B agreed", "B implemented", "Now do C"):
            self.assertIn(phrase, model.calls[-1].context)
        self.assertEqual(model.calls[-1].context.count("Decision B agreed"), 1)
        saved = agent.workflow_store.get(third.run_id, tenant_id="company")
        self.assertEqual([item["content"] for item in saved.snapshot["context"]["active"] if item["pinned"]], ["Now do C"])
        self.assertEqual(len(saved.snapshot["context"]["transcript"]), 1)
        self.assertEqual(saved.snapshot["context_import"]["version"], 1)
        self.assertIn("historical", model.calls[-1].context.lower())

    def test_third_run_imports_semantic_summary_with_original_first_run_sources(self):
        from core_agent.config import AgentConfig
        from tests import test_runtime_observability as runtime_tests
        agent, model = self.app(guardrails.RuntimeGuardrailTests.call(), ModelResponse(message="First verified final"),
            guardrails.RuntimeGuardrailTests.call(call_id="second-call"), ModelResponse(message="Second verified final"),
            ModelResponse(message="Third answer"), ModelResponse(message="Fourth safe answer"))
        self.guard(agent, *("clear",) * 8)
        guardrails.RuntimeGuardrailTests.policy(agent, "allow")
        raw = agent.agent_config.to_dict()
        raw["context"]["compaction_interval"] = 1
        agent.agent_config = AgentConfig.from_dict(raw)
        generate = model.generate
        summaries = []
        def generate_with_summary(**call):
            if call["instructions"].startswith("SEMANTIC CONTEXT SUMMARY"):
                summaries.append(call)
                return runtime_tests.SemanticRuntimeTests.semantic_answer(call, "Decision B verified; preserve latest correction")
            return generate(**call)
        with patch.object(model, "generate", side_effect=generate_with_summary):
            first = agent.run({"prompt": "Use B"}, task_id="one", identity="owner", session_id="chat", tenant_id="company")
            second = self.admit(agent, "two", first.run_id)
            agent.resume_task("two")
            third = self.admit(agent, "three", second.run_id)
            agent.resume_task("three")
        self.assertGreaterEqual(len(summaries), 2)
        self.assertIn("Decision B verified", model.calls[-1].context)
        self.assertIn("Second verified final", model.calls[-1].context)
        saved = agent.workflow_store.get(third.run_id, tenant_id="company")
        historical_summary = next(item for item in saved.snapshot["context"]["active"] if item["kind"] == "summary")
        self.assertTrue(any(source["run_id"] == first.run_id for source in historical_summary["provenance"]["sources"].values()))
        self.reject(agent, "Use B")
        self.admit(agent, "four", third.run_id)
        agent.resume_task("four")
        for derived in ("Use B", "Decision B verified", "First verified final", "Second verified final", "Third answer"):
            self.assertNotIn(derived, model.calls[-1].context)

    def test_foreign_nonterminal_child_and_missing_sources_fail_closed(self):
        for scope in ({"chat": "other"}, {"owner": "other"}, {"tenant": "other"},
                      {"state": "RUNNING"}, {"parent": "parent"}, {"missing": True}):
            with self.subTest(scope=scope):
                agent, model = self.app(ModelResponse(message="should not run"))
                args = dict(scope)
                missing = args.pop("missing", False)
                previous = self.seed(agent, **args)
                self.admit(agent, "new", "missing" if missing else previous.run_id)
                with self.assertRaises(CoreError) as caught:
                    agent.resume_task("new")
                self.assertEqual(caught.exception.code, "CHECKPOINT_INVALID")
                self.assertEqual(model.calls, ())

    def test_legacy_tool_protocol_replay_and_undelivered_input_are_not_imported(self):
        agent, model = self.app(ModelResponse(message="new result"))
        calls = {"tool_calls": [{"id": "old-call", "function": {"name": "core_task_list", "arguments": "{}"}}],
                 "reasoning_replay": {"secret": "OPAQUE_REPLAY"}}
        old = self.seed(agent, items=(ContextItem("prompt", "Previous goal", 10, pinned=True),
            ContextItem("assistant_tool_calls", json.dumps(calls), 10, provider_replay={"secret": "OPAQUE_REPLAY"}),
            ContextItem("tool_result", json.dumps({"tool_call_id": "old-call", "output": "old result"}), 10),
            ContextItem("unprocessed_due_to_failure", "PRIVATE_UNDELIVERED", 10)),
            state="FAILED")
        self.admit(agent, "new", old.run_id)
        agent.resume_task("new")
        self.assertIn("old result", model.calls[-1].context)
        self.assertIn("FAILED", model.calls[-1].context)
        self.assertNotIn("OPAQUE_REPLAY", model.calls[-1].context)
        self.assertNotIn("PRIVATE_UNDELIVERED", model.calls[-1].context)
        self.assertTrue(all(message["role"] == "user" and "tool_calls" not in message for message in model.calls[-1].messages))

    def test_cancelled_disposition_with_provenance_is_excluded_before_source_resolution(self):
        agent, model = self.app(ModelResponse(message="continued after cancellation"))
        old = self.seed(agent, state="CANCELLED")
        snapshot = copy.deepcopy(old.snapshot)
        private = ContextItem("unprocessed_due_to_cancel", "PRIVATE_UNDELIVERED", 10,
            provenance={"version": 1, "sources": {f"{old.run_id}:1": {"run_id": old.run_id, "sequence": 1}}})
        snapshot["context"] = agent._context_to_dict(ContextState((private,), (private,), (1, 1)))
        agent.workflow_store._records[old.run_id] = replace(old, snapshot=snapshot)
        self.admit(agent, "new", old.run_id)
        agent.resume_task("new")
        self.assertNotIn("PRIVATE_UNDELIVERED", model.calls[-1].context)
        self.assertIn("CANCELLED", model.calls[-1].context)

    def test_partial_final_has_real_result_identity_and_label(self):
        agent, model = self.app(ModelResponse(message="continue partial"))
        result = {"message": "Half finished", "complete": False, "completion_reason": "budget_exhausted"}
        old = self.seed(agent, result=result)
        current = self.admit(agent, "new", old.run_id)
        agent.resume_task("new")
        saved = agent.workflow_store.get(current.run_id, tenant_id="company")
        sources = {key: value for item in saved.snapshot["context"]["active"] for key, value in item["provenance"]["sources"].items()}
        digest = hashlib.sha256(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        self.assertEqual(sources[f"{old.run_id}:result:{digest}"], {"kind": "terminal_result", "run_id": old.run_id, "result_digest": digest})
        final = next(item for item in saved.snapshot["context"]["active"] if "Half finished" in item["content"])
        self.assertFalse(json.loads(json.loads(final["content"])["content"])["complete"])
        self.assertIn("budget_exhausted", model.calls[-1].context)

    def test_import_commit_survives_crash_and_is_not_repeated(self):
        class Crash(BaseException):
            pass
        agent, model = self.app(ModelResponse(message="done"))
        old = self.seed(agent, result={"message": "Remember once", "complete": True})
        current = self.admit(agent, "new", old.run_id)
        transition = agent._record_transition
        def crash_after_commit(*args, **kwargs):
            updated = transition(*args, **kwargs)
            if kwargs.get("event_kind") == "context.imported":
                raise Crash()
            return updated
        with patch.object(agent, "_record_transition", side_effect=crash_after_commit):
            with self.assertRaises(Crash):
                agent.resume_task("new")
        imported = copy.deepcopy(agent.workflow_store.get(current.run_id, tenant_id="company").snapshot["context_import"])
        agent.resume_task("new")
        self.assertEqual(model.calls[-1].context.count("Remember once"), 1)
        self.assertEqual(agent.workflow_store.get(current.run_id, tenant_id="company").snapshot["context_import"], imported)

    def reject(self, agent, text):
        from core_agent.guardrails import GuardrailClassifier
        from tests.test_material_reviews import Model as DetectorModel
        peer = self.admit(agent, "denial-" + str(len(agent.workflow_store._records)))
        token = agent.workflow_store.acquire_lease(peer.run_id, tenant_id="company", owner_id="owner", worker_id="test", ttl=100)
        digest = hashlib.sha256(json.dumps(text, separators=(",", ":")).encode()).hexdigest()
        review = agent.material_review_store.create(peer, source_id="review", source_kind="input", payload=text,
            completed_result_ref={"material_digest": digest}, deadline=agent.workflow_store.current_time()+60, lease_token=token)
        review = agent.material_review_store.classify(peer, review["review_id"], GuardrailClassifier(DetectorModel("suspicious")),
            lease_token=token, continuation={"version": 1, "phase": "input"}, snapshot=peer.snapshot)
        agent.workflow_store.resolve_wait(review["wait_id"], tenant_id="company", outcome={"reason": "rejected"}, actor_id="owner")

    def test_current_prompt_is_reviewed_before_import_and_old_reviews_stay_readonly(self):
        agent, model = self.app(ModelResponse(message="Old confirmed result"), ModelResponse(message="continued"))
        self.guard(agent, "clear", "suspicious")
        old = agent.run({"prompt": "Old private goal"}, task_id="old", identity="owner", session_id="chat", tenant_id="company")
        rows = copy.deepcopy(agent.material_review_store.rows)
        current = self.admit(agent, "new", old.run_id)
        sleeping = agent.resume_task("new")
        self.assertNotIn("context_import", agent.workflow_store.get(current.run_id, tenant_id="company").snapshot)
        agent.workflow_store.resolve_wait(sleeping.wait_id, tenant_id="company", outcome={"reason": "allowed"}, actor_id="owner")
        agent.resume_task("new")
        self.assertIn("Old confirmed result", model.calls[-1].context)
        for review_id, row in rows.items():
            self.assertEqual(agent.material_review_store.rows[review_id], row)

    def test_revoked_source_removes_prior_final_and_rebuilds_summary_from_originals(self):
        agent, model = self.app(ModelResponse(message="CONTAMINATED FINAL"), ModelResponse(message="safe continuation"))
        self.guard(agent, "clear", "clear")
        old_result = agent.run({"prompt": "REVOKED ORIGINAL"}, task_id="old", identity="owner", session_id="chat", tenant_id="company")
        old = agent.workflow_store.get(old_result.run_id, tenant_id="company")
        snapshot = copy.deepcopy(old.snapshot)
        safe = ContextItem("user_message", "SAFE LATE CORRECTION", 5,
            provenance={"version": 1, "sources": {f"{old.run_id}:2": {"run_id": old.run_id, "sequence": 2}}})
        snapshot["context"]["transcript"].append(agent._context_to_dict(ContextState((safe,), (), (1, 0)))["active"][0])
        snapshot["context"]["sequence_range"][1] = 2
        sources = snapshot["context"]["active"][0]["provenance"]["sources"] | safe.provenance["sources"]
        summary = ContextItem("summary", "CONTAMINATED SUMMARY", 5,
            provenance={"version": 1, "summary_version": 1, "sources": sources})
        snapshot["context"]["active"] = agent._context_to_dict(ContextState((summary,), (), (1, 0)))["active"]
        agent.workflow_store._records[old.run_id] = replace(old, snapshot=snapshot)
        self.reject(agent, "REVOKED ORIGINAL")
        self.admit(agent, "new", old.run_id)
        agent.resume_task("new")
        text = model.calls[-1].context
        self.assertIn("SAFE LATE CORRECTION", text)
        for denied in ("REVOKED ORIGINAL", "CONTAMINATED FINAL", "CONTAMINATED SUMMARY"):
            self.assertNotIn(denied, text)

    def test_negative_decision_on_exact_final_text_is_checked(self):
        agent, model = self.app(ModelResponse(message="REJECT THIS RESULT"), ModelResponse(message="safe"))
        self.guard(agent, "clear", "clear")
        old = agent.run({"prompt": "safe prompt"}, task_id="old", identity="owner", session_id="chat", tenant_id="company")
        self.reject(agent, "REJECT THIS RESULT")
        self.admit(agent, "new", old.run_id)
        agent.resume_task("new")
        self.assertNotIn("REJECT THIS RESULT", model.calls[-1].context)

    def test_revocation_after_import_rechecks_old_sources_before_model(self):
        agent, model = self.app(ModelResponse(message="derived private claim"), ModelResponse(message="safe"))
        self.guard(agent, "clear", "clear")
        old = agent.run({"prompt": "late revoked input"}, task_id="old", identity="owner", session_id="chat", tenant_id="company")
        self.admit(agent, "new", old.run_id)
        transition = agent._record_transition
        def revoke_after_import(*args, **kwargs):
            updated = transition(*args, **kwargs)
            if kwargs.get("event_kind") == "context.imported":
                self.reject(agent, "late revoked input")
            return updated
        with patch.object(agent, "_record_transition", side_effect=revoke_after_import):
            agent.resume_task("new")
        self.assertNotIn("late revoked input", model.calls[-1].context)
        self.assertNotIn("derived private claim", model.calls[-1].context)

    def test_unknown_source_import_version_and_changed_result_digest_fail_closed(self):
        for corruption in ("version", "result"):
            with self.subTest(corruption=corruption):
                agent, model = self.app(ModelResponse(message="must not run"))
                old = self.seed(agent, result={"message": "old immutable result"})
                self.admit(agent, "new", old.run_id)
                if corruption == "version":
                    snapshot = copy.deepcopy(old.snapshot)
                    snapshot["context_import"] = {"version": 99, "previous_run_id": None, "sources": {}}
                    agent.workflow_store._records[old.run_id] = replace(old, snapshot=snapshot)
                transition = agent._record_transition
                def corrupt_after_import(*args, **kwargs):
                    updated = transition(*args, **kwargs)
                    if corruption == "result" and kwargs.get("event_kind") == "context.imported":
                        agent.workflow_store._records[old.run_id] = replace(old, result={"message": "changed result"})
                    return updated
                with patch.object(agent, "_record_transition", side_effect=corrupt_after_import):
                    with self.assertRaises(CoreError) as caught:
                        agent.resume_task("new")
                self.assertEqual(caught.exception.code, "CHECKPOINT_INVALID")
                self.assertEqual(model.calls, ())

    def test_lease_loss_before_import_commit_leaves_no_projection(self):
        agent, model = self.app(ModelResponse(message="must not run"))
        old = self.seed(agent, result={"message": "historical content"})
        current = self.admit(agent, "new", old.run_id)
        transition = agent._record_transition
        def lose_lease(*args, **kwargs):
            if kwargs.get("event_kind") == "context.imported":
                agent.workflow_store._leases.pop(current.run_id, None)
            return transition(*args, **kwargs)
        with patch.object(agent, "_record_transition", side_effect=lose_lease):
            with self.assertRaises(CoreError) as caught:
                agent.resume_task("new")
        self.assertEqual(caught.exception.code, "LEASE_LOST")
        self.assertNotIn("context_import", agent.workflow_store.get(current.run_id, tenant_id="company").snapshot)
        self.assertEqual(model.calls, ())


class HistoricalReviewContract:
    def historical_review(self, verdict):
        from core_agent.guardrails import GuardrailClassifier
        from core_agent.workflow import WorkflowRecord
        from tests.test_material_reviews import Model as DetectorModel
        scope = (str(uuid.uuid4()), str(uuid.uuid4()))
        def create():
            return self.workflow.create(WorkflowRecord(str(uuid.uuid4()), str(uuid.uuid4()),
                scope[1], scope[0], "owner", None, "RUNNING", 1, {"prompt": "review"}, {}))
        old = create()
        token = self.workflow.acquire_lease(old.run_id, tenant_id=old.tenant_id, owner_id="owner", worker_id="test", ttl=100)
        review = self.store.create(old, source_id="input", source_kind="input", payload="allowed content",
            lease_token=token, deadline=self.workflow.current_time()+60)
        review = self.store.classify(old, review["review_id"], GuardrailClassifier(DetectorModel(verdict)),
            lease_token=token, continuation={"version": 1, "phase": "input"}, snapshot=old.snapshot)
        if verdict == "suspicious":
            self.workflow.resolve_wait(review["wait_id"], tenant_id=old.tenant_id, outcome={"reason": "allowed"}, actor_id="owner")
            token = self.workflow.acquire_lease(old.run_id, tenant_id=old.tenant_id, owner_id="owner", worker_id="test", ttl=100)
        old = self.workflow.get(old.run_id, tenant_id=old.tenant_id)
        old = self.workflow.transition(old.run_id, tenant_id=old.tenant_id, owner_id="owner",
            expected_version=old.version, state="COMPLETED", snapshot=old.snapshot, result={"message": "done"},
            event_kind="task.completed", lease_token=token)
        current = create()
        current_token = self.workflow.acquire_lease(current.run_id, tenant_id=current.tenant_id, owner_id="owner", worker_id="test", ttl=100)
        return old, current, current_token, review

    def test_historical_clear_and_owner_allowed_references_are_readonly(self):
        for verdict, expected in (("clear", "clear"), ("suspicious", "allowed")):
            with self.subTest(verdict=verdict):
                old, current, token, review = self.historical_review(verdict)
                with self.workflow._execution_lock(current, token) as (_, connection):
                    before = copy.deepcopy(self.store._load(old, review["review_id"], connection, lock=False))
                    # Historical reads cannot refresh/resolve any old review or wait.
                    with patch.object(self.store, "_refresh", side_effect=AssertionError("foreign mutation")):
                        reference = self.store.visibility_reference(current, review["review_id"],
                            source_run_id=old.run_id, lease_token=token, connection=connection)
                    after = self.store._load(old, review["review_id"], connection, lock=False)
                self.assertEqual(reference["state"], expected)
                self.assertEqual(before, after)
                self.assertNotIn("payload", reference)

    def test_foreign_review_uses_current_lease_and_borrowed_connection_without_source_lock(self):
        old, current, token, review = self.historical_review("clear")
        with self.assertRaises(CoreError) as caught:
            self.store.visibility_reference(current, review["review_id"], source_run_id=old.run_id, lease_token="stale")
        self.assertEqual(caught.exception.code, "LEASE_LOST")
        current = self.workflow.transition(current.run_id, tenant_id=current.tenant_id, owner_id="owner",
            expected_version=current.version, state="RUNNING", snapshot=current.snapshot,
            event_kind="test.running", lease_token=token)
        with self.workflow._execution_lock(current, token) as (_, connection):
            with patch.object(self.workflow, "get", wraps=self.workflow.get) as get:
                self.store.visibility_reference(current, review["review_id"], source_run_id=old.run_id,
                    lease_token=token, connection=connection)
                source_reads = [call for call in get.call_args_list if call.args[0] == old.run_id]
                self.assertTrue(source_reads)
                self.assertTrue(all(not call.kwargs.get("lock", False) for call in source_reads))
                self.assertTrue(all(call.kwargs.get("connection") is connection for call in source_reads))


class MemoryHistoricalReviewTests(HistoricalReviewContract, unittest.TestCase):
    def setUp(self):
        from core_agent.material_reviews import MemoryMaterialReviewStore
        from core_agent.workflow import InMemoryWorkflowStore
        self.workflow = InMemoryWorkflowStore()
        self.store = MemoryMaterialReviewStore(self.workflow)


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "set TEST_DATABASE_URL for historical review persistence proof")
class PostgresHistoricalReviewTests(HistoricalReviewContract, unittest.TestCase):
    def setUp(self):
        from core_agent.database import PostgresDatabase
        from core_agent.material_reviews import PostgresMaterialReviewStore
        from core_agent.workflow import PostgresWorkflowStore
        database = PostgresDatabase(os.environ["TEST_DATABASE_URL"], min_size=0, max_size=1)
        self.addCleanup(database.close)
        database.migrate()
        self.workflow = PostgresWorkflowStore(database)
        self.store = PostgresMaterialReviewStore(database, self.workflow)


if __name__ == "__main__":
    unittest.main()
