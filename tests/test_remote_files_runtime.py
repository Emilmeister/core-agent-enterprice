"""Explicit remote attachments through the authenticated, protected runtime."""
import asyncio
import copy
import json
import threading
import unittest
import uuid
from contextvars import copy_context
from types import SimpleNamespace
from unittest.mock import patch

from core_agent.errors import CoreError
from core_agent.execution import ExecutionResult
from core_agent.interactions import SETTINGS_KEYS
from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
from core_agent.python_exec import PythonContinuationStopped
from core_agent.remote_agents import RemoteAgentCard, RemoteAgentConnection, RemoteEvent
from core_agent.tools import ToolCall
from core_agent.workspace import WorkspaceBinding
from tests.test_auth import AuthAppTestCase, TEST_DATABASE_URL


class RemoteFilesRuntimeTests(AuthAppTestCase):
    durable_blobs = True

    async def asyncSetUp(self):
        with patch("core_agent.runtime.CoreAgent.recover_workflows", return_value=()):
            await super().asyncSetUp()
        self.agent = self.app.state.core_agent
        self.tenant = self.app.state.authenticator.settings.tenant
        self.app.state.remote_registry_store.create(self.tenant, {
            "name": "delivery", "url": "https://peer.example/a2a", "description": "Delivery",
            "enabled": True, "header_name": "Authorization",
        }, actor_id="alice")
        discovery = patch("core_agent.remote_agents.connect_peer", side_effect=lambda peer, **kwargs:
            RemoteAgentConnection(RemoteAgentCard(peer["name"], peer["description"], peer["url"], False, (), "HTTP+JSON")))
        discovery.start()
        self.addCleanup(discovery.stop)
        sender = patch.object(RemoteAgentConnection, "send_task", return_value=RemoteEvent(
            "message", None, "delivered", True, ({"text": "delivered"},)))
        self.sent = sender.start()
        self.addCleanup(sender.stop)
        original = self.agent.task_scheduler.start_remote

        def settled(*args, **kwargs):
            task = original(*args, **kwargs)
            self.agent.task_scheduler.wait(task.id, timeout=5,
                owner_id=kwargs["owner_id"], tenant_id=kwargs["tenant_id"])
            return task

        admission = patch.object(self.agent.task_scheduler, "start_remote", side_effect=settled)
        admission.start()
        self.addCleanup(admission.stop)
        self.context = "remote-files-" + uuid.uuid4().hex
        self.agent.model = ScriptedModel([ModelResponse(message="ready")])
        task = await self.submit("owner-a", uuid.uuid4().hex, self.context)
        record = await self.finished(task)
        self.binding = WorkspaceBinding(record.tenant_id, record.owner_id, record.context_id)
        self.folder = self.agent.tool_runtime.environment_manager.backend.chats.workspace(self.binding)
        (self.folder / "chosen.txt").write_bytes(b"chosen bytes")
        (self.folder / "empty.txt").write_bytes(b"")
        (self.folder / "extra.txt").write_bytes(b"must not send")
        (self.folder / "final.txt").write_bytes(b"final bytes")

    async def finished(self, task):
        async with asyncio.timeout(8):
            while True:
                record = self.agent.workflow_store.lookup_task(task["id"])
                if record.state in {"COMPLETED", "FAILED", "CANCELLED", "ABORTED"}:
                    break
                await asyncio.sleep(0.01)
        self.assertEqual(record.state, "COMPLETED", record.error_code)
        return record

    def script(self, *responses):
        model = ScriptedModel(responses)
        self.agent.model = model
        return model

    @staticmethod
    def send(files=None, call_id="send"):
        arguments = {"agent_name": "delivery", "task": "Deliver parcel"}
        if files is not None:
            arguments["files"] = files
        return ModelResponse(tool_requests=(ToolRequest(call_id, "core_agent_send_message", arguments),))

    @staticmethod
    def final_selection():
        return ModelResponse(tool_requests=(ToolRequest("final", "core_response_files", {"paths": ["final.txt"]}),))

    def limit(self, value):
        settings = self.agent.interaction_store.get_settings(self.tenant)
        values = {key: getattr(settings, key) for key in SETTINGS_KEYS}
        values["attachment_limit_bytes"] = value
        self.agent.interaction_store.update_settings(self.tenant, values, settings.revision)

    async def test_only_explicit_ordered_files_are_sent_and_final_selection_is_independent(self):
        self.script(self.final_selection(), self.send(["chosen.txt", "empty.txt"]), ModelResponse(message="done"))
        record = await self.finished(await self.submit("owner-a", uuid.uuid4().hex, self.context))
        self.assertEqual(self.sent.call_count, 1)
        wire = self.sent.call_args.kwargs
        self.assertEqual(wire["files"], (
            {"name": "chosen.txt", "media_type": "text/plain", "raw": b"chosen bytes"},
            {"name": "empty.txt", "media_type": "text/plain", "raw": b""},
        ))
        self.assertNotIn("context_id", wire)
        self.assertNotIn("task_id", wire)
        self.assertEqual([ref["name"] for ref in record.result["outgoing_files"]], ["final.txt"])
        contract = next(iter(record.snapshot["remote_calls"].values()))["contract"]
        self.assertEqual(contract["version"], 2)
        self.assertEqual(contract["caller_scope"], {key: getattr(record, key) for key in (
            "owner_id", "context_id", "task_id", "run_id")})
        self.assertEqual(contract["owner_id"], record.run_id)

    async def test_missing_and_empty_selection_do_not_inherit_workspace_or_final_files(self):
        self.script(self.final_selection(), self.send(), self.send([], "send-empty"), ModelResponse(message="done"))
        record = await self.finished(await self.submit("owner-a", uuid.uuid4().hex, self.context))
        self.assertEqual(self.sent.call_count, 2)
        self.assertTrue(all(call.kwargs["files"] == () for call in self.sent.call_args_list))
        self.assertEqual([ref["name"] for ref in record.result["outgoing_files"]], ["final.txt"])
        self.assertTrue(all(entry["contract"]["outgoing_files"] == [] for entry in record.snapshot["remote_calls"].values()))

    async def test_bad_last_file_or_aggregate_limit_creates_no_handle_and_preserves_final_selection(self):
        cases = ((["chosen.txt", "missing.txt"], 25_000_000, "FILE_NOT_FOUND"),
                 (["chosen.txt", "extra.txt"], 15, "ATTACHMENTS_TOO_LARGE"))
        for files, ceiling, code in cases:
            with self.subTest(code=code):
                self.limit(ceiling)
                model = self.script(self.final_selection(), self.send(files), ModelResponse(message="continue"))
                record = await self.finished(await self.submit("owner-a", uuid.uuid4().hex, self.context))
                self.assertIn(code, str(model.calls[-1].messages))
                self.assertEqual(self.agent.task_scheduler.list(owner_id=record.run_id, tenant_id=self.tenant), ())
                self.assertEqual([ref["name"] for ref in record.result["outgoing_files"]], ["final.txt"])
                self.assertEqual((self.folder / "chosen.txt").read_bytes(), b"chosen bytes")
        self.assertEqual(self.sent.call_count, 0)

    async def test_hitl_approves_frozen_public_receipts_and_sends_original_bytes_after_restoration(self):
        policy = self.agent.interaction_store.get_policy(self.tenant, "core_agent_send_message", "builtin:core_agent_send_message")
        self.agent.interaction_store.update_policy(self.tenant, "core_agent_send_message", policy.origin,
            mode="require_hitl", guardrails_exempt=False, expected_revision=policy.revision, actor_id="alice")
        model = self.script(self.send(["chosen.txt", "empty.txt"]), ModelResponse(message="done"))
        task = await self.submit("owner-a", uuid.uuid4().hex, self.context)
        interactions = await self.http.get("/api/interactions", headers=self.headers("owner-a"), params={"task_id": task["id"]})
        approval = interactions.json()["interactions"][0]
        public = approval["subject"]["remote_binding"]
        self.assertEqual([receipt["name"] for receipt in public["files"]], ["chosen.txt", "empty.txt"])
        self.assertEqual(len(public["selection_digest"]), 64)
        self.assertNotIn("blob_id", json.dumps(approval))
        self.assertNotIn("caller_scope", public)
        self.assertEqual(self.sent.call_count, 0)
        (self.folder / "chosen.txt").write_bytes(b"changed")
        (self.folder / "empty.txt").unlink()
        self.limit(1)
        response = await self.http.post(f"/api/hitl/{approval['wait_id']}/decision", headers=self.headers("owner-b"),
            json={"decision": "allow", "subject_digest": approval["subject_digest"]})
        self.assertEqual(response.status_code, 200, response.text)
        # Force snapshot restoration instead of relying on the original runtime cache.
        self.agent._drop_run_runtime(self.agent.workflow_store.lookup_task(task["id"]).run_id)
        await asyncio.to_thread(self.agent.resume_task, task["id"])
        await self.finished(task)
        self.assertEqual(self.sent.call_count, 1)
        self.assertEqual([file["raw"] for file in self.sent.call_args.kwargs["files"]], [b"chosen bytes", b""])
        self.assertNotIn("blob_id", str(model.calls[-1].messages))

    async def test_persisted_source_scope_and_changed_call_arguments_fail_closed(self):
        self.script(self.send(["chosen.txt"]), ModelResponse(message="done"))
        record = await self.finished(await self.submit("owner-a", uuid.uuid4().hex, self.context))
        altered = copy.deepcopy(record)
        next(iter(altered.snapshot["remote_calls"].values()))["contract"]["caller_scope"]["task_id"] = "other-task"
        with self.assertRaises(CoreError) as error:
            self.agent._validate_remote_snapshot(altered)
        self.assertEqual(error.exception.code, "CHECKPOINT_INVALID")
        snapshot = copy.deepcopy(record.snapshot)
        snapshot["tool_calls"] = 1
        with self.assertRaises(CoreError) as error:
            self.agent._remote_binding(snapshot, ToolCall("send", "core_agent_send_message", {
                "agent_name": "delivery", "task": "Deliver parcel", "files": ["extra.txt"]}))
        self.assertEqual(error.exception.code, "CHECKPOINT_INVALID")

    async def test_nested_python_selects_the_same_files_without_replaying_prefix(self):
        prefix, remainder, stopped = [], [], []

        def execute(manager, **kwargs):
            prefix.append("prefix")
            result = ExecutionResult(-9, "prefix output", "", (), (), status="failed", cleanup="sandbox_terminated")
            session = SimpleNamespace(cancel=lambda _: stopped.append("stopped"), wait=lambda *_: result)
            kwargs["on_start"](session, SimpleNamespace(id="process"))

            def broker():
                try:
                    kwargs["dispatch"]("core_agent_send_message", {
                        "agent_name": "delivery", "task": "Deliver parcel", "files": ["chosen.txt", "empty.txt"],
                    }, "stable-request")
                    remainder.append("remainder")
                except PythonContinuationStopped:
                    pass

            thread = threading.Thread(target=copy_context().run, args=(broker,))
            thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            return result

        self.script(ModelResponse(tool_requests=(ToolRequest("python", "core_python_exec", {"code": "pass"}),)),
            ModelResponse(message="done"))
        with patch("core_agent.runtime.execute_python", side_effect=execute):
            record = await self.finished(await self.submit("owner-a", uuid.uuid4().hex, self.context))
        self.assertEqual((prefix, remainder, stopped), (["prefix"], [], ["stopped"]))
        self.assertEqual(self.sent.call_count, 1)
        self.assertEqual([file["raw"] for file in self.sent.call_args.kwargs["files"]], [b"chosen bytes", b""])
        self.assertEqual(len(self.agent.task_scheduler.list(owner_id=record.run_id, tenant_id=self.tenant)), 1)

    async def test_denied_send_is_hidden_and_never_prepares_files(self):
        policy = self.agent.interaction_store.get_policy(self.tenant, "core_agent_send_message", "builtin:core_agent_send_message")
        self.agent.interaction_store.update_policy(self.tenant, "core_agent_send_message", policy.origin,
            mode="deny", guardrails_exempt=False, expected_revision=policy.revision, actor_id="alice")
        model = self.script(self.send(["chosen.txt"]), ModelResponse(message="denied"))
        with patch.object(self.agent.response_files_service, "prepare", side_effect=AssertionError("Denied files were read")):
            await self.finished(await self.submit("owner-a", uuid.uuid4().hex, self.context))
        self.assertNotIn("core_agent_send_message", model.calls[0].tools)
        self.assertIn("POLICY_DENIED", str(model.calls[-1].messages))
        self.assertEqual(self.sent.call_count, 0)


@unittest.skipUnless(TEST_DATABASE_URL, "Real PostgreSQL fixture is required")
class PostgresRemoteFilesRuntimeTests(RemoteFilesRuntimeTests):
    use_postgres = True
