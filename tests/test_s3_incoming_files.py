"""Original incoming bytes survive local-cache loss before a review decision."""
import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from core_agent.chat_files import ChatFileService, MemoryChatFileStore
from core_agent.errors import CoreError
from core_agent.workflow import InMemoryWorkflowStore, WorkflowRecord
from core_agent.workspace import ChatWorkspaces, WorkspaceBinding
from tests.test_workspace_durability import Blobs


class S3IncomingFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.binding = WorkspaceBinding("company", "owner", "chat")
        self.workflow = InMemoryWorkflowStore()
        self.record = self.workflow.create(WorkflowRecord(
            "run", "task", "chat", "company", "owner", None, "RUNNING", 1, {}, {}))
        self.store = MemoryChatFileStore(self.workflow, lambda binding: None)
        self.blobs = Blobs()
        self.blobs.tenant_id, self.blobs.max_bytes = "company", 1024
        self.pins = []
        self.blobs.pin = lambda connection, digests: self.pins.append(set(digests))
        self.service = self.fresh("first")
        self.options = dict(tenant_id="company", actor_id="caller", message_id="message",
                            request_digest="request", source="upload")

    def fresh(self, name):
        service = ChatFileService(self.store, ChatWorkspaces(Path(self.temp.name) / name),
            protected_inputs_enabled=False, blob_store=self.blobs)
        self.addCleanup(service.close)
        return service

    def prepare(self):
        return self.service.prepare([{"name": "note.txt", "raw": b"original", "media_type": "text/plain"}],
                                    **self.options)

    def bind(self, batch):
        return self.service.bind(batch["batch_id"], self.binding, task_id="task", run_id="run",
            actor_id="caller", message_id="message", request_digest="request", lease_token=batch["lease_token"])

    def test_private_original_is_uploaded_before_binding_and_survives_empty_cache(self):
        batch = self.prepare()
        digest = batch["manifest"]["entries"][0]["sha256"]
        self.assertEqual(self.blobs.objects[digest], b"original")
        self.assertEqual(batch["state"], "staging")
        self.assertEqual(batch["manifest"]["destination"]["directory"], "inputs")
        self.assertEqual(list((Path(self.temp.name) / "first/private/uploads").iterdir()), [])
        self.bind(batch)
        self.assertIn({digest}, self.pins)
        restarted = self.fresh("empty-cache")
        material = restarted.review_material(batch["batch_id"], self.binding, run_id="run", task_id="task")
        self.assertEqual(material["documents"][0]["text"], "original")
        self.assertEqual(material["state"], "accepted_quarantine")
        self.assertFalse((restarted.workspaces.workspace(self.binding) / "inputs").exists())
        self.assertEqual(list((Path(self.temp.name) / "empty-cache/private/uploads").iterdir()), [])

    def test_excluded_original_is_downloadable_but_cannot_be_published(self):
        batch = self.prepare()
        self.bind(batch)
        self.service.record_decision(batch["batch_id"], self.binding, decision_ref="deny", allow=False)
        restarted = self.fresh("denied-cache")
        self.assertEqual(restarted.owner_download(batch["batch_id"], self.binding, 0,
                         run_id="run", task_id="task")["content"], b"original")
        with self.assertRaises(CoreError) as caught:
            restarted.publish(batch["batch_id"], self.binding)
        self.assertEqual(caught.exception.code, "FILE_BATCH_NOT_READY")

    def test_scope_is_checked_before_any_remote_read(self):
        batch = self.prepare()
        self.bind(batch)
        for binding in (WorkspaceBinding("foreign", "owner", "chat"),
                        WorkspaceBinding("company", "stranger", "chat")):
            with self.subTest(binding=binding), self.assertRaises(CoreError):
                self.service.owner_download(batch["batch_id"], binding, 0, run_id="run", task_id="task")
        self.assertEqual(self.blobs.gets, [])
        with self.assertRaises(CoreError):
            self.service.prepare([{"raw": b"foreign"}], **(self.options | {"tenant_id": "foreign"}))
        self.assertEqual(len(self.blobs.puts), 1)

    def test_original_integrity_is_checked_and_temporary_files_are_removed(self):
        batch = self.prepare()
        self.bind(batch)
        self.blobs.objects[batch["manifest"]["entries"][0]["sha256"]] = b"tampered"
        with self.assertRaises(CoreError) as caught:
            self.service.review_material(batch["batch_id"], self.binding, run_id="run", task_id="task")
        self.assertEqual(caught.exception.code, "ARTIFACT_INTEGRITY_FAILED")
        self.assertEqual(list((Path(self.temp.name) / "first/private/uploads").iterdir()), [])

    def test_failed_put_rejects_stage_without_admission(self):
        self.blobs.put = lambda content: (_ for _ in ()).throw(CoreError("STORAGE_UNAVAILABLE"))
        with self.assertRaises(CoreError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "STORAGE_UNAVAILABLE")
        self.assertEqual([row["state"] for row in self.store.rows.values()], ["rejected"])
        self.assertTrue(all(row["task_id"] is None for row in self.store.rows.values()))

    def test_materialization_cleanup_failure_preserves_result_and_logs_finite_cause(self):
        batch = self.prepare()
        self.bind(batch)
        with self.assertLogs("core_agent.chat_files", "WARNING") as captured, patch.object(
                self.service, "reject", side_effect=PermissionError(13, "PRIVATE")):
            material = self.service.review_material(batch["batch_id"], self.binding, run_id="run", task_id="task")
        self.assertEqual(material["documents"][0]["text"], "original")
        logged = "\n".join(captured.output)
        self.assertIn('"errno": 13', logged)
        self.assertNotIn("PRIVATE", logged)
        self.assertEqual(list((Path(self.temp.name) / "first/private/uploads").iterdir()), [])

    def test_crashed_materialization_is_bounded_and_swept_before_another_read(self):
        batch = self.prepare()
        self.bind(batch)
        self.service.clock = lambda: 1000
        stage = self.service._new_stage("company", "private-file-cache", batch["batch_id"],
                                        "private-cache", cache=True)
        # Crash before the first GET/manifest write: DB ownership already exists.
        os.mkdir(stage["batch_id"], dir_fd=self.service.uploads)
        with self.assertRaises(CoreError) as caught:
            self.service.review_material(batch["batch_id"], self.binding, run_id="run", task_id="task")
        self.assertEqual(caught.exception.code, "STORAGE_UNAVAILABLE")
        self.assertEqual(self.blobs.gets, [])
        self.service.clock = lambda: 1301
        material = self.service.review_material(batch["batch_id"], self.binding, run_id="run", task_id="task")
        self.assertEqual(material["documents"][0]["text"], "original")
        self.assertIsNotNone(self.store.get(stage["batch_id"], "company")["cleaned_at"])
        self.assertEqual(list((Path(self.temp.name) / "first/private/uploads").iterdir()), [])
