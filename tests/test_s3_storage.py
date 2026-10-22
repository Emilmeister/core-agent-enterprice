import base64
import hashlib
import io
import os
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from botocore.response import StreamingBody
from botocore.stub import Stubber
from psycopg.conninfo import make_conninfo
from psycopg.sql import SQL, Identifier

from core_agent.errors import CoreError


class S3BlobTests(unittest.TestCase):
    def setUp(self):
        from core_agent import s3_storage

        self.store = s3_storage.S3BlobStore(
            endpoint_url="https://s3.example.test", bucket="private-files", region="test",
            access_key_id="test-key", secret_access_key="test-secret", tenant_id="tenant-a",
            max_bytes=1024,
        )
        self.addCleanup(self.store.close)
        self.stub = Stubber(self.store.client)
        self.stub.activate()
        self.addCleanup(self.stub.deactivate)
        self.content = b"immutable bytes"
        self.digest = hashlib.sha256(self.content).hexdigest()
        self.params = {"Bucket": "private-files", "Key": self.store.key(self.digest)}
        self.checksum = base64.b64encode(bytes.fromhex(self.digest)).decode()

    def head(self, **overrides):
        response = {"ContentLength": len(self.content), "ChecksumSHA256": self.checksum, **overrides}
        self.stub.add_response("head_object", response, {**self.params, "ChecksumMode": "ENABLED"})

    def missing(self):
        self.stub.add_client_error("head_object", "NoSuchKey", http_status_code=404,
            expected_params={**self.params, "ChecksumMode": "ENABLED"})

    def test_existing_identical_bytes_do_not_upload(self):
        self.head()
        self.assertEqual(self.store.put(self.content), self.digest)
        self.stub.assert_no_pending_responses()

    def test_startup_check_is_read_only_and_redacts_bucket_failure(self):
        self.stub.add_response("head_bucket", {}, {"Bucket": self.store.bucket})
        self.store.check()
        self.stub.add_client_error("head_bucket", "AccessDenied", service_message="test-secret", http_status_code=403)
        with self.assertRaises(CoreError) as error:
            self.store.check()
        self.assertEqual(error.exception.code, "STORAGE_UNAVAILABLE")
        self.assertNotIn("test-secret", str(error.exception))
        self.stub.assert_no_pending_responses()

    def test_missing_bytes_use_conditional_put_and_verified_receipt(self):
        self.missing()
        self.stub.add_response("put_object", {}, {**self.params, "Body": self.content,
            "ContentLength": len(self.content), "IfNoneMatch": "*", "ChecksumSHA256": self.checksum})
        self.head()
        self.assertEqual(self.store.put(self.content), self.digest)
        self.stub.assert_no_pending_responses()

    def test_unknown_put_is_reconciled_on_same_object(self):
        self.missing()
        self.stub.add_client_error("put_object", "RequestTimeout", http_status_code=400)
        self.head()
        self.assertEqual(self.store.put(self.content), self.digest)
        self.stub.assert_no_pending_responses()

    def test_unconfirmed_put_fails_without_leaking_provider_details(self):
        self.missing()
        self.stub.add_client_error("put_object", "RequestTimeout", service_message="test-secret")
        self.missing()
        with self.assertRaises(CoreError) as error:
            self.store.put(self.content)
        self.assertEqual(error.exception.code, "STORAGE_UNAVAILABLE")
        self.assertNotIn("test-secret", str(error.exception))
        self.assertTrue(error.exception.retryable)

    def test_checksum_corruption_fails_without_overwrite(self):
        self.head(ChecksumSHA256=base64.b64encode(b"x" * 32).decode())
        with self.assertRaises(CoreError) as error:
            self.store.put(self.content)
        self.assertEqual(error.exception.code, "ARTIFACT_INTEGRITY_FAILED")
        self.stub.assert_no_pending_responses()

    def test_missing_server_checksum_requires_verified_download(self):
        self.stub.add_response("head_object", {"ContentLength": len(self.content)},
            {**self.params, "ChecksumMode": "ENABLED"})
        body = io.BytesIO(self.content)
        self.stub.add_response("get_object", {"ContentLength": len(self.content),
            "Body": StreamingBody(body, len(self.content))}, self.params)
        self.assertEqual(self.store.put(self.content), self.digest)
        self.assertTrue(body.closed)
        self.stub.assert_no_pending_responses()

    def test_get_bounds_and_checks_content_and_closes_body(self):
        body = io.BytesIO(b"x" * len(self.content))
        self.stub.add_response("get_object", {"ContentLength": len(self.content),
            "Body": StreamingBody(body, len(self.content))}, self.params)
        with self.assertRaises(CoreError) as error:
            self.store.get(self.digest, len(self.content))
        self.assertEqual(error.exception.code, "ARTIFACT_INTEGRITY_FAILED")
        self.assertTrue(body.closed)

    def test_invalid_digest_size_and_excess_content_fail_before_io(self):
        for digest, size in (("../escape", 1), (self.digest, -1), (self.digest, True), (self.digest, 1025)):
            with self.subTest(digest=digest, size=size), self.assertRaises(CoreError):
                self.store.get(digest, size)
        with self.assertRaises(CoreError) as error:
            self.store.put(b"x" * 1025)
        self.assertEqual(error.exception.code, "ARTIFACT_TOO_LARGE")

    def test_tenant_keys_are_disjoint_and_credentials_are_explicit(self):
        self.assertIn(hashlib.sha256(b"tenant-a").hexdigest(), self.params["Key"])
        with patch.dict(os.environ, {"AWS_ACCESS_KEY_ID": "ambient", "AWS_SECRET_ACCESS_KEY": "ambient"}):
            from core_agent.s3_storage import S3BlobStore
            with self.assertRaises(CoreError):
                S3BlobStore(endpoint_url="https://s3.example.test", bucket="private-files", region="test",
                    access_key_id="", secret_access_key="", tenant_id="tenant-b")


