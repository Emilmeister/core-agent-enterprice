"""ENT-AC-64: final file selection uses the actual protected tool loop."""
import asyncio
import hashlib
import json
import unittest
import uuid
from dataclasses import replace
from unittest.mock import patch

from core_agent.model import ModelResponse, ToolRequest
from core_agent.model import ScriptedModel
from core_agent.guardrails import GuardrailClassifier
from core_agent.interactions import SETTINGS_KEYS
from core_agent.workspace import WorkspaceBinding
from tests.test_auth import AuthAppTestCase, TEST_DATABASE_URL


class ResponseFilesRuntimeTests(AuthAppTestCase):
    durable_blobs = True
    def set_limit(self, agent, value):
        tenant = self.app.state.authenticator.settings.tenant
        settings = agent.interaction_store.get_settings(tenant)
        values = {key: getattr(settings, key) for key in SETTINGS_KEYS}
        values["attachment_limit_bytes"] = value
        agent.interaction_store.update_settings(tenant, values, settings.revision)

    async def settled(self, task):
        agent = self.app.state.core_agent
        async with asyncio.timeout(3):
            while True:
                record = agent.workflow_store.lookup_task(task["id"])
                if record.state in {"COMPLETED", "FAILED", "CANCELLED", "ABORTED"}:
                    break
                await asyncio.sleep(0.01)
        self.assertEqual(record.state, "COMPLETED", record.error_code)
        return record

    async def workspace(self):
        self.assertIn("core_response_files", self.app.state.core_agent.platform_config.allowed_builtin_tools)
        task = await self.submit("owner-a", uuid.uuid4().hex, "output-chat")
        agent = self.app.state.core_agent
        record = await self.settled(task)
        binding = WorkspaceBinding(record.tenant_id, record.owner_id, record.context_id)
        return agent, agent.tool_runtime.environment_manager.backend.chats.workspace(binding)

    def choose(self, paths, call_id):
        return ModelResponse(tool_requests=(ToolRequest(call_id, "core_response_files", {"paths": paths}),))

    def policy(self, agent, mode):
        tenant = self.app.state.authenticator.settings.tenant
        current = agent.interaction_store.get_policy(tenant, "core_response_files", "builtin:core_response_files")
        agent.interaction_store.update_policy(tenant, "core_response_files", current.origin, mode=mode,
            guardrails_exempt=False, expected_revision=current.revision, actor_id="test-fixture-owner")

    async def selection(self, *responses):
        self.model._responses = [*responses, ModelResponse(message="Files ready")]
        task = await self.submit("owner-a", uuid.uuid4().hex, "output-chat")
        agent = self.app.state.core_agent
        record = await self.settled(task)
        return agent._terminal_result(record), record

    async def test_success_replaces_the_whole_set_and_recovery_preserves_snapshot(self):
        agent, folder = await self.workspace()
        (folder / "first.txt").write_bytes(b"first")
        (folder / "second.txt").write_bytes(b"second")
        result, record = await self.selection(self.choose(["first.txt"], "first"),
                                              self.choose(["second.txt"], "second"))
        self.assertEqual([entry["name"] for entry in result.outgoing_files], ["second.txt"])
        self.assertEqual(result.outgoing_files[0]["sha256"], hashlib.sha256(b"second").hexdigest())
        (folder / "second.txt").unlink()
        agent._runtime_cache.clear()
        recovered = agent.resume_task(record.task_id)
        self.assertEqual(recovered.to_dict(), result.to_dict())
        self.assertEqual(recovered.outgoing_files, result.outgoing_files)
        self.assertEqual((folder / "first.txt").read_bytes(), b"first")

    async def test_clear_and_invalid_replacement_preserve_exact_selection_semantics(self):
        _agent, folder = await self.workspace()
        (folder / "safe.txt").write_bytes(b"safe")
        result, _record = await self.selection(self.choose(["safe.txt"], "good"),
                                               self.choose(["../escape"], "bad"))
        self.assertEqual([entry["name"] for entry in result.outgoing_files], ["safe.txt"])
        self.assertIn("INVALID_FILE_PATH", self.model.calls[-1].context)
        cleared, record = await self.selection(self.choose([], "clear"))
        self.assertEqual(cleared.outgoing_files, ())
        self.assertFalse(record.result.get("outgoing_files"))
        self.assertEqual((folder / "safe.txt").read_bytes(), b"safe")

    async def test_oversized_new_set_is_a_tool_result_and_keeps_prior_set(self):
        agent, folder = await self.workspace()
        (folder / "first.txt").write_bytes(b"abc")
        (folder / "second.txt").write_bytes(b"defg")
        self.set_limit(agent, 5)
        result, _record = await self.selection(self.choose(["first.txt"], "good"),
            self.choose(["first.txt", "second.txt"], "too-large"))
        self.assertEqual([entry["name"] for entry in result.outgoing_files], ["first.txt"])
        self.assertIn("ATTACHMENTS_TOO_LARGE", self.model.calls[-1].context)
        self.assertIn('"actual_bytes":7', self.model.calls[-1].context.replace(" ", ""))
        self.assertIn('"allowed_bytes":5', self.model.calls[-1].context.replace(" ", ""))
        self.assertEqual((folder / "second.txt").read_bytes(), b"defg")
        self.assertNotIn('"raw":', json.dumps(result.to_dict()))
        self.assertNotIn('"content":', json.dumps(result.to_dict()))

    async def test_owner_download_is_frozen_shared_and_scoped_after_limit_changes(self):
        agent, folder = await self.workspace()
        (folder / "result.txt").write_bytes(b"immutable result")
        result, record = await self.selection(self.choose(["result.txt"], "choose"))
        ref = result.outgoing_files[0]
        (folder / "result.txt").write_bytes(b"changed")
        self.set_limit(agent, 1)
        path = f"/api/chats/output-chat/tasks/{record.task_id}/files/{ref['file_id']}"
        for owner in ("owner-a", "owner-b"):
            response = await self.http.get(path, headers=self.headers(owner))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.content, b"immutable result")
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")
            self.assertTrue(response.headers["content-disposition"].startswith("attachment;"))
        external = await self.http.get(path, headers=self.headers("external-a"))
        self.assertEqual(external.status_code, 403)
        for wrong in (path.replace("output-chat", "other-chat"),
                      path.replace(record.task_id, uuid.uuid4().hex),
                      path.replace(ref["file_id"], ref["blob_id"])):
            response = await self.http.get(wrong, headers=self.headers("owner-a"))
            self.assertEqual(response.status_code, 404, response.text)
        bad_query = await self.http.get(path + "?path=result.txt", headers=self.headers("owner-a"))
        self.assertEqual(bad_query.status_code, 400)

    async def test_nested_python_selection_uses_the_same_durable_manifest(self):
        _agent, folder = await self.workspace()
        (folder / "nested.txt").write_bytes(b"nested result")
        response = ModelResponse(tool_requests=(ToolRequest("python", "core_python_exec", {
            "code": "print('prefix'); print(tools.call('core_response_files', {'paths': ['nested.txt']})); print('suffix')",
        }),))
        result, record = await self.selection(response)
        self.assertEqual([entry["name"] for entry in result.outgoing_files], ["nested.txt"])
        self.assertEqual(result.usage.tool_calls, 2)
        self.assertEqual(record.snapshot["outgoing_files"], list(result.outgoing_files))
        self.assertNotIn("prepared_response_files", record.snapshot)
        self.assertIn("prefix", self.model.calls[-1].context)
        self.assertIn("suffix", self.model.calls[-1].context)

    async def test_denied_selection_is_hidden_and_cannot_snapshot_files(self):
        agent, folder = await self.workspace()
        (folder / "private.txt").write_bytes(b"private result")
        self.policy(agent, "deny")
        with patch.object(agent.response_files_service, "prepare", wraps=agent.response_files_service.prepare) as prepare:
            result, _record = await self.selection(self.choose(["private.txt"], "stale-selection"))
        prepare.assert_not_called()
        self.assertEqual(result.outgoing_files, ())
        for call in self.model.calls[-2:]:
            self.assertNotIn("core_response_files", call.tools)
            self.assertNotIn("core_response_files", call.instructions)

    async def test_hitl_approval_defers_snapshot_and_external_cannot_approve(self):
        agent, folder = await self.workspace()
        (folder / "approved.txt").write_bytes(b"approved result")
        self.policy(agent, "require_hitl")
        self.model._responses = [self.choose(["approved.txt"], "needs-owner"), ModelResponse(message="Files ready")]
        with patch.object(agent.response_files_service, "prepare", wraps=agent.response_files_service.prepare) as prepare:
            task = await self.submit("owner-a", uuid.uuid4().hex, "output-chat")
            pending = await self.http.get("/api/interactions", headers=self.headers("owner-a"), params={"task_id": task["id"]})
            self.assertEqual(pending.status_code, 200, pending.text)
            approval, = pending.json()["interactions"]
            self.assertEqual(approval["kind"], "tool_approval")
            prepare.assert_not_called()
            current = agent.workflow_store.lookup_task(task["id"])
            self.assertFalse(current.snapshot.get("outgoing_files"))
            # A policy edit applies to new calls; this accepted wait still needs its owner.
            self.policy(agent, "allow")
            path = f"/api/hitl/{approval['wait_id']}/decision"
            decision = {"decision": "allow", "subject_digest": approval["subject_digest"]}
            denied = await self.http.post(path, headers=self.headers("external-a"), json=decision)
            self.assertEqual(denied.status_code, 403)
            prepare.assert_not_called()
            allowed = await self.http.post(path, headers=self.headers("owner-b"), json=decision)
            self.assertEqual(allowed.status_code, 200, allowed.text)
            record = await self.settled(task)
        prepare.assert_called_once()
        self.assertEqual([entry["name"] for entry in record.result["outgoing_files"]], ["approved.txt"])
        self.assertNotIn("prepared_response_files", record.snapshot)

    async def test_budget_partial_preserves_selected_files_and_completion_provenance(self):
        agent, folder = await self.workspace()
        (folder / "partial.txt").write_bytes(b"partial result")
        agent.platform_config = replace(agent.platform_config, max_model_turns=2, max_tool_calls=1)
        result, record = await self.selection(self.choose(["partial.txt"], "last-tool"))
        self.assertFalse(result.complete)
        self.assertEqual(result.completion_reason, "budget_exhausted")
        self.assertEqual([entry["name"] for entry in result.outgoing_files], ["partial.txt"])
        fetched = await self.http.get(f"/a2a/owner/tasks/{record.task_id}", headers=self.headers("owner-b"))
        self.assertEqual(fetched.status_code, 200, fetched.text)
        artifact, = fetched.json()["artifacts"]
        provenance = artifact["metadata"]["provenance"]
        self.assertFalse(provenance["complete"])
        self.assertEqual(provenance["completion_reason"], "budget_exhausted")
        self.assertEqual(provenance["outgoingFiles"][0]["file_id"], result.outgoing_files[0]["file_id"])
        self.assertEqual(artifact["parts"][-1]["filename"], "partial.txt")

    async def test_guardrail_wait_persists_receipt_and_selection_together_without_recapture(self):
        agent, folder = await self.workspace()
        (folder / "frozen.txt").write_bytes(b"frozen before review")
        verdicts = ["clear", "clear", "suspicious", *(["clear"] * 5)]
        agent.guardrail_classifier = GuardrailClassifier(ScriptedModel([
            ModelResponse(message=json.dumps({"verdict": verdict}), finish_reason="stop") for verdict in verdicts
        ]), token_counter=len)
        self.model._responses = [self.choose(["frozen.txt"], "frozen-selection"), ModelResponse(message="Files ready")]
        with patch.object(agent.response_files_service, "prepare", wraps=agent.response_files_service.prepare) as prepare:
            task = await self.submit("owner-a", uuid.uuid4().hex, "output-chat")
            response = await self.http.get("/api/interactions", headers=self.headers("owner-a"), params={"task_id": task["id"]})
            self.assertEqual(response.status_code, 200, response.text)
            review, = response.json()["interactions"]
            self.assertEqual(review["kind"], "guardrail")
            before = agent.workflow_store.lookup_task(task["id"])
            self.assertEqual(before.snapshot["pending_completed_result"]["call"]["id"], "frozen-selection")
            refs = before.snapshot["outgoing_files"]
            self.assertEqual([entry["name"] for entry in refs], ["frozen.txt"])
            self.assertNotIn("prepared_response_files", before.snapshot)
            (folder / "frozen.txt").unlink()
            agent._runtime_cache.clear()
            allowed = await self.http.post(f"/api/guardrails/{review['wait_id']}/decision", headers=self.headers("owner-b"),
                json={"decision": "allow", "subject_digest": review["subject_digest"]})
            self.assertEqual(allowed.status_code, 200, allowed.text)
            record = await self.settled(task)
        prepare.assert_called_once()
        self.assertEqual(record.result["outgoing_files"], refs)
        downloaded = await self.http.get(f"/api/chats/output-chat/tasks/{record.task_id}/files/{refs[0]['file_id']}",
            headers=self.headers("owner-a"))
        self.assertEqual(downloaded.content, b"frozen before review")
        self.assertEqual(downloaded.status_code, 200)


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL is required")
class PostgresResponseFilesRuntimeTests(ResponseFilesRuntimeTests):
    use_postgres = True
