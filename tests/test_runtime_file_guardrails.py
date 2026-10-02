"""Accepted batches cross the actual runtime guardrail/publication boundary."""
import json
import unittest
from unittest.mock import patch

from core_agent.chat_files import ChatFileService, MemoryChatFileStore
from core_agent.errors import CoreError
from core_agent.model import ModelResponse
from core_agent.workflow import SuspendedRun
from core_agent.workspace import ChatWorkspaces, WorkspaceBinding
from tests import test_runtime_guardrails as guardrails


class RuntimeFileGuardrailTests(unittest.TestCase):
    setUp = guardrails.RuntimeGuardrailTests.setUp
    app = guardrails.RuntimeGuardrailTests.app
    guard = guardrails.RuntimeGuardrailTests.guard
    resolve = staticmethod(guardrails.RuntimeGuardrailTests.resolve)

    def files(self, *verdicts, files=None):
        agent, model = self.app(ModelResponse(message="done"))
        detector = self.guard(agent, *verdicts)
        service = ChatFileService(MemoryChatFileStore(agent.workflow_store, lambda binding: None),
                                  ChatWorkspaces(self.temp.name + "/files"), clock=lambda: self.now[0])
        self.addCleanup(service.close)
        agent.chat_file_service = service
        record, *_ = agent._new_workflow({"prompt": "perform task"}, task_id="task", identity="owner",
            session_id="chat", tenant_id="company", defer_initialization=True)
        binding = WorkspaceBinding("company", "owner", "chat")
        batch = service.prepare(files or [{"name": "report.txt", "media_type": "text/plain", "raw": b"private file text"}],
            tenant_id="company", actor_id="owner", message_id="message", request_digest="digest", source="a2a")
        service.bind(batch["batch_id"], binding, task_id=record.task_id, run_id=record.run_id,
            actor_id="owner", message_id="message", request_digest="digest", lease_token=batch["lease_token"])
        record.snapshot["file_batch_id"] = batch["batch_id"]
        agent.workflow_store._records[record.run_id] = record
        return agent, model, detector, service, batch, binding

    def test_file_wait_hides_names_and_bytes_until_exact_owner_allow(self):
        agent, model, detector, service, batch, binding = self.files("clear", "suspicious")
        sleeping = agent.resume_task("task")
        self.assertIsInstance(sleeping, SuspendedRun)
        self.assertEqual(model.calls, ())
        target = service.workspaces.workspace(binding) / "attachments" / batch["batch_id"]
        self.assertFalse(target.exists())
        record = agent.workflow_store.lookup_task("task")
        self.assertNotIn("report.txt", json.dumps(record.snapshot))
        wait = agent.workflow_store.get_wait(sleeping.wait_id, tenant_id="company")
        self.assertEqual(wait.subject["affected_scope"], "file_batch")
        self.assertEqual(wait.subject["batch_id"], batch["batch_id"])
        private = agent.material_review_store.owner_read_payload(record, wait.source_id)
        self.assertEqual(private["sealed_ref"], {"batch_id": batch["batch_id"], "index": 0})
        downloaded = service.owner_download(batch["batch_id"], binding, 0, run_id=record.run_id, task_id="task")
        self.assertEqual(downloaded["content"], b"private file text")
        self.resolve(agent, sleeping, "allowed")
        agent._runtime_cache.clear()
        self.assertEqual(agent.resume_task("task").message, "done")
        self.assertEqual((target / "report.txt").read_bytes(), b"private file text")
        self.assertIn("report.txt", model.calls[-1].context)
        self.assertNotIn("private file text", model.calls[-1].context)
        self.assertIn("/workspace/attachments/", model.calls[-1].context)
        self.assertEqual(detector.generate.call_count, 2)

    def test_reject_excludes_entire_batch_and_continues_with_safe_notice(self):
        agent, model, detector, service, batch, binding = self.files("clear", "clear", "clear", "suspicious", files=[
            {"name": "good.txt", "media_type": "text/plain", "raw": b"good"},
            {"name": "bad.txt", "media_type": "text/plain", "raw": b"bad"}])
        sleeping = agent.resume_task("task")
        self.assertIsInstance(sleeping, SuspendedRun)
        self.resolve(agent, sleeping, "rejected")
        self.assertEqual(agent.resume_task("task").message, "done")
        self.assertEqual(service.store.get(batch["batch_id"], "company")["state"], "excluded")
        self.assertFalse((service.workspaces.workspace(binding) / "attachments" / batch["batch_id"]).exists())
        self.assertIn("MATERIAL_REJECTED", model.calls[-1].context)
        self.assertIn(batch["batch_id"], model.calls[-1].context)
        self.assertNotIn("good.txt", model.calls[-1].context)
        self.assertNotIn("bad.txt", model.calls[-1].context)

    def test_unsupported_and_empty_files_require_owner_even_with_clear_detector(self):
        for data, media in ((b"%PDF binary", "application/pdf"), (b"", "text/plain"), (b"bad\x00text", "text/plain"),
                            (b"bad\x7ftext", "text/plain"), ("bad\u0085text".encode(), "text/plain")):
            with self.subTest(data=data):
                agent, model, detector, service, batch, binding = self.files("clear", files=[
                    {"name": "private-name", "media_type": media, "raw": data}])
                sleeping = agent.resume_task("task")
                self.assertIsInstance(sleeping, SuspendedRun)
                wait = agent.workflow_store.get_wait(sleeping.wait_id, tenant_id="company")
                self.assertEqual(wait.subject["reason"], "incomplete_extraction")
                self.assertEqual(detector.generate.call_count, 1)
                self.assertEqual(model.calls, ())
                self.resolve(agent, sleeping, "timeout")
                self.assertEqual(agent.resume_task("task").message, "done")
                self.assertIn("MATERIAL_TIMEOUT", model.calls[-1].context)

    def test_tampered_bytes_after_allow_never_publish(self):
        agent, model, detector, service, batch, binding = self.files("clear", "suspicious")
        sleeping = agent.resume_task("task")
        self.resolve(agent, sleeping, "allowed")
        (service.workspaces.root / "private/uploads" / batch["batch_id"] / "report.txt").write_bytes(b"changed")
        with self.assertRaises(CoreError) as caught:
            agent.resume_task("task")
        self.assertEqual(caught.exception.code, "ARTIFACT_INTEGRITY_FAILED")
        self.assertEqual(model.calls, ())
        self.assertFalse((service.workspaces.workspace(binding) / "attachments" / batch["batch_id"]).exists())

    def test_publication_crash_recovers_same_bytes_without_detector_replay(self):
        from core_agent import chat_files
        agent, model, detector, service, batch, binding = self.files("clear", "clear", "clear")
        rename = chat_files._rename
        def interrupted(source_fd, source, target_fd, target):
            rename(source_fd, source, target_fd, target)
            if target_fd not in (service.uploads, service.quarantine):
                raise SystemExit("after publication rename")
        with patch("core_agent.chat_files._rename", side_effect=interrupted), self.assertRaises(SystemExit):
            agent.resume_task("task")
        self.assertEqual(service.store.get(batch["batch_id"], "company")["state"], "accepted_ready")
        self.assertEqual(model.calls, ())
        self.assertEqual(agent.resume_task("task").message, "done")
        self.assertEqual(detector.generate.call_count, 3)
        target = service.workspaces.workspace(binding) / "attachments" / batch["batch_id"]
        self.assertEqual((target / "report.txt").read_bytes(), b"private file text")

    def test_lease_loss_after_classification_prevents_any_publication(self):
        agent, model, detector, service, batch, binding = self.files("clear", "clear", "clear")
        decision = service.record_decision
        def expire(*args, **kwargs):
            agent.workflow_store._leases.clear()
            return decision(*args, **kwargs)
        with patch.object(service, "record_decision", side_effect=expire), self.assertRaises(CoreError) as caught:
            agent.resume_task("task")
        self.assertEqual(caught.exception.code, "LEASE_LOST")
        self.assertFalse((service.workspaces.workspace(binding) / "attachments" / batch["batch_id"]).exists())
        self.assertEqual(service.store.get(batch["batch_id"], "company")["state"], "accepted_quarantine")
        self.assertEqual(model.calls, ())

    def test_followup_batch_wait_keeps_inbox_unread_and_publishes_before_delivery(self):
        agent, model, detector, service, batch, binding = self.files("clear", "clear", "suspicious")
        record = agent.workflow_store.lookup_task("task")
        record.snapshot.pop("file_batch_id")
        # Construct the accepted inbox fixture; admission atomicity is covered by
        # its own store/transport suite, not claimed by this runtime boundary test.
        service.store.rows[batch["batch_id"]]["sequence"] = 1
        agent.workflow_store.append_inbound("task", tenant_id="company", owner_id="owner", context_id="chat",
            message_id="followup", content="read attached", provenance={"file_batch_id": batch["batch_id"]})
        sleeping = agent.resume_task("task")
        self.assertIsInstance(sleeping, SuspendedRun)
        self.assertEqual(len(agent.workflow_store.pending_inbound(record)), 1)
        self.assertEqual(model.calls, ())
        self.resolve(agent, sleeping, "allowed")
        self.assertEqual(agent.resume_task("task").message, "done")
        self.assertEqual(agent.workflow_store.pending_inbound(record), ())
        self.assertIn("read attached", model.calls[-1].context)
        self.assertIn("report.txt", model.calls[-1].context)
        self.assertEqual(detector.generate.call_count, 3)

    def test_reupload_same_bytes_with_new_name_remains_denied(self):
        agent, model, detector, service, first, binding = self.files("clear", "suspicious")
        sleeping = agent.resume_task("task")
        self.resolve(agent, sleeping, "rejected")
        self.assertEqual(agent.resume_task("task").message, "done")
        agent.model._responses.append(ModelResponse(message="continued"))
        second_record, *_ = agent._new_workflow({"prompt": "new task"}, task_id="second", identity="owner",
            session_id="chat", tenant_id="company", defer_initialization=True)
        batch = service.prepare([{"name": "new-name.txt", "media_type": "text/plain", "raw": b"private file text"}],
            tenant_id="company", actor_id="owner", message_id="second", request_digest="second", source="a2a")
        service.bind(batch["batch_id"], binding, task_id="second", run_id=second_record.run_id,
            actor_id="owner", message_id="second", request_digest="second", lease_token=batch["lease_token"])
        second_record.snapshot["file_batch_id"] = batch["batch_id"]
        detector.generate.side_effect = [ModelResponse(message='{"verdict":"clear"}', finish_reason="stop")]
        self.assertEqual(agent.resume_task("second").message, "continued")
        self.assertEqual(detector.generate.call_count, 3)
        self.assertEqual(service.store.get(batch["batch_id"], "company")["state"], "excluded")
        self.assertNotIn("new-name.txt", model.calls[-1].context)
        reference = service.store.get(batch["batch_id"], "company")["decision_ref"]
        decision = agent.material_review_store.rows[reference]
        self.assertEqual(decision["run_id"], agent.workflow_store.lookup_task("task").run_id)
        self.assertEqual(agent.workflow_store.get_wait(decision["wait_id"], tenant_id="company").outcome["reason"], "rejected")

    def test_rejected_complete_file_text_cannot_reenter_as_initial_prompt(self):
        agent, model, detector, service, batch, binding = self.files("clear", "suspicious")
        sleeping = agent.resume_task("task")
        self.resolve(agent, sleeping, "rejected")
        agent.resume_task("task")
        model._responses.append(ModelResponse(message="continued"))
        agent._new_workflow({"prompt": "private file text"}, task_id="second", identity="owner",
            session_id="chat", tenant_id="company", defer_initialization=True)
        detector.generate.side_effect = [ModelResponse(message='{"verdict":"clear"}', finish_reason="stop")]
        self.assertEqual(agent.resume_task("second").message, "continued")
        self.assertNotIn("private file text", model.calls[-1].context)
        self.assertIn("MATERIAL_REJECTED", model.calls[-1].context)
        self.assertEqual(detector.generate.call_count, 2)

    def test_rejected_initial_text_cannot_reenter_as_complete_file(self):
        agent, model, detector, service, batch, binding = self.files("suspicious", "clear", "clear", files=[
            {"name": "same.txt", "media_type": "text/plain", "raw": b"perform task"}])
        sleeping = agent.resume_task("task")
        wait = agent.workflow_store.get_wait(sleeping.wait_id, tenant_id="company")
        self.resolve(agent, sleeping, "rejected")
        self.assertEqual(agent.resume_task("task").message, "done")
        self.assertEqual(detector.generate.call_count, 1)
        stored = service.store.get(batch["batch_id"], "company")
        self.assertEqual(stored["state"], "excluded")
        self.assertEqual(stored["decision_ref"], wait.source_id)
        self.assertNotIn("same.txt", model.calls[-1].context)

    def test_file_raw_bytes_are_not_confused_with_json_encoded_inline_text(self):
        agent, model, detector, service, batch, binding = self.files("clear", "suspicious", files=[
            {"name": "quoted.txt", "media_type": "text/plain", "raw": b'"hello"'}])
        sleeping = agent.resume_task("task")
        self.resolve(agent, sleeping, "rejected")
        agent.resume_task("task")
        model._responses.append(ModelResponse(message="continued"))
        agent._new_workflow({"prompt": "hello"}, task_id="second", identity="owner",
            session_id="chat", tenant_id="company", defer_initialization=True)
        detector.generate.side_effect = [ModelResponse(message='{"verdict":"clear"}', finish_reason="stop")]
        self.assertEqual(agent.resume_task("second").message, "continued")
        self.assertEqual(detector.generate.call_count, 3)
        self.assertIn("hello", model.calls[-1].context)
        self.assertNotIn("MATERIAL_REJECTED", model.calls[-1].context)

    def test_unverified_utf8_file_denial_blocks_identical_inline_text(self):
        for media, content in (("application/octet-stream", b"private octet text"),
                               ("application/pdf", b"private pdf text"),
                               ("text/plain", b"private\x00control text")):
            with self.subTest(media=media):
                agent, model, detector, service, batch, binding = self.files("clear", files=[
                    {"name": "opaque-file", "media_type": media, "raw": content}])
                sleeping = agent.resume_task("task")
                self.assertIsInstance(sleeping, SuspendedRun)
                wait = agent.workflow_store.get_wait(sleeping.wait_id, tenant_id="company")
                self.assertEqual(wait.subject["reason"], "incomplete_extraction")
                self.assertEqual(model.calls, ())
                self.assertEqual(detector.generate.call_count, 1)
                material = service.review_material(batch["batch_id"], binding,
                    run_id=agent.workflow_store.lookup_task("task").run_id, task_id="task")
                self.assertEqual(material["documents"][0]["text"], "")
                self.assertFalse(material["documents"][0]["complete"])
                self.resolve(agent, sleeping, "rejected")
                self.assertEqual(agent.resume_task("task").message, "done")
                model._responses.append(ModelResponse(message="continued"))
                agent._new_workflow({"prompt": content.decode()}, task_id="second", identity="owner",
                    session_id="chat", tenant_id="company", defer_initialization=True)
                detector.generate.side_effect = [ModelResponse(message='{"verdict":"clear"}', finish_reason="stop")]
                self.assertEqual(agent.resume_task("second").message, "continued")
                self.assertIn("MATERIAL_REJECTED", model.calls[-1].context)
                self.assertNotIn("private", model.calls[-1].context)
                self.assertEqual(detector.generate.call_count, 1)

    def test_inline_denial_blocks_identical_unverified_utf8_file(self):
        for media, content in (("application/octet-stream", b"private octet text"),
                               ("application/pdf", b"private pdf text"),
                               ("text/plain", b"private\x00control text")):
            with self.subTest(media=media):
                agent, model, detector, service, batch, binding = self.files("suspicious", files=[
                    {"name": "opaque-file", "media_type": media, "raw": content}])
                agent.workflow_store.lookup_task("task").request["prompt"] = content.decode()
                sleeping = agent.resume_task("task")
                self.assertIsInstance(sleeping, SuspendedRun)
                wait = agent.workflow_store.get_wait(sleeping.wait_id, tenant_id="company")
                self.resolve(agent, sleeping, "rejected")
                result = agent.resume_task("task")
                self.assertNotIsInstance(result, SuspendedRun)
                self.assertEqual(result.message, "done")
                stored = service.store.get(batch["batch_id"], "company")
                self.assertEqual(stored["state"], "excluded")
                self.assertEqual(stored["decision_ref"], wait.source_id)
                self.assertNotIn("opaque-file", model.calls[-1].context)
                self.assertEqual(detector.generate.call_count, 1)
