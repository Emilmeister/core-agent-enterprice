"""Incoming S3 receipts use committed metadata before any original-byte read."""
import copy
import uuid
from dataclasses import replace
from unittest.mock import patch

from psycopg.types.json import Jsonb

from core_agent.errors import CoreError
from core_agent.history import _json_identity
from core_agent.model import ModelResponse
from core_agent.peer_conversations import read_conversations
from core_agent.workspace import WorkspaceBinding
from tests.test_cloudru_storage_runtime import CloudStorageRuntimeFixture
from tests import test_owner_history


class S3PeerProjectionTests(CloudStorageRuntimeFixture):
    async def incoming(self, *, publish):
        self.model._responses = [ModelResponse(message="Ready")]
        task = await self.submit("peer-files")
        source = self.agent.workflow_store.lookup_task(task["id"])
        snapshot = copy.deepcopy(source.snapshot)
        snapshot.pop("terminal_intent", None)
        snapshot.pop("execution_owner", None)
        token = uuid.uuid4().hex
        with self.app.state.database.transaction() as connection:
            connection.execute("""UPDATE core_runs SET state='RUNNING',snapshot=%s,lease_owner='projection-test',
                lease_token=%s,lease_expires_at=EXTRACT(EPOCH FROM clock_timestamp())+600 WHERE run_id=%s""",
                (Jsonb(snapshot), token, source.run_id))
        source = self.agent.workflow_store.lookup_task(task["id"])
        binding = WorkspaceBinding(source.tenant_id, source.owner_id, source.context_id)
        service = self.agent.chat_file_service
        batch = service.prepare([{"name": "response.txt", "raw": b"independent incoming text", "media_type": "text/plain"}],
            tenant_id=self.tenant, actor_id="peer", message_id=uuid.uuid4().hex, request_digest="request", source="a2a")
        with self.app.state.database.transaction() as connection:
            service.bind(batch["batch_id"], binding, run_id=source.run_id, task_id=source.task_id,
                **{key: batch[key] for key in ("actor_id", "message_id", "request_digest", "lease_token")},
                connection=connection)
        if publish:
            service.record_decision(batch["batch_id"], binding, decision_ref="trusted-test-allow", allow=True, lease_token=token)
            service.publish(batch["batch_id"], binding, lease_token=token)
        contract = {"version": 2, "tenant_id": self.tenant, "owner_id": source.run_id,
            "peer_id": "peer", "peer_revision": 1, "peer_name": "peer", "url": "https://peer.example/a2a",
            "binding": "HTTP+JSON", "message_id": uuid.uuid4().hex, "task": "Public request",
            "timeout_seconds": 60, "poll_interval_seconds": 5, "attachment_limit_bytes": 1024,
            "caller_scope": {key: getattr(source, key) for key in ("owner_id", "context_id", "task_id", "run_id")},
            "outgoing_files": []}
        with patch.object(self.agent.task_scheduler, "_launch"):
            remote = self.agent.task_scheduler.start_remote(contract, owner_id=source.run_id,
                tenant_id=self.tenant, task_id=uuid.uuid4().hex)
        checkpoint = {"version": 1, "send_started": True, "deadline": self.agent.workflow_store.current_time() + 60,
            "remote_task_id": "remote-task", "remote_context_id": "remote-context", "next_poll_at": None,
            "cancel_started": False}
        result = {"agent_name": "peer", "remote_state": "TASK_STATE_COMPLETED", "text": "Public reply",
                  "file_batch_id": batch["batch_id"]}
        with self.app.state.database.transaction() as connection:
            connection.execute("UPDATE core_background_tasks SET state='completed',checkpoint=%s,result=%s WHERE id=%s",
                (Jsonb(checkpoint), Jsonb(result), remote.id))
        return source, binding, batch, remote.id

    async def test_remote_result_metadata_is_scoped_without_original_get_before_review(self):
        source, binding, batch, operation = await self.incoming(publish=False)
        payload = {"tool_name": "core_agent_send_message", "status": "succeeded",
                   "output": {"task_id": operation, "state": "completed", "result": {}}}
        with patch.object(self.agent.workspace_blob_store, "get", side_effect=AssertionError("premature original GET")) as get:
            projected, batches = self.agent._remote_result_batches(source, payload)
            self.assertEqual([item[0] for item in batches], [batch["batch_id"]])
            self.assertEqual(projected["output"]["result"]["files"][0]["actual_name"], "response.txt")
            with self.assertRaises(CoreError):
                self.agent._remote_result_batches(replace(source, context_id="foreign"), payload)
            get.assert_not_called()
        self.assertFalse((self.agent.chat_file_service.workspaces.workspace(binding) / "a2a").exists())
        self.assertEqual(self.agent.chat_file_service.store.get(batch["batch_id"], self.tenant)["state"],
                         "accepted_quarantine")

    async def test_peer_history_after_cache_loss_keeps_text_alias_denial_without_get(self):
        source, binding, batch, operation = await self.incoming(publish=True)
        entry = batch["manifest"]["entries"][0]
        self.assertEqual(entry["text_digest"], _json_identity("independent incoming text")["material_digest"])
        await self.restart([])
        admission = self.agent.tool_runtime.environment_manager.validate_workspace_scope.__self__
        with patch.object(self.agent.workspace_blob_store, "get", side_effect=AssertionError("GET in peer projection")) as get:
            values, _ = read_conversations(self.agent, admission, binding, operation_id=operation)
            detail, = values
            self.assertEqual(detail["material_status"], "available")
            self.assertEqual(detail["files"][0]["sha256"], entry["sha256"])
            self.assertNotIn("text_digest", detail["files"][0])
            get.assert_not_called()
        test_owner_history.OwnerHistoryTests.review_material(self, source, _json_identity("independent incoming text"))
        with patch.object(self.agent.workspace_blob_store, "get", side_effect=AssertionError("GET in peer projection")) as get:
            values, _ = read_conversations(self.agent, admission, binding, operation_id=operation)
            detail, = values
            self.assertEqual(detail["material_status"], "rejected")
            self.assertEqual(detail["files"], [])
            get.assert_not_called()
