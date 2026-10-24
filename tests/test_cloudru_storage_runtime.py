"""Cloud composition with real PostgreSQL/MinIO and portable process fixtures."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

import httpx
import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.sql import Identifier, SQL
from psycopg.types.json import Jsonb

from core_agent.database import PostgresDatabase
from core_agent.errors import CoreError
from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
from core_agent.owner_api import policy_catalog
from core_agent.postgres_workspace import PostgresWorkspaceDurability
from core_agent.s3_ledger import PostgresS3BlobStore
from core_agent.workspace import WorkspaceBinding
from tests.app_support import UnsandboxedTestLauncher, create_app

_SETTINGS = ("TEST_DATABASE_URL", "TEST_S3_ENDPOINT", "TEST_S3_BUCKET",
             "TEST_S3_ACCESS_KEY_ID", "TEST_S3_SECRET_ACCESS_KEY")
_CONFIG = {name: os.getenv(name) for name in _SETTINGS}
_ORIGIN = "https://agent.example.test"
_PASSWORD = "local-storage-owner-password"


class CleanupCrash(BaseException):
    pass


@unittest.skipUnless(all(_CONFIG.values()), "PostgreSQL and S3 integration fixtures are required")
class CloudStorageRuntimeFixture(unittest.IsolatedAsyncioTestCase):
    launcher_type = UnsandboxedTestLauncher

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.schema = "cloud_storage_" + uuid.uuid4().hex
        self.tenant = uuid.uuid4().hex
        with psycopg.connect(_CONFIG["TEST_DATABASE_URL"], autocommit=True) as connection:
            connection.execute(SQL("CREATE SCHEMA {}").format(Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        self.dsn = make_conninfo(_CONFIG["TEST_DATABASE_URL"], options="-c search_path=" + self.schema)
        self.environment = patch.dict(os.environ, {
            "CORE_AGENT_ENVIRONMENT": "development", "CORE_AGENT_AUTH_MODE": "cloudru",
            "CORE_AGENT_TENANT_ID": self.tenant, "CORE_AGENT_OWNER_PASSWORD": _PASSWORD,
            "AGENT_URL": _ORIGIN, "CORE_AGENT_MEMORY": "disabled", "WORKSPACE_STORAGE_TYPE": "s3",
            "ARTIFACT_S3_ENDPOINT_URL": _CONFIG["TEST_S3_ENDPOINT"],
            "ARTIFACT_S3_BUCKET": _CONFIG["TEST_S3_BUCKET"], "ARTIFACT_S3_REGION": "us-east-1",
            "ARTIFACT_S3_ACCESS_KEY_ID": _CONFIG["TEST_S3_ACCESS_KEY_ID"],
            "ARTIFACT_S3_SECRET_ACCESS_KEY": _CONFIG["TEST_S3_SECRET_ACCESS_KEY"],
            "LOG_LEVEL": "ERROR", "CORE_AGENT_STARTUP_DIAGNOSTICS": "false",
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.apps = []
        self.addCleanup(lambda: [app.state.close() for app in reversed(self.apps)])
        await self.restart([])

    def drop_schema(self):
        with psycopg.connect(_CONFIG["TEST_DATABASE_URL"], autocommit=True) as connection:
            connection.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(self.schema)))

    async def restart(self, responses):
        if self.apps:
            await self.http.aclose()
            self.apps[-1].state.close()
        pod = Path(self.temp.name) / str(len(self.apps))
        os.environ.update(LOCAL_WORKSPACE_ROOT=str(pod / "scratch"), CHAT_WORKSPACE_ROOT=str(pod / "chats"),
                          DURABLE_STORAGE_ROOT=str(pod / "durable"))
        self.model = ScriptedModel(responses)
        self.model.model = "local-storage-proof"
        self.app = create_app(model=self.model, database=PostgresDatabase(self.dsn), sandbox_launcher=self.launcher_type())
        self.apps.append(self.app)
        self.agent = self.app.state.core_agent
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=_ORIGIN)
        self.addAsyncCleanup(self.http.aclose)
        for name, origin in policy_catalog(self.agent).items():
            current = self.agent.interaction_store.get_policy(self.tenant, name, origin)
            if current.mode != "allow":
                self.agent.interaction_store.update_policy(self.tenant, name, origin, mode="allow",
                    guardrails_exempt=False, expected_revision=current.revision, actor_id="local-test")
        login = await self.http.post("/api/auth/login", headers={"Origin": _ORIGIN}, json={"password": _PASSWORD})
        self.assertEqual(login.status_code, 200, login.text)

    @staticmethod
    def terminal(code, identifier="write"):
        return ModelResponse(tool_requests=(ToolRequest(identifier, "core_terminal_exec", {
            "argv": [sys.executable, "-c", code]}),))

    async def submit(self, context):
        response = await self.http.post("/a2a/owner/message:send", headers={"Origin": _ORIGIN, "A2A-Version": "1.0"},
            json={"message": {"messageId": uuid.uuid4().hex, "contextId": context, "role": "ROLE_USER",
                              "parts": [{"text": "Complete the requested task"}]}})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["task"]


class CloudStorageRuntimeTests(CloudStorageRuntimeFixture):
    async def test_history_after_restart_retains_published_file_after_move_and_edit(self):
        self.model._responses = [self.terminal("open('report.txt','w').write('published bytes')"),
            ModelResponse(tool_requests=(ToolRequest("publish", "core_response_files", {"paths": ["report.txt"]}),)),
            ModelResponse(message="Published")]
        task = await self.submit("cold-history")
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED", task)
        record = self.agent.workflow_store.lookup_task(task["id"])
        artifact = record.snapshot["published_artifacts"][0]
        reference = artifact["parts"][0]["ref"]
        self.model._responses = [self.terminal("from pathlib import Path; Path('report.txt').rename('moved.txt'); Path('moved.txt').write_text('edited bytes')"),
                                 ModelResponse(message="Moved and edited")]
        changed = await self.submit("cold-history")
        self.assertEqual(changed["status"]["state"], "TASK_STATE_COMPLETED", changed)
        await self.restart([])
        database = self.app.state.database
        database.pool.resize(min_size=1, max_size=1)
        blobs = self.agent.workspace_blob_store
        get = blobs.get

        def download_outside_transaction(digest, size):
            with database.pool.connection(timeout=1) as connection:
                connection.execute("SELECT 1")
            return get(digest, size)

        # This must be the first content projection: task/file reads can warm the cache.
        with patch.object(blobs, "get", side_effect=download_outside_transaction) as fetched:
            history = await self.http.get("/api/chats/cold-history/history", params={"limit": 50})
        self.assertEqual(history.status_code, 200, history.text)
        self.assertTrue(fetched.called)
        publications = [value for item in history.json()["items"] for value in item.get("response_artifacts", [])]
        self.assertTrue(any(value["artifact_id"] == artifact["artifact_id"]
                            and value["parts"][0]["sha256"] == reference["sha256"] for value in publications))
        download = await self.http.get(f"/api/chats/cold-history/tasks/{task['id']}/files/{reference['file_id']}")
        self.assertEqual(download.status_code, 200, download.text)
        self.assertEqual(download.content, b"published bytes")
        moved = await self.http.get("/api/chats/cold-history/files/content", params={"path": "moved.txt"})
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(moved.content, b"edited bytes")
        self.assertFalse(self.model.calls)

    @unittest.skipUnless(sys.platform == "linux", "Linux directory SGID inheritance is required")
    async def test_first_python_call_checkpoints_inherited_setgid_and_restores_files(self):
        workspaces = self.agent.tool_runtime.environment_manager.backend.chats
        workspaces.root.chmod(0o2700)
        self.model._responses = [ModelResponse(tool_requests=(ToolRequest("write", "core_python_exec", {
            "code": "from pathlib import Path; Path('report.txt').write_text('saved by Python')"}),)),
            ModelResponse(message="Saved")]
        task = await self.submit("inherited-mode")
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED", task)
        record = self.agent.workflow_store.lookup_task(task["id"])
        binding = WorkspaceBinding(self.tenant, record.owner_id, "inherited-mode")
        folder = workspaces.workspace(binding)
        mode = (folder / "attachments").stat().st_mode & 0o7777
        self.assertTrue(mode & 0o2000)
        with self.agent.workflow_store.database.transaction() as connection:
            manifest = connection.execute(
                "SELECT manifest FROM core_workspace_manifests WHERE tenant_id=%s AND context_id=%s",
                (self.tenant, "inherited-mode")).fetchone()["manifest"]
        self.assertEqual(manifest["entries"]["attachments"]["mode"], mode)
        await self.restart([])
        restored = await self.http.get("/api/chats/inherited-mode/files/content", params={"path": "report.txt"})
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.content, b"saved by Python")
        folder = self.agent.tool_runtime.environment_manager.backend.chats.workspace(binding)
        self.assertEqual((folder / "attachments").stat().st_mode & 0o7777, mode)
        self.assertFalse(self.model.calls)

    async def test_invalid_tree_aborts_once_and_preserves_durable_files(self):
        self.model._responses = [self.terminal("open('report.txt','w').write('saved report')"),
            ModelResponse(tool_requests=(ToolRequest("publish", "core_response_files", {"paths": ["report.txt"]}),)),
            ModelResponse(message="Saved")]
        published = await self.submit("invalid-tree")
        saved = self.agent.workflow_store.lookup_task(published["id"])
        reference = saved.snapshot["published_artifacts"][0]["parts"][0]["ref"]
        download = f"/api/chats/invalid-tree/tasks/{saved.task_id}/files/{reference['file_id']}"
        database = self.agent.workflow_store.database
        with database.transaction() as connection:
            before = connection.execute("SELECT revision,manifest FROM core_workspace_manifests WHERE tenant_id=%s AND context_id=%s",
                                        (self.tenant, "invalid-tree")).fetchone()
        self.model._responses = [self.terminal("import os; open('report.txt','w').write('uncommitted'); os.mkfifo('unsupported')")]
        task = await self.submit("invalid-tree")
        with self.assertRaises(CoreError) as caught:
            await asyncio.to_thread(self.agent.resume_task, task["id"])
        self.assertEqual(caught.exception.code, "SIDE_EFFECT_UNKNOWN")
        record = self.agent.workflow_store.lookup_task(task["id"])
        self.assertEqual(record.state, "ABORTED")
        self.assertEqual(record.error_code, "SIDE_EFFECT_UNKNOWN")
        self.assertNotIn("terminal_intent", record.snapshot)
        self.assertTrue(record.snapshot["execution_owner"]["cleanup_confirmed"])
        with database.transaction() as connection:
            after = connection.execute("SELECT revision,manifest FROM core_workspace_manifests WHERE tenant_id=%s AND context_id=%s",
                                       (self.tenant, "invalid-tree")).fetchone()
        self.assertEqual(after, before)
        calls = len(self.model.calls)
        for _ in range(2):
            recovered = await asyncio.to_thread(self.agent._recover_workflows_once)
            self.assertEqual(recovered, ())
        self.assertEqual(len(self.model.calls), calls)
        await self.restart([])
        self.assertEqual((await self.http.get(download)).content, b"saved report")
        restored = await self.http.get("/api/chats/invalid-tree/files/content", params={"path": "report.txt"})
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.content, b"saved report")
        self.assertFalse(self.model.calls)

    async def test_real_composition_publication_restore_and_owner_cleanup(self):
        self.model._responses = [self.terminal("from pathlib import Path; Path('.git').mkdir(); Path('.git/config').write_text('project'); Path('report.txt').write_text('immutable report')"),
            ModelResponse(tool_requests=(ToolRequest("publish", "core_response_files", {"paths": ["report.txt"]}),)),
            ModelResponse(message="Published")]
        self.assertIsInstance(self.agent.workspace_durability, PostgresWorkspaceDurability)
        self.assertIsInstance(self.agent.response_files_service.artifact_store.blob_store, PostgresS3BlobStore)
        task = await self.submit("files")
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED", task)
        record = self.agent.workflow_store.lookup_task(task["id"])
        reference = record.snapshot["published_artifacts"][0]["parts"][0]["ref"]
        download = f"/api/chats/files/tasks/{task['id']}/files/{reference['file_id']}"
        await self.restart([])
        frozen = await self.http.get(download)
        self.assertEqual(frozen.status_code, 200, frozen.text)
        self.assertEqual(frozen.content, b"immutable report")
        listing = await self.http.get("/api/chats/files/files")
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual({item["path"] for item in listing.json()["files"]}, {".git/config", "report.txt"})
        selected = next(item for item in listing.json()["files"] if item["path"] == "report.txt")
        deleted = await self.http.post("/api/chats/files/files/delete", headers={"Origin": _ORIGIN}, json={
            "request_id": "remove-report", "files": [{key: selected[key] for key in ("path", "identity_token")}]})
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["state"], "completed", deleted.text)
        self.assertEqual(deleted.json()["results"][0]["status"], "deleted", deleted.text)
        await self.restart([])
        listing = await self.http.get("/api/chats/files/files")
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual([item["path"] for item in listing.json()["files"]], [".git/config"])
        self.assertEqual((await self.http.get(download)).content, b"immutable report")

    async def test_background_launch_receipt_does_not_snapshot_a_live_process(self):
        code = "import time; time.sleep(1); open('background.txt','w').write('finished')"
        self.model._responses = [ModelResponse(tool_requests=(ToolRequest("start", "core_task_start", {
            "tool": "core_terminal_exec", "arguments": {"argv": [sys.executable, "-c", code]}}),)),
            ModelResponse(message="Finished")]
        started = threading.Event()
        launcher = self.agent.tool_runtime.environment_manager.backend.launcher
        start, execute, generate = launcher.start, self.agent.tool_runtime.execute, self.model.generate
        def spawn(*args, **kwargs):
            result = start(*args, **kwargs)
            started.set()
            return result
        def dispatched(call, *args, **kwargs):
            result = execute(call, *args, **kwargs)
            if call.name == "core_task_start":
                self.assertTrue(started.wait(5), "background process did not start")
            return result
        def model(**kwargs):
            if len(self.model.calls) == 1:
                with self.agent.workflow_store.database.transaction() as connection:
                    row = connection.execute("SELECT latest_root_run_id FROM core_chats WHERE tenant_id=%s AND context_id='background'",
                                             (self.tenant,)).fetchone()
                child = self.agent.task_scheduler.list(owner_id=row["latest_root_run_id"], tenant_id=self.tenant)[0]
                self.model._responses.insert(0, ModelResponse(tool_requests=(ToolRequest("wait", "core_task_wait", {
                    "task_id": child.id}),)))
            return generate(**kwargs)
        with patch.object(launcher, "start", spawn), patch.object(self.agent.tool_runtime, "execute", dispatched), patch.object(self.model, "generate", model):
            task = await self.submit("background")
            record = self.agent.workflow_store.lookup_task(task["id"])
            child = self.agent.task_scheduler.list(owner_id=record.run_id, tenant_id=self.tenant)[0]
            completed = await asyncio.to_thread(self.agent.task_scheduler.wait, child.id, timeout=10,
                                                owner_id=record.run_id, tenant_id=self.tenant)
            self.assertEqual(completed.state, "completed", completed.error)
        self.assertEqual(self.agent.workflow_store.lookup_task(task["id"]).state, "WAITING_TASK")
        child_record = self.agent.workflow_store.lookup_task(child.id)
        self.assertEqual(child_record.state, "COMPLETED")
        with self.agent.workflow_store.database.transaction() as connection:
            manifest = connection.execute("SELECT manifest FROM core_workspace_manifests WHERE tenant_id=%s AND context_id=%s",
                                          (self.tenant, "background")).fetchone()["manifest"]
        self.assertEqual(manifest["entries"]["background.txt"]["size"], len(b"finished"))
        await self.restart([ModelResponse(message="Finished")])
        downloaded = await self.http.get("/api/chats/background/files/content", params={"path": "background.txt"})
        self.assertEqual(downloaded.status_code, 200, downloaded.text)
        self.assertEqual(downloaded.content, b"finished")
        self.assertEqual(self.agent.workflow_store.lookup_task(task["id"]).state, "WAITING_TASK")
        await asyncio.to_thread(self.agent._recover_workflows_once)
        await asyncio.to_thread(self.agent.resume_task, task["id"])
        self.assertEqual(self.agent.workflow_store.lookup_task(task["id"]).state, "COMPLETED")

    async def test_empty_durable_manifest_cannot_overwrite_nonempty_uninitialized_cache(self):
        self.model._responses = [ModelResponse(message="Empty")]
        task = await self.submit("empty")
        record = self.agent.workflow_store.lookup_task(task["id"])
        binding = WorkspaceBinding(record.tenant_id, record.owner_id, record.context_id)
        await self.restart([])
        workspaces = self.agent.tool_runtime.environment_manager.backend.chats
        target = workspaces.workspace(binding) / "uncommitted"
        target.write_bytes(b"keep")
        with self.assertRaises(CoreError) as caught:
            self.agent.workspace_durability.ensure_restored(binding)
        self.assertEqual(caught.exception.code, "WORKSPACE_RESTORE_CONFLICT")
        self.assertEqual(target.read_bytes(), b"keep")

    async def pending_cleanup(self, context):
        self.model._responses = [self.terminal("open('remove.txt','w').write('old'); open('keep.txt','w').write('retained')"),
                                 ModelResponse(message="Written")]
        task = await self.submit(context)
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED", task)
        record = self.agent.workflow_store.lookup_task(task["id"])
        binding = WorkspaceBinding(record.tenant_id, record.owner_id, record.context_id)
        listing = await self.http.get(f"/api/chats/{context}/files")
        selected = next(item for item in listing.json()["files"] if item["path"] == "remove.txt")
        cleanup = self.agent.workspace_cleanup
        operation = await asyncio.to_thread(cleanup._prepare, self.tenant, context, "local-test", {
            "request_id": "delete", "files": [{key: selected[key] for key in ("path", "identity_token")}]})
        self.assertEqual(operation["state"], "pending")
        return binding, cleanup

    async def test_fresh_pod_recovers_cleanup_before_and_after_uncommitted_local_deletion(self):
        for after_delete in (False, True):
            with self.subTest(after_delete=after_delete):
                context = "cleanup-" + str(after_delete).lower()
                binding, cleanup = await self.pending_cleanup(context)
                if after_delete:
                    with patch.object(cleanup, "_save", side_effect=CleanupCrash), self.assertRaises(CleanupCrash):
                        await asyncio.to_thread(cleanup._execute, self.tenant, context, "delete")
                    self.assertFalse((cleanup.workspaces.workspace(binding) / "remove.txt").exists())
                with cleanup.database.transaction() as connection:
                    pointer = connection.execute("SELECT manifest FROM core_workspace_manifests WHERE tenant_id=%s AND context_id=%s",
                                                 (self.tenant, context)).fetchone()
                    self.assertIn("remove.txt", pointer["manifest"]["entries"])
                self.assertEqual((await cleanup.get(self.tenant, context, "delete"))["state"], "pending")
                await self.restart([])
                await asyncio.to_thread(self.agent.workspace_cleanup.recover, tenant_id=self.tenant)
                receipt = await self.agent.workspace_cleanup.get(self.tenant, context, "delete")
                self.assertEqual(receipt["state"], "completed", receipt)
                self.assertEqual(receipt["results"][0]["status"], "deleted", receipt)
                listing = await self.http.get(f"/api/chats/{context}/files")
                self.assertEqual(listing.status_code, 200, listing.text)
                self.assertEqual([item["path"] for item in listing.json()["files"]], ["keep.txt"])
                downloaded = await self.http.get(f"/api/chats/{context}/files/content", params={"path": "keep.txt"})
                self.assertEqual(downloaded.content, b"retained")

    async def test_cleanup_recovery_retains_selected_file_when_durable_identity_changed(self):
        for changed in ("sha256", "mode"):
            with self.subTest(changed=changed):
                context = "changed-" + changed
                binding, cleanup = await self.pending_cleanup(context)
                blobs = self.agent.workspace_durability.codec.blob_store
                digest = blobs.put(b"new") if changed == "sha256" else None
                with cleanup.database.transaction() as connection:
                    pointer = connection.execute("SELECT manifest FROM core_workspace_manifests WHERE tenant_id=%s AND context_id=%s",
                                                 (self.tenant, context)).fetchone()
                    manifest = pointer["manifest"]
                    if digest:
                        blobs.pin(connection, [digest])
                        manifest["entries"]["remove.txt"]["sha256"] = digest
                    else:
                        manifest["entries"]["remove.txt"]["mode"] ^= 0o100
                    connection.execute("UPDATE core_workspace_manifests SET manifest=%s,revision=revision+1 WHERE tenant_id=%s AND context_id=%s",
                                       (Jsonb(manifest), self.tenant, context))
                await self.restart([])
                await asyncio.to_thread(self.agent.workspace_cleanup.recover, tenant_id=self.tenant)
                receipt = await self.agent.workspace_cleanup.get(self.tenant, context, "delete")
                self.assertEqual(receipt["state"], "completed", receipt)
                self.assertEqual(receipt["results"][0], {"path": "remove.txt", "status": "skipped", "reason": "identity_changed"})
                downloaded = await self.http.get(f"/api/chats/{context}/files/content", params={"path": "remove.txt"})
                self.assertEqual(downloaded.status_code, 200, downloaded.text)
                self.assertEqual(downloaded.content, b"new" if digest else b"old")
