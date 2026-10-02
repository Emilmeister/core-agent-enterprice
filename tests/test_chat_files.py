import base64
import os
import tempfile
import threading
import unittest
import uuid
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from a2a.types import Task, TaskStatus, TaskState

from core_agent.chat_files import (
    ChatFileService, MemoryChatFileStore, PostgresChatFileStore, _rename,
)
from core_agent.database import PostgresDatabase
from core_agent.errors import CoreError
from core_agent.workflow import InMemoryWorkflowStore, PostgresWorkflowStore, WorkflowRecord
from core_agent.workspace import ChatWorkspaces, WorkspaceBinding


class ChatFileContract:
    def setup_files(self):
        self.now = 1000.0
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspaces = ChatWorkspaces(self.directory.name)
        self.binding = WorkspaceBinding(str(uuid.uuid4()), "external-owner", str(uuid.uuid4()))
        self.record = self.workflow.create(WorkflowRecord(
            str(uuid.uuid4()), str(uuid.uuid4()), self.binding.context_id,
            self.binding.tenant_id, self.binding.owner_id, None, "RUNNING", 1,
            {"prompt": "files"}, {},
        ))
        self.service = ChatFileService(self.store, self.workspaces, clock=lambda: self.now)
        self.addCleanup(self.service.close)
        self.options = dict(tenant_id=self.binding.tenant_id, actor_id="actor", message_id="message",
                            request_digest="original-message-digest", source="test")

    def prepare(self, files=None):
        return self.service.prepare(files or [{"name": "report.pdf", "raw": b"one"},
                                             {"name": "empty", "raw": b""}], **self.options)

    def bind(self, batch):
        with (self.database.transaction() if hasattr(self, "database") else nullcontext()) as conn:
            return self.service.bind(batch["batch_id"], self.binding, task_id=self.record.task_id,
                run_id=self.record.run_id, actor_id="actor", message_id="message",
                request_digest=self.options["request_digest"], lease_token=batch["lease_token"], connection=conn)

    def accept(self, batch):
        self.bind(batch)
        self.service.record_decision(batch["batch_id"], self.binding, decision_ref="guardrail-decision", allow=True)

    def target(self, batch):
        return self.workspaces.workspace(self.binding) / "attachments" / batch["batch_id"]

    def test_complete_private_accept_decision_publish_restart(self):
        batch = self.prepare()
        self.assertEqual(batch["manifest"]["total_bytes"], 3)
        self.assertFalse(self.target(batch).exists())
        self.bind(batch)
        with self.assertRaises(CoreError) as error:
            self.service.publish(batch["batch_id"], self.binding)
        self.assertEqual(error.exception.code, "FILE_BATCH_NOT_READY")
        self.assertFalse(self.target(batch).exists())
        self.service.record_decision(batch["batch_id"], self.binding, decision_ref="decision", allow=True)
        manifest = self.service.publish(batch["batch_id"], self.binding)
        self.assertEqual((self.target(batch) / "report.pdf").read_bytes(), b"one")
        self.assertEqual((self.target(batch) / "empty").read_bytes(), b"")
        restarted = ChatFileService(self.store, self.workspaces, clock=lambda: self.now)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.publish(batch["batch_id"], self.binding), manifest)
        row = self.store.get(batch["batch_id"], self.binding.tenant_id)
        self.assertEqual(row["state"], "published")
        self.assertEqual(row["created_at"], 1000)
        self.assertEqual(len(list(self.target(batch).parent.iterdir())), 1)

    def test_limit_is_aggregate_exact_decimal_and_empty_is_valid(self):
        for files, expected in (([{"raw": b"x" * 13_000_000}] * 2, 26_000_000),
                                ([{"base64": base64.b64encode(b"x" * 25_000_001).decode()}], 25_000_001)):
            with self.assertRaises(CoreError) as error:
                self.prepare(files)
            self.assertEqual(error.exception.code, "ATTACHMENTS_TOO_LARGE")
            self.assertEqual(error.exception.data, {"allowed_bytes": 25_000_000, "actual_bytes": expected})
            self.assertEqual(os.listdir(self.service.uploads), [])
        batch = self.prepare([{"raw": b"x" * 25_000_000}, {"raw": b""}])
        self.assertEqual(batch["manifest"]["total_bytes"], 25_000_000)
        self.assertEqual(len(batch["manifest"]["entries"]), 2)

    def test_strict_decode_whole_batch_rejection_preserves_old_files(self):
        old = self.workspaces.workspace(self.binding) / "old"
        old.write_bytes(b"keep")
        for file in ({"base64": "not base64"}, {"base64": "Zh=="}, {"base64": "Zg==\n"},
                     {"raw": "text"}, {"uri": "https://example.test/private"},
                     {"raw": b"x", "base64": "eA=="}):
            with self.subTest(file=file), self.assertRaises(CoreError):
                self.prepare([{"raw": b"first"}, file])
            self.assertEqual(os.listdir(self.service.uploads), [])
        self.assertEqual(old.read_bytes(), b"keep")
        self.assertFalse((old.parent / "attachments").exists())

    def test_names_reserved_under_chat_scope_and_originals_retained(self):
        attachments = self.workspaces.workspace(self.binding) / "attachments"
        attachments.mkdir()
        (attachments / "report.pdf").mkdir()
        (attachments / "report_2.pdf").symlink_to("missing")
        first = self.prepare([{"name": "report.pdf", "raw": b"1"}] * 2)
        second = self.prepare([{"name": "report.pdf", "raw": b"2"}])
        first = self.bind(first)
        second = self.bind(second)
        self.assertEqual([e["actual_name"] for e in first["manifest"]["entries"]], ["report_3.pdf", "report_4.pdf"])
        self.assertEqual(second["manifest"]["entries"][0]["actual_name"], "report_5.pdf")
        nasty = ["../x", "C:\\x", "\0bad\n", "..", "", "отчёт.pdf", ".manifest.json", r"\u0000.txt"]
        self.options["request_metadata"] = {"control\0key": "original\0value", "literal": r"\u0000"}
        batch = self.prepare([{"name": name, "raw": b""} for name in nasty])
        for entry, name in zip(batch["manifest"]["entries"], nasty):
            self.assertEqual(entry["original_name"], name)
            self.assertNotIn("/", entry["actual_name"])
            self.assertNotIn("\\", entry["actual_name"])
            self.assertNotIn("\0", entry["actual_name"])
            self.assertFalse(entry["actual_name"].startswith("."))
        batch = self.bind(batch)
        self.service.record_decision(batch["batch_id"], self.binding, decision_ref="names-reviewed", allow=True)
        manifest = self.service.publish(batch["batch_id"], self.binding)
        self.assertEqual(manifest["metadata"], self.options["request_metadata"])
        restarted = ChatFileService(self.store, self.workspaces, clock=lambda: self.now)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.publish(batch["batch_id"], self.binding), manifest)
        for index, name in enumerate(nasty):
            result = restarted.owner_download(batch["batch_id"], self.binding, index,
                run_id=self.record.run_id, task_id=self.record.task_id)
            self.assertEqual(result["entry"]["original_name"], name)

    def test_concurrent_batches_reserve_distinct_names_under_chat_lock(self):
        barrier = threading.Barrier(2)
        results, errors = [], []

        def upload():
            try:
                batch = self.prepare([{"name": "report.pdf", "raw": b"x"}])
                barrier.wait(timeout=5)
                results.append(self.bind(batch))
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=upload) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual({r["manifest"]["entries"][0]["actual_name"] for r in results}, {"report.pdf", "report_2.pdf"})

    def test_failed_final_names_clean_bytes_and_caller_records_rejection(self):
        batch = self.prepare()
        rename = os.rename

        def fail(source, target, **kwargs):
            if source.startswith(".input"):
                raise OSError("rename failed")
            return rename(source, target, **kwargs)

        with patch("core_agent.chat_files.os.rename", side_effect=fail), self.assertRaises(CoreError):
            self.bind(batch)
        self.assertEqual(os.listdir(self.service.uploads), [])
        self.service.reject(batch["batch_id"], self.binding.tenant_id, batch["lease_token"])
        self.assertEqual(self.store.get(batch["batch_id"], self.binding.tenant_id)["state"], "rejected")

    def test_write_and_fsync_failure_remove_all_unaccepted_bytes(self):
        import core_agent.chat_files as module
        original = module._write
        calls = 0

        def failing(fd, name, content, *args):
            nonlocal calls
            if not name.startswith(".manifest"):
                calls += 1
                if calls == 2:
                    raise OSError("disk full")
            return original(fd, name, content, *args)

        with patch.object(module, "_write", side_effect=failing), self.assertRaises(CoreError):
            self.prepare()
        self.assertEqual(os.listdir(self.service.uploads), [])
        original_fsync = os.fsync
        failed = False

        def fsync(fd):
            nonlocal failed
            if not failed and os.fstat(fd).st_size == 3:
                failed = True
                raise OSError("fsync failed")
            return original_fsync(fd)

        with patch.object(module.os, "fsync", side_effect=fsync), self.assertRaises(CoreError):
            self.prepare()
        self.assertEqual(os.listdir(self.service.uploads), [])

    def test_recovery_after_rename_before_published_commit_and_integrity_failure(self):
        batch = self.prepare()
        self.accept(batch)
        import core_agent.chat_files as module

        def crash(*args):
            _rename(*args)
            raise OSError("process died after rename")

        with patch.object(module, "_rename", side_effect=crash), self.assertRaises(CoreError):
            self.service.publish(batch["batch_id"], self.binding)
        self.assertEqual(self.store.get(batch["batch_id"], self.binding.tenant_id)["state"], "accepted_ready")
        self.assertEqual((self.target(batch) / "report.pdf").read_bytes(), b"one")
        restarted = ChatFileService(self.store, self.workspaces, clock=lambda: self.now)
        self.addCleanup(restarted.close)
        restarted.publish(batch["batch_id"], self.binding)
        (self.target(batch) / "report.pdf").write_bytes(b"bad")
        with self.assertRaises(CoreError) as error:
            restarted.publish(batch["batch_id"], self.binding)
        self.assertEqual(error.exception.code, "ARTIFACT_INTEGRITY_FAILED")
        self.assertTrue(self.target(batch).exists())

    def test_later_denial_excludes_ready_batch_before_or_after_uncommitted_rename(self):
        import core_agent.chat_files as module

        for renamed in (False, True):
            with self.subTest(uncommitted_rename=renamed):
                batch = self.prepare()
                self.accept(batch)
                before = self.store.get(batch["batch_id"], self.binding.tenant_id)
                if renamed:
                    def crash(*args):
                        _rename(*args)
                        raise OSError("process died after rename")

                    with patch.object(module, "_rename", side_effect=crash), self.assertRaises(CoreError):
                        self.service.publish(batch["batch_id"], self.binding)
                    self.assertTrue(self.target(batch).exists())
                self.service.record_decision(batch["batch_id"], self.binding,
                    decision_ref="later-exact-denial", allow=False)
                row = self.store.get(batch["batch_id"], self.binding.tenant_id)
                self.assertEqual((row["state"], row["decision_ref"]), ("excluded", "later-exact-denial"))
                self.assertEqual(row["manifest"], before["manifest"])
                self.assertFalse(self.target(batch).exists())
                self.assertEqual(self.service.owner_download(batch["batch_id"], self.binding, 0,
                    run_id=self.record.run_id, task_id=self.record.task_id)["content"], b"one")
                restarted = ChatFileService(self.store, self.workspaces, clock=lambda: self.now)
                self.addCleanup(restarted.close)
                for publish in (self.service, restarted):
                    with self.assertRaises(CoreError) as error:
                        publish.publish(batch["batch_id"], self.binding)
                    self.assertEqual(error.exception.code, "FILE_BATCH_NOT_READY")
                with self.assertRaises(CoreError):
                    self.service.record_decision(batch["batch_id"], self.binding,
                        decision_ref="new-allow", allow=True)

    def test_denial_retry_syncs_retraction_after_rename_before_exclusion_commit(self):
        import core_agent.chat_files as module

        batch = self.prepare()
        self.accept(batch)

        def crash(*args):
            _rename(*args)
            raise OSError("process died after rename")

        with patch.object(module, "_rename", side_effect=crash), self.assertRaises(CoreError):
            self.service.publish(batch["batch_id"], self.binding)
        with patch.object(module, "_rename", side_effect=crash), self.assertRaises(CoreError):
            self.service.record_decision(batch["batch_id"], self.binding,
                decision_ref="later-denial", allow=False)
        self.assertEqual(self.store.get(batch["batch_id"], self.binding.tenant_id)["state"], "accepted_ready")
        self.assertFalse(self.target(batch).exists())
        parents = (self.target(batch).parent.stat(), os.fstat(self.service.quarantine))
        expected = {(entry.st_dev, entry.st_ino) for entry in parents}
        synced = set()
        fsync = os.fsync

        def sync(descriptor):
            entry = os.fstat(descriptor)
            synced.add((entry.st_dev, entry.st_ino))
            return fsync(descriptor)

        with patch.object(module.os, "fsync", side_effect=sync):
            self.service.record_decision(batch["batch_id"], self.binding,
                decision_ref="later-denial", allow=False)
        self.assertTrue(expected <= synced)
        self.assertEqual(self.store.get(batch["batch_id"], self.binding.tenant_id)["state"], "excluded")
        self.assertEqual(self.service.owner_download(batch["batch_id"], self.binding, 0,
            run_id=self.record.run_id, task_id=self.record.task_id)["content"], b"one")

    def test_no_replace_target_or_symlink_and_wrong_scope(self):
        batch = self.prepare()
        self.accept(batch)
        self.target(batch).mkdir()
        with self.assertRaises(CoreError):
            self.service.publish(batch["batch_id"], self.binding)
        self.assertEqual(list(self.target(batch).iterdir()), [])
        self.target(batch).rmdir()
        outside = Path(self.directory.name) / "outside"
        outside.mkdir()
        self.target(batch).symlink_to(outside, target_is_directory=True)
        with self.assertRaises(CoreError):
            self.service.publish(batch["batch_id"], self.binding)
        self.assertEqual(list(outside.iterdir()), [])
        for binding in (WorkspaceBinding("other", self.binding.owner_id, self.binding.context_id),
                        WorkspaceBinding(self.binding.tenant_id, "other", self.binding.context_id)):
            with self.assertRaises(CoreError) as error:
                self.service.publish(batch["batch_id"], binding)
            self.assertEqual(error.exception.code, "FILE_BATCH_NOT_FOUND")

    def test_cancel_blocks_stale_allow_and_accepted_content_is_preserved(self):
        batch = self.prepare()
        self.accept(batch)
        self.workflow.request_cancel(self.record.run_id, tenant_id=self.binding.tenant_id, owner_id=self.binding.owner_id)
        with self.assertRaises(CoreError) as error:
            self.service.publish(batch["batch_id"], self.binding)
        self.assertEqual(error.exception.code, "FILE_BATCH_TASK_CLOSED")
        self.now += 100000
        self.assertEqual(self.service.sweep(startup=True)["cleaned"], 0)
        self.assertFalse(self.target(batch).exists())

    def test_manifest_immutable_scope_and_cas_lease(self):
        batch = self.prepare()
        with self.assertRaises(CoreError):
            self.service.renew(batch["batch_id"], self.binding.tenant_id, "wrong")
        self.bind(batch)
        with self.assertRaises(CoreError):
            with self.store.locked(batch["batch_id"], self.binding.tenant_id) as (row, _):
                row["manifest"]["entries"][0]["sha256"] = "changed"
        with self.assertRaises(CoreError):
            self.service.reject(batch["batch_id"], self.binding.tenant_id, batch["lease_token"])
        self.assertEqual(self.store.get(batch["batch_id"], self.binding.tenant_id)["manifest"]["entries"][0]["size_bytes"], 3)

    def test_sweep_original_age_bound_backlog_and_live_lease(self):
        accepted = self.prepare()
        self.bind(accepted)
        first, second, active = self.prepare(), self.prepare(), self.prepare()
        self.now += 299
        self.service.renew(active["batch_id"], self.binding.tenant_id, active["lease_token"])
        self.now += 2
        self.assertEqual(self.service.sweep()["cleaned"], 0)
        self.assertEqual(self.service.sweep(startup=True, limit=1), {"cleaned": 1, "has_more": True})
        self.assertEqual(self.service.sweep(startup=True, limit=1)["cleaned"], 1)
        self.assertEqual(self.store.get(first["batch_id"], self.binding.tenant_id)["created_at"], 1000)
        self.assertEqual(self.store.get(second["batch_id"], self.binding.tenant_id)["state"], "rejected")
        self.assertIn(active["batch_id"], os.listdir(self.service.uploads))
        self.assertIn(accepted["batch_id"], os.listdir(self.service.uploads))


    def test_sealed_read_checks_every_scope_and_survives_terminal_history(self):
        batch = self.prepare([{"name": "private.txt", "media_type": "text/plain", "raw": b"private bytes",
                               "metadata": {"title": "untrusted entry"}}])
        args = dict(run_id=self.record.run_id, task_id=self.record.task_id)
        with self.assertRaises(CoreError):
            self.service.owner_download(batch["batch_id"], self.binding, 0, **args)
        self.bind(batch)
        for binding, overrides in ((WorkspaceBinding("other", self.binding.owner_id, self.binding.context_id), {}),
                                   (WorkspaceBinding(self.binding.tenant_id, "other", self.binding.context_id), {}),
                                   (WorkspaceBinding(self.binding.tenant_id, self.binding.owner_id, "other"), {}),
                                   (self.binding, {"run_id": "other"}), (self.binding, {"task_id": "other"})):
            with self.subTest(binding=binding, overrides=overrides), self.assertRaises(CoreError) as caught:
                self.service.owner_download(batch["batch_id"], binding, 0, **(args | overrides))
            self.assertEqual(caught.exception.code, "FILE_BATCH_NOT_FOUND")
        for index in (-1, 1, True, "0"):
            with self.subTest(index=index), self.assertRaises(CoreError):
                self.service.owner_download(batch["batch_id"], self.binding, index, **args)
        self.workflow.transition(self.record.run_id, tenant_id=self.binding.tenant_id, owner_id=self.binding.owner_id,
            expected_version=self.record.version, state="COMPLETED", snapshot={}, event_kind="task.completed")
        result = self.service.owner_download(batch["batch_id"], self.binding, 0, **args)
        self.assertEqual(result["content"], b"private bytes")
        self.assertEqual(result["manifest"]["entries"][0]["metadata"], {"title": "untrusted entry"})
        self.assertNotIn(self.directory.name, str(result))

    def test_extraction_requires_complete_text_and_preserves_all_review_metadata(self):
        batch = self.prepare([{"name": "text.txt", "media_type": "text/plain; charset=utf-8", "raw": "текст".encode()},
                              {"name": "binary.txt", "media_type": "text/plain", "raw": b"text\x00"},
                              {"name": "empty.txt", "media_type": "text/plain", "raw": b""},
                              {"name": "bad.txt", "media_type": "text/plain", "raw": b"\xff"},
                              {"name": "pdf", "media_type": "application/pdf", "raw": b"some text"}])
        bound = self.bind(batch)
        result = self.service.review_material(batch["batch_id"], self.binding, run_id=self.record.run_id, task_id=self.record.task_id)
        self.assertEqual(result["manifest"], bound["manifest"])
        self.assertEqual([item["complete"] for item in result["documents"]], [True, False, False, False, False])
        self.assertEqual(result["documents"][0]["text"], "текст")
        self.assertFalse(self.target(batch).exists())

    def test_trusted_message_limit_does_not_mutate_service_or_existing_batch(self):
        batch = self.service.prepare([{"raw": b"12345", "media_type": "text/plain"}], limit_bytes=5, **self.options)
        self.bind(batch)
        with self.assertRaises(CoreError) as caught:
            self.service.prepare([{"raw": b"12345"}], limit_bytes=4, **self.options)
        self.assertEqual(caught.exception.code, "ATTACHMENTS_TOO_LARGE")
        self.assertEqual(caught.exception.data, {"allowed_bytes": 4, "actual_bytes": 5})
        self.assertEqual(self.service.limit_bytes, 25_000_000)
        self.service.limit_bytes = 1
        result = self.service.owner_download(batch["batch_id"], self.binding, 0,
            run_id=self.record.run_id, task_id=self.record.task_id)
        self.assertEqual(result["content"], b"12345")

    def test_scoped_private_read_rejects_corruption_and_symlinks(self):
        batch = self.prepare([{"name": "private.txt", "media_type": "text/plain", "raw": b"safe"}])
        self.bind(batch)
        target = self.workspaces.root / "private/uploads" / batch["batch_id"] / "private.txt"
        target.write_bytes(b"evil")
        with self.assertRaises(CoreError) as caught:
            self.service.owner_download(batch["batch_id"], self.binding, 0,
                run_id=self.record.run_id, task_id=self.record.task_id)
        self.assertEqual(caught.exception.code, "ARTIFACT_INTEGRITY_FAILED")
        target.unlink()
        target.symlink_to(self.workspaces.root / "missing")
        with self.assertRaises(CoreError) as caught:
            self.service.review_material(batch["batch_id"], self.binding,
                run_id=self.record.run_id, task_id=self.record.task_id)
        self.assertEqual(caught.exception.code, "ARTIFACT_INTEGRITY_FAILED")
    def test_company_scope_precedes_expired_upload_limit(self):
        foreign_options = self.options | {"tenant_id": "foreign"}
        foreign = self.service.prepare([{"raw": b"foreign"}], **foreign_options)
        self.now += 1
        own = self.prepare()
        self.assertEqual(own["manifest"]["tenant_id"], self.binding.tenant_id)
        self.now += 301
        self.assertEqual(self.service.sweep(startup=True, limit=1,
                         tenant_id=self.binding.tenant_id)["cleaned"], 1)
        self.assertEqual(self.store.get(foreign["batch_id"], "foreign")["state"], "staging")
        self.assertIn(foreign["batch_id"], os.listdir(self.service.uploads))
        self.assertNotIn(own["batch_id"], os.listdir(self.service.uploads))
        self.assertEqual(self.service.sweep(startup=True)["cleaned"], 1)


