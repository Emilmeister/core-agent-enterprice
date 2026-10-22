"""Real PostgreSQL/S3 incoming originals with portable Landlock-profile plumbing."""
import asyncio
import base64
from dataclasses import replace
import os
import sys
import uuid
from contextlib import contextmanager
from unittest.mock import patch

from core_agent.errors import CoreError
from core_agent.execution import EnvironmentSpec
from core_agent.interactions import interaction_digest
from core_agent.model import ModelResponse
from core_agent.sandbox_landlock import _validate_workspace
from core_agent.workspace import WorkspaceBinding
from tests import test_cloudru_storage_runtime as storage
from tests.app_support import UnsandboxedTestLauncher


class PortableLandlockLauncher(UnsandboxedTestLauncher):
    """Exercise profile selection and its protected-input check, not Linux isolation."""
    isolation_level = "landlock"

    def start(self, argv, **options):
        _validate_workspace(options["workspace_path"], options["readonly_input_path"])
        return super().start(argv, **options)


class S3IncomingRuntimeTests(storage.CloudStorageRuntimeFixture):
    launcher_type = PortableLandlockLauncher
    original = b"\x00\xffprivate original"

    async def upload(self, context, *, task_id=None, raw=None):
        message = {"messageId": uuid.uuid4().hex, "contextId": context, "role": "ROLE_USER",
                   "parts": [{"text": "Review the attached file"}, {
                       "raw": base64.b64encode(self.original if raw is None else raw).decode(), "filename": "original.bin",
                       "mediaType": "application/octet-stream"}]}
        if task_id is not None:
            message["taskId"] = task_id
        return await self.http.post("/a2a/owner/message:send",
            headers={"Origin": storage._ORIGIN, "A2A-Version": "1.0"}, json={"message": message})

    async def pending(self, context):
        response = await self.upload(context)
        self.assertEqual(response.status_code, 200, response.text)
        record = self.agent.workflow_store.lookup_task(response.json()["task"]["id"])
        self.assertEqual(record.state, "WAITING_INPUT")
        self.assertEqual(self.agent.workflow_store.get_wait(record.snapshot["wait_id"], tenant_id=self.tenant).kind, "guardrail")
        batch = self.agent.chat_file_service.store.get(record.snapshot["file_batch_id"], self.tenant)
        self.assertEqual(batch["manifest"]["storage"], {"version": 1, "backend": "s3"})
        path = batch["manifest"]["entries"][0]["relative_path"]
        self.assertEqual(path, f"inputs/{batch['batch_id']}/original.bin")
        return record, batch, path

    async def decide(self, record, decision):
        wait = self.agent.workflow_store.get_wait(record.snapshot["wait_id"], tenant_id=self.tenant)
        response = await self.http.post(f"/api/guardrails/{wait.wait_id}/decision",
            headers={"Origin": storage._ORIGIN},
            json={"decision": decision, "subject_digest": interaction_digest(wait)})
        self.assertEqual(response.status_code, 200, response.text)
        await asyncio.to_thread(self.agent.resume_task, record.task_id)
        self.assertEqual(self.agent.workflow_store.lookup_task(record.task_id).state, "COMPLETED")

    async def test_put_failure_precedes_root_and_followup_acceptance(self):
        service = self.agent.chat_file_service
        self.assertIs(service.blob_store, self.agent.workspace_blob_store)
        self.assertFalse(service.protected_inputs_enabled)
        with patch.object(service.blob_store, "put", side_effect=CoreError("STORAGE_UNAVAILABLE")):
            denied = await self.upload("denied-root")
        self.assertGreaterEqual(denied.status_code, 400, denied.text)
        with self.agent.workflow_store.database.transaction() as connection:
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM core_runs WHERE tenant_id=%s AND context_id='denied-root'",
                                               (self.tenant,)).fetchone()["n"], 0)
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM core_a2a_tasks WHERE tenant=%s AND context_id='denied-root'",
                                               (self.tenant,)).fetchone()["n"], 0)
        record, _, _ = await self.pending("pending-root")
        with self.agent.workflow_store.database.transaction() as connection:
            before = connection.execute("SELECT count(*) AS n FROM core_inbound_messages WHERE run_id=%s", (record.run_id,)).fetchone()["n"]
        with patch.object(service.blob_store, "put", side_effect=CoreError("STORAGE_UNAVAILABLE")):
            denied = await self.upload("pending-root", task_id=record.task_id, raw=b"\x00\xffnew incoming bytes")
        self.assertGreaterEqual(denied.status_code, 400, denied.text)
        with self.agent.workflow_store.database.transaction() as connection:
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM core_inbound_messages WHERE run_id=%s",
                                               (record.run_id,)).fetchone()["n"], before)
        self.assertEqual(self.agent.workflow_store.lookup_task(record.task_id).state, "WAITING_INPUT")
        self.assertEqual(self.model.calls, ())

    async def test_pending_review_downloads_original_after_restart_and_checks_scope_before_get(self):
        record, batch, path = await self.pending("review")
        await self.restart([])
        downloaded = await self.http.get(f"/api/guardrails/{record.snapshot['wait_id']}/file")
        self.assertEqual(downloaded.status_code, 200, downloaded.text)
        self.assertEqual(downloaded.content, self.original)
        binding = WorkspaceBinding(self.tenant, record.owner_id, record.context_id)
        self.assertFalse((self.agent.chat_file_service.workspaces.workspace(binding) / path).exists())
        service = self.agent.chat_file_service
        with patch.object(service.blob_store, "get", side_effect=AssertionError("unauthorized S3 GET")) as get:
            for field in ("tenant_id", "owner_id", "context_id"):
                with self.subTest(field=field), self.assertRaises(CoreError):
                    service.owner_download(batch["batch_id"], replace(binding, **{field: "foreign"}), 0,
                                           run_id=record.run_id, task_id=record.task_id)
            for run_id, task_id in (("foreign", record.task_id), (record.run_id, "foreign")):
                with self.subTest(run_id=run_id, task_id=task_id), self.assertRaises(CoreError):
                    service.owner_download(batch["batch_id"], binding, 0, run_id=run_id, task_id=task_id)
            get.assert_not_called()
        self.assertEqual(self.model.calls, ())

    async def test_original_get_outage_after_acceptance_defers_delivery_and_recovers_before_review(self):
        with patch.object(self.agent.workspace_blob_store, "get", side_effect=CoreError("STORAGE_UNAVAILABLE")):
            response = await self.upload("get-outage")
        self.assertEqual(response.status_code, 200, response.text)
        record = self.agent.workflow_store.lookup_task(response.json()["task"]["id"])
        batch_id = record.snapshot["file_batch_id"]
        self.assertEqual(record.snapshot["file_delivery_pending"], {
            "batch_id": batch_id, "error_code": "STORAGE_UNAVAILABLE"})
        self.assertEqual(self.agent.chat_file_service.store.get(batch_id, self.tenant)["state"], "accepted_quarantine")
        self.assertEqual(self.model.calls, ())
        self.assertEqual(self.agent.tool_runtime.environment_manager.backend.launcher.processes, [])
        await self.restart([])
        await asyncio.to_thread(self.agent.resume_task, record.task_id)
        record = self.agent.workflow_store.lookup_task(record.task_id)
        self.assertEqual(record.state, "WAITING_INPUT")
        self.assertEqual(self.agent.workflow_store.get_wait(record.snapshot["wait_id"], tenant_id=self.tenant).kind, "guardrail")
        self.assertEqual(self.model.calls, ())
        self.assertEqual(self.agent.tool_runtime.environment_manager.backend.launcher.processes, [])
        downloaded = await self.http.get(f"/api/guardrails/{record.snapshot['wait_id']}/file")
        self.assertEqual(downloaded.status_code, 200, downloaded.text)
        self.assertEqual(downloaded.content, self.original)
        self.model._responses = [ModelResponse(message="Accepted after review")]
        await self.decide(record, "allow")

    async def test_allowed_working_copy_edits_and_deletion_survive_restart_without_changing_original(self):
        record, _, path = await self.pending("working-copy")
        original_url = f"/api/guardrails/{record.snapshot['wait_id']}/file"
        await self.restart([ModelResponse(message="File accepted")])
        await self.decide(record, "allow")
        await self.restart([])
        copy = await self.http.get("/api/chats/working-copy/files/content", params={"path": path})
        self.assertEqual(copy.status_code, 200, copy.text)
        self.assertEqual(copy.content, self.original)
        self.model._responses = [self.terminal(f"from pathlib import Path; Path({path!r}).write_bytes(b'edited')"),
                                 ModelResponse(message="Edited")]
        await self.submit("working-copy")
        await self.restart([])
        copy = await self.http.get("/api/chats/working-copy/files/content", params={"path": path})
        self.assertEqual(copy.status_code, 200, copy.text)
        self.assertEqual(copy.content, b"edited")
        self.assertEqual((await self.http.get(original_url)).content, self.original)
        self.model._responses = [self.terminal(f"from pathlib import Path; Path({path!r}).unlink()"),
                                 ModelResponse(message="Deleted")]
        await self.submit("working-copy")
        await self.restart([])
        self.assertEqual((await self.http.get("/api/chats/working-copy/files/content", params={"path": path})).status_code, 404)
        downloaded = await self.http.get(original_url)
        self.assertEqual(downloaded.status_code, 200, downloaded.text)
        self.assertEqual(downloaded.content, self.original)

    async def test_rejected_batch_never_publishes_a_working_copy_after_restart(self):
        record, _, path = await self.pending("reject")
        await self.restart([ModelResponse(message="Continuing without the file")])
        await self.decide(record, "reject")
        await self.restart([])
        self.assertEqual((await self.http.get("/api/chats/reject/files/content", params={"path": path})).status_code, 404)
        downloaded = await self.http.get(f"/api/guardrails/{record.snapshot['wait_id']}/file")
        self.assertEqual(downloaded.status_code, 200, downloaded.text)
        self.assertEqual(downloaded.content, self.original)

    async def capacity_publication(self, limit):
        coordinator = self.agent.workspace_durability
        coordinator.codec.max_bytes = 4096
        coordinator.codec.max_files = 100
        size = 4096 - len(self.original) - 1 if limit == "bytes" else 1
        contexts = ("resident-one", "resident-two") if limit == "aggregate" else ("capacity",)
        for context in contexts:
            content = b"x" * (4000 if limit == "aggregate" else size)
            self.model._responses = [self.terminal(f"from pathlib import Path; Path('keep').write_bytes({content!r})"),
                                     ModelResponse(message="Existing file saved")]
            task = await self.submit(context)
            self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED", task)
        self.model._responses = []
        record, batch, path = await self.pending("capacity")
        binding = WorkspaceBinding(self.tenant, record.owner_id, record.context_id)
        workspace = self.agent.chat_file_service.workspaces.workspace(binding)
        if limit == "entries":
            # The original, its manifest, batch directory, and inputs parent
            # require four entries in addition to existing sandbox directories.
            coordinator.codec.max_files = sum(1 for _ in workspace.rglob("*")) + 3
        before = {item.relative_to(workspace).as_posix(): item.read_bytes()
                  for item in workspace.rglob("*") if item.is_file()}
        calls = self.model.calls
        wait = self.agent.workflow_store.get_wait(record.snapshot["wait_id"], tenant_id=self.tenant)
        response = await self.http.post(f"/api/guardrails/{wait.wait_id}/decision",
            headers={"Origin": storage._ORIGIN},
            json={"decision": "allow", "subject_digest": interaction_digest(wait)})
        self.assertEqual(response.status_code, 200, response.text)
        self.model._responses = [ModelResponse(message="Accepted after space was freed")]
        await asyncio.to_thread(self.agent.resume_task, record.task_id)
        pending = self.agent.workflow_store.lookup_task(record.task_id)
        self.assertEqual(pending.snapshot.get("file_delivery_pending"), {
            "batch_id": batch["batch_id"], "error_code": "FILE_PUBLICATION_PENDING"})
        self.assertEqual(self.agent.chat_file_service.store.get(batch["batch_id"], self.tenant)["state"], "accepted_ready")
        self.assertFalse((workspace / "inputs").exists())
        self.assertEqual({item.relative_to(workspace).as_posix(): item.read_bytes()
                          for item in workspace.rglob("*") if item.is_file()}, before)
        self.assertEqual(self.model.calls, calls)
        original = await self.http.get(f"/api/guardrails/{wait.wait_id}/file")
        self.assertEqual(original.status_code, 200, original.text)
        self.assertEqual(original.content, self.original)
        released = WorkspaceBinding(self.tenant, record.owner_id, contexts[0])
        with coordinator.local_lock(released):
            (self.agent.chat_file_service.workspaces.workspace(released) / "keep").unlink()
        if limit == "aggregate":
            # Reclaim the selected chat's committed resident accounting too.
            with self.agent.workflow_store.database.transaction() as connection:
                row = connection.execute("SELECT latest_root_run_id FROM core_chats WHERE tenant_id=%s AND context_id=%s",
                                         (self.tenant, contexts[0])).fetchone()
            completed = self.agent.workflow_store.get(row["latest_root_run_id"], tenant_id=self.tenant, owner_id=record.owner_id)
            coordinator.transition(completed, None, lambda connection: None)
        await asyncio.to_thread(self.agent.resume_task, record.task_id)
        resumed = self.agent.workflow_store.lookup_task(record.task_id)
        self.assertEqual(resumed.state, "COMPLETED", {
            "pending": resumed.snapshot.get("file_delivery_pending"),
            "files": [item.relative_to(workspace).as_posix() for item in workspace.rglob("*")]})
        self.assertEqual((workspace / path).read_bytes(), self.original)
        self.assertEqual(self.agent.chat_file_service.store.get(batch["batch_id"], self.tenant)["state"], "published")

    async def test_publication_counts_manifest_bytes_before_mutating_workspace(self):
        await self.capacity_publication("bytes")

    async def test_publication_counts_parent_directories_and_metadata_before_mutating_workspace(self):
        await self.capacity_publication("entries")

    async def test_publication_respects_other_resident_chats_before_mutating_workspace(self):
        await self.capacity_publication("aggregate")

    async def test_private_cache_lease_blocks_fresh_app_until_expired_stage_is_swept(self):
        record, batch, _ = await self.pending("cache-lease")
        service = self.agent.chat_file_service
        stage = service._new_stage(self.tenant, "private-file-cache", batch["batch_id"], "private-cache", cache=True)
        os.mkdir(stage["batch_id"], dir_fd=service.uploads)
        await self.restart([])
        service = self.agent.chat_file_service
        binding = WorkspaceBinding(self.tenant, record.owner_id, record.context_id)
        with patch.object(service.blob_store, "get", side_effect=AssertionError("GET before cache capacity")) as get:
            with self.assertRaises(CoreError) as caught:
                service.review_material(batch["batch_id"], binding, run_id=record.run_id, task_id=record.task_id)
            self.assertEqual(caught.exception.code, "STORAGE_UNAVAILABLE")
            get.assert_not_called()
        with service.store.database.transaction() as connection:
            self.assertEqual(connection.execute("""SELECT count(*) AS n FROM core_chat_file_batches
                WHERE tenant_id=%s AND lease_owner='private-file-cache' AND cleaned_at IS NULL""",
                (self.tenant,)).fetchone()["n"], 1)
        with service.store.locked(stage["batch_id"], self.tenant) as (row, _):
            row["lease_expires_at"] = service.clock() - 1
        downloaded = service.owner_download(batch["batch_id"], binding, 0, run_id=record.run_id, task_id=record.task_id)
        self.assertEqual(downloaded["content"], self.original)
        self.assertIsNotNone(service.store.get(stage["batch_id"], self.tenant)["cleaned_at"])
        self.assertEqual(os.listdir(service.uploads), [])
        with service.store.database.transaction() as connection:
            self.assertEqual(connection.execute("""SELECT count(*) AS n FROM core_chat_file_batches
                WHERE tenant_id=%s AND lease_owner='private-file-cache' AND cleaned_at IS NULL""",
                (self.tenant,)).fetchone()["n"], 0)

    async def test_revoke_ready_batch_on_fresh_cache_does_not_create_workspace_before_restore(self):
        self.model._responses = [self.terminal("from pathlib import Path; Path('keep').write_bytes(b'existing project')"),
                                 ModelResponse(message="Existing file saved")]
        await self.submit("revoke-ready")
        self.model._responses = []
        record, batch, path = await self.pending("revoke-ready")
        binding = WorkspaceBinding(self.tenant, record.owner_id, record.context_id)
        self.agent.chat_file_service.record_decision(batch["batch_id"], binding,
            decision_ref="initial-allow", allow=True)
        await self.restart([])
        service = self.agent.chat_file_service
        workspace = service.workspaces.root.joinpath(*service.workspaces._components(binding))
        self.assertFalse(workspace.exists())
        with patch.object(service.blob_store, "get", side_effect=AssertionError("GET under review transaction")) as get:
            with service.store.database.transaction() as connection:
                service.record_decision(batch["batch_id"], binding, decision_ref="later-deny",
                                        allow=False, connection=connection)
            get.assert_not_called()
        self.assertEqual(service.store.get(batch["batch_id"], self.tenant)["state"], "excluded")
        self.assertFalse(workspace.exists())
        self.agent.workspace_durability.ensure_restored(binding)
        self.assertEqual((workspace / "keep").read_bytes(), b"existing project")
        self.assertFalse((workspace / path).exists())
        original = service.owner_download(batch["batch_id"], binding, 0, run_id=record.run_id, task_id=record.task_id)
        self.assertEqual(original["content"], self.original)

    async def ready_with_waiting_child(self, context):
        record, batch, path = await self.pending(context)
        binding = WorkspaceBinding(self.tenant, record.owner_id, context)
        service = self.agent.chat_file_service
        service.record_decision(batch["batch_id"], binding, decision_ref="review-allow", allow=True)
        # An already-created child can await its model without a live process;
        # its next terminal command still shares this chat's filesystem.
        child = self.agent.tool_runtime.environment_manager.create(EnvironmentSpec(
            self.tenant, record.run_id + "-child", "", (".",), (), {}, chat=binding))
        self.agent.tool_runtime.environment_manager.require_workspace_quiescent(binding)
        return record, batch, path, binding, child

    @staticmethod
    def child_command(child, code):
        return child.execute({"argv": [sys.executable, "-c", code]})

    async def test_failed_publication_removes_copy_before_waiting_child_can_read_or_checkpoint(self):
        record, batch, path, binding, child = await self.ready_with_waiting_child("publication-rollback")
        service = self.agent.chat_file_service
        with patch.object(service.blob_store, "put", side_effect=CoreError("STORAGE_UNAVAILABLE")):
            with self.assertRaises(CoreError) as caught:
                service.publish(batch["batch_id"], binding)
        self.assertEqual(caught.exception.code, "FILE_PUBLICATION_PENDING")
        self.assertEqual(service.store.get(batch["batch_id"], self.tenant)["state"], "accepted_ready")
        result = self.child_command(child,
            f"from pathlib import Path; print(Path({path!r}).exists()); Path('child.txt').write_text('safe')")
        self.assertEqual(result.stdout.strip(), "False")
        coordinator = self.agent.workspace_durability
        coordinator.transition(record, None, lambda connection: None)
        with coordinator.database.transaction() as connection:
            manifest = connection.execute("SELECT manifest FROM core_workspace_manifests WHERE tenant_id=%s AND context_id=%s",
                                          (self.tenant, record.context_id)).fetchone()["manifest"]
        self.assertNotIn(path, manifest["entries"])
        service.publish(batch["batch_id"], binding)
        result = self.child_command(child, f"from pathlib import Path; print(Path({path!r}).read_bytes().hex())")
        self.assertEqual(result.stdout.strip(), self.original.hex())
        self.assertEqual(service.owner_download(batch["batch_id"], binding, 0,
            run_id=record.run_id, task_id=record.task_id)["content"], self.original)

    async def test_unknown_publication_commit_fences_child_and_file_capture_until_readback(self):
        record, batch, path, binding, child = await self.ready_with_waiting_child("publication-unknown")
        service, coordinator = self.agent.chat_file_service, self.agent.workspace_durability
        transaction = coordinator.database.transaction
        unavailable = False

        @contextmanager
        def lose_commit_acknowledgement():
            nonlocal unavailable
            if unavailable:
                raise CoreError("DATABASE_UNAVAILABLE")
            with transaction() as connection:
                yield connection
                published = connection.execute("SELECT state FROM core_chat_file_batches WHERE batch_id=%s",
                                               (batch["batch_id"],)).fetchone()["state"] == "published"
            if published:
                unavailable = True
                raise CoreError("DATABASE_UNAVAILABLE")

        with patch.object(coordinator.database, "transaction", lose_commit_acknowledgement):
            with self.assertRaises(CoreError) as caught:
                service.publish(batch["batch_id"], binding)
            self.assertEqual(caught.exception.code, "FILE_PUBLICATION_PENDING")
            self.assertTrue(unavailable)
            processes = len(self.agent.tool_runtime.environment_manager.backend.launcher.processes)
            operations = (
                lambda: self.child_command(child, f"from pathlib import Path; Path({path!r}).unlink()"),
                lambda: coordinator.transition(record, None, lambda connection: None),
                lambda: self.agent.response_files_service.prepare(binding, [path], task_id=record.task_id,
                    run_id=record.run_id, limit_bytes=4096),
            )
            for operation in operations:
                with self.assertRaises(CoreError) as blocked:
                    operation()
                self.assertEqual(blocked.exception.code, "FILE_PUBLICATION_PENDING")
            self.assertEqual(len(self.agent.tool_runtime.environment_manager.backend.launcher.processes), processes)
        self.assertEqual(service.store.get(batch["batch_id"], self.tenant)["state"], "published")
        result = self.child_command(child,
            f"from pathlib import Path; p=Path({path!r}); print(p.read_bytes().hex()); p.write_bytes(b'child edit')")
        self.assertEqual(result.stdout.strip(), self.original.hex())
        service.publish(batch["batch_id"], binding)
        self.assertEqual((child.workspace / path).read_bytes(), b"child edit")
        # The simulated outage also prevented disposable-cache lease cleanup;
        # its normal expiry sweep must release that independent reservation.
        with patch.object(service, "clock", return_value=service.clock() + service.lease_seconds + 1):
            self.assertEqual(service.owner_download(batch["batch_id"], binding, 0,
                run_id=record.run_id, task_id=record.task_id)["content"], self.original)

    async def test_unknown_uncommitted_publication_stays_fenced_until_copy_can_be_removed(self):
        record, batch, path, binding, child = await self.ready_with_waiting_child("publication-uncommitted")
        service, coordinator = self.agent.chat_file_service, self.agent.workspace_durability
        transaction = coordinator.database.transaction
        unavailable = False

        @contextmanager
        def unavailable_readback():
            if unavailable:
                raise CoreError("DATABASE_UNAVAILABLE")
            with transaction() as connection:
                yield connection

        def failed_upload(content):
            nonlocal unavailable
            unavailable = True
            raise CoreError("STORAGE_UNAVAILABLE")

        with patch.object(coordinator.database, "transaction", unavailable_readback), \
                patch.object(service.blob_store, "put", failed_upload):
            with self.assertRaises(CoreError) as caught:
                service.publish(batch["batch_id"], binding)
            self.assertEqual(caught.exception.code, "FILE_PUBLICATION_PENDING")
            self.assertTrue((child.workspace / path).exists())
            with self.assertRaises(CoreError) as blocked:
                self.child_command(child, f"from pathlib import Path; print(Path({path!r}).read_bytes())")
            self.assertEqual(blocked.exception.code, "FILE_PUBLICATION_PENDING")
        result = self.child_command(child, f"from pathlib import Path; print(Path({path!r}).exists())")
        self.assertEqual(result.stdout.strip(), "False")
        self.assertEqual(service.store.get(batch["batch_id"], self.tenant)["state"], "accepted_ready")
        with patch.object(service, "clock", return_value=service.clock() + service.lease_seconds + 1):
            service.publish(batch["batch_id"], binding)
        self.assertEqual((child.workspace / path).read_bytes(), self.original)
