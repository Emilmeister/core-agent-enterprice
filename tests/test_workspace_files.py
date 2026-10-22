"""Read-only workspace APIs use canonical ownership and real descriptor traversal."""
import asyncio
import hashlib
import importlib.util
import json
import os
import tempfile
import threading
import unittest
import uuid
from dataclasses import replace
from types import SimpleNamespace
from contextlib import nullcontext
from urllib.parse import quote
from unittest.mock import patch

from starlette.requests import ClientDisconnect

from core_agent.errors import CoreError
from core_agent.chat_files import ChatFileService, MemoryChatFileStore, _manifest, _rename
from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
from core_agent.owner_api import WorkspaceDownload
from core_agent.workspace import ChatWorkspaces, WorkspaceBinding
from core_agent.remote_agents import _task_event
from core_agent.workflow import InMemoryWorkflowStore, WorkflowRecord
from tests.test_auth import AuthAppTestCase, TEST_DATABASE_URL
from tests import test_admission as admission_tests


class WorkspaceFileAPITests(AuthAppTestCase):
    @property
    def manager(self):
        return self.app.state.core_agent.tool_runtime.environment_manager.backend.chats

    def binding(self, task):
        record = self.app.state.core_agent.workflow_store.lookup_task(task["id"])
        return WorkspaceBinding(record.tenant_id, record.owner_id, record.context_id)

    async def files(self, context="files", *, token="owner-b", content=False, **params):
        suffix = "/files/content" if content else "/files"
        return await self.http.get("/api/chats/" + quote(context, safe="") + suffix,
                                   headers=self.headers(token), params=params)

    async def test_known_empty_workspace_list_does_not_create_folder_or_execute(self):
        task = await self.submit("external-a", "root", "files")
        before = sorted(str(path) for path in self.manager.root.rglob("*"))
        record = self.app.state.core_agent.workflow_store.lookup_task(task["id"])
        calls = len(self.model.calls)
        response = await self.files()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        payload = response.json()
        self.assertEqual(set(payload), {"files", "next_cursor", "listed_at", "active", "cleanup_block_reason", "workspace_revision"})
        self.assertEqual(payload["workspace_revision"], 0)
        self.assertEqual(payload["files"], [])
        self.assertIsNone(payload["next_cursor"])
        self.assertFalse(payload["active"])
        self.assertIsNone(payload["cleanup_block_reason"])
        self.assertEqual(sorted(str(path) for path in self.manager.root.rglob("*")), before)
        self.assertEqual(self.app.state.core_agent.workflow_store.lookup_task(task["id"]), record)
        self.assertEqual(len(self.model.calls), calls)

    async def test_owner_reads_restore_workspace_only_after_authorization(self):
        task = await self.submit("external-a", "root", "files")
        binding = self.binding(task)
        backend = self.app.state.core_agent.tool_runtime.environment_manager.backend
        restored = []

        def restore(scope):
            self.assertEqual(scope, binding)
            restored.append(scope)
            (self.manager.workspace(scope) / "restored.txt").write_bytes(b"durable file")

        with patch.object(backend, "workspace_durability", SimpleNamespace(ensure_restored=restore), create=True):
            self.assertEqual((await self.files(token="external-a")).status_code, 403)
            self.assertEqual((await self.files("missing")).status_code, 404)
            self.assertEqual(restored, [])
            listed = await self.files()
            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertEqual([item["path"] for item in listed.json()["files"]], ["restored.txt"])
            downloaded = await self.files(content=True, path="restored.txt")
            self.assertEqual(downloaded.status_code, 200, downloaded.text)
            self.assertEqual(downloaded.content, b"durable file")
        self.assertEqual(restored, [binding, binding])

    async def test_recursive_files_age_boundary_paging_and_safe_download(self):
        task = await self.submit("external-a", "root", "files")
        workspace = self.manager.workspace(self.binding(task))
        (workspace / "a").mkdir()
        (workspace / "a" / "résumé.txt").write_bytes(b"nested contents")
        (workspace / "a.txt").write_bytes(b"flat")
        now = 2_000_000_000_000_000_000
        os.utime(workspace / "a.txt", ns=(now-86400*10**9, now-86400*10**9))
        os.utime(workspace / "a" / "résumé.txt", ns=(now-86400*10**9-1, now-86400*10**9-1))
        with patch("core_agent.workspace.time.time_ns", return_value=now):
            first = await self.files(limit="1")
            self.assertEqual(first.status_code, 200, first.text)
            item = first.json()["files"][0]
            self.assertEqual(item["path"], "a.txt")
            self.assertEqual(set(item), {"name", "path", "size", "mtime_ns", "identity_token"})
            self.assertEqual(item["size"], 4)
            second = await self.files(limit="1", cursor=first.json()["next_cursor"])
            self.assertEqual(second.json()["files"][0]["path"], "a/résumé.txt")
            self.assertEqual(second.json()["listed_at"], first.json()["listed_at"])
            self.assertIsNone(second.json()["next_cursor"])
            old = await self.files(older_than_days="1")
            self.assertEqual([item["path"] for item in old.json()["files"]], ["a/résumé.txt"])
        downloaded = await self.files(content=True, path="a/résumé.txt")
        self.assertEqual(downloaded.status_code, 200, downloaded.text)
        self.assertEqual(downloaded.content, b"nested contents")
        self.assertEqual(downloaded.headers["content-type"], "application/octet-stream")
        self.assertIn("filename*=UTF-8''r%C3%A9sum%C3%A9.txt", downloaded.headers["content-disposition"])
        self.assertEqual(downloaded.headers["x-content-type-options"], "nosniff")
        self.assertIn("sandbox", downloaded.headers["content-security-policy"])

    async def test_scope_and_invalid_queries_are_rejected(self):
        task = await self.submit("external-a", "root", "files")
        (self.manager.workspace(self.binding(task)) / "private.txt").write_text("private")
        self.tokens["dual"] = {**self.tokens["owner-a"], "realm_access": {"roles": ["agent-owner", "agent-external"]}}
        for token in ("external-a", "external-b", "dual"):
            self.assertEqual((await self.files(token=token)).status_code, 403)
            self.assertEqual((await self.files(token=token, content=True, path="private.txt")).status_code, 403)
        self.assertEqual((await self.files("missing")).status_code, 404)
        for values in ({"limit": "0"}, {"limit": "101"}, {"older_than_days": "-1"}, {"older_than_days": "nan"},
                       {"older_than_days": "1e2"}, {"older_than_days": "100001"}, {"tenant": "other"}, {"cursor": "bad"},
                       {"directory": "../"}):
            self.assertEqual((await self.files(**values)).status_code, 400, values)
        for path in ("", "../private.txt", "/etc/passwd", "a//b", "a/./b", "a\\b", "\0", "a\nb"):
            self.assertEqual((await self.files(content=True, path=path)).status_code, 400, repr(path))
        auth = self.app.state.authenticator
        with patch.object(auth, "settings", replace(auth.settings, tenant="other")):
            self.assertEqual((await self.files()).status_code, 404)

    async def test_links_special_files_and_service_controls_are_not_exposed(self):
        task = await self.submit("external-a", "root", "files")
        workspace = self.manager.workspace(self.binding(task))
        (workspace / "plain").write_bytes(b"allowed")
        (workspace / ".manifest.json").write_text("user content")
        identifier = uuid.uuid4()
        batches = [workspace / "attachments" / value for value in (identifier.hex, str(identifier))]
        for batch in batches:
            batch.mkdir(parents=True)
            for name in (".manifest.json", ".manifest-temporary", "published.txt"):
                (batch / name).write_text(name)
        private = self.manager.root / "private"
        private.mkdir(exist_ok=True)
        (private / "secret").write_text("private quarantine")
        (workspace / "link").symlink_to(private / "secret")
        (workspace / "dirlink").symlink_to(private, target_is_directory=True)
        os.link(private / "secret", workspace / "hardlink")
        os.mkfifo(workspace / "fifo")
        listed = await self.files()
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual([item["path"] for item in listed.json()["files"]],
                         sorted([".manifest.json", *(f"attachments/{batch.name}/published.txt" for batch in batches), "plain"]))
        controls = [f"attachments/{batch.name}/{name}" for batch in batches
                    for name in (".manifest.json", ".manifest-temporary")]
        for path in ("link", "dirlink/secret", "hardlink", "fifo", *controls, "missing"):
            response = await self.files(content=True, path=path)
            self.assertEqual(response.status_code, 404, (path, response.text))
        for path in controls:
            with self.assertRaises(CoreError) as error:
                self.manager.open_file(self.binding(task), path)
            self.assertEqual(error.exception.code, "FILE_NOT_FOUND")
        self.assertEqual((await self.files(content=True, path=".manifest.json")).content, b"user content")

    async def test_real_published_hex_batch_manifest_is_private_to_all_workspace_readers(self):
        response = await self.http.post("/a2a/external/message:send", headers=self.headers("external-a"), json={
            "message": {"messageId": "published-file", "contextId": "files", "role": "ROLE_USER",
                        "parts": [{"raw": "aGVsbG8=", "filename": "incoming.txt", "mediaType": "text/plain"}]}})
        self.assertEqual(response.status_code, 200, response.text)
        task = response.json()["task"]
        binding = self.binding(task)
        receipt = task["metadata"]["file_receipt"]
        batch_id = receipt["batch_id"]
        self.assertEqual(uuid.UUID(batch_id).hex, batch_id)
        service = self.app.state.core_agent.chat_file_service
        self.assertEqual(service.store.get(batch_id, binding.tenant_id)["state"], "published")
        control = f"a2a/{batch_id}/.manifest.json"
        self.assertTrue((self.manager.workspace(binding) / control).is_file())
        public = receipt["entries"][0]["relative_path"]
        listed = await self.files()
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual([item["path"] for item in listed.json()["files"]], [public])
        self.assertEqual([item["path"] for item in self.manager.preview(binding)["files"]], [public])
        hidden = await self.files(content=True, path=control)
        self.assertEqual(hidden.status_code, 404, hidden.text)
        with self.assertRaises(CoreError) as error:
            self.manager.open_file(binding, control)
        self.assertEqual(error.exception.code, "FILE_NOT_FOUND")
        self.assertEqual((await self.files(content=True, path=public)).content, b"hello")

    async def test_a2a_control_manifest_is_hidden_but_public_artifact_manifest_is_downloadable(self):
        task = await self.submit("external-a", "root", "files")
        folder = self.manager.workspace(self.binding(task)) / "a2a" / "local-operation"
        folder.mkdir(parents=True)
        for name in (".manifest.json", ".manifest-temporary", "manifest.json", "artifact-001-part-001.json"):
            (folder / name).write_text(name)
        listed = await self.files()
        self.assertEqual([item["path"] for item in listed.json()["files"]], [
            "a2a/local-operation/artifact-001-part-001.json", "a2a/local-operation/manifest.json"])
        for name in (".manifest.json", ".manifest-temporary"):
            self.assertEqual((await self.files(content=True, path="a2a/local-operation/" + name)).status_code, 404)
        self.assertEqual((await self.files(content=True, path="a2a/local-operation/manifest.json")).content, b"manifest.json")

    async def test_remote_json_artifact_uses_real_canonical_store_and_owner_download(self):
        from core_agent.remote_artifacts import prepare_remote_artifacts
        agent = self.app.state.core_agent
        context = admission_tests.AuthAdmissionTests.context(self)
        admitted = await self.app.state.a2a_request_handler.admission_handler(
            admission_tests.AuthAdmissionTests.sdk_message("artifact-root", "files"), context)
        binding = self.binding({"id": admitted.task.id})
        service = agent.chat_file_service
        operation_id = uuid.uuid4().hex
        event = _task_event({"task": {"id": "remote", "contextId": "remote-context",
            "status": {"state": "TASK_STATE_COMPLETED"}, "artifacts": [
                {"artifactId": "ordered", "name": "JSON report", "parts": [{"data": {"value": "artifact-canary"}}]}]}})
        stage = prepare_remote_artifacts(service, event, tenant_id=binding.tenant_id, actor_id=binding.owner_id,
            message_id=operation_id, request_digest="artifact-digest", operation_id=operation_id, limit_bytes=10000)
        database = getattr(service.store, "database", None)
        with database.transaction() if database is not None else nullcontext() as connection:
            service.bind(stage["batch_id"], binding, task_id=admitted.task.id, run_id=admitted.run_id,
                actor_id=binding.owner_id, message_id=operation_id, request_digest="artifact-digest",
                lease_token=stage["lease_token"], connection=connection)
        material = service.review_material(stage["batch_id"], binding, task_id=admitted.task.id, run_id=admitted.run_id)
        self.assertIn("artifact-canary", repr(material["documents"]))
        service.record_decision(stage["batch_id"], binding, decision_ref="test-whole-batch-allow", allow=True,
                                lease_token=admitted.lease_token)
        manifest = service.publish(stage["batch_id"], binding, lease_token=admitted.lease_token)
        path = f"a2a/{operation_id}/artifact-001-part-001.json"
        self.assertEqual(manifest["artifacts"][0]["parts"][0]["relative_path"], path)
        self.assertEqual(json.loads((await self.files(content=True, path=path)).content), {"value": "artifact-canary"})
        self.assertNotIn("artifact-canary", repr(manifest))
        self.assertEqual(self.model.calls, ())

    async def test_directory_limit_cursor_binding_restart_and_file_identity(self):
        task = await self.submit("external-a", "root", "files")
        workspace = self.manager.workspace(self.binding(task))
        (workspace / "folder").mkdir()
        for path in ("a", "b", "folder/c", "folder/d"):
            (workspace / path).write_text(path)
        first = (await self.files(limit="1")).json()
        cursor = first["next_cursor"]
        with patch("core_agent.workspace.time.time_ns", return_value=1):
            second = await self.files(limit="1", cursor=cursor)
        self.assertEqual(second.json()["listed_at"], first["listed_at"])
        self.assertEqual(second.json()["files"][0]["path"], "b")
        for params in ({"directory": "folder"}, {"older_than_days": "1"}):
            self.assertEqual((await self.files(cursor=cursor, **params)).status_code, 400)
        self.assertEqual((await self.files(cursor=cursor[:-2] + "xx")).status_code, 400)
        await self.submit("external-a", "another", "other")
        self.assertEqual((await self.files("other", cursor=cursor)).status_code, 400)
        with patch.object(self.manager, "_cursor_key", ChatWorkspaces(self.manager.root)._cursor_key):
            self.assertEqual((await self.files(cursor=cursor)).status_code, 400)
        self.assertEqual((await self.files(directory="missing")).status_code, 404)
        with patch.object(self.manager, "scan_limit", 2):
            exceeded = await self.files(limit="1")
            self.assertEqual(exceeded.status_code, 409, exceeded.text)
            self.assertIn("WORKSPACE_SCAN_LIMIT", exceeded.text)
            narrowed = await self.files(directory="folder")
            self.assertEqual(narrowed.status_code, 200, narrowed.text)
            self.assertEqual([item["path"] for item in narrowed.json()["files"]], ["folder/c", "folder/d"])
        (workspace / "a").write_text("changed contents")
        changed = (await self.files(limit="1")).json()["files"][0]
        self.assertNotEqual(changed["identity_token"], first["files"][0]["identity_token"])
        with patch.object(self.manager, "depth_limit", 0):
            self.assertEqual((await self.files()).status_code, 409)

    async def test_scan_vanished_entry_is_explicit_conflict_even_with_directory(self):
        task = await self.submit("external-a", "root", "files")
        workspace = self.manager.workspace(self.binding(task))
        (workspace / "folder").mkdir()
        (workspace / "folder" / "vanished").write_text("race")
        original = os.stat
        def stat_entry(path, *args, **kwargs):
            if path == "vanished" and kwargs.get("dir_fd") is not None:
                raise FileNotFoundError()
            return original(path, *args, **kwargs)
        with patch("core_agent.workspace.os.stat", side_effect=stat_entry):
            response = await self.files(directory="folder")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("WORKSPACE_UNAVAILABLE", response.text)

    async def test_download_vanished_between_stat_and_open_is_conflict(self):
        task = await self.submit("external-a", "root", "files")
        (self.manager.workspace(self.binding(task)) / "vanished").write_text("race")
        original = os.open
        def open_entry(path, *args, **kwargs):
            if path == "vanished" and kwargs.get("dir_fd") is not None:
                raise FileNotFoundError()
            return original(path, *args, **kwargs)
        with patch("core_agent.workspace.os.open", side_effect=open_entry):
            response = await self.files(content=True, path="vanished")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("WORKSPACE_UNAVAILABLE", response.text)

    async def test_age_decimal_precision_preserves_strict_boundary(self):
        task = await self.submit("external-a", "root", "files")
        workspace = self.manager.workspace(self.binding(task))
        (workspace / "one-day").write_text("exact boundary")
        now = 2_000_000_000_000_000_000
        os.utime(workspace / "one-day", ns=(now-86400*10**9, now-86400*10**9))
        with patch("core_agent.workspace.time.time_ns", return_value=now):
            response = await self.files(older_than_days="0.999999999999999999999999999999999999999999999999")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([item["path"] for item in response.json()["files"]], ["one-day"])

    async def test_waiting_root_and_slash_context_reads_leave_workflow_unchanged(self):
        agent = self.app.state.core_agent
        agent.model = ScriptedModel([ModelResponse(tool_requests=(
            ToolRequest("ask", "core_ask_owner", {"question": "Confirm?"}),))])
        context = "company/отчёты/" + "x" * 400
        task = await self.submit("owner-a", "root", context)
        binding = self.binding(task)
        (self.manager.workspace(binding) / "report").write_text("ready")
        before = agent.workflow_store.lookup_task(task["id"])
        calls = len(agent.model.calls)
        response = await self.files(context)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["active"])
        self.assertEqual(response.json()["cleanup_block_reason"], "CONTEXT_BUSY")
        self.assertEqual((await self.files(context, content=True, path="report")).content, b"ready")
        self.assertEqual(agent.workflow_store.lookup_task(task["id"]), before)
        self.assertEqual(len(agent.model.calls), calls)

    async def test_download_holds_open_inode_bounds_growth_and_closes_on_disconnect(self):
        task = await self.submit("external-a", "root", "files")
        binding = self.binding(task)
        workspace = self.manager.workspace(binding)
        file = workspace / "report"
        file.write_bytes(b"original")
        stream, size = self.manager.open_file(binding, "report")
        with file.open("ab") as writer:
            writer.write(b" appended")
        file.rename(workspace / "previous")
        file.write_bytes(b"replacement")
        response = WorkspaceDownload(stream, size, "report")
        frames = []
        async def receive():
            return {"type": "http.disconnect"}
        async def send(frame):
            frames.append(frame)
        scope = {"type": "http", "asgi": {"spec_version": "2.4"}}
        await response(scope, receive, send)
        self.assertEqual(b"".join(frame.get("body", b"") for frame in frames), b"original")
        self.assertTrue(stream.closed)
        self.assertNotIn("content-length", response.headers)
        stream, size = self.manager.open_file(binding, "report")
        async def disconnected(frame):
            raise OSError("client disconnected")
        with self.assertRaises(ClientDisconnect):
            await WorkspaceDownload(stream, size, "report")(scope, receive, disconnected)
        self.assertTrue(stream.closed)

    async def test_cancel_during_file_open_closes_late_descriptor(self):
        task = await self.submit("external-a", "root", "files")
        binding = self.binding(task)
        (self.manager.workspace(binding) / "report").write_bytes(b"original")
        original = self.manager.open_file
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        opened = []
        def delayed(*args):
            stream, size = original(*args)
            opened.append(stream)
            entered.set()
            release.wait(2)
            finished.set()
            return stream, size
        with patch.object(self.manager, "open_file", side_effect=delayed):
            request = asyncio.create_task(self.files(content=True, path="report"))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                request.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await request
                release.set()
                self.assertTrue(await asyncio.to_thread(finished.wait, 2))
                for _ in range(20):
                    if opened[0].closed:
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(opened[0].closed)
            finally:
                release.set()
                for stream in opened:
                    stream.close()

    async def test_workspace_root_symlink_does_not_escape_and_new_root_keeps_binding(self):
        task = await self.submit("external-a", "root", "files")
        workspace = self.manager.workspace(self.binding(task))
        (workspace / "report").write_text("original owner")
        await self.submit("owner-b", "next", "files")
        self.assertEqual((await self.files(content=True, path="report")).content, b"original owner")
        workspace.rename(workspace.with_name("original-workspace"))
        workspace.symlink_to(workspace.with_name("original-workspace"), target_is_directory=True)
        self.assertEqual((await self.files()).status_code, 409)
        self.assertEqual((await self.files(content=True, path="report")).status_code, 409)


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL not set")
class PostgresWorkspaceFileAPITests(WorkspaceFileAPITests):
    use_postgres = True


