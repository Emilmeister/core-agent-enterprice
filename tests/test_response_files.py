import hashlib
import os
import stat
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from core_agent.artifacts import InMemoryArtifactStore, PostgresArtifactStore
from core_agent.database import PostgresDatabase
from core_agent.errors import CoreError
from core_agent.workspace import ChatWorkspaces, WorkspaceBinding


class ResponseFileContract:
    def setup_files(self):
        from core_agent.response_files import ResponseFileService

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspaces = ChatWorkspaces(self.directory.name)
        self.binding = WorkspaceBinding(str(uuid.uuid4()), "owner", str(uuid.uuid4()))
        self.workspace = self.workspaces.workspace(self.binding)
        self.options = dict(task_id=str(uuid.uuid4()), run_id=str(uuid.uuid4()), limit_bytes=25_000_000)
        self.service = ResponseFileService(self.workspaces, self.store)

    def prepare(self, paths, **options):
        return self.service.prepare(self.binding, paths, **{**self.options, **options})

    def load(self, refs, **options):
        pinned = {"limit_bytes": refs[0]["limit_bytes"]} if refs else {}
        return self.service.load(self.binding, refs, **{**self.options, **pinned, **options})

    def write(self, name, content):
        target = self.workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def assert_code(self, code, callback):
        with self.assertRaises(CoreError) as error:
            callback()
        self.assertEqual(error.exception.code, code)
        return error.exception

    def test_ordered_private_refs_public_receipts_and_frozen_reconstruction(self):
        first = self.write("reports/a.pdf", b"same bytes")
        second = self.write("other/a.txt", b"same bytes")
        refs = self.prepare(["reports/a.pdf", "other/a.txt"])
        self.assertIsInstance(refs, tuple)
        self.assertEqual(len(refs), 2)
        private_keys = {"schema_version", "limit_bytes", "tenant_id", "owner_id", "context_id", "task_id", "run_id",
                        "file_id", "blob_id", "name", "media_type", "size_bytes", "sha256"}
        public_keys = {"file_id", "name", "media_type", "size_bytes", "sha256"}
        for ref in refs:
            self.assertEqual(set(ref), private_keys)
            self.assertEqual(ref["schema_version"], 1)
            self.assertEqual(ref["limit_bytes"], 25_000_000)
            self.assertEqual(ref["tenant_id"], self.binding.tenant_id)
            self.assertEqual(ref["owner_id"], self.binding.owner_id)
            self.assertEqual(ref["context_id"], self.binding.context_id)
            self.assertEqual(ref["task_id"], self.options["task_id"])
            self.assertEqual(ref["run_id"], self.options["run_id"])
            self.assertEqual(ref["sha256"], hashlib.sha256(b"same bytes").hexdigest())
            uuid.UUID(ref["file_id"])
        self.assertNotEqual(refs[0]["file_id"], refs[1]["file_id"])
        self.assertEqual([ref["name"] for ref in refs], ["a.pdf", "a.txt"])
        self.assertEqual([ref["media_type"] for ref in refs], ["application/pdf", "text/plain"])
        receipts = self.service.receipts(refs)
        self.assertEqual([set(item) for item in receipts], [public_keys, public_keys])
        first.write_bytes(b"edited")
        second.unlink()
        self.assertEqual(self.load(refs), ((refs[0], b"same bytes"), (refs[1], b"same bytes")))
        self.write("new.bin", b"new")
        replacement = self.prepare(["new.bin"])
        self.assertEqual(self.load(replacement)[0][1], b"new")
        self.assertEqual([content for _, content in self.load(refs)], [b"same bytes", b"same bytes"])
        with patch.object(self.workspaces, "open_file", side_effect=AssertionError("clear opens no source")):
            self.assertEqual(self.prepare([]), ())
            self.assertEqual(self.load(()), ())

    def test_empty_files_and_exact_aggregate_boundary(self):
        self.write("empty", b"")
        self.write("a", b"1234")
        self.write("b", b"567")
        refs = self.prepare(["empty", "a", "b"], limit_bytes=7)
        self.assertEqual([content for _, content in self.load(refs, limit_bytes=7)], [b"", b"1234", b"567"])
        self.write("b", b"5678")
        with patch.object(self.store, "put", wraps=self.store.put) as put:
            error = self.assert_code("ATTACHMENTS_TOO_LARGE", lambda: self.prepare(["a", "b"], limit_bytes=7))
            self.assertEqual(error.data, {"allowed_bytes": 7, "actual_bytes": 8})
            put.assert_not_called()
        self.assertEqual([content for _, content in self.load(refs)], [b"", b"1234", b"567"])
        with patch.object(self.store, "get", wraps=self.store.get) as get:
            error = self.assert_code("ATTACHMENTS_TOO_LARGE", lambda: self.load(refs, limit_bytes=6))
            self.assertEqual(error.data, {"allowed_bytes": 6, "actual_bytes": 7})
            get.assert_not_called()

    def test_default_25mb_single_and_aggregate_limits(self):
        large = self.write("large", b"")
        with large.open("wb") as stream:
            stream.truncate(25_000_000)
        refs = self.prepare(["large"])
        self.assertEqual(len(self.load(refs)[0][1]), 25_000_000)
        self.write("one", b"1")
        with patch.object(self.store, "put", wraps=self.store.put) as put:
            error = self.assert_code("ATTACHMENTS_TOO_LARGE", lambda: self.prepare(["large", "one"]))
            self.assertEqual(error.data, {"allowed_bytes": 25_000_000, "actual_bytes": 25_000_001})
            put.assert_not_called()

    def test_same_bytes_metadata_overwrite_never_grants_other_scope(self):
        self.write("own.pdf", b"shared")
        own = self.prepare(["own.pdf"])
        other = replace(self.binding, owner_id="other", context_id="other-chat")
        (self.workspaces.workspace(other) / "foreign.txt").write_bytes(b"shared")
        foreign = self.service.prepare(other, ["foreign.txt"], **self.options)
        # In-memory content IDs are deliberately shared, including overwritten metadata.
        self.assertEqual(self.load(own)[0], (own[0], b"shared"))
        for key in ("tenant_id", "owner_id", "context_id", "task_id", "run_id"):
            changed = {**own[0], key: "foreign"}
            with self.subTest(key=key), patch.object(self.store, "get", wraps=self.store.get) as get:
                self.assert_code("FILE_NOT_FOUND", lambda: self.load((own[0], changed)))
                get.assert_not_called()
        self.assert_code("FILE_NOT_FOUND", lambda: self.load(foreign))

    def test_selection_pins_one_bounded_ceiling_for_the_whole_manifest(self):
        self.write("a", b"1234")
        self.write("b", b"567")
        refs = self.prepare(["a", "b"], limit_bytes=7)
        self.assertEqual([ref["limit_bytes"] for ref in refs], [7, 7])
        self.assertEqual([content for _, content in self.load(refs)], [b"1234", b"567"])
        for malformed in (({**refs[0], "limit_bytes": 0}, refs[1]),
                          (refs[0], {**refs[1], "limit_bytes": 8}),
                          ({**refs[0], "limit_bytes": 2147483648}, refs[1])):
            with self.subTest(refs=malformed), patch.object(self.store, "get", wraps=self.store.get) as get:
                self.assert_code("ARTIFACT_INTEGRITY_FAILED", lambda: self.load(malformed, limit_bytes=7))
                get.assert_not_called()
        with patch.object(self.store, "get", wraps=self.store.get) as get:
            self.assert_code("ARTIFACT_INTEGRITY_FAILED", lambda: self.load(refs, limit_bytes=8))
            get.assert_not_called()

    def test_manifest_validation_never_reads_source_or_blobs_and_returns_copies(self):
        self.write("file", b"frozen")
        refs = self.prepare(["file"])
        with patch.object(self.store, "get", side_effect=AssertionError("manifest validation reads no blobs")), \
                patch.object(self.workspaces, "open_file", side_effect=AssertionError("manifest validation opens no source")):
            validated = self.service.validate_refs(self.binding, refs, **self.options)
            self.assertIsInstance(validated, tuple)
            self.assertEqual(validated, refs)
            self.assertIsNot(validated[0], refs[0])
            self.assert_code("FILE_NOT_FOUND", lambda: self.service.validate_refs(self.binding,
                (refs[0], {**refs[0], "task_id": "foreign"}), **self.options))
        validated[0]["name"] = "changed"
        self.assertEqual(refs[0]["name"], "file")

    def test_nested_colon_filename_round_trips_without_relaxing_input_url_rejection(self):
        self.write("reports/result:final.txt", b"frozen")
        refs = self.prepare(["reports/result:final.txt"])
        self.assertEqual(refs[0]["name"], "result:final.txt")
        self.assertEqual(self.load(refs), ((refs[0], b"frozen"),))
        for path in ("https://example.com/file", "file:private", "reports/../file"):
            self.assert_code("INVALID_FILE_PATH", lambda: self.prepare([path]))

    def test_invalid_paths_and_nonregular_sources_preserve_old_set(self):
        source = self.write("valid", b"original")
        old = self.prepare(["valid"])
        for paths in (["/etc/passwd"], ["https://example.com/a"], ["../valid"], ["a//b"],
                      ["a/./b"], ["a\\b"], ["\0"], ["a\nb"], [""], [123], ["valid", "valid"]):
            with self.subTest(paths=paths), patch.object(self.store, "put", wraps=self.store.put) as put:
                self.assert_code("INVALID_FILE_PATH", lambda: self.prepare(paths))
                put.assert_not_called()
        (self.workspace / "folder").mkdir()
        (self.workspace / "link").symlink_to(source)
        os.mkfifo(self.workspace / "fifo")
        for path in ("missing", "folder", "link", "fifo"):
            self.assert_code("FILE_NOT_FOUND", lambda: self.prepare(["valid", path]))
        os.link(source, self.workspace / "hardlink")
        self.assert_code("FILE_NOT_FOUND", lambda: self.prepare(["hardlink"]))
        self.assertEqual(source.read_bytes(), b"original")
        self.assertEqual(self.load(old)[0][1], b"original")

    def test_changed_read_and_later_replacement_reject_entire_capture(self):
        self.write("old", b"old")
        old = self.prepare(["old"])
        first = self.write("first", b"first")
        second = self.write("second", b"second")
        opened = []
        original_open = self.workspaces.open_file
        for mutation in ("same-size", "growth", "replacement", "hardlink"):
            self.write("first", b"first")
            second.write_bytes(b"second")
            fired = False

            def capture(binding, path):
                nonlocal fired
                stream, size = original_open(binding, path)
                opened.append(stream)
                if path == "second" and not fired:
                    original_read = stream.read

                    def read(*args):
                        nonlocal fired
                        result = original_read(*args)
                        if not fired:
                            fired = True
                            if mutation == "replacement":
                                first.rename(self.workspace / "moved")
                                first.write_bytes(b"first")
                            elif mutation == "hardlink":
                                os.link(first, self.workspace / "hardlink")
                            else:
                                first.write_bytes(b"other" if mutation == "same-size" else b"growing")
                        return result

                    stream.read = read
                return stream, size

            with self.subTest(mutation=mutation), patch.object(self.workspaces, "open_file", side_effect=capture), \
                    patch.object(self.store, "put", wraps=self.store.put) as put:
                self.assert_code("FILE_CHANGED", lambda: self.prepare(["first", "second"]))
                put.assert_not_called()
            self.assertTrue(all(stream.closed for stream in opened))
            (self.workspace / "hardlink").unlink(missing_ok=True)
            self.assertEqual(self.load(old)[0][1], b"old")

    def test_read_and_store_failures_close_sources_and_keep_old_shared_blobs(self):
        self.write("old", b"old")
        old = self.prepare(["old"])
        self.write("a", b"old")
        self.write("b", b"next")
        opened = []
        original_open = self.workspaces.open_file

        def unreadable(binding, path):
            stream, size = original_open(binding, path)
            opened.append(stream)
            if path == "b":
                stream.read = lambda *_: (_ for _ in ()).throw(OSError("read failed"))
            return stream, size

        with patch.object(self.workspaces, "open_file", side_effect=unreadable), \
                patch.object(self.store, "put", wraps=self.store.put) as put:
            self.assert_code("FILE_READ_FAILED", lambda: self.prepare(["a", "b"]))
            put.assert_not_called()
        self.assertTrue(all(stream.closed for stream in opened))
        original_put = self.store.put
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("storage failed")
            return original_put(*args, **kwargs)

        with patch.object(self.store, "put", side_effect=fail_second), \
                patch.object(self.store, "delete", side_effect=AssertionError("shared blobs must survive")):
            self.assert_code("FILE_READ_FAILED", lambda: self.prepare(["a", "b"]))
        self.assertEqual(self.load(old)[0][1], b"old")
        self.assertEqual((self.workspace / "a").read_bytes(), b"old")
        self.assertEqual((self.workspace / "b").read_bytes(), b"next")

    def test_corrupt_or_missing_last_blob_never_returns_a_prefix(self):
        self.write("first", b"first")
        self.write("last", b"last")
        refs = self.prepare(["first", "last"])
        get = self.store.get

        def corrupt(tenant, identifier, **kwargs):
            metadata, content = get(tenant, identifier, **kwargs)
            return metadata, b"evil" if identifier == refs[-1]["blob_id"] else content

        with patch.object(self.store, "get", side_effect=corrupt):
            self.assert_code("ARTIFACT_INTEGRITY_FAILED", lambda: self.load(refs))
        self.store.delete(self.binding.tenant_id, refs[-1]["blob_id"])
        self.assert_code("ARTIFACT_INTEGRITY_FAILED", lambda: self.load(refs))

    def test_invalid_manifest_and_limits_are_rejected_before_blob_lookup(self):
        self.write("file", b"bytes")
        refs = self.prepare(["file"])
        invalid = ({**refs[0], "schema_version": 2}, {**refs[0], "size_bytes": -1},
                   {**refs[0], "size_bytes": True}, {**refs[0], "sha256": "../bad"},
                   {**refs[0], "name": "../path"}, {**refs[0], "file_id": "bad"},
                   {**refs[0], "extra": "untrusted"})
        for ref in invalid:
            with self.subTest(ref=ref), patch.object(self.store, "get", wraps=self.store.get) as get:
                self.assert_code("ARTIFACT_INTEGRITY_FAILED", lambda: self.load((ref,)))
                get.assert_not_called()
        self.assert_code("ARTIFACT_INTEGRITY_FAILED", lambda: self.load((refs[0], refs[0])))
        for limit in (0, -1, True, "25", 2147483648):
            self.assert_code("CONFIG_INVALID", lambda: self.prepare(["file"], limit_bytes=limit))


