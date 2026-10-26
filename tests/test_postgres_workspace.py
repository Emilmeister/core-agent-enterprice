"""Real PostgreSQL fencing and atomic workspace/outcome publication."""
import os
from pathlib import Path
import tempfile
import unittest
import uuid
import sys
import asyncio
import copy

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.sql import Identifier, SQL
from psycopg.types.json import Jsonb

from core_agent.database import PostgresDatabase
from core_agent.errors import CoreError
from core_agent.execution import EnvironmentSpec, LocalTerminalBackend, TerminalSessionManager
from core_agent.model import ModelResponse, ToolRequest
from core_agent.postgres_workspace import PostgresWorkspaceDurability
from core_agent.workflow import PostgresWorkflowStore, WorkflowRecord
from core_agent.workspace import WorkspaceBinding
from core_agent.workspace_durability import WorkspaceDurability
from tests.test_workspace_durability import Blobs
from tests.test_auth import AuthAppTestCase
from tests.app_support import UnsandboxedTestLauncher


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "TEST_DATABASE_URL is required")
class PostgresWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        self.schema = "workspace_" + uuid.uuid4().hex
        with psycopg.connect(self.url, autocommit=True) as connection:
            connection.execute(SQL("CREATE SCHEMA {}").format(Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        self.database = PostgresDatabase(make_conninfo(self.url, options="-c search_path=" + self.schema),
                                         min_size=1, max_size=1)
        self.addCleanup(self.database.close)
        self.database.migrate()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = TerminalSessionManager(LocalTerminalBackend(self.root / "scratch", chat_root=self.root / "chat"))
        self.addCleanup(self.manager.close)
        self.binding = WorkspaceBinding("tenant", "owner", "context")
        self.blobs = Blobs()
        self.pins = []
        self.blobs.pin = lambda connection, digests: self.pins.append(set(digests))
        self.coordinator = self.service(self.manager)
        self.workflow = PostgresWorkflowStore(self.database)
        record = WorkflowRecord("run", "task", "context", "tenant", "owner", None, "RUNNING", 1, {"prompt": "work"}, {})
        self.record = self.workflow.create(record)
        self.token = self.workflow.acquire_lease("run", tenant_id="tenant", owner_id="owner", worker_id="worker", ttl=60)
        with self.database.transaction() as connection:
            connection.execute("INSERT INTO core_chats(tenant_id,owner_id,context_id,latest_root_run_id) VALUES ('tenant','owner','context','run')")

    def drop_schema(self):
        with psycopg.connect(self.url, autocommit=True) as connection:
            connection.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(self.schema)))

    def service(self, manager):
        return PostgresWorkspaceDurability(self.database, WorkspaceDurability(manager.backend.chats, self.blobs), manager)

    def outcome(self, connection):
        return self.workflow.transition("run", tenant_id="tenant", owner_id="owner", expected_version=self.record.version,
            state="RUNNING", snapshot=self.record.snapshot, event_kind="tool.completed", lease_token=self.token,
            connection=connection)

    def write(self, content=b"saved"):
        self.coordinator.ensure_restored(self.binding)
        (self.manager.backend.chats.workspace(self.binding) / "file").write_bytes(content)

    def test_outcome_and_manifest_commit_together_and_restore_without_database_connection(self):
        self.write()
        def inspect_pool():
            with self.database.transaction() as connection:
                self.assertEqual(connection.execute("SELECT 1 AS value").fetchone()["value"], 1)
        self.blobs.after_put = inspect_pool
        updated = self.coordinator.transition(self.record, self.token, self.outcome)
        self.assertEqual(updated.version, self.record.version + 1)
        self.assertEqual(len(self.pins), 1)
        manager = TerminalSessionManager(LocalTerminalBackend(self.root / "scratch2", chat_root=self.root / "chat2"))
        self.addCleanup(manager.close)
        self.service(manager).ensure_restored(self.binding)
        self.assertEqual((manager.backend.chats.workspace(self.binding) / "file").read_bytes(), b"saved")

    def test_failed_outcome_rolls_back_manifest_reference(self):
        self.write()
        def fail(connection):
            raise CoreError("SESSION_CONFLICT")
        with self.assertRaises(CoreError):
            self.coordinator.transition(self.record, self.token, fail)
        with self.database.transaction() as connection:
            self.assertIsNone(connection.execute("SELECT * FROM core_workspace_manifests").fetchone())
        self.assertEqual(self.workflow.get("run", tenant_id="tenant", owner_id="owner").version, self.record.version)

    def test_lost_lease_after_upload_cannot_publish(self):
        self.write()
        def expire():
            with self.database.transaction() as connection:
                connection.execute("UPDATE core_runs SET lease_expires_at=0 WHERE run_id='run'")
        self.blobs.after_put = expire
        with self.assertRaises(CoreError) as caught:
            self.coordinator.transition(self.record, self.token, self.outcome)
        self.assertEqual(caught.exception.code, "LEASE_LOST")
        with self.database.transaction() as connection:
            self.assertIsNone(connection.execute("SELECT * FROM core_workspace_manifests").fetchone())

    def execution_record(self):
        record = self.workflow.register_execution(self.record, instance_id="instance", worker_id="worker",
            generation=self.token, lease_token=self.token, execution_host={"container": "same-host"})
        self.record = self.workflow.transition(record.run_id, tenant_id=record.tenant_id,
            owner_id=record.owner_id, expected_version=record.version, state="EXECUTING",
            snapshot={**record.snapshot, "pending_call": {"name": "core_terminal_exec"}},
            event_kind="tool.intent", lease_token=self.token)
        return copy.deepcopy(self.record.snapshot)

    def test_confirmed_cleanup_during_checkpoint_preserves_receipt_without_reopening_execution(self):
        original = self.execution_record()
        for phase in ("before_upload", "after_upload"):
            for proof_kind in ("none", "new", "existing"):
                with self.subTest(phase=phase, proof=proof_kind):
                    snapshot = copy.deepcopy(original)
                    snapshot["execution_owner"]["future_receipt"] = {"opaque": "preserved"}
                    proof = {"kind": "operator", "receipt_id": "stop-proof"}
                    if proof_kind == "existing":
                        snapshot["execution_owner"]["cleanup_proof"] = proof
                    with self.database.transaction() as connection:
                        connection.execute("UPDATE core_runs SET snapshot=%s,state='EXECUTING' WHERE run_id='run'",
                                           (Jsonb(snapshot),))
                    self.record = self.workflow.get("run", tenant_id="tenant", owner_id="owner")
                    self.write((phase + proof_kind).encode())
                    confirmed = []
                    def confirm():
                        confirmed.append(self.workflow.confirm_execution(self.record,
                            self.record.snapshot["execution_owner"], proof=proof if proof_kind == "new" else None))
                    self.blobs.after_put = confirm if phase == "after_upload" else None
                    if phase == "before_upload":
                        confirm()
                    updated = self.coordinator.transition(self.record, self.token, self.outcome)
                    self.assertEqual(len(confirmed), 1)
                    self.assertEqual(confirmed[0].version, self.record.version)
                    self.assertEqual(updated.snapshot["execution_owner"], confirmed[0].snapshot["execution_owner"])
                    self.assertTrue(updated.snapshot["execution_owner"]["cleanup_confirmed"])
                    self.assertFalse(updated.snapshot["execution_owner"]["local_execution_pending"])
                    self.assertFalse(self.record.snapshot["execution_owner"]["cleanup_confirmed"])
                    with self.assertRaises(CoreError) as caught:
                        self.workflow.transition("run", tenant_id="tenant", owner_id="owner",
                            expected_version=updated.version, state="EXECUTING", snapshot=updated.snapshot,
                            event_kind="tool.intent", lease_token=self.token)
                    self.assertEqual(caught.exception.code, "LEASE_LOST")
                    self.record = updated

    def test_cleanup_receipt_does_not_relax_checkpoint_fences(self):
        snapshot = self.execution_record()
        owner = snapshot["execution_owner"]
        owner.update(future_receipt={"opaque": "original"}, cleanup_proof={"receipt_id": "original"})
        confirmed = {**owner, "cleanup_confirmed": True, "local_execution_pending": False}
        cases = [(field, {**confirmed, field: value}, {}, "LEASE_LOST") for field, value in (
            ("instance_id", "other"), ("worker_id", "other"), ("generation", "other"),
            ("execution_host", {"container": "other"}), ("future_receipt", {"opaque": "other"}),
            ("cleanup_confirmed", False), ("local_execution_pending", True),
            ("cleanup_confirmed", 1), ("local_execution_pending", 0),
            ("cleanup_proof", {"receipt_id": "other"}))]
        cases.extend((
            ("removed-proof", {key: value for key, value in confirmed.items() if key != "cleanup_proof"}, {}, "LEASE_LOST"),
            ("new-field", {**confirmed, "unknown_new_field": True}, {}, "LEASE_LOST"),
            ("stale-token", confirmed, {"lease_token": "other"}, "LEASE_LOST"),
            ("expired-token", confirmed, {"lease_expires_at": 0}, "LEASE_LOST"),
            ("new-version", confirmed, {"version": self.record.version + 1}, "SESSION_CONFLICT"),
        ))
        for phase in ("before_upload", "after_upload"):
            for name, actual, changes, code in cases:
                with self.subTest(phase=phase, change=name):
                    with self.database.transaction() as connection:
                        connection.execute("""UPDATE core_runs SET snapshot=%s,version=%s,lease_token=%s,
                            lease_expires_at=EXTRACT(EPOCH FROM clock_timestamp())+60 WHERE run_id='run'""",
                            (Jsonb(snapshot), self.record.version, self.token))
                    self.record = self.workflow.get("run", tenant_id="tenant", owner_id="owner")
                    self.blobs.after_put = None
                    self.write((phase + name).encode())
                    def change_receipt():
                        with self.database.transaction() as connection:
                            connection.execute("""UPDATE core_runs SET snapshot=%s,version=%s,lease_token=%s,
                                lease_expires_at=COALESCE(%s,lease_expires_at) WHERE run_id='run'""",
                                (Jsonb({**snapshot, "execution_owner": actual}),
                                 changes.get("version", self.record.version), changes.get("lease_token", self.token),
                                 changes.get("lease_expires_at")))
                    self.blobs.after_put = change_receipt if phase == "after_upload" else None
                    if phase == "before_upload":
                        change_receipt()
                    with self.assertRaises(CoreError) as caught:
                        self.coordinator.transition(self.record, self.token, self.outcome)
                    self.assertEqual(caught.exception.code, code)
                    with self.database.transaction() as connection:
                        self.assertIsNone(connection.execute("SELECT * FROM core_workspace_manifests").fetchone())

    def test_confirmed_cleanup_cannot_reverse_or_retire_another_generation(self):
        snapshot = self.execution_record()
        owner = snapshot["execution_owner"]
        confirmed = {**owner, "cleanup_confirmed": True, "local_execution_pending": False}
        for name, previous, actual in (
            ("reverse", confirmed, owner),
            ("other-generation", {**owner, "generation": "old"}, {**confirmed, "generation": "old"}),
            ("false-like-flag", {**owner, "cleanup_confirmed": 0}, confirmed),
        ):
            for lock in (False, True):
                with self.subTest(change=name, lock=lock), self.database.transaction() as connection:
                    connection.execute("UPDATE core_runs SET snapshot=%s WHERE run_id='run'",
                                       (Jsonb({**snapshot, "execution_owner": actual}),))
                    self.record.snapshot["execution_owner"] = previous
                    with self.assertRaises(CoreError) as caught:
                        self.coordinator._run(connection, self.record, self.token, lock=lock)
                    self.assertEqual(caught.exception.code, "LEASE_LOST")

    def test_cleanup_revision_survives_empty_cache_restore(self):
        self.write()
        self.record = self.coordinator.transition(self.record, self.token, self.outcome)
        with self.coordinator.local_lock(self.binding):
            (self.manager.backend.chats.workspace(self.binding) / "file").unlink()
            with self.database.transaction() as connection:
                revision = self.coordinator.prune(connection, self.binding, ["file"])
                connection.execute("UPDATE core_chats SET workspace_revision=workspace_revision+1")
            self.coordinator.note_cleanup(self.binding, revision)
        manager = TerminalSessionManager(LocalTerminalBackend(self.root / "scratch2", chat_root=self.root / "chat2"))
        self.addCleanup(manager.close)
        self.service(manager).ensure_restored(self.binding)
        self.assertEqual(manager.backend.chats.preview(self.binding)["files"], [])
        self.assertEqual(len(self.blobs.puts), 1)

    def test_live_background_process_blocks_snapshot_without_publishing_an_outcome(self):
        self.write()
        self.manager.backend.workspace_durability = self.coordinator
        session = self.manager.create(EnvironmentSpec("tenant", "run", "", (".",), (), {}, chat=self.binding))
        handle = session.start({"argv": [sys.executable, "-c", "import time; time.sleep(30)"]})
        try:
            with self.assertRaises(CoreError) as caught:
                self.coordinator.transition(self.record, self.token, self.outcome)
            self.assertEqual(caught.exception.code, "EXECUTION_CLEANUP_PENDING")
            self.assertEqual(self.blobs.puts, [])
        finally:
            session.cancel(handle.id)

    def test_aggregate_cache_budget_blocks_third_chat_before_reading_objects(self):
        self.coordinator.codec.max_bytes = 4
        self.write(b"abc")
        self.coordinator.transition(self.record, self.token, self.outcome)
        with self.database.transaction() as connection:
            manifest = connection.execute("SELECT manifest FROM core_workspace_manifests").fetchone()["manifest"]
            for context in ("second", "third"):
                binding = WorkspaceBinding("tenant", "owner", context)
                scoped = {**manifest, "scope": self.manager.backend.chats._scope(binding)}
                connection.execute("INSERT INTO core_chats(tenant_id,owner_id,context_id) VALUES ('tenant','owner',%s)", (context,))
                connection.execute("INSERT INTO core_workspace_manifests VALUES ('tenant','owner',%s,1,%s)", (context, Jsonb(scoped)))
        self.coordinator.ensure_restored(WorkspaceBinding("tenant", "owner", "second"))
        reads = len(self.blobs.gets)
        with self.assertRaises(CoreError) as caught:
            self.coordinator.ensure_restored(WorkspaceBinding("tenant", "owner", "third"))
        self.assertEqual(caught.exception.code, "WORKSPACE_CACHE_LIMIT")
        self.assertEqual(len(self.blobs.gets), reads)


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "TEST_DATABASE_URL is required")
class PostgresWorkspaceRuntimeTests(AuthAppTestCase):
    use_postgres = True

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.blobs = Blobs()
        self.blobs.pin = lambda connection, digests: None
        self.agent = self.app.state.core_agent
        self.attach(self.agent.tool_runtime.environment_manager)

    def attach(self, manager):
        service = PostgresWorkspaceDurability(self.agent.workflow_store.database,
            WorkspaceDurability(manager.backend.chats, self.blobs), manager)
        manager.backend.workspace_durability = service
        self.agent.workspace_durability = service
        self.agent.tool_runtime.environment_manager = manager

    async def test_authenticated_tools_restore_full_project_after_fresh_cache(self):
        scripts = ["from pathlib import Path; Path('.git').mkdir(); Path('.git/config').write_text('project'); Path('gone').write_text('old')",
                   "from pathlib import Path; Path('gone').unlink()",
                   "from pathlib import Path; assert not Path('gone').exists(); print(Path('.git/config').read_text())"]
        self.model._responses = [response for index, script in enumerate(scripts) for response in (
            ModelResponse(tool_requests=(ToolRequest(str(index), "core_terminal_exec", {"argv": [sys.executable, "-c", script]}),)),
            ModelResponse(message="done"))]
        first = await self.submit("external-a", "first", "durable-chat")
        await self.submit("external-a", "second", "durable-chat")
        previous = self.agent.tool_runtime.environment_manager
        previous.close()
        manager = TerminalSessionManager(LocalTerminalBackend(Path(self.temp.name) / "fresh-scratch",
            chat_root=Path(self.temp.name) / "fresh-chats", launcher=UnsandboxedTestLauncher()))
        manager.validate_workspace_scope = previous.validate_workspace_scope
        self.attach(manager)
        self.addCleanup(manager.close)
        await self.submit("external-a", "third", "durable-chat")
        self.assertEqual(first["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertIn("project", self.model.calls[-1].context)
        self.assertEqual(len(self.model.calls), 6)

    async def test_storage_failure_after_command_never_replays_the_command(self):
        # This test advances recovery explicitly, without a competing periodic scan.
        self.agent._recovery_stop.set()
        await asyncio.to_thread(self.agent._recovery_thread.join, 5)
        self.assertFalse(self.agent._recovery_thread.is_alive())
        self.model._responses = [ModelResponse(tool_requests=(ToolRequest("once", "core_terminal_exec", {
            "argv": [sys.executable, "-c", "open('counter','a').write('x')"]}),)), ModelResponse(message="done")]
        original = self.blobs.put
        self.blobs.put = lambda content: (_ for _ in ()).throw(CoreError("STORAGE_UNAVAILABLE"))
        await self.http.post("/a2a/external/message:send", headers=self.headers("external-a"), json={
            "message": {"messageId": "fail-once", "contextId": "durability-failure", "role": "ROLE_USER",
                        "parts": [{"text": "write once"}]}})
        with self.agent.workflow_store.database.transaction() as connection:
            row = connection.execute("SELECT * FROM core_runs WHERE tenant_id=%s AND context_id=%s",
                (os.environ["CORE_AGENT_TENANT_ID"], "durability-failure")).fetchone()
        self.assertIsNotNone(row)
        record = self.agent.workflow_store._record(row)
        self.assertNotEqual(record.state, "COMPLETED")
        binding = WorkspaceBinding(record.tenant_id, record.owner_id, record.context_id)
        counter = self.agent.tool_runtime.environment_manager.backend.chats.workspace(binding) / "counter"
        self.assertEqual(counter.read_text(), "x")
        # Recovery prepares the abort while storage is still unavailable.
        # This attempt owns a new lease but retires the same process.
        with self.assertRaises(CoreError) as pending:
            await asyncio.to_thread(self.agent.resume_task, record.task_id)
        self.assertEqual(pending.exception.code, "STORAGE_UNAVAILABLE")
        interrupted = self.agent.workflow_store.get(record.run_id, tenant_id=record.tenant_id, owner_id=record.owner_id)
        self.assertEqual(interrupted.snapshot["terminal_intent"]["state"], "ABORTED")
        self.assertTrue(interrupted.snapshot["execution_owner"]["cleanup_confirmed"])
        self.blobs.put = original
        with self.assertRaises(CoreError) as recovered_error:
            await asyncio.to_thread(self.agent.resume_task, record.task_id)
        self.assertEqual(recovered_error.exception.code, "SIDE_EFFECT_UNKNOWN")
        recovered = self.agent.workflow_store.get(record.run_id, tenant_id=record.tenant_id, owner_id=record.owner_id)
        self.assertIn(recovered.state, {"FAILED", "ABORTED"})
        self.assertEqual(counter.read_text(), "x")
        self.assertEqual(len(self.model.calls), 1)