class MemoryChatFileTests(ChatFileContract, unittest.TestCase):
    def test_scoped_rowless_cleanup_retains_legacy_foreign_and_global_references(self):
        import json
        own, legacy, referenced = self.prepare(), self.prepare(), self.prepare()
        foreign = self.service.prepare([{"raw": b"foreign"}], **(self.options | {"tenant_id": "foreign"}))
        for batch in (own, legacy, referenced, foreign):
            del self.store.rows[batch["batch_id"]]
        legacy_path = Path(self.directory.name) / "private/uploads" / legacy["batch_id"] / ".manifest.json"
        manifest = json.loads(legacy_path.read_text())
        manifest.pop("tenant_id")
        legacy_path.write_text(json.dumps(manifest))
        self.workflow.create(WorkflowRecord(str(uuid.uuid4()), str(uuid.uuid4()), "foreign-context",
            "foreign", "foreign-owner", None, "RUNNING", 1,
            {}, {"file_batch_id": referenced["batch_id"]}))
        self.now += 24 * 3600
        cleaned = 0
        for _ in range(5):
            result = self.service.sweep(tenant_id=self.binding.tenant_id, limit=1)
            cleaned += result["cleaned"]
            if not result["has_more"]:
                break
        self.assertEqual(cleaned, 1)
        remaining = os.listdir(self.service.uploads)
        self.assertNotIn(own["batch_id"], remaining)
        for batch in (legacy, foreign, referenced):
            self.assertIn(batch["batch_id"], remaining)
        self.assertEqual(self.service.sweep()["cleaned"], 2)
        self.assertIn(referenced["batch_id"], os.listdir(self.service.uploads))

    def test_scoped_rowless_scan_is_bounded_and_reaches_own_batch(self):
        foreign = [self.service.prepare([{"raw": b"foreign"}],
            **(self.options | {"tenant_id": "foreign"})) for _ in range(3)]
        own = self.prepare()
        for batch in (*foreign, own):
            del self.store.rows[batch["batch_id"]]
        self.now += 24 * 3600
        with patch("core_agent.chat_files.os.open", wraps=os.open) as opened:
            result = self.service.sweep(tenant_id=self.binding.tenant_id, limit=1)
        self.assertEqual(sum(call.args[0] == ".manifest.json" for call in opened.call_args_list), 1)
        cleaned = result["cleaned"]
        for _ in range(4):
            result = self.service.sweep(tenant_id=self.binding.tenant_id, limit=1)
            cleaned += result["cleaned"]
            if not result["has_more"]:
                break
        self.assertEqual(cleaned, 1)
        self.assertNotIn(own["batch_id"], os.listdir(self.service.uploads))
        for batch in foreign:
            self.assertIn(batch["batch_id"], os.listdir(self.service.uploads))

    def setUp(self):
        self.workflow = InMemoryWorkflowStore()
        self.store = MemoryChatFileStore(self.workflow, lambda binding: None)
        self.setup_files()

    def test_upload_lease_duration_must_be_finite_positive_number(self):
        for duration in (0, -1, True, False, None, "300", float("nan"),
                         float("inf"), float("-inf"), 10**1000):
            with self.subTest(duration=duration), self.assertRaises(CoreError) as error:
                ChatFileService(self.store, self.workspaces, lease_seconds=duration)
            self.assertEqual(error.exception.code, "CONFIG_INVALID")

    def test_directory_descriptor_cleanup_on_failures_and_repeated_close(self):
        self.workspaces.workspace(self.binding)
        batch = self.prepare()
        self.accept(batch)
        self.service.publish(batch["batch_id"], self.binding)
        opened = set()
        original_open, original_dup, original_close = os.open, os.dup, os.close

        def track(operation):
            def call(*args, **kwargs):
                descriptor = operation(*args, **kwargs)
                opened.add(descriptor)
                return descriptor
            return call

        def close(descriptor):
            original_close(descriptor)
            opened.discard(descriptor)

        with (patch("core_agent.chat_files.os.open", side_effect=track(original_open)),
              patch("core_agent.chat_files.os.dup", side_effect=track(original_dup)),
              patch("core_agent.chat_files.os.close", side_effect=close)):
            with patch("core_agent.chat_files.os.fsync", side_effect=OSError("fsync failed")):
                with self.assertRaises(OSError):
                    with self.service._attachments(self.binding):
                        self.fail("fsync failure must prevent returning a descriptor")
            self.assertEqual(opened, set())
            original_stat = os.stat

            def fail_stat(path, *args, **kwargs):
                if path == batch["batch_id"] and kwargs.get("dir_fd") == self.service.uploads:
                    raise PermissionError("stat failed")
                return original_stat(path, *args, **kwargs)

            with patch("core_agent.chat_files.os.stat", side_effect=fail_stat):
                with self.assertRaises(CoreError) as error:
                    self.service.publish(batch["batch_id"], self.binding)
            self.assertEqual(error.exception.code, "FILE_PUBLICATION_PENDING")
            self.assertEqual(opened, set())
            for failing_name in ("private", "uploads", "quarantine"):
                def fail_open(path, *args, **kwargs):
                    if path == failing_name:
                        raise OSError("open failed")
                    return track(original_open)(path, *args, **kwargs)

                with patch("core_agent.chat_files.os.open", side_effect=fail_open):
                    with self.assertRaises(OSError):
                        ChatFileService(self.store, self.workspaces)
                self.assertEqual(opened, set())
            service = ChatFileService(self.store, self.workspaces)
            service.close()
            self.assertEqual(opened, set())
            # A recycled descriptor must not be closed by repeated service.close.
            reused = track(original_open)(self.directory.name, os.O_RDONLY)
            service.close()
            os.fstat(reused)
            close(reused)

    def test_rowless_original_age_and_authoritative_reference_check(self):
        orphan, referenced, accepted, corrupt = self.prepare(), self.prepare(), self.prepare(), self.prepare()
        self.bind(accepted)
        for row in (orphan, referenced, corrupt):
            del self.store.rows[row["batch_id"]]  # Model a database restoration/legacy orphan.
        with self.workflow._lock:
            self.workflow._records[self.record.run_id].snapshot["file_batch_id"] = referenced["batch_id"]
        corrupt_path = Path(self.directory.name) / "private/uploads" / corrupt["batch_id"] / ".manifest.json"
        corrupt_path.write_text("invalid")
        self.now += 24 * 3600
        # New service uses original manifest age, not mtime or process start time.
        restarted = ChatFileService(self.store, self.workspaces, clock=lambda: self.now)
        self.addCleanup(restarted.close)
        total = 0
        for _ in range(8):
            result = restarted.sweep(startup=True, limit=1)
            total += result["cleaned"]
            if not result["has_more"]:
                break
        self.assertEqual(total, 1)
        remaining = os.listdir(restarted.uploads)
        self.assertNotIn(orphan["batch_id"], remaining)
        for row in (referenced, accepted, corrupt):
            self.assertIn(row["batch_id"], remaining)

    def test_extra_unmanifested_files_are_never_published(self):
        batch = self.prepare()
        self.accept(batch)
        private = Path(self.directory.name) / "private/quarantine" / batch["batch_id"]
        (private / "extra").write_bytes(b"unreviewed")
        with self.assertRaises(CoreError) as error:
            self.service.publish(batch["batch_id"], self.binding)
        self.assertEqual(error.exception.code, "ARTIFACT_INTEGRITY_FAILED")
        self.assertFalse(self.target(batch).exists())


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "set TEST_DATABASE_URL for file batch persistence proofs")
class PostgresChatFileTests(ChatFileContract, unittest.TestCase):
    def setUp(self):
        self.database = PostgresDatabase(os.environ["TEST_DATABASE_URL"], min_size=0, max_size=5)
        self.addCleanup(self.database.close)
        self.database.migrate()
        self.workflow = PostgresWorkflowStore(self.database)
        self.store = PostgresChatFileStore(self.database, self.workflow)
        self.setup_files()
        self.addCleanup(self.cleanup_staging)
        task = Task(id=self.record.task_id, context_id=self.binding.context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING))
        with self.database.transaction() as conn:
            conn.execute("INSERT INTO core_chats (tenant_id,context_id,owner_id) VALUES (%s,%s,%s)",
                         (self.binding.tenant_id, self.binding.context_id, self.binding.owner_id))
            conn.execute("""INSERT INTO core_a2a_tasks
                         (task_id,owner,tenant,context_id,state,status_timestamp,payload)
                         VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                         (self.record.task_id, self.binding.owner_id, self.binding.tenant_id,
                          self.binding.context_id, TaskState.TASK_STATE_WORKING, 0, task.SerializeToString()))

    def cleanup_staging(self):
        # Sweeps are global; another test's expired stage must not enter its backlog.
        with self.database.transaction() as conn:
            rows = conn.execute("""SELECT batch_id, lease_token FROM core_chat_file_batches
                WHERE tenant_id=%s AND state IN ('staging','rejected') AND cleaned_at IS NULL""",
                (self.binding.tenant_id,)).fetchall()
        for row in rows:
            self.service.reject(row["batch_id"], self.binding.tenant_id, row["lease_token"])

    def test_admission_rollback_keeps_stage_unaccepted(self):
        batch = self.prepare()
        attachments = self.workspaces.workspace(self.binding) / "attachments"
        attachments.mkdir()
        (attachments / "report.pdf").mkdir()
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.database.transaction() as conn:
                self.service.bind(batch["batch_id"], self.binding, task_id=self.record.task_id,
                    run_id=self.record.run_id, actor_id="actor", message_id="message",
                    request_digest=self.options["request_digest"], lease_token=batch["lease_token"], connection=conn)
                raise RuntimeError("rollback")
        self.assertEqual(self.store.get(batch["batch_id"], self.binding.tenant_id)["state"], "staging")
        self.assertFalse(self.target(batch).exists())
        self.service.reject(batch["batch_id"], self.binding.tenant_id, batch["lease_token"])
        self.assertEqual(self.store.get(batch["batch_id"], self.binding.tenant_id)["state"], "rejected")
        self.assertEqual(os.listdir(self.service.uploads), [])

    def test_database_rejects_invalid_state_and_accepted_manifest_mutation(self):
        from psycopg import Error
        from psycopg.types.json import Jsonb
        batch = self.prepare()
        # Existing unencoded manifests remain readable and immutable on upgrade.
        with self.database.transaction() as conn:
            conn.execute("UPDATE core_chat_file_batches SET manifest=%s WHERE batch_id=%s",
                (Jsonb(batch["manifest"]), batch["batch_id"]))
        with self.assertRaises(Error), self.database.transaction() as conn:
            conn.execute("UPDATE core_chat_file_batches SET state='published' WHERE batch_id=%s", (batch["batch_id"],))
        self.bind(batch)
        with self.assertRaises(Error), self.database.transaction() as conn:
            conn.execute("UPDATE core_chat_file_batches SET manifest='{}' WHERE batch_id=%s", (batch["batch_id"],))
        self.service.record_decision(batch["batch_id"], self.binding, decision_ref="legacy-reviewed", allow=True)
        self.assertEqual(self.service.publish(batch["batch_id"], self.binding), batch["manifest"])