class A2AWorkspaceBatchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspaces = ChatWorkspaces(directory.name)
        self.binding = WorkspaceBinding("tenant", "owner", "chat")
        self.workflow = InMemoryWorkflowStore()
        self.record = self.workflow.create(WorkflowRecord("run", "task", "chat", "tenant", "owner",
            None, "RUNNING", 1, {"prompt": "Read files"}, {}))
        self.store = MemoryChatFileStore(self.workflow, lambda binding: None)
        self.service = ChatFileService(self.store, self.workspaces, clock=lambda: 1000)
        self.addCleanup(self.service.close)
        self.options = dict(tenant_id="tenant", actor_id="owner", message_id="local-operation",
                            request_digest="digest", source="a2a")

    def bind(self, batch):
        return self.service.bind(batch["batch_id"], self.binding, task_id="task", run_id="run",
            actor_id="owner", message_id="local-operation", request_digest="digest", lease_token=batch["lease_token"])

    def accept(self, batch):
        self.bind(batch)
        self.service.record_decision(batch["batch_id"], self.binding, decision_ref="allow", allow=True)
        return self.service.publish(batch["batch_id"], self.binding)

    def materialize(self, event, **changes):
        self.assertIsNotNone(importlib.util.find_spec("core_agent.remote_artifacts"), "artifact normalization helper missing")
        from core_agent.remote_artifacts import prepare_remote_artifacts
        return prepare_remote_artifacts(self.service, event, tenant_id="tenant", actor_id="owner",
            message_id="local-operation", request_digest="digest", operation_id="local-operation", limit_bytes=100_000, **changes)

    def test_new_a2a_input_uses_batch_folder_while_ui_and_legacy_batches_keep_attachments(self):
        a2a = self.service.prepare([{"raw": b"incoming", "name": "same.txt"}], **self.options)
        manifest = self.accept(a2a)
        self.assertEqual(manifest["entries"][0]["relative_path"], f"a2a/{a2a['batch_id']}/same.txt")
        ui = self.service.prepare([{"raw": b"UI", "name": "same.txt"}], **{**self.options, "source": "ui"})
        ui_manifest = self.accept(ui)
        self.assertTrue(ui_manifest["entries"][0]["relative_path"].startswith("attachments/"))
        restarted = ChatFileService(self.store, self.workspaces)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.publish(a2a["batch_id"], self.binding), manifest)

    def test_mixed_artifacts_materialize_as_sealed_files_and_safe_ordered_manifest(self):
        event = _task_event({"task": {"id": "remote-id", "contextId": "remote-chat",
            "status": {"state": "TASK_STATE_COMPLETED", "message": {"messageId": "status", "role": "ROLE_AGENT",
                "parts": [{"text": "Done"}, {"data": {"message-data": "message-canary"}}]}}, "artifacts": [
            {"artifactId": "../remote-artifact", "name": "Report", "parts": [
                {"text": "artifact-canary", "mediaType": "text/markdown"}, {"data": {"value": "json-canary"}},
                {"raw": "", "filename": "../same.txt", "mediaType": "text/plain"},
                {"text": "hidden-canary", "metadata": {"private": True}}]},
            {"artifactId": "second", "parts": [{"text": "second text"}]}]}})
        batch = self.materialize(event)
        self.assertFalse((self.workspaces.workspace(self.binding) / "a2a/local-operation").exists())
        self.bind(batch)
        with self.assertRaises(CoreError):
            self.service.publish(batch["batch_id"], self.binding)
        review = self.service.review_material(batch["batch_id"], self.binding, run_id="run", task_id="task")
        self.assertIn("artifact-canary", repr(review["documents"]))
        self.assertIn("json-canary", repr(review["documents"]))
        self.assertNotIn("hidden-canary", repr(review))
        self.service.record_decision(batch["batch_id"], self.binding, decision_ref="allow", allow=True)
        manifest = self.service.publish(batch["batch_id"], self.binding)
        folder = self.workspaces.workspace(self.binding) / "a2a/local-operation"
        public = json.loads((folder / "manifest.json").read_bytes())
        self.assertEqual([item["artifact_id"] for item in public["artifacts"]], ["../remote-artifact", "second"])
        self.assertEqual([part["type"] for part in public["artifacts"][0]["parts"]], ["text", "data", "file"])
        self.assertEqual(public["messages"][0]["message_id"], "status")
        self.assertNotIn("artifact-canary", repr(public))
        self.assertNotIn("json-canary", repr(manifest))
        for entry in manifest["entries"]:
            content = (self.workspaces.workspace(self.binding) / entry["relative_path"]).read_bytes()
            self.assertEqual(len(content), entry["size_bytes"])
            self.assertEqual(hashlib.sha256(content).hexdigest(), entry["sha256"])
            self.assertTrue(entry["relative_path"].startswith("a2a/local-operation/"))
        self.assertEqual(manifest["total_bytes"], sum(entry["size_bytes"] for entry in manifest["entries"]))
        self.assertEqual((folder / "artifact-001-part-001.md").read_bytes(), b"artifact-canary")
        self.assertEqual(json.loads((folder / "artifact-001-part-002.json").read_bytes()), {"value": "json-canary"})
        self.assertEqual((folder / "artifact-001-part-003.txt").read_bytes(), b"")
        for entry in manifest["entries"]:
            stream, _ = self.workspaces.open_file(self.binding, entry["relative_path"])
            stream.close()
        (folder / "artifact-001-part-002.json").unlink()
        with self.assertRaises(CoreError) as error:
            self.service.publish(batch["batch_id"], self.binding)
        self.assertEqual(error.exception.code, "ARTIFACT_INTEGRITY_FAILED")
        self.assertFalse((folder / "artifact-001-part-002.json").exists())

    def test_immediate_data_message_is_not_given_an_artifact_id_and_limit_counts_manifest(self):
        event = _task_event({"message": {"messageId": "direct", "role": "ROLE_AGENT", "parts": [
            {"text": "Done"}, {"data": {"only": "json"}}]}})
        batch = self.materialize(event)
        manifest = self.accept(batch)
        self.assertEqual(manifest["artifacts"], [])
        self.assertEqual(manifest["messages"][0]["message_id"], "direct")
        self.assertNotIn("artifact_id", repr(manifest["messages"]))
        self.assertGreater(manifest["total_bytes"], len(b'{"only":"json"}'))
        from core_agent.remote_artifacts import prepare_remote_artifacts
        with self.assertRaises(CoreError) as error:
            prepare_remote_artifacts(self.service, event, tenant_id="tenant", actor_id="owner", message_id="another",
                request_digest="digest", operation_id="local-operation", limit_bytes=manifest["total_bytes"] - 1)
        self.assertEqual(error.exception.code, "ATTACHMENTS_TOO_LARGE")

    def test_destination_traversal_rejected_before_creating_batch(self):
        with self.assertRaises(CoreError) as error:
            self.service.prepare([{"raw": b"x"}], **self.options, destination_id="../escape")
        self.assertEqual(error.exception.code, "INVALID_FILE_INPUT")
        self.assertEqual(self.store.rows, {})

    def test_a2a_rename_crash_recovers_single_target_and_later_denial_retracts_it(self):
        batch = self.service.prepare([{"raw": b"accepted", "name": "result.txt"}],
            **self.options, destination_id="local-operation")
        self.bind(batch)
        self.service.record_decision(batch["batch_id"], self.binding, decision_ref="allow", allow=True)
        class Crash(BaseException):
            pass
        def rename_then_crash(*args):
            _rename(*args)
            raise Crash()
        with patch("core_agent.chat_files._rename", side_effect=rename_then_crash), self.assertRaises(Crash):
            self.service.publish(batch["batch_id"], self.binding)
        target = self.workspaces.workspace(self.binding) / "a2a/local-operation"
        self.assertEqual((target / "result.txt").read_bytes(), b"accepted")
        self.assertEqual(self.store.get(batch["batch_id"], "tenant")["state"], "accepted_ready")
        self.service.record_decision(batch["batch_id"], self.binding, decision_ref="deny", allow=False)
        self.assertFalse(target.exists())
        self.assertEqual(self.store.get(batch["batch_id"], "tenant")["state"], "excluded")

    def test_a2a_rename_crash_publication_retry_uses_existing_target_without_copy(self):
        batch = self.service.prepare([{"raw": b"accepted", "name": "result.txt"}],
            **self.options, destination_id="local-operation")
        self.bind(batch)
        self.service.record_decision(batch["batch_id"], self.binding, decision_ref="allow", allow=True)
        class Crash(BaseException):
            pass
        def rename_then_crash(*args):
            _rename(*args)
            raise Crash()
        with patch("core_agent.chat_files._rename", side_effect=rename_then_crash), self.assertRaises(Crash):
            self.service.publish(batch["batch_id"], self.binding)
        target = self.workspaces.workspace(self.binding) / "a2a/local-operation"
        identity = (target / "result.txt").stat().st_ino
        restarted = ChatFileService(self.store, self.workspaces)
        self.addCleanup(restarted.close)
        with patch("core_agent.chat_files._rename", side_effect=AssertionError("must reuse atomic rename")):
            manifest = restarted.publish(batch["batch_id"], self.binding)
        self.assertEqual((target / "result.txt").stat().st_ino, identity)
        self.assertEqual(manifest["entries"][0]["relative_path"], "a2a/local-operation/result.txt")

    def test_legacy_a2a_batch_without_destination_keeps_original_path(self):
        batch = self.service.prepare([{"raw": b"old", "name": "old.txt"}], **{**self.options, "source": "legacy"})
        with self.store.locked(batch["batch_id"], "tenant") as (row, _):
            row["manifest"]["source"] = "a2a"
            with self.service._private(batch["batch_id"]) as (_, directory):
                _manifest(directory, row["manifest"])
        manifest = self.accept(batch)
        self.assertEqual(manifest["entries"][0]["relative_path"], f"attachments/{batch['batch_id']}/old.txt")

    def test_duplicate_names_and_destination_symlink_never_cross_chat_or_overwrite(self):
        first = self.service.prepare([{"raw": b"a", "name": "../same.txt"}, {"raw": b"", "name": "../same.txt"}], **self.options)
        manifest = self.accept(first)
        self.assertEqual(len({entry["relative_path"] for entry in manifest["entries"]}), 2)
        self.assertTrue(all("../" not in entry["relative_path"] for entry in manifest["entries"]))
        second = self.service.prepare([{"raw": b"b", "name": "../same.txt"}], **self.options)
        second_manifest = self.accept(second)
        self.assertEqual(second_manifest["entries"][0]["actual_name"], manifest["entries"][0]["actual_name"])
        for entry in manifest["entries"]:
            with self.assertRaises(CoreError):
                self.workspaces.open_file(WorkspaceBinding("tenant", "owner", "other-chat"), entry["relative_path"])
        target = self.workspaces.workspace(self.binding) / "a2a"
        target.rename(target.with_name("original-a2a"))
        target.symlink_to(target.with_name("original-a2a"), target_is_directory=True)
        with self.assertRaises(CoreError):
            self.service.owner_download(first["batch_id"], self.binding, 0, run_id="run", task_id="task")
