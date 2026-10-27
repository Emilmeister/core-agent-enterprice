import hashlib
import os
import unittest
import uuid
from unittest.mock import patch

from psycopg.types.json import Jsonb

from core_agent.artifacts import PostgresArtifactStore
from core_agent.chat_files import PostgresChatFileStore
from core_agent.errors import CoreError
from core_agent.s3_ledger import PostgresS3BlobStore
from tests import test_s3_storage


@unittest.skipUnless(os.getenv("TEST_S3_ENDPOINT") and os.getenv("TEST_DATABASE_URL"),
                     "set TEST_S3_ENDPOINT and TEST_DATABASE_URL for staging/GC proofs")
class S3LedgerTests(unittest.TestCase):
    def setUp(self):
        test_s3_storage.S3PostgresArtifactTests.setUp(self)
        self.now = 1_000_000.0
        self.ledger = PostgresS3BlobStore(self.database, self.blobs, clock=lambda: self.now)
        self.store = PostgresArtifactStore(self.database, self.directory.name,
            blob_store=self.ledger, max_bytes=1024, cache_max_bytes=1024)

    def rows(self):
        with self.database.pool.connection() as connection:
            return connection.execute("SELECT * FROM core_s3_blobs WHERE tenant_id = %s ORDER BY updated_at",
                (self.tenant,)).fetchall()

    def incoming_batch(self, digest, *, state="accepted_quarantine", tenant=None):
        tenant = tenant or self.tenant
        identifier = uuid.uuid4().hex
        manifest = {"schema_version": 1, "storage": {"version": 1, "backend": "s3"},
            "metadata": {"original": "nul\x00surrogate\ud800"},
            "entries": [{"actual_name": "input", "sha256": digest, "size_bytes": 8}]}
        envelope = PostgresChatFileStore._encode_manifest(manifest)
        accepted = state not in {"staging", "rejected"}
        with self.database.transaction() as connection:
            if accepted:
                connection.execute("""INSERT INTO core_runs
                    (run_id,task_id,context_id,tenant_id,owner_id,state,request,snapshot,created_at,updated_at)
                    VALUES (%s,%s,%s,%s,'owner','RUNNING','{}','{}',%s,%s)""",
                    (identifier, identifier, identifier, tenant, self.now, self.now))
                connection.execute("INSERT INTO core_chats (tenant_id,context_id,owner_id) VALUES (%s,%s,'owner')",
                    (tenant, identifier))
                connection.execute("""INSERT INTO core_a2a_tasks (task_id,owner,tenant,context_id,state,payload)
                    VALUES (%s,'owner',%s,%s,0,%s)""", (identifier, tenant, identifier, b"{}"))
                if tenant == self.tenant:
                    self.ledger.pin(connection, [digest])
            connection.execute("""INSERT INTO core_chat_file_batches
                (batch_id,tenant_id,actor_id,message_id,request_digest,created_at,storage_key,
                 lease_owner,lease_token,lease_expires_at,state,manifest,context_id,owner_id,task_id,run_id,
                 decision_ref,published_at)
                VALUES (%s,%s,'actor','message','request',%s,%s,'actor','lease',%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (identifier, tenant, self.now, identifier, self.now + 200_000, state, Jsonb(envelope),
                 identifier if accepted else None, "owner" if accepted else None,
                 identifier if accepted else None, identifier if accepted else None,
                 "review" if state in {"accepted_ready", "published", "excluded"} else None,
                 self.now if state == "published" else None))
        return identifier, manifest

    def test_incoming_reference_survives_review_publication_exclusion_and_shared_digest(self):
        digest = self.ledger.put(b"incoming")
        for state in ("accepted_quarantine", "accepted_ready", "published", "excluded"):
            self.incoming_batch(digest, state=state)
        self.now += 100_000
        self.assertEqual(self.ledger.sweep(), 0)
        self.assertEqual(self.ledger.get(digest, 8), b"incoming")
        self.assertEqual(len(self.rows()), 1)

    def test_incoming_staging_lease_and_rejected_cleanup_bound_retention(self):
        for state in ("staging", "rejected"):
            with self.subTest(state=state):
                digest = self.ledger.put(b"incoming")
                identifier, _ = self.incoming_batch(digest, state=state)
                self.now += 100_000
                self.ledger.sweep()
                self.assertEqual(self.ledger.get(digest, 8), b"incoming")
                with self.database.transaction() as connection:
                    if state == "staging":
                        connection.execute("UPDATE core_chat_file_batches SET lease_expires_at=0 WHERE batch_id=%s",
                            (identifier,))
                    else:
                        connection.execute("UPDATE core_chat_file_batches SET cleaned_at=%s WHERE batch_id=%s",
                            (self.now, identifier))
                self.assertEqual(self.ledger.sweep(), 0)
                self.now += 86_401
                self.assertGreaterEqual(self.ledger.sweep(), 1)
                self.assertFalse(any(row["state"] == "live" for row in self.rows()))

    def test_foreign_incoming_reference_does_not_pin_this_tenants_blob(self):
        digest = self.ledger.put(b"incoming")
        self.incoming_batch(digest, tenant="foreign-tenant")
        self.now += 100_000
        self.assertEqual(self.ledger.sweep(), 1)

    def test_incoming_native_envelope_preserves_escaped_metadata_and_reference(self):
        digest = self.ledger.put(b"incoming")
        identifier, manifest = self.incoming_batch(digest)
        with self.database.pool.connection() as connection:
            row = connection.execute("SELECT * FROM core_chat_file_batches WHERE batch_id=%s", (identifier,)).fetchone()
        self.assertEqual(row["manifest"]["storage"], {"version": 1, "backend": "s3"})
        self.assertEqual(row["manifest"]["entries"], [{"actual_name": "input", "sha256": digest, "size_bytes": 8}])
        self.assertEqual(PostgresChatFileStore._decode(row)["manifest"], manifest)

    def test_intent_precedes_network_without_borrowed_connection(self):
        original = self.blobs.put

        def upload(content, **kwargs):
            row, = self.rows()  # Pool size one proves the uploader holds no DB connection.
            self.assertEqual(row["state"], "staging")
            self.assertEqual(row["generation"], kwargs["object_id"])
            with self.database.transaction() as connection:
                with self.assertRaises(CoreError):
                    self.ledger.pin(connection, [row["digest"]])
            return original(content, **kwargs)

        with patch.object(self.blobs, "put", side_effect=upload):
            digest = self.ledger.put(b"staged")
        self.assertEqual(self.ledger.get(digest, 6), b"staged")
        self.assertEqual(self.rows()[0]["state"], "live")

    def test_repeated_put_reuses_generation_and_skips_upload(self):
        digest = self.ledger.put(b"same")
        generation = self.rows()[0]["generation"]
        with patch.object(self.blobs.client, "put_object", side_effect=AssertionError("duplicate upload")):
            self.assertEqual(self.ledger.put(b"same"), digest)
        self.assertEqual(self.rows()[0]["generation"], generation)

    def test_active_artifact_is_pinned_and_not_collected(self):
        stored = self.store.put(self.tenant, b"active", media_type="text/plain", provenance={})
        self.now += 100_000
        self.assertEqual(self.ledger.sweep(), 0)
        self.assertEqual(self.ledger.last_sweep_scanned, 1)
        self.assertEqual(self.ledger.get(stored.digest[7:], 6), b"active")
        self.store.delete(self.tenant, stored.id)
        self.assertEqual(self.ledger.sweep(), 0)
        self.now += 86_401
        self.assertEqual(self.ledger.sweep(), 1)
        self.assertEqual(self.rows()[0]["state"], "deleted")

    def test_workspace_reference_and_staging_lease_block_collection(self):
        digest = self.ledger.put(b"workspace")
        self.assertEqual(self.ledger.sweep(), 0)
        with self.database.transaction() as connection:
            self.ledger.pin(connection, [digest])
            connection.execute("""INSERT INTO core_workspace_manifests
                (tenant_id,owner_id,context_id,revision,manifest) VALUES (%s,'owner','context',1,%s)""",
                (self.tenant, Jsonb({"entries": {"file": {"type": "file", "sha256": digest}}})))
        self.now += 100_000
        self.assertEqual(self.ledger.sweep(), 0)
        self.assertEqual(self.ledger.get(digest, 9), b"workspace")

    def test_failed_upload_stays_unpublished_until_staging_and_retention_expire(self):
        with patch.object(self.blobs, "put", side_effect=CoreError("STORAGE_UNAVAILABLE", retryable=True)):
            with self.assertRaises(CoreError):
                self.ledger.put(b"unknown")
        row, = self.rows()
        self.assertEqual(row["state"], "staging")
        self.assertEqual(self.ledger.sweep(), 0)
        with self.database.transaction() as connection:
            with self.assertRaises(CoreError):
                self.ledger.pin(connection, [row["digest"]])
        self.now += 100_000
        self.assertEqual(self.ledger.sweep(), 1)

    def test_unknown_delete_cannot_remove_new_generation(self):
        digest = self.ledger.put(b"replace")
        old_generation = self.rows()[0]["generation"]
        self.now += 100_000
        with patch.object(self.blobs, "delete", side_effect=CoreError("STORAGE_UNAVAILABLE", "PRIVATE", retryable=True)):
            with self.assertLogs("core_agent.s3_ledger", level="WARNING") as captured:
                self.assertEqual(self.ledger.sweep(), 0)
        self.assertIn("STORAGE_UNAVAILABLE", "\n".join(captured.output))
        self.assertNotIn("PRIVATE", "\n".join(captured.output))
        self.assertEqual(self.rows()[0]["state"], "deleting")
        self.store.put(self.tenant, b"replace", media_type="text/plain", provenance={})
        live, = [row for row in self.rows() if row["state"] == "live"]
        self.assertNotEqual(live["generation"], old_generation)
        self.now += 100_000
        self.assertEqual(self.ledger.sweep(), 1)
        self.blobs.delete(digest, object_id=old_generation)  # Delayed old DELETE remains harmless.
        self.assertEqual(self.ledger.get(digest, 7), b"replace")

    def test_deleted_generation_tombstone_cleans_late_put(self):
        digest = self.ledger.put(b"late")
        generation = self.rows()[0]["generation"]
        self.now += 100_000
        self.assertEqual(self.ledger.sweep(), 1)
        self.blobs.put(b"late", object_id=generation)
        self.now += 100_000
        self.assertEqual(self.ledger.sweep(), 1)
        with self.assertRaises(CoreError) as error:
            self.blobs.get(digest, 4, object_id=generation)
        self.assertEqual(error.exception.code, "NOT_FOUND")

    def test_missing_reference_pin_and_unbounded_gc_are_rejected(self):
        with self.database.transaction() as connection:
            with self.assertRaises(CoreError):
                self.ledger.pin(connection, [hashlib.sha256(b"missing").hexdigest()])
        for limit in (0, 101, True):
            with self.assertRaises(CoreError):
                self.ledger.sweep(limit=limit)
