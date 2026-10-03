"""ENT-MIG-01: real schema upgrades preserve admitted state and opaque blobs.

These checks cover database compatibility and fail-closed version checks;
operator mapping/import and a production cutover remain separate.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from a2a.types import a2a_pb2
from cryptography.fernet import Fernet
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.errors import RaiseException
from psycopg.sql import SQL, Identifier
from psycopg.types.json import Jsonb

from core_agent import database as database_module
from core_agent.artifacts import PostgresArtifactStore
from core_agent.database import PostgresDatabase
from core_agent.errors import CoreError
from core_agent.remote_registry import PostgresRemoteRegistry


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "TEST_DATABASE_URL is required")
class EnterpriseMigrationTests(unittest.TestCase):
    def setUp(self):
        self.admin = PostgresDatabase(os.environ["TEST_DATABASE_URL"], min_size=0, max_size=1)
        self.addCleanup(self.admin.close)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.key = Fernet.generate_key()

    def database_at(self, version):
        schema = "enterprise_upgrade_" + uuid.uuid4().hex
        with self.admin.transaction() as connection:
            connection.execute(SQL("CREATE SCHEMA {}").format(Identifier(schema)))

        def remove_schema():
            with self.admin.transaction() as connection:
                connection.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(schema)))

        self.addCleanup(remove_schema)
        url = make_conninfo(os.environ["TEST_DATABASE_URL"], options="-c search_path=" + schema)
        database = PostgresDatabase(url, min_size=0, max_size=1)
        self.addCleanup(database.close)
        migrations = {v: sql for v, sql in database_module.MIGRATIONS.items() if v <= version}
        with patch.object(database_module, "SCHEMA_VERSION", version), \
                patch.dict(database_module.MIGRATIONS, migrations, clear=True):
            database.migrate()
        self.assertEqual(database.schema_version(), version)
        return database, url

    def test_remote_registration_delete_upgrade_restart_and_tombstone(self):
        database, url = self.database_at(26)
        registry = PostgresRemoteRegistry(database, self.key)
        values = {"name": "upgrade-peer", "url": "https://peer.example/a2a", "description": "Pinned",
                  "enabled": True, "header_name": "Authorization", "header_value": "Bearer migration-pinned"}
        peer = registry.create("company", values, actor_id="owner")
        database.migrate()
        with patch.object(database_module, "SCHEMA_VERSION", 26):
            with self.assertRaises(CoreError) as error:
                database.verify_schema()
            self.assertEqual(error.exception.code, "DATABASE_SCHEMA_MISMATCH")
        reopened = PostgresDatabase(url, min_size=0, max_size=1)
        self.addCleanup(reopened.close)
        reopened.verify_schema()
        registry = PostgresRemoteRegistry(reopened, self.key)
        self.assertEqual(registry.list("company"), [peer])
        registry.delete("company", peer["id"], expected_revision=1, actor_id="owner")
        restarted = PostgresDatabase(url, min_size=0, max_size=1)
        self.addCleanup(restarted.close)
        registry = PostgresRemoteRegistry(restarted, self.key)
        self.assertEqual(registry.list("company"), [])
        self.assertEqual(registry.get_revision("company", peer["id"], 1), peer)
        self.assertEqual(registry.resolve_headers("company", peer["id"], 1), {"Authorization": values["header_value"]})
        replacement = registry.create("company", values, actor_id="owner")
        self.assertNotEqual(replacement["id"], peer["id"])
        with self.assertRaises(RaiseException):
            with restarted.transaction() as connection:
                connection.execute("UPDATE core_remote_agents SET deleted=false WHERE tenant_id=%s AND id=%s",
                                   ("company", peer["id"]))
        self.assertEqual(registry.list("company"), [replacement])

    def seed(self, database, version):
        tenant, owner = ("default", "anonymous") if version == 12 else ("company", "company-owners")
        tables = ["core_runs", "core_a2a_tasks", "core_artifacts"]
        with database.transaction() as connection:
            for run in ("root-a", "root-b"):
                task = a2a_pb2.Task(id=run, context_id="chat")
                task.status.state = a2a_pb2.TASK_STATE_WORKING
                connection.execute("""INSERT INTO core_a2a_tasks
                    (task_id,owner,tenant,context_id,state,payload) VALUES (%s,%s,%s,'chat',%s,%s)""",
                    (run, owner, tenant, task.status.state, task.SerializeToString()))
                connection.execute("""INSERT INTO core_runs
                    (run_id,task_id,context_id,tenant_id,owner_id,state,request,snapshot,created_at,updated_at)
                    VALUES (%s,%s,'chat',%s,%s,'WAITING',%s,%s,1700000000,1700000001)""",
                    (run, run, tenant, owner, Jsonb({"prompt": "Сохранить ё и\nисходные данные"}),
                     Jsonb({"version": 1, "private_marker": "original", "dispatch_outcome": "unknown"})))
            if version >= 13:
                tables += ["core_chats", "core_root_messages"]
                connection.execute("""INSERT INTO core_chats
                    (tenant_id,context_id,owner_id,latest_root_run_id) VALUES (%s,'chat',%s,'root-a')""",
                    (tenant, owner))
                connection.execute("""INSERT INTO core_root_messages
                    (tenant_id,actor_id,message_id,request_digest,owner_id,context_id,task_id)
                    VALUES (%s,'owner-actor','message','original-digest',%s,'chat','root-a')""",
                    (tenant, owner))
            if version >= 14:
                tables.append("core_waits")
                for run, outcome, resolved, applied in (
                    ("root-a", None, None, None),
                    ("root-b", Jsonb({"status": "rejected"}), 1700000100, 1700000101),
                ):
                    connection.execute("""INSERT INTO core_waits
                        (wait_id,run_id,tenant_id,owner_id,context_id,generation,kind,source_id,
                         subject,continuation,deadline,outcome,resolved_at,applied_at,created_at)
                        VALUES (%s,%s,%s,%s,'chat',1,'tool_approval','exact-call',%s,%s,
                                1700086400,%s,%s,%s,1700000000)""",
                        ("wait-" + run, run, tenant, owner, Jsonb({"digest": "frozen-call"}),
                         Jsonb({"version": 1, "call_id": "exact-call"}), outcome, resolved, applied))
            if version >= 15:
                tables += ["core_owner_settings", "core_tool_policies"]
                connection.execute("""INSERT INTO core_owner_settings
                    (tenant_id,revision,hitl_timeout_seconds) VALUES (%s,7,1234)""", (tenant,))
                connection.execute("""INSERT INTO core_tool_policies
                    (tenant_id,canonical_name,origin,mode,guardrails_exempt,revision)
                    VALUES (%s,'core_terminal_exec','builtin','deny',true,4)""", (tenant,))
            if version >= 16:
                tables.append("core_chat_file_batches")
                batch = "a" * 32
                connection.execute("""INSERT INTO core_chat_file_batches
                    (batch_id,tenant_id,actor_id,message_id,request_digest,created_at,storage_key,
                     lease_owner,lease_token,lease_expires_at,state,manifest,context_id,owner_id,task_id,run_id)
                    VALUES (%s,%s,'owner-actor','message','original-digest',1700000000,%s,
                            'old-worker','old-lease',1700000100,'accepted_quarantine',%s,'chat',%s,'root-a','root-a')""",
                    (batch, tenant, batch, Jsonb({"schema_version": 1,
                     "entries": [{"name": "report.txt", "digest": "sha256:" + "b" * 64}]}), owner))
            if version >= 20:
                tables.append("core_background_tasks")
                connection.execute("""INSERT INTO core_background_tasks
                    (id,owner_run_id,tenant_id,kind,state,required,recoverable,contract,checkpoint,created_at,updated_at)
                    VALUES ('remote','root-a',%s,'remote_a2a','working',true,true,%s,%s,1700000000,1700000001)""",
                    (tenant, Jsonb({"version": 1, "peer_revision": 1}),
                     Jsonb({"version": 1, "send_committed": True, "remote_task_id": "remote-task",
                            "deadline": 1700086400, "next_poll_at": 1700000300})))
            if version >= 22:
                tables.append("core_cron_schedules")
                connection.execute("""INSERT INTO core_cron_schedules
                    (id,tenant_id,context_id,owner_id,storage_version,revision,prompt,expression,
                     timezone,enabled,deleted,created_at,updated_at)
                    VALUES ('schedule',%s,'chat',%s,1,3,'Не терять исправления','0 18 * * *',
                            'Europe/Moscow',false,true,to_timestamp(1700000000),to_timestamp(1700000001))""",
                    (tenant, owner))
        peer = None
        if version >= 19:
            peer = PostgresRemoteRegistry(database, self.key).create(tenant,
                {"name": "peer", "url": "https://peer.example/a2a", "description": "Migration",
                 "enabled": True, "header_name": "X-Api-Key", "header_value": "migration-secret-canary"},
                actor_id="owner-actor")
            tables += ["core_remote_agents", "core_remote_agent_revisions"]
        content = "Transport bytes: ё\n\x00".encode()
        artifact = PostgresArtifactStore(database, self.temporary.name).put(
            tenant, content, media_type="application/octet-stream", provenance={"task": "root-a"})
        return tables, tenant, peer, artifact, content

    @staticmethod
    def rows(database, tables):
        with database.pool.connection() as connection:
            return {table: connection.execute(SQL("SELECT * FROM {}").format(Identifier(table))).fetchall()
                    for table in tables}

    def upgrade_and_restart(self, database, url, tables):
        before = self.rows(database, tables)
        database.migrate()
        database.migrate()
        database.close()
        reopened = PostgresDatabase(url, min_size=0, max_size=1)
        self.addCleanup(reopened.close)
        reopened.verify_schema()
        after = self.rows(reopened, tables)
        for table in tables:
            columns = before[table][0].keys()
            self.assertCountEqual(before[table], [{key: row[key] for key in columns} for row in after[table]], table)
        return reopened

    def test_pre_enterprise_upgrade_keeps_legacy_scope_and_blocks_old_or_unknown_builds(self):
        database, url = self.database_at(12)
        tables, tenant, _, artifact, content = self.seed(database, 12)
        reopened = self.upgrade_and_restart(database, url, tables)
        self.assertEqual(PostgresArtifactStore(reopened, self.temporary.name).get(tenant, artifact.id)[1], content)
        with reopened.pool.connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM core_chats").fetchone()["n"], 0)
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM core_root_messages").fetchone()["n"], 0)
        before = self.rows(reopened, tables)
        old = {v: sql for v, sql in database_module.MIGRATIONS.items() if v <= 12}
        with patch.object(database_module, "SCHEMA_VERSION", 12), \
                patch.dict(database_module.MIGRATIONS, old, clear=True):
            with self.assertRaises(CoreError) as error:
                reopened.verify_schema()
            self.assertEqual(error.exception.code, "DATABASE_SCHEMA_MISMATCH")
            with self.assertRaises(CoreError) as error:
                reopened.migrate()
            self.assertEqual(error.exception.code, "DATABASE_SCHEMA_UNSUPPORTED")
        self.assertEqual(self.rows(reopened, tables), before)
        with reopened.transaction() as connection:
            connection.execute("INSERT INTO core_schema_migrations(version) VALUES (%s)",
                               (database_module.SCHEMA_VERSION + 1,))
        with self.assertRaises(CoreError) as error:
            reopened.migrate()
        self.assertEqual(error.exception.code, "DATABASE_SCHEMA_UNSUPPORTED")
        self.assertEqual(self.rows(reopened, tables), before)

    def test_every_enterprise_schema_upgrade_preserves_admitted_state_and_blobs(self):
        for version in range(13, database_module.SCHEMA_VERSION):
            with self.subTest(source_schema=version):
                database, url = self.database_at(version)
                tables, tenant, peer, artifact, content = self.seed(database, version)
                reopened = self.upgrade_and_restart(database, url, tables)
                stored, retrieved = PostgresArtifactStore(reopened, self.temporary.name).get(tenant, artifact.id)
                self.assertEqual(stored, artifact)
                self.assertEqual(retrieved, content)
                with self.assertRaises(CoreError) as error:
                    PostgresArtifactStore(reopened, self.temporary.name).get("other-company", artifact.id)
                self.assertEqual(error.exception.code, "NOT_FOUND")
                if version >= 15:
                    with reopened.pool.connection() as connection:
                        settings = connection.execute("SELECT * FROM core_owner_settings").fetchone()
                    self.assertEqual(settings["hitl_timeout_seconds"], 1234)
                    self.assertEqual(settings["attachment_limit_bytes"], 25_000_000)
                    self.assertEqual(settings["remote_timeout_seconds"], 86_400)
                    self.assertEqual(settings["remote_poll_interval_seconds"], 300)
                if peer is not None:
                    registry = PostgresRemoteRegistry(reopened, self.key)
                    self.assertEqual(registry.get_revision(tenant, peer["id"], 1), peer)
                    self.assertEqual(registry.resolve_headers(tenant, peer["id"], 1),
                                     {"X-Api-Key": "migration-secret-canary"})

    @unittest.skipUnless(os.getenv("TEST_POSTGRES_CONTAINER"), "TEST_POSTGRES_CONTAINER is required")
    def test_quiesced_backup_restores_schema_state_blobs_and_encrypted_peer_before_cutover_writes(self):
        database, url = self.database_at(23)
        tables, tenant, peer, artifact, content = self.seed(database, 23)
        before = self.rows(database, tables)
        connection = conninfo_to_dict(url)
        schema = connection["options"].removeprefix("-c search_path=")
        command = ["docker", "exec", "-i", os.environ["TEST_POSTGRES_CONTAINER"]]
        target = ["--username", connection["user"], "--dbname", connection["dbname"]]
        with tempfile.TemporaryDirectory() as backup_folder:
            backup = Path(backup_folder)
            dump = subprocess.run(command + ["pg_dump", *target, "--format=custom", "--schema", schema],
                                  check=True, capture_output=True, timeout=30)
            (backup / "database.dump").write_bytes(dump.stdout)
            shutil.copytree(self.temporary.name, backup / "volume")
            upgraded = self.upgrade_and_restart(database, url, tables)
            upgraded.close()
            with self.admin.transaction() as admin:
                admin.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(schema)))
            shutil.rmtree(self.temporary.name)
            subprocess.run(command + ["pg_restore", *target, "--exit-on-error"],
                           input=(backup / "database.dump").read_bytes(), check=True,
                           capture_output=True, timeout=30)
            shutil.copytree(backup / "volume", self.temporary.name)
        restored = PostgresDatabase(url, min_size=0, max_size=1)
        self.addCleanup(restored.close)
        self.assertEqual(restored.schema_version(), 23)
        after = self.rows(restored, tables)
        for table in tables:
            self.assertCountEqual(after[table], before[table], table)
        with self.assertRaises(CoreError) as error:
            restored.verify_schema()
        self.assertEqual(error.exception.code, "DATABASE_SCHEMA_MISMATCH")
        with patch.object(database_module, "SCHEMA_VERSION", 23):
            restored.verify_schema()
        stored, retrieved = PostgresArtifactStore(restored, self.temporary.name).get(tenant, artifact.id)
        self.assertEqual((stored, retrieved), (artifact, content))
        with self.assertRaises(CoreError) as error:
            PostgresArtifactStore(restored, self.temporary.name).get("other-company", artifact.id)
        self.assertEqual(error.exception.code, "NOT_FOUND")
        registry = PostgresRemoteRegistry(restored, self.key)
        self.assertEqual(registry.get_revision(tenant, peer["id"], 1), peer)
        self.assertEqual(registry.resolve_headers(tenant, peer["id"], 1),
                         {"X-Api-Key": "migration-secret-canary"})
        self.upgrade_and_restart(restored, url, tables)
