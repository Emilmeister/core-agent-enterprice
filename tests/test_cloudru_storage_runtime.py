"""Cloud composition with real PostgreSQL/MinIO and portable process fixtures."""
import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

import httpx
import psycopg
from cryptography.fernet import Fernet
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.sql import Identifier, SQL
from psycopg.types.json import Jsonb

from core_agent.database import PostgresDatabase
from core_agent.errors import CoreError
from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
from core_agent.owner_api import policy_catalog
from core_agent.postgres_workspace import PostgresWorkspaceDurability
from core_agent.remote_registry import PostgresRemoteRegistry
from core_agent.s3_ledger import PostgresS3BlobStore
from core_agent.s3_storage import S3BlobStore
from core_agent.workspace import WorkspaceBinding
from tests.app_support import UnsandboxedTestLauncher, create_app

_SETTINGS = ("TEST_DATABASE_URL", "TEST_S3_ENDPOINT", "TEST_S3_BUCKET",
             "TEST_S3_ACCESS_KEY_ID", "TEST_S3_SECRET_ACCESS_KEY")
_CONFIG = {name: os.getenv(name) for name in _SETTINGS}
_POSTGRES_CONTAINER = os.getenv("TEST_POSTGRES_CONTAINER")
_DOCKER = shutil.which("docker")
_DOCKER_ENV = {name: os.environ[name] for name in (
    "PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
) if name in os.environ}
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
            connection.execute(SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(Identifier(self.schema)))

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
    @unittest.skipUnless(_POSTGRES_CONTAINER, "TEST_POSTGRES_CONTAINER is required for coordinated backup proof")
    async def test_quiesced_backup_restores_referenced_s3_generations_and_encrypted_peer(self):
        key = Fernet.generate_key()
        os.environ["PUSH_NOTIFICATION_ENCRYPTION_KEY"] = key.decode()
        transport = S3BlobStore(endpoint_url=_CONFIG["TEST_S3_ENDPOINT"], bucket=_CONFIG["TEST_S3_BUCKET"],
            region="us-east-1", access_key_id=_CONFIG["TEST_S3_ACCESS_KEY_ID"],
            secret_access_key=_CONFIG["TEST_S3_SECRET_ACCESS_KEY"], tenant_id=self.tenant)
        self.addCleanup(transport.close)

        def cleanup_objects():
            for app in self.apps:
                app.state.close()
            for page in transport.client.get_paginator("list_objects_v2").paginate(
                    Bucket=transport.bucket, Prefix=transport.prefix + "/"):
                for item in page.get("Contents", []):
                    transport.client.delete_object(Bucket=transport.bucket, Key=item["Key"])
            remaining = transport.client.list_objects_v2(Bucket=transport.bucket, Prefix=transport.prefix + "/")
            self.assertEqual(remaining.get("Contents", []), [])

        self.addCleanup(cleanup_objects)
        content = "Согласованный snapshot\n".encode()
        await self.restart([
            self.terminal("from pathlib import Path; Path('.git').mkdir(); "
                          "Path('.git/config').write_bytes(b'hidden project'); "
                          f"Path('report.txt').write_bytes({content!r})"),
            ModelResponse(tool_requests=(ToolRequest("publish", "core_response_files", {"paths": ["report.txt"]}),)),
            ModelResponse(message="Published"),
        ])
        task = await self.submit("backup")
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED", task)
        record = self.agent.workflow_store.lookup_task(task["id"])
        reference = record.snapshot["published_artifacts"][0]["parts"][0]["ref"]
        download = f"/api/chats/backup/tasks/{task['id']}/files/{reference['file_id']}"
        self.assertEqual((await self.http.get(download)).content, content)
        peer_secret = "backup-peer-secret-canary"
        peer = self.agent.remote_registry.create(self.tenant,
            {"name": "backup-peer", "url": "https://peer.example/a2a", "description": "Local restore proof",
             "enabled": True, "header_name": "X-Api-Key", "header_value": peer_secret}, actor_id="local-test")
        await self.http.aclose()
        self.app.state.close()  # No application writers remain during either snapshot.
        tables = ("core_runs", "core_a2a_tasks", "core_artifacts", "core_workspace_manifests",
                  "core_s3_blobs", "core_remote_agents", "core_remote_agent_revisions")
        with psycopg.connect(self.dsn, row_factory=psycopg.rows.dict_row) as connection:
            before = {table: connection.execute(SQL("SELECT * FROM {}").format(Identifier(table))).fetchall()
                      for table in tables}
        referenced = {row["digest"][7:] for row in before["core_artifacts"] if row["state"] == "active"}
        referenced.update(entry["sha256"] for row in before["core_workspace_manifests"]
                          for entry in row["manifest"]["entries"].values() if entry["type"] == "file")
        blobs = [row for row in before["core_s3_blobs"] if row["state"] == "live" and row["digest"] in referenced]
        self.assertTrue(referenced)
        self.assertEqual({row["digest"] for row in blobs}, referenced)
        self.assertTrue(all(row["tenant_id"] == self.tenant for row in blobs))
        keys = {transport.key(row["digest"], object_id=row["generation"]):
                f"{transport.prefix}/backup-{self.schema}/{row['digest']}/{row['generation']}" for row in blobs}
        parameters = conninfo_to_dict(self.dsn)
        self.assertIsNotNone(_DOCKER, "Docker is required for TEST_POSTGRES_CONTAINER")
        command = [_DOCKER, "exec", "-i", _POSTGRES_CONTAINER]
        target = ["--username", parameters["user"], "--dbname", parameters["dbname"]]
        dump = subprocess.run(command + ["pg_dump", *target, "--format=custom", "--schema", self.schema],
                              check=True, capture_output=True, timeout=30, env=_DOCKER_ENV).stdout
        sql_dump = subprocess.run(command + ["pg_restore", "--file=-"], input=dump,
                                  check=True, capture_output=True, timeout=30, env=_DOCKER_ENV).stdout
        self.assertNotIn(key, sql_dump)
        self.assertNotIn(peer_secret.encode(), sql_dump)
        for source, backup in keys.items():
            transport.client.copy_object(Bucket=transport.bucket, Key=backup,
                CopySource={"Bucket": transport.bucket, "Key": source}, MetadataDirective="COPY")
        for source in keys:
            transport.client.delete_object(Bucket=transport.bucket, Key=source)
        with psycopg.connect(_CONFIG["TEST_DATABASE_URL"], autocommit=True) as connection:
            connection.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(self.schema)))
        shutil.rmtree(self.temp.name)
        Path(self.temp.name).mkdir()
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])
        subprocess.run(command + ["pg_restore", *target, "--exit-on-error"], input=dump,
                       check=True, capture_output=True, timeout=30, env=_DOCKER_ENV)
        with psycopg.connect(self.dsn, row_factory=psycopg.rows.dict_row) as connection:
            for table in tables:
                restored = connection.execute(SQL("SELECT * FROM {}").format(Identifier(table))).fetchall()
                self.assertCountEqual(restored, before[table], table)

        await self.restart([])
        missing = await self.http.get(download)
        self.assertGreaterEqual(missing.status_code, 400, missing.text)
        self.assertEqual(missing.json(), {"error": {"code": "NOT_FOUND"}})
        self.assertEqual(self.agent.workflow_store.lookup_task(task["id"]), record)
        for source, backup in keys.items():
            transport.client.copy_object(Bucket=transport.bucket, Key=source,
                CopySource={"Bucket": transport.bucket, "Key": backup}, MetadataDirective="COPY")
        restored = await self.http.get(download)
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.content, content)
        for path, expected in (("report.txt", content), (".git/config", b"hidden project")):
            response = await self.http.get("/api/chats/backup/files/content", params={"path": path})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.content, expected)
        registry = self.agent.remote_registry
        self.assertEqual(registry.get_revision(self.tenant, peer["id"], 1), peer)
        self.assertEqual(registry.resolve_headers(self.tenant, peer["id"], 1), {"X-Api-Key": peer_secret})
        with self.assertRaises(CoreError) as wrong_key:
            PostgresRemoteRegistry(self.app.state.database, Fernet.generate_key()).resolve_headers(self.tenant, peer["id"], 1)
        self.assertEqual(wrong_key.exception.code, "REMOTE_AGENT_CREDENTIAL_UNAVAILABLE")
        self.assertEqual(self.agent.workflow_store.lookup_task(task["id"]), record)
        self.assertFalse(self.model.calls)

    async def test_production_persists_memory_and_tasks_across_restart(self):
        os.environ.update(CORE_AGENT_ENVIRONMENT="production", DATABASE_AUTO_MIGRATE="false",
                          PUSH_NOTIFICATION_ENCRYPTION_KEY=Fernet.generate_key().decode(),
                          CORE_AGENT_MEMORY="required", MEMORY_STORAGE_TYPE="postgres")
        document = None
        task = None
        for backend in ("landlock", "bubblewrap"):
            os.environ["SANDBOX_BACKEND"] = backend
            # The fixture checks production composition; native gates prove isolation.
            with self.subTest(backend=backend), patch.object(PostgresDatabase, "migrate", side_effect=AssertionError("serving must not migrate")):
                await self.restart([ModelResponse(message="Production ready")])
                self.assertIs(self.agent.memory_registry.store.database, self.app.state.database)
                self.assertIsInstance(self.agent.workspace_blob_store, PostgresS3BlobStore)
                memory = self.agent.memory_registry.service("production-proof", "owner", tenant_id=self.tenant)
                if document is None:
                    document, _ = memory.create(title="Persistent note", body="Survives a new process.", namespace="subject/owner")
                    task = await self.submit("production-proof")
                    self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED", task)
                else:
                    self.assertEqual(memory.read(document.id).body, "Survives a new process.")
                    history = await self.http.get("/api/chats/production-proof/history")
                    self.assertEqual(history.status_code, 200, history.text)
                    self.assertIn(task["id"], history.text)

    async def test_production_rejects_incomplete_configuration(self):
        values = {"CORE_AGENT_ENVIRONMENT": "production", "DATABASE_AUTO_MIGRATE": "false",
                  "PUSH_NOTIFICATION_ENCRYPTION_KEY": Fernet.generate_key().decode(),
                  "SESSION_DATABASE_URL": self.dsn}
        for override, code in (
            ({"WORKSPACE_STORAGE_TYPE": "posix"}, "CONFIG_INVALID"),
            ({"WORKSPACE_STORAGE_TYPE": ""}, "CONFIG_INVALID"),
            ({"ARTIFACT_S3_SECRET_ACCESS_KEY": ""}, "CONFIG_INVALID"),
            ({"SESSION_STORAGE_TYPE": "in-memory"}, "CONFIG_INVALID"),
            ({"TASK_STORAGE_TYPE": "in-memory"}, "CONFIG_INVALID"),
            ({"DATABASE_AUTO_MIGRATE": "true"}, "PRODUCTION_AUTO_MIGRATE_FORBIDDEN"),
            ({"PUSH_NOTIFICATION_ENCRYPTION_KEY": ""}, "PUSH_ENCRYPTION_KEY_REQUIRED"),
            ({"PUSH_NOTIFICATION_ENCRYPTION_KEY": "invalid"}, "REMOTE_AGENT_CREDENTIAL_UNAVAILABLE"),
            ({"DURABLE_STORAGE_ROOT": ""}, "DURABLE_STORAGE_REQUIRED"),
            ({"CORE_AGENT_MEMORY": "required", "MEMORY_STORAGE_TYPE": "in-memory"}, "CONFIG_INVALID"),
            ({"LOCAL_WORKSPACE_ROOT": os.environ["CHAT_WORKSPACE_ROOT"]}, "CONFIG_INVALID"),
        ):
            with self.subTest(override=override), patch.dict(os.environ, {**values, **override}):
                with self.assertRaises(CoreError) as caught:
                    create_app(model=self.model)
                self.assertEqual(caught.exception.code, code)

    async def test_production_fails_closed_when_sandbox_or_s3_preflight_fails(self):
        values = {"CORE_AGENT_ENVIRONMENT": "production", "DATABASE_AUTO_MIGRATE": "false",
                  "PUSH_NOTIFICATION_ENCRYPTION_KEY": Fernet.generate_key().decode()}
        for target, code in (("sandbox", "SANDBOX_UNAVAILABLE"), ("s3", "STORAGE_UNAVAILABLE")):
            launcher = UnsandboxedTestLauncher()
            database = PostgresDatabase(self.dsn)
            failed = (patch.object(launcher, "preflight", side_effect=CoreError(code)) if target == "sandbox"
                      else patch("core_agent.s3_storage.S3BlobStore.check", side_effect=CoreError(code)))
            with self.subTest(target=target), patch.dict(os.environ, values), failed:
                with self.assertRaises(CoreError) as caught:
                    create_app(model=self.model, database=database, sandbox_launcher=launcher)
                self.assertEqual(caught.exception.code, code)
                self.assertTrue(database._closed)
                self.assertTrue(launcher._closed)

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

    async def test_cancel_background_persists_known_result_after_cleanup_confirmation(self):
        code = "import time; open('cancelled.txt','w').write('once'); time.sleep(60)"
        self.model._responses = [ModelResponse(tool_requests=(ToolRequest("start", "core_task_start", {
            "tool": "core_terminal_exec", "arguments": {"argv": [sys.executable, "-c", code]}}),)),
            ModelResponse(message="Cancelled and observed")]
        started, confirmed = threading.Event(), threading.Event()
        manager = self.agent.tool_runtime.environment_manager
        launcher, workflow = manager.backend.launcher, self.agent.workflow_store
        start, execute, generate = launcher.start, self.agent.tool_runtime.execute, self.model.generate
        confirm, checkpoint = workflow.confirm_execution, self.agent.workspace_durability.transition
        stale_checkpoints = []
        children = []

        def spawn(*args, **kwargs):
            process = start(*args, **kwargs)
            marker = Path(kwargs["workspace_path"]) / "cancelled.txt"
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists(), "background command did not start")
            started.set()
            return process

        def dispatched(call, *args, **kwargs):
            result = execute(call, *args, **kwargs)
            if call.name == "core_task_start":
                self.assertTrue(started.wait(5), "background process did not start")
            elif call.name == "core_terminal_exec":
                # Keep the worker's EXECUTING snapshot until the real cancel
                # callback has stopped its process and persisted the receipt.
                self.assertTrue(confirmed.wait(5), "cancel did not confirm cleanup")
            return result

        def confirmed_execution(record, owner, **kwargs):
            current = confirm(record, owner, **kwargs)
            if record.snapshot.get("background_tool") and current.cancel_requested:
                self.assertTrue(current.snapshot["execution_owner"]["cleanup_confirmed"])
                confirmed.set()
            return current

        def publish(record, token, operation):
            if (record.snapshot.get("background_tool") and confirmed.is_set()
                    and not record.snapshot["execution_owner"]["cleanup_confirmed"]):
                current = workflow.lookup_task(record.task_id)
                self.assertEqual(current.version, record.version)
                self.assertEqual(current.snapshot["execution_owner"]["generation"], token)
                self.assertTrue(current.snapshot["execution_owner"]["cleanup_confirmed"])
                stale_checkpoints.append(record.task_id)
            return checkpoint(record, token, operation)

        def model(**kwargs):
            if len(self.model.calls) == 1:
                with workflow.database.transaction() as connection:
                    row = connection.execute("SELECT latest_root_run_id FROM core_chats WHERE tenant_id=%s AND context_id='cancel-background'",
                                             (self.tenant,)).fetchone()
                children.extend(self.agent.task_scheduler.list(owner_id=row["latest_root_run_id"], tenant_id=self.tenant))
                self.model._responses.insert(0, ModelResponse(tool_requests=(ToolRequest("cancel", "core_task_cancel", {
                    "task_id": children[0].id}),)))
            elif len(self.model.calls) == 2:
                self.model._responses.insert(0, ModelResponse(tool_requests=(ToolRequest("wait", "core_task_wait", {
                    "task_id": children[0].id}),)))
            return generate(**kwargs)

        with patch.object(launcher, "start", spawn), patch.object(self.agent.tool_runtime, "execute", dispatched), \
                patch.object(workflow, "confirm_execution", confirmed_execution), \
                patch.object(self.agent.workspace_durability, "transition", publish), patch.object(self.model, "generate", model):
            task = await asyncio.wait_for(self.submit("cancel-background"), timeout=30)
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED", task)
        self.assertEqual(stale_checkpoints, [children[0].id])
        child = workflow.lookup_task(children[0].id)
        self.assertEqual(child.state, "CANCELLED")
        self.assertIsNone(child.error_code)
        self.assertTrue(child.snapshot["execution_owner"]["cleanup_confirmed"])
        # Cancellation can stop material review after the raw result is saved.
        receipt = child.snapshot["pending_completed_result"]["outcome"]
        self.assertEqual(receipt["status"], "failed")
        self.assertIsNotNone(receipt["output"]["exit_code"])
        self.assertTrue(launcher.processes)
        self.assertTrue(all(process.returncode is not None for process in launcher.processes))
        scheduled = self.agent.task_scheduler.get(children[0].id, owner_id=child.parent_run_id, tenant_id=self.tenant)
        self.assertEqual(scheduled.state, "canceled")
        await self.restart([])
        restored = await self.http.get("/api/chats/cancel-background/files/content", params={"path": "cancelled.txt"})
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.content, b"once")

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
