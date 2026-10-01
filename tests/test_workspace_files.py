"""Read-only workspace APIs use canonical ownership and real descriptor traversal."""
import asyncio
import os
import threading
import unittest
import uuid
from dataclasses import replace
from urllib.parse import quote
from unittest.mock import patch

from starlette.requests import ClientDisconnect

from core_agent.errors import CoreError
from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
from core_agent.owner_api import WorkspaceDownload
from core_agent.workspace import ChatWorkspaces, WorkspaceBinding
from tests.test_auth import AuthAppTestCase, TEST_DATABASE_URL


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
        control = f"attachments/{batch_id}/.manifest.json"
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