@unittest.skipUnless(os.getenv("TEST_S3_ENDPOINT"), "set TEST_S3_ENDPOINT for local S3 transport proof")
class LocalS3IntegrationTests(unittest.TestCase):
    def test_immutable_roundtrip_dedup_and_corruption(self):
        from core_agent.s3_storage import S3BlobStore

        store = S3BlobStore(endpoint_url=os.environ["TEST_S3_ENDPOINT"], bucket=os.environ["TEST_S3_BUCKET"],
            region="us-east-1", access_key_id=os.environ["TEST_S3_ACCESS_KEY_ID"],
            secret_access_key=os.environ["TEST_S3_SECRET_ACCESS_KEY"], tenant_id=uuid.uuid4().hex)
        self.addCleanup(store.close)
        content = b"local transport proof"
        digest = store.put(content)
        self.addCleanup(store.delete, digest)
        self.assertEqual(store.get(digest, len(content)), content)
        with patch.object(store.client, "put_object", side_effect=AssertionError("unchanged content uploaded")):
            self.assertEqual(store.put(content), digest)
        store.client.put_object(Bucket=store.bucket, Key=store.key(digest), Body=b"corrupt")
        with self.assertRaises(CoreError) as error:
            store.get(digest, len(content))
        self.assertEqual(error.exception.code, "ARTIFACT_INTEGRITY_FAILED")


@unittest.skipUnless(os.getenv("TEST_S3_ENDPOINT") and os.getenv("TEST_DATABASE_URL"),
                     "set TEST_S3_ENDPOINT and TEST_DATABASE_URL for S3 artifact durability proof")
