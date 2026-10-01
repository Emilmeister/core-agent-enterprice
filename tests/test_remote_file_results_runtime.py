"""Remote result batches cross the actual tool and file publication guard."""
import asyncio
import base64
import copy
import json
import threading
import unittest
import uuid
from unittest.mock import patch

from core_agent.runtime import _MaterialSuspended
from core_agent.tools import ToolCall
from core_agent.errors import CoreError
from core_agent.guardrails import GuardrailClassifier
from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
from core_agent.remote_agents import RemoteAgentCard, RemoteAgentConnection, RemoteEvent
from core_agent.owner_api import interaction_digest
from core_agent.workspace import WorkspaceBinding
from tests.test_auth import AuthAppTestCase, TEST_DATABASE_URL
from tests.app_support import create_app
from core_agent.database import PostgresDatabase
import httpx


class Detector:
    def __init__(self, suspicious_text=False):
        self.suspicious_text = suspicious_text
        self.calls = []

    def count_tokens(self, text):
        return max(1, len(text) // 4)

    def generate(self, **kwargs):
        self.calls.append(kwargs["context"])
        suspicious = self.suspicious_text and "Remote answer" in kwargs["context"]
        if suspicious:
            self.suspicious_text = False
        return ModelResponse(message=json.dumps({"verdict": "suspicious" if suspicious else "clear"}), finish_reason="stop")


class RemoteFileResultTests(AuthAppTestCase):
    durable_blobs = True

    async def asyncSetUp(self):
        with patch("core_agent.runtime.CoreAgent.recover_workflows", return_value=()):
            await super().asyncSetUp()
        self.agent = self.app.state.core_agent
        self.tenant = self.app.state.authenticator.settings.tenant
        self.app.state.remote_registry_store.create(self.tenant, {
            "name": "delivery", "url": "https://peer.example/a2a", "description": "Delivery",
            "enabled": True, "header_name": "Authorization"}, actor_id="alice")
        discovery = patch("core_agent.remote_agents.connect_peer", side_effect=lambda peer, **kwargs:
            RemoteAgentConnection(RemoteAgentCard(peer["name"], peer["description"], peer["url"], False, (), "HTTP+JSON")))
        discovery.start()
        self.addCleanup(discovery.stop)
        self.response = RemoteEvent("message", None, "Remote answer", True, (
            {"text": "Remote answer"},
            {"raw": base64.b64encode(b"private file text").decode(), "filename": "report.txt", "mediaType": "text/plain"},
            {"raw": "", "filename": "empty.txt", "mediaType": "text/plain"}))
        sender = patch.object(RemoteAgentConnection, "send_task", side_effect=lambda **kwargs: self.response)
        self.sent = sender.start()
        self.addCleanup(sender.stop)
        self.settle = True
        self.remote_started = threading.Event()
        original = self.agent.task_scheduler.start_remote

        def settled(*args, **kwargs):
            task = original(*args, **kwargs)
            self.operation_id = task.id
            self.operation_owner = kwargs["owner_id"]
            if self.settle:
                self.agent.task_scheduler.wait(task.id, timeout=5, owner_id=kwargs["owner_id"], tenant_id=kwargs["tenant_id"])
            self.remote_started.set()
            return self.agent.task_scheduler.get(task.id, owner_id=kwargs["owner_id"], tenant_id=kwargs["tenant_id"])

        admission = patch.object(self.agent.task_scheduler, "start_remote", side_effect=settled)
        admission.start()
        self.addCleanup(admission.stop)
        self.detector = Detector()
        self.agent.guardrail_classifier = GuardrailClassifier(self.detector)
        self.model = ScriptedModel([ModelResponse(tool_requests=(ToolRequest("send", "core_agent_send_message", {
            "agent_name": "delivery", "task": "Ask peer", "files": []}),)), ModelResponse(message="done")])
        self.model.model = "auth-test-model"
        self.agent.model = self.model

    async def asyncTearDown(self):
        record = getattr(self, "record", None)
        if record is not None:
            current = self.agent.workflow_store.lookup_task(record.task_id)
            if current.state not in {"COMPLETED", "FAILED", "CANCELLED", "REJECTED", "ABORTED"}:
                await asyncio.to_thread(self.agent.cancel_task, current.task_id)

    async def start(self):
        task = await self.submit("owner-a", uuid.uuid4().hex, "result-" + uuid.uuid4().hex)
        record = self.agent.workflow_store.lookup_task(task["id"])
        self.record = record
        self.binding = WorkspaceBinding(record.tenant_id, record.owner_id, record.context_id)
        operation = self.agent.task_scheduler.list(owner_id=record.run_id, tenant_id=record.tenant_id)[0]
        self.batch = self.agent.chat_file_service.store.get(operation.result["file_batch_id"], record.tenant_id)
        self.target = self.agent.chat_file_service.workspaces.workspace(self.binding) / "attachments" / self.batch["batch_id"]
        return record

    async def decide(self, record, decision):
        response = await self.http.post(f"/api/guardrails/{record.snapshot['wait_id']}/decision",
            headers=self.headers("owner-a"), json={"decision": decision, "subject_digest": interaction_digest(self.agent.workflow_store.get_wait(
                record.snapshot["wait_id"], tenant_id=record.tenant_id, owner_id=record.owner_id))})
        self.assertEqual(response.status_code, 200, response.text)
        result = await asyncio.to_thread(self.agent.resume_task, record.task_id)
        return self.agent.workflow_store.lookup_task(record.task_id), result

    async def test_whole_text_and_ordered_empty_batch_wait_before_any_publication(self):
        self.detector.suspicious_text = True
        self.assertIs(self.agent.task_scheduler.chat_file_service, self.agent.chat_file_service)
        record = await self.start()
        self.assertEqual(record.state, "WAITING_INPUT")
        self.assertFalse(self.target.exists())
        self.assertEqual(len(self.model.calls), 1)
        while record.state == "WAITING_INPUT":
            record, _ = await self.decide(record, "allow")
        self.assertEqual(record.state, "COMPLETED", record.error_code)
        self.assertEqual((self.target / "report.txt").read_bytes(), b"private file text")
        self.assertEqual((self.target / "empty.txt").read_bytes(), b"")
        context = self.model.calls[-1].context
        self.assertIn("Remote answer", context)
        self.assertIn("report.txt", context)
        self.assertNotIn("file_batch_id", context)
        self.assertNotIn("private file text", context)
        self.assertEqual(self.sent.call_count, 1)

    async def test_file_rejection_withholds_entire_result_and_every_file(self):
        record = await self.start()
        self.assertEqual(record.state, "WAITING_INPUT")
        self.assertFalse(self.target.exists())
        record, _ = await self.decide(record, "reject")
        self.assertEqual(record.state, "COMPLETED", record.error_code)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.agent.chat_file_service.store.get(self.batch["batch_id"], self.tenant)["state"], "excluded")
        self.assertIn("MATERIAL_REJECTED", self.model.calls[-1].context)
        self.assertNotIn("Remote answer", self.model.calls[-1].context)
        self.assertNotIn("report.txt", self.model.calls[-1].context)
        self.assertEqual(self.sent.call_count, 1)


    def exempt(self, name="core_agent_send_message"):
        policy = self.agent.interaction_store.get_policy(self.tenant, name, "builtin:" + name)
        self.agent.interaction_store.update_policy(self.tenant, name, policy.origin,
            mode="allow", guardrails_exempt=True, expected_revision=policy.revision, actor_id="alice")

    async def test_text_rejection_excludes_files_without_classifying_or_publishing_them(self):
        self.detector.suspicious_text = True
        record = await self.start()
        count = len(self.detector.calls)
        record, _ = await self.decide(record, "reject")
        self.assertEqual(record.state, "COMPLETED", record.error_code)
        self.assertEqual(len(self.detector.calls), count)
        self.assertEqual(self.agent.chat_file_service.store.get(self.batch["batch_id"], self.tenant)["state"], "excluded")
        self.assertFalse(self.target.exists())
        self.assertNotIn("Remote answer", self.model.calls[-1].context)
        self.assertNotIn("report.txt", self.model.calls[-1].context)
        history = await self.http.get(f"/api/chats/{record.context_id}/history", headers=self.headers("owner-a"))
        self.assertEqual(history.status_code, 200, history.text)
        self.assertNotIn("report.txt", history.text)
        self.assertNotIn("file_batch_id", history.text)

    async def test_trusted_exemption_allows_empty_binary_without_classifier_or_fake_review(self):
        self.exempt()
        self.response = RemoteEvent("message", None, "Remote answer", True, (
            {"text": "Remote answer"}, {"raw": "", "filename": "empty.txt", "mediaType": "text/plain"},
            {"raw": base64.b64encode(b"\xff\x00").decode(), "filename": "binary.bin", "mediaType": "application/octet-stream"}))
        record = await self.start()
        self.assertEqual(record.state, "COMPLETED", record.error_code)
        self.assertEqual((self.target / "empty.txt").read_bytes(), b"")
        self.assertEqual((self.target / "binary.bin").read_bytes(), b"\xff\x00")
        self.assertEqual(len(self.detector.calls), 1)  # Only original input crosses the detector.
        batch = self.agent.chat_file_service.store.get(self.batch["batch_id"], self.tenant)
        self.assertTrue(batch["decision_ref"].startswith("exempt:"))
        self.assertNotIn("file_batch_id", self.model.calls[-1].context)
        sources = record.snapshot["context"]["transcript"][-2]["provenance"]["sources"]
        identities = [material for source in sources.values() for material in source.get("materials", [])]
        self.assertEqual(sum(item.get("material_kind") == "file_sha256" for item in identities), 2)

    async def test_previous_denied_identical_bytes_remain_excluded_under_later_exemption(self):
        record = await self.start()
        first_batch = self.batch["batch_id"]
        self.model._responses = [ModelResponse(tool_requests=(ToolRequest("send-again", "core_agent_send_message", {
            "agent_name": "delivery", "task": "Ask peer again", "files": []}),)), ModelResponse(message="done")]
        self.exempt()
        record, _ = await self.decide(record, "reject")
        self.assertEqual(record.state, "COMPLETED", record.error_code)
        operations = self.agent.task_scheduler.list(owner_id=record.run_id, tenant_id=record.tenant_id)
        self.assertEqual(len(operations), 2)
        for operation in operations:
            batch_id = operation.result["file_batch_id"]
            self.assertEqual(self.agent.chat_file_service.store.get(batch_id, self.tenant)["state"], "excluded")
            self.assertFalse((self.target.parent / batch_id).exists())
        self.assertTrue(any(operation.result["file_batch_id"] != first_batch for operation in operations))
        self.assertNotIn("Remote answer", self.model.calls[-1].context)
        self.assertEqual(self.sent.call_count, 2)

    async def test_publication_outage_reuses_saved_result_and_reviews_without_send_replay(self):
        self.response = RemoteEvent("message", None, "Remote answer", True, self.response.parts[:2])
        with patch.object(self.agent.chat_file_service, "publish", side_effect=OSError("storage unavailable")):
            record = await self.start()
        self.assertEqual(record.state, "MODEL_RESPONDED")
        self.assertEqual(len(self.model.calls), 1)
        self.assertFalse(self.target.exists())
        count = len(self.detector.calls)
        self.agent._runtime_cache.clear()
        await asyncio.to_thread(self.agent.resume_task, record.task_id)
        record = self.agent.workflow_store.lookup_task(record.task_id)
        self.assertEqual(record.state, "COMPLETED", record.error_code)
        self.assertEqual(len(self.detector.calls), count)
        self.assertEqual(self.sent.call_count, 1)
        self.assertEqual((self.target / "report.txt").read_bytes(), b"private file text")
        self.assertNotIn("file_batch_id", self.model.calls[-1].context)

    async def test_last_file_corruption_after_wait_prevents_every_publication(self):
        record = await self.start()
        private = self.agent.chat_file_service.workspaces.root / "private/uploads" / self.batch["batch_id"]
        (private / "empty.txt").write_bytes(b"tampered")
        with self.assertRaises(CoreError) as caught:
            await self.decide(record, "allow")
        self.assertEqual(caught.exception.code, "ARTIFACT_INTEGRITY_FAILED")
        self.assertFalse(self.target.exists())
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.sent.call_count, 1)

    async def test_nested_python_wait_stops_before_remainder_and_reuses_known_result(self):
        self.model._responses = [ModelResponse(tool_requests=(ToolRequest("python", "core_python_exec", {
            "code": "print('prefix-proof', flush=True); print(tools.call('core_agent_send_message', {'agent_name': 'delivery', 'task': 'Ask peer', 'files': []})); print('remainder-proof', flush=True)"}),)),
            ModelResponse(message="done")]
        record = await self.start()
        self.assertEqual(record.state, "WAITING_INPUT", record.error_code)
        self.assertFalse(self.target.exists())
        self.assertEqual(record.snapshot["python_execution"]["phase"], "stopped")
        while record.state == "WAITING_INPUT":
            record, _ = await self.decide(record, "allow")
        self.assertEqual(record.state, "COMPLETED", record.error_code)
        context = self.model.calls[-1].context
        self.assertIn("prefix-proof", context)
        self.assertIn("PYTHON_CONTINUATION_INTERRUPTED", context)
        self.assertIn("report.txt", context)
        self.assertNotIn("file_batch_id", context)
        self.assertNotIn("remainder-proof\\n", context)
        self.assertEqual(self.sent.call_count, 1)
        self.assertEqual((self.target / "report.txt").read_bytes(), b"private file text")

    async def test_nested_python_publication_pending_stops_before_remainder(self):
        self.response = RemoteEvent("message", None, "Remote answer", True, self.response.parts[:2])
        self.model._responses = [ModelResponse(tool_requests=(ToolRequest("python", "core_python_exec", {
            "code": "print('prefix-proof', flush=True); tools.call('core_agent_send_message', {'agent_name': 'delivery', 'task': 'Ask peer', 'files': []}); print('remainder-proof', flush=True)"}),)),
            ModelResponse(message="done")]
        with patch.object(self.agent.chat_file_service, "publish", side_effect=OSError("storage unavailable")):
            record = await self.start()
        self.assertEqual(record.state, "MODEL_RESPONDED", record.error_code)
        self.assertEqual(record.snapshot["python_execution"]["phase"], "stopped")
        self.assertFalse(self.target.exists())
        await asyncio.to_thread(self.agent.resume_task, record.task_id)
        record = self.agent.workflow_store.lookup_task(record.task_id)
        self.assertEqual(record.state, "COMPLETED", record.error_code)
        self.assertEqual(self.sent.call_count, 1)
        self.assertNotIn("file_batch_id", self.model.calls[-1].context)
        self.assertEqual((self.target / "report.txt").read_bytes(), b"private file text")


    async def test_get_list_wait_reuse_published_batch_and_complete_file_provenance(self):
        self.response = RemoteEvent("message", None, "Remote answer", True, self.response.parts[:2])
        original = self.model.generate
        def generate(**kwargs):
            names = {1: "core_task_get", 2: "core_task_list", 3: "core_task_wait"}
            name = names.get(len(self.model.calls))
            if name is not None:
                arguments = {} if name == "core_task_list" else {"task_id": self.operation_id}
                self.model._responses = [ModelResponse(tool_requests=(ToolRequest(name, name, arguments),)),
                    ModelResponse(message="done")]
            return original(**kwargs)
        with patch.object(self.model, "generate", side_effect=generate):
            record = await self.start()
        self.assertEqual(record.state, "COMPLETED", record.error_code)
        self.assertEqual(self.sent.call_count, 1)
        transcript = record.snapshot["context"]["transcript"]
        for name in ("core_agent_send_message", "core_task_get", "core_task_list", "core_task_wait"):
            result = next(item for item in transcript if item["kind"] == "tool_result"
                and json.loads(item["content"])["tool_name"] == name)
            self.assertIn("report.txt", result["content"])
            self.assertNotIn("file_batch_id", result["content"])
            identities = [material for source in result["provenance"]["sources"].values()
                          for material in source.get("materials", [])]
            self.assertEqual(sum(item.get("material_kind") == "file_sha256" for item in identities), 1)
        self.assertFalse(self.agent.task_scheduler.mailbox(record.run_id, self.tenant).poll())

    async def test_completed_result_recovery_preserves_owner_wait_and_never_replays_send(self):
        self.detector.suspicious_text = True
        record = await self.start()
        wait_id = record.snapshot["wait_id"]
        self.agent._runtime_cache.clear()
        resumed = await asyncio.to_thread(self.agent.resume_task, record.task_id)
        self.assertEqual(resumed.wait_id, wait_id)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.sent.call_count, 1)
        while record.state == "WAITING_INPUT":
            record, _ = await self.decide(record, "allow")
        self.assertEqual(record.state, "COMPLETED", record.error_code)
        self.assertEqual(self.sent.call_count, 1)

    async def test_nested_python_clear_receives_guarded_file_receipts(self):
        self.response = RemoteEvent("message", None, "Remote answer", True, self.response.parts[:2])
        original = self.model.generate
        def generate(**kwargs):
            if len(self.model.calls) == 1:
                self.model._responses = [ModelResponse(tool_requests=(ToolRequest("python", "core_python_exec", {
                    "code": "value=tools.call('core_task_get', {'task_id': '" + self.operation_id + "'}); print(value['result']['files'][0]['actual_name']); print('remainder-proof')"}),)),
                    ModelResponse(message="done")]
            return original(**kwargs)
        with patch.object(self.model, "generate", side_effect=generate):
            record = await self.start()
        self.assertEqual(record.state, "COMPLETED", record.error_code)
        item = next(item for item in record.snapshot["context"]["transcript"]
            if item["kind"] == "tool_result" and json.loads(item["content"])["tool_name"] == "core_python_exec")
        result = json.loads(item["content"])
        self.assertIn("report.txt", result["output"]["stdout"], result)
        self.assertIn("remainder-proof", result["output"]["stdout"])
        self.assertNotIn("file_batch_id", self.model.calls[-1].context)
        identities = [material for source in item["provenance"]["sources"].values()
                      for material in source.get("materials", [])]
        self.assertEqual(sum(material.get("material_kind") == "file_sha256" for material in identities), 1)
        self.assertEqual(self.sent.call_count, 1)

    async def test_coordinator_task_wait_delivers_completed_batch_through_same_guard(self):
        self.settle = False
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.sent.side_effect = lambda **kwargs: (gate.wait(5), self.response)[1]
        original = self.model.generate
        def generate(**kwargs):
            if len(self.model.calls) == 1:
                self.model._responses = [ModelResponse(tool_requests=(ToolRequest("wait", "core_task_wait", {
                    "task_id": self.operation_id}),)), ModelResponse(message="done")]
            return original(**kwargs)
        with patch.object(self.model, "generate", side_effect=generate):
            task = await self.submit("owner-a", uuid.uuid4().hex, "result-" + uuid.uuid4().hex)
        record = self.agent.workflow_store.lookup_task(task["id"])
        self.record = record
        self.assertEqual(record.state, "WAITING_TASK", {"error": record.error_code, "keys": list(record.snapshot), "calls": len(self.model.calls)})
        gate.set()
        await asyncio.to_thread(self.agent.task_scheduler.wait, self.operation_id, 5,
            owner_id=record.run_id, tenant_id=record.tenant_id)
        self.binding = WorkspaceBinding(record.tenant_id, record.owner_id, record.context_id)
        operation = self.agent.task_scheduler.get(self.operation_id, owner_id=record.run_id, tenant_id=record.tenant_id)
        self.batch = self.agent.chat_file_service.store.get(operation.result["file_batch_id"], record.tenant_id)
        self.target = self.agent.chat_file_service.workspaces.workspace(self.binding) / "attachments" / self.batch["batch_id"]
        initial = next(json.loads(item["content"]) for item in record.snapshot["context"]["transcript"]
            if item["kind"] == "tool_result" and json.loads(item["content"])["tool_name"] == "core_agent_send_message")
        self.assertNotEqual(initial["output"]["state"], "completed")
        candidate, batches = self.agent._remote_result_batches(record, initial)
        self.assertEqual(batches, [])
        self.assertEqual(candidate, initial)
        await asyncio.to_thread(self.agent._recover_workflows_once)
        workers = tuple(self.agent._recovery_workers.values())
        for worker, _control in workers:
            await asyncio.to_thread(worker.join, 5)
        record = self.agent.workflow_store.lookup_task(record.task_id)
        self.assertEqual(record.state, "WAITING_INPUT", record.error_code)
        self.assertFalse(self.target.exists())
        while record.state == "WAITING_INPUT":
            record, _ = await self.decide(record, "allow")
        self.assertEqual(record.state, "COMPLETED", record.error_code)
        self.assertEqual(self.sent.call_count, 1)
        self.assertNotIn("file_batch_id", self.model.calls[-1].context)

    async def test_file_review_timeout_excludes_every_file_and_original_text(self):
        record = await self.start()
        self.agent.workflow_store.resolve_wait(record.snapshot["wait_id"], tenant_id=self.tenant,
            outcome={"reason": "timeout"})
        await asyncio.to_thread(self.agent.resume_task, record.task_id)
        record = self.agent.workflow_store.lookup_task(record.task_id)
        self.assertEqual(record.state, "COMPLETED", record.error_code)
        self.assertEqual(self.agent.chat_file_service.store.get(self.batch["batch_id"], self.tenant)["state"], "excluded")
        self.assertFalse(self.target.exists())
        self.assertIn("MATERIAL_TIMEOUT", self.model.calls[-1].context)
        self.assertNotIn("Remote answer", self.model.calls[-1].context)
        self.assertEqual(self.sent.call_count, 1)

    async def test_actual_child_and_grandchild_publish_with_canonical_root_and_source(self):
        self.response = RemoteEvent("message", None, "Remote answer", True, self.response.parts[:2])
        def delegate(identifier, tools):
            return ModelResponse(tool_requests=(ToolRequest(identifier, "core_delegate", {
                "instruction": "Ask delivery and report the outcome", "tools": tools, "skills": [],
                "budget": {"turns": 5, "tool_calls": 5}}),))
        self.model._responses = [delegate("child", ["core_delegate", "core_agent_send_message"]),
            delegate("grandchild", ["core_agent_send_message"]),
            ModelResponse(tool_requests=(ToolRequest("send", "core_agent_send_message", {
                "agent_name": "delivery", "task": "Ask peer", "files": []}),)),
            ModelResponse(message="grandchild done"), ModelResponse(message="child done"), ModelResponse(message="root done")]
        task = await self.submit("owner-a", uuid.uuid4().hex, "result-" + uuid.uuid4().hex)
        self.assertTrue(await asyncio.to_thread(self.remote_started.wait, 5))
        root = self.agent.workflow_store.lookup_task(task["id"])
        self.record = root
        child_task = self.agent.task_scheduler.list(owner_id=root.run_id, tenant_id=self.tenant)[0]
        child = self.agent.workflow_store.lookup_task(child_task.id)
        grandchild_task = self.agent.task_scheduler.list(owner_id=child.run_id, tenant_id=self.tenant)[0]
        await asyncio.to_thread(self.agent.task_scheduler.wait, grandchild_task.id, 5,
            owner_id=child.run_id, tenant_id=self.tenant)
        source = self.agent.workflow_store.get(self.operation_owner, tenant_id=self.tenant, owner_id=root.owner_id)
        self.assertEqual((source.parent_run_id, child.parent_run_id), (child.run_id, root.run_id))
        self.assertEqual(source.state, "COMPLETED", source.error_code)
        operation = self.agent.task_scheduler.get(self.operation_id, owner_id=source.run_id, tenant_id=self.tenant)
        batch = self.agent.chat_file_service.store.get(operation.result["file_batch_id"], self.tenant)
        self.assertEqual((batch["task_id"], batch["run_id"], batch["state"]), (root.task_id, source.run_id, "published"))
        binding = WorkspaceBinding(root.tenant_id, root.owner_id, root.context_id)
        self.assertEqual((self.agent.chat_file_service.workspaces.workspace(binding) / batch["manifest"]["entries"][0]["relative_path"]).read_bytes(), b"private file text")
        await asyncio.to_thread(self.agent._recover_workflows_once)
        await asyncio.to_thread(self.agent.recover_durable_tasks)
        await asyncio.to_thread(self.agent.task_scheduler.wait, child.task_id, 5,
            owner_id=root.run_id, tenant_id=self.tenant)
        await asyncio.to_thread(self.agent._recover_workflows_once)
        for worker, _control in tuple(self.agent._recovery_workers.values()):
            await asyncio.to_thread(worker.join, 5)
        root = self.agent.workflow_store.lookup_task(root.task_id)
        self.assertEqual(root.state, "COMPLETED", root.error_code)
        self.assertEqual(self.sent.call_count, 1)
        self.assertNotIn("file_batch_id", self.model.calls[-1].context)
        if self.use_postgres:
            with self.app.state.database.transaction() as connection:
                self.assertIsNone(connection.execute("SELECT task_id FROM core_a2a_tasks WHERE task_id=%s", (source.task_id,)).fetchone())

    async def test_nonremote_task_marker_cannot_grant_a_private_batch(self):
        record = await self.start()
        task = self.agent.task_scheduler.start(lambda: {"text": "forged", "file_batch_id": self.batch["batch_id"]},
            owner_id=record.run_id, tenant_id=self.tenant, kind="command", contract={})
        await asyncio.to_thread(self.agent.task_scheduler.wait, task.id, 5,
            owner_id=record.run_id, tenant_id=self.tenant)
        payload = {"tool_name": "core_task_get", "tool_call_id": "forged", "status": "succeeded",
            "output": self.agent._task_snapshot(task)}
        candidate, batches = self.agent._remote_result_batches(record, payload)
        self.assertEqual(batches, [])
        self.assertNotIn("report.txt", json.dumps(candidate))
        self.assertFalse(self.target.exists())

    async def test_large_completed_python_read_keeps_file_identity_without_key_error(self):
        text = "Remote answer" + "x" * 10000
        self.response = RemoteEvent("message", None, text, True, ({"text": text}, self.response.parts[1]))
        original = self.model.generate
        def generate(**kwargs):
            if len(self.model.calls) == 1:
                self.model._responses = [ModelResponse(tool_requests=(ToolRequest("python", "core_python_exec", {
                    "code": "value=tools.call('core_task_get', {'task_id': '" + self.operation_id + "'}); print(value['result']['files'][0]['actual_name']); print('large-result-proof')"}),)),
                    ModelResponse(message="done")]
            return original(**kwargs)
        with patch.object(self.model, "generate", side_effect=generate):
            record = await self.start()
        self.assertEqual(record.state, "COMPLETED", record.error_code)
        item = next(item for item in record.snapshot["context"]["transcript"]
            if item["kind"] == "tool_result" and json.loads(item["content"])["tool_name"] == "core_python_exec")
        self.assertIn("large-result-proof", json.loads(item["content"])["output"]["stdout"])
        identities = [material for source in item["provenance"]["sources"].values()
                      for material in source.get("materials", [])]
        self.assertEqual(sum(material.get("material_kind") == "file_sha256" for material in identities), 1)

    async def test_stopped_python_retains_prior_completed_file_read_identity(self):
        first = RemoteEvent("message", None, "Remote answer", True, self.response.parts[:2])
        second = self.response
        self.sent.side_effect = [first, second]
        original = self.model.generate
        def generate(**kwargs):
            if len(self.model.calls) == 1:
                self.model._responses = [ModelResponse(tool_requests=(ToolRequest("python", "core_python_exec", {
                    "code": "value=tools.call('core_task_get', {'task_id': '" + self.operation_id + "'}); print(value['result']['files'][0]['actual_name'], flush=True); tools.call('core_agent_send_message', {'agent_name': 'delivery', 'task': 'Another request', 'files': []}); print('remainder-proof', flush=True)"}),)),
                    ModelResponse(message="done")]
            return original(**kwargs)
        with patch.object(self.model, "generate", side_effect=generate):
            record = await self.start()
        self.assertEqual(record.state, "WAITING_INPUT", record.error_code)
        while record.state == "WAITING_INPUT":
            record, _ = await self.decide(record, "allow")
        self.assertEqual(record.state, "COMPLETED", record.error_code)
        item = next(item for item in record.snapshot["context"]["transcript"]
            if item["kind"] == "tool_result" and json.loads(item["content"])["tool_name"] == "core_python_exec")
        self.assertIn("report.txt", json.loads(item["content"])["output"]["stdout"])
        identities = [material for source in item["provenance"]["sources"].values()
                      for material in source.get("materials", [])]
        self.assertEqual(sum(material.get("material_kind") == "file_sha256" for material in identities), 3)
        self.assertEqual(self.sent.call_count, 2)

    async def test_exempt_live_python_read_stops_after_durable_receipt_on_publication_outage(self):
        self.exempt("core_task_get")
        self.response = RemoteEvent("message", None, "Remote answer", True, self.response.parts[:2])
        original_model = self.model.generate
        def generate(**kwargs):
            if len(self.model.calls) == 1:
                self.model._responses = [ModelResponse(tool_requests=(ToolRequest("python", "core_python_exec", {
                    "code": "print('read-prefix-proof', flush=True); tools.call('core_task_get', {'task_id': '" + self.operation_id + "'}); print('remainder-proof', flush=True)"}),)),
                    ModelResponse(message="done")]
            return original_model(**kwargs)
        original_publish = self.agent.chat_file_service.publish
        publications = []
        def publish(*args, **kwargs):
            publications.append(args[0])
            if len(publications) > 1:
                raise OSError("storage unavailable")
            return original_publish(*args, **kwargs)
        with patch.object(self.model, "generate", side_effect=generate), patch.object(self.agent.chat_file_service, "publish", side_effect=publish):
            record = await self.start()
        self.assertEqual(record.state, "MODEL_RESPONDED", record.error_code)
        self.assertEqual(record.snapshot["python_execution"]["phase"], "stopped")
        self.assertIn("file_delivery_pending", record.snapshot)
        await asyncio.to_thread(self.agent.resume_task, record.task_id)
        record = self.agent.workflow_store.lookup_task(record.task_id)
        self.assertEqual(record.state, "COMPLETED", record.error_code)
        self.assertEqual(self.sent.call_count, 1)
        self.assertIn("PYTHON_CONTINUATION_INTERRUPTED", self.model.calls[-1].context)
        self.assertNotIn("file_batch_id", self.model.calls[-1].context)

    async def test_ready_batch_later_exact_negative_excludes_before_publication(self):
        await self.ready_negative("file_attachment")

    async def test_ready_batch_later_exact_text_negative_excludes_before_publication(self):
        await self.ready_negative("tool_result")

    async def ready_negative(self, kind):
        self.response = RemoteEvent("message", None, "Remote answer", True, self.response.parts[:2])
        with patch.object(self.agent.chat_file_service, "publish", side_effect=OSError("storage unavailable")):
            record = await self.start()
        batch = self.agent.chat_file_service.store.get(self.batch["batch_id"], self.tenant)
        self.assertEqual(batch["state"], "accepted_ready")
        self.assertFalse(self.target.exists())
        original_allow = batch["decision_ref"]
        material = self.agent.chat_file_service.review_material(batch["batch_id"], self.binding,
            run_id=record.run_id, task_id=record.task_id)
        document = material["documents"][0]
        entry = material["manifest"]["entries"][0]
        self.detector.suspicious_text = True
        snapshot = copy.deepcopy(record.snapshot)
        saved = snapshot["pending_completed_result"]["call"]
        call = ToolCall(saved["id"], saved["name"], saved["arguments"])
        lease = self.agent.workflow_store.acquire_lease(record.run_id, tenant_id=record.tenant_id,
            owner_id=record.owner_id, worker_id="later-negative-fixture", ttl=60)
        try:
            with self.assertRaises(_MaterialSuspended):
                file_options = {"sealed_ref": {"batch_id": batch["batch_id"], "index": 0},
                    "material_digest": entry["sha256"], "material_kind": "file_sha256", "text_digest": document["text_digest"],
                    "documents": [json.dumps(material["manifest"]), document["text"]], "complete": document["complete"]}
                self.agent._guard_material(record, snapshot, source_id="later-source:" + batch["batch_id"],
                    source_kind=kind, payload=({"manifest": material["manifest"], "index": 0}
                        if kind == "file_attachment" else snapshot["pending_completed_result"]["outcome"]),
                    **(file_options if kind == "file_attachment" else {}),
                    continuation=self.agent._tool_wait_continuation(snapshot, call, "tool_result"), lease_token=lease)
        finally:
            try:
                self.agent.workflow_store.release_lease(record.run_id, tenant_id=record.tenant_id,
                    worker_id="later-negative-fixture", token=lease)
            except CoreError as error:
                if error.code != "LEASE_LOST":
                    raise
        record = self.agent.workflow_store.lookup_task(record.task_id)
        denying_review = self.agent.workflow_store.get_wait(record.snapshot["wait_id"], tenant_id=self.tenant).source_id
        self.assertNotEqual(denying_review, original_allow)
        record, _ = await self.decide(record, "reject")
        self.assertEqual(record.state, "COMPLETED", record.error_code)
        batch = self.agent.chat_file_service.store.get(batch["batch_id"], self.tenant)
        self.assertEqual((batch["state"], batch["decision_ref"]), ("excluded", denying_review))
        self.assertEqual(self.agent.material_review_store.get(record, original_allow)["state"], "clear")
        self.assertFalse(self.target.exists())
        self.assertEqual(self.sent.call_count, 1)
        self.assertIn("MATERIAL_REJECTED", self.model.calls[-1].context)
        self.assertNotIn("Remote answer", self.model.calls[-1].context)


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL is required")
class PostgresRemoteFileResultTests(RemoteFileResultTests):
    use_postgres = True

    async def test_actual_restart_and_pool_one_resume_exact_saved_candidate(self):
        self.response = RemoteEvent("message", None, "Remote answer", True, self.response.parts[:2])
        self.detector.suspicious_text = True
        record = await self.start()
        wait_id = record.snapshot["wait_id"]
        self.app.state.close()
        database = PostgresDatabase(TEST_DATABASE_URL, min_size=1, max_size=1, timeout=1)
        with patch("core_agent.runtime.CoreAgent.recover_workflows", return_value=()):
            self.app = create_app(model=self.model, database=database,
                base_url="https://agent.example.test", auth_transport=httpx.MockTransport(self.introspect))
        self.addCleanup(self.app.state.close)
        await self.http.aclose()
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://agent.example.test")
        self.addAsyncCleanup(self.http.aclose)
        self.agent = self.app.state.core_agent
        self.agent.guardrail_classifier = GuardrailClassifier(self.detector)
        record = self.agent.workflow_store.lookup_task(record.task_id)
        self.assertEqual(record.snapshot["wait_id"], wait_id)
        async with asyncio.timeout(10):
            record, _ = await self.decide(record, "allow")
        self.assertEqual(record.state, "COMPLETED", record.error_code)
        self.assertEqual(self.sent.call_count, 1)
        self.assertEqual((self.target / "report.txt").read_bytes(), b"private file text")
        self.assertNotIn("file_batch_id", self.model.calls[-1].context)