class MemoryResponseFileTests(ResponseFileContract, unittest.TestCase):
    def setUp(self):
        self.store = InMemoryArtifactStore()
        self.setup_files()


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "set TEST_DATABASE_URL for outgoing file persistence proofs")
class PostgresResponseFileTests(ResponseFileContract, unittest.TestCase):
    def setUp(self):
        self.database = PostgresDatabase(os.environ["TEST_DATABASE_URL"], min_size=0, max_size=1)
        self.addCleanup(self.database.close)
        self.blobs = tempfile.TemporaryDirectory()
        self.addCleanup(self.blobs.cleanup)
        self.store = PostgresArtifactStore(self.database, self.blobs.name)
        self.setup_files()

    def test_load_uses_borrowed_pool_one_transaction_without_closing_it(self):
        self.write("file", b"persisted")
        refs = self.prepare(["file"])
        with self.database.transaction() as connection:
            with patch.object(self.database.pool, "connection", side_effect=AssertionError("nested pool checkout")):
                self.assertEqual(self.load(refs, connection=connection)[0][1], b"persisted")
            self.assertEqual(connection.execute("SELECT 1 AS value").fetchone()["value"], 1)


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "set TEST_DATABASE_URL for blob durability proofs")
class PostgresArtifactSupportTests(unittest.TestCase):
    def setUp(self):
        self.database = PostgresDatabase(os.environ["TEST_DATABASE_URL"], min_size=0, max_size=1)
        self.addCleanup(self.database.close)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = PostgresArtifactStore(self.database, self.directory.name, max_bytes=1024)
        self.tenant = str(uuid.uuid4())

    def put(self, content=b"value"):
        return self.store.put(self.tenant, content, media_type="application/octet-stream", provenance={"run_id": self.tenant})

    def test_empty_blob_and_borrowed_connection(self):
        stored = self.put(b"")
        with self.database.transaction() as connection:
            with patch.object(self.database.pool, "connection", side_effect=AssertionError("nested checkout")):
                self.assertEqual(self.store.get(self.tenant, stored.id, connection=connection), (stored, b""))
            self.assertEqual(connection.execute("SELECT 1 AS value").fetchone()["value"], 1)

    def test_existing_and_corrupt_blob_reads_are_bounded(self):
        stored = self.put()
        with patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")):
            self.assertEqual(self.put(), stored)
            self.assertEqual(self.store.get(self.tenant, stored.id)[1], b"value")
            with self.store._blob(stored.digest).open("wb") as stream:
                stream.truncate(100_000_000)
            with self.assertRaises(CoreError) as error:
                self.store.get(self.tenant, stored.id)
            self.assertEqual(error.exception.code, "ARTIFACT_INTEGRITY_FAILED")
            with self.assertRaises(CoreError) as error:
                self.put()
            self.assertEqual(error.exception.code, "ARTIFACT_INTEGRITY_FAILED")

    def test_file_and_directory_fsync_precede_metadata_commit(self):
        events = []
        original_fsync = os.fsync
        original_transaction = self.database.transaction

        def fsync(descriptor):
            events.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
            original_fsync(descriptor)

        @contextmanager
        def transaction():
            events.append("metadata")
            with original_transaction() as connection:
                yield connection

        with patch("core_agent.artifacts.os.fsync", side_effect=fsync), \
                patch.object(self.database, "transaction", side_effect=transaction):
            stored = self.put()
            self.assertEqual(events, ["file", "directory", "metadata"])
            events.clear()
            self.assertEqual(self.put(), stored)
            self.assertEqual(events, ["file", "directory", "metadata"])

    def test_directory_fsync_failure_creates_no_metadata_grant(self):
        original_fsync = os.fsync

        def fsync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError("directory durability failed")
            return original_fsync(descriptor)

        with patch("core_agent.artifacts.os.fsync", side_effect=fsync):
            with self.assertRaises(OSError):
                self.put()
        with self.database.transaction() as connection:
            self.assertEqual(connection.execute("SELECT count(*) AS count FROM core_artifacts WHERE tenant_id = %s", (self.tenant,)).fetchone()["count"], 0)