class S3PostgresArtifactTests(unittest.TestCase):
    def setUp(self):
        from core_agent.artifacts import PostgresArtifactStore
        from core_agent.database import PostgresDatabase
        from core_agent.s3_storage import S3BlobStore

        self.tenant = uuid.uuid4().hex
        admin = PostgresDatabase(os.environ["TEST_DATABASE_URL"], min_size=0, max_size=1)
        self.addCleanup(admin.close)
        schema = "s3_" + self.tenant
        with admin.transaction() as connection:
            connection.execute(SQL("CREATE SCHEMA {}").format(Identifier(schema)))

        def drop_schema():
            with admin.transaction() as connection:
                connection.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(schema)))

        self.addCleanup(drop_schema)
        self.database = PostgresDatabase(make_conninfo(os.environ["TEST_DATABASE_URL"],
            options="-c search_path=" + schema), min_size=0, max_size=1)
        self.addCleanup(self.database.close)
        self.database.migrate()
        self.blobs = S3BlobStore(endpoint_url=os.environ["TEST_S3_ENDPOINT"], bucket=os.environ["TEST_S3_BUCKET"],
            region="us-east-1", access_key_id=os.environ["TEST_S3_ACCESS_KEY_ID"],
            secret_access_key=os.environ["TEST_S3_SECRET_ACCESS_KEY"], tenant_id=self.tenant)
        self.addCleanup(self.blobs.close)
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = PostgresArtifactStore(self.database, self.directory.name, blob_store=self.blobs,
                                          max_bytes=1024, cache_max_bytes=1024)

    def test_replacement_cache_hydrates_then_reads_under_borrowed_transaction(self):
        from core_agent.artifacts import PostgresArtifactStore

        stored = self.store.put(self.tenant, b"persisted", media_type="text/plain", provenance={})
        self.addCleanup(self.blobs.delete, stored.digest[7:])
        replacement = PostgresArtifactStore(self.database, self.directory.name, blob_store=self.blobs,
                                           max_bytes=1024, cache_max_bytes=1024)
        with self.database.transaction() as connection:
            with self.assertRaises(CoreError) as error:
                replacement.get(self.tenant, stored.id, connection=connection)
            self.assertEqual(error.exception.code, "STORAGE_NOT_PREFETCHED")
            self.assertEqual(error.exception.data, {})
        replacement.prefetch(self.tenant, [stored.id])
        with self.database.transaction() as connection:
            with patch.object(self.blobs, "get", side_effect=AssertionError("S3 under DB transaction")):
                self.assertEqual(replacement.get(self.tenant, stored.id, connection=connection), (stored, b"persisted"))
        self.assertEqual(list(Path(self.directory.name).rglob("*")), [])

    def test_delete_revokes_metadata_but_defers_blob_gc(self):
        stored = self.store.put(self.tenant, b"persisted", media_type="text/plain", provenance={})
        self.addCleanup(self.blobs.delete, stored.digest[7:])
        with patch.object(self.blobs, "delete", side_effect=AssertionError("unfenced S3 deletion")):
            self.store.delete(self.tenant, stored.id)
            self.store.purge_unreferenced([stored.digest])
        with self.assertRaises(CoreError) as error:
            self.store.get(self.tenant, stored.id)
        self.assertEqual(error.exception.code, "NOT_FOUND")

    def test_tenant_mismatch_never_reaches_blob_transport(self):
        with patch.object(self.blobs, "put", side_effect=AssertionError("cross-tenant write")):
            with self.assertRaises(CoreError):
                self.store.put("other", b"value", media_type="text/plain", provenance={})

    def test_request_prefetch_survives_eviction_and_rechecks_metadata(self):
        from core_agent.artifacts import PostgresArtifactStore

        stored = self.store.put(self.tenant, b"persisted", media_type="text/plain", provenance={})
        self.addCleanup(self.blobs.delete, stored.digest[7:])
        replacement = PostgresArtifactStore(self.database, self.directory.name, blob_store=self.blobs,
                                           max_bytes=1024, cache_max_bytes=1024)
        attempts = []

        def operation():
            attempts.append(1)
            with self.database.transaction() as connection:
                loaded = replacement.get(self.tenant, stored.id, connection=connection)
                replacement._cache.clear()
                replacement._cache_bytes = 0
                self.assertEqual(replacement.get(self.tenant, stored.id, connection=connection), loaded)
                return loaded

        self.assertEqual(replacement.run_prefetched(operation), (stored, b"persisted"))
        self.assertEqual(len(attempts), 2)

    def test_projection_memory_slot_is_bounded_and_released_after_failure(self):
        with patch.object(self.store._prefetch_slots, "acquire", return_value=False):
            with self.assertRaises(CoreError) as error:
                self.store.run_prefetched(lambda: self.fail("overlapping projection"))
        self.assertEqual(error.exception.code, "STORAGE_UNAVAILABLE")
        with self.assertRaisesRegex(ValueError, "rollback"):
            self.store.run_prefetched(lambda: (_ for _ in ()).throw(ValueError("rollback")))
        self.assertEqual(self.store.run_prefetched(lambda: 7), 7)
