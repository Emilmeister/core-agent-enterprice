"""Durable exact-selection cleanup, real filesystem and canonical admission."""
import asyncio
import copy
import os
import json
import threading
import unittest
import uuid
from unittest.mock import patch

from core_agent.errors import CoreError
from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
from core_agent.workspace import WorkspaceBinding
from core_agent.workspace_cleanup import WorkspaceCleanupService
from tests.test_auth import AuthAppTestCase, TEST_DATABASE_URL
from tests import test_admission as admission_tests


class Crash(BaseException):
    pass


class WorkspaceCleanupTests(AuthAppTestCase):
    context = admission_tests.AuthAdmissionTests.context
    async def asyncSetUp(self):
        # Crash and malformed-protocol fixtures drive recovery explicitly.
        # A separate coordinator would execute their still-valid durable intent.
        with patch("core_agent.runtime.CoreAgent.recover_workflows"):
            await super().asyncSetUp()
        self.agent = self.app.state.core_agent
        manager = self.agent.tool_runtime.environment_manager
        self.admission = manager.validate_workspace_scope.__self__
        self.workspaces = manager.backend.chats
        self.service = WorkspaceCleanupService(self.admission, self.workspaces)
        self.admission.workspace_cleanup = self.service
        task = await self.submit("external-a", "initial", "cleanup")
        record = self.agent.workflow_store.lookup_task(task["id"])
        self.binding = WorkspaceBinding(record.tenant_id, record.owner_id, record.context_id)
        self.folder = self.workspaces.workspace(self.binding)

    async def asyncTearDown(self):
        self.agent.close()
        if self.service.database:
            # Recovery scans every tenant; retained intents belong to this fixture.
            with self.service.database.transaction() as connection:
                connection.execute("DELETE FROM core_workspace_cleanups WHERE tenant_id=%s", (self.binding.tenant_id,))

    def restart(self):
        self.service = WorkspaceCleanupService(self.admission, self.workspaces)
        self.admission.workspace_cleanup = self.service

    async def assert_pending_blocks_new_admission(self):
        self.assertTrue(self.service.preview_state(self.binding)["cleanup_pending"])
        handler = self.app.state.a2a_request_handler.admission_handler
        with self.assertRaises(CoreError) as error:
            await handler(admission_tests.AuthAdmissionTests.sdk_message("new-root", "cleanup"), self.context())
        self.assertEqual(error.exception.code, "WORKSPACE_CLEANUP_PENDING")
        duplicate = await handler(admission_tests.AuthAdmissionTests.sdk_message("initial", "cleanup"), self.context())
        self.assertIsNone(duplicate.run_id)

    def selection(self, *paths):
        for path in paths:
            (self.folder / path).parent.mkdir(parents=True, exist_ok=True)
            (self.folder / path).write_bytes(path.encode())
        revision = self.service.preview_state(self.binding)["workspace_revision"]
        rows = self.workspaces.preview(self.binding, workspace_revision=revision)["files"]
        return [{"path": item["path"], "identity_token": item["identity_token"]} for item in rows if item["path"] in paths]

    async def delete(self, files, request_id="request", actor="owner-actor"):
        return await self.service.delete(self.binding.tenant_id, self.binding.context_id, actor,
                                         {"request_id": request_id, "files": files})

    async def test_empty_canonical_chat_cleanup_without_fabricated_root(self):
        context = "empty-scheduled-chat"
        binding = WorkspaceBinding(self.binding.tenant_id, self.binding.owner_id, context)
        if self.service.database:
            with self.service.database.transaction() as connection:
                connection.execute("INSERT INTO core_chats(tenant_id,context_id,owner_id) VALUES (%s,%s,%s)",
                                   (binding.tenant_id, context, binding.owner_id))
        else:
            self.admission.chats[(binding.tenant_id, context)] = {
                "owner_id": binding.owner_id, "latest_root_run_id": None, "workspace_revision": 0}
        self.assertEqual(self.service.preview_state(binding), {"workspace_revision": 0, "cleanup_pending": False})
        folder = self.workspaces.workspace(binding)
        (folder / "old.txt").write_text("safe")
        selected = self.workspaces.preview(binding)["files"][0]
        receipt = await self.service.delete(binding.tenant_id, context, "owner-actor", {
            "request_id": "empty-chat-cleanup", "files": [
                {"path": selected["path"], "identity_token": selected["identity_token"]}]})
        self.assertEqual(receipt["totals"], {"deleted": 1, "skipped": 0, "errors": 0, "deleted_bytes": 4})
        self.assertFalse((folder / "old.txt").exists())
        self.assertEqual(self.service.preview_state(binding)["workspace_revision"], 1)
        with self.service._locked(self.binding.tenant_id, self.binding.context_id) as (_, chat, _, _connection):
            original_run_id = chat["latest_root_run_id"]
        if self.service.database:
            with self.service.database.transaction() as connection:
                row = connection.execute("SELECT latest_root_run_id FROM core_chats WHERE tenant_id=%s AND context_id=%s",
                                         (binding.tenant_id, context)).fetchone()
                self.assertIsNone(row["latest_root_run_id"])
                connection.execute("UPDATE core_chats SET latest_root_run_id=%s WHERE tenant_id=%s AND context_id=%s",
                                   (original_run_id, binding.tenant_id, context))
        else:
            self.assertIsNone(self.admission.chats[(binding.tenant_id, context)]["latest_root_run_id"])
            self.admission.chats[(binding.tenant_id, context)]["latest_root_run_id"] = original_run_id
        with self.assertRaises(CoreError) as error:
            self.service.preview_state(binding)
        self.assertEqual(error.exception.code, "TASK_NOT_FOUND")

    async def test_selected_only_revision_idempotency_and_detached_receipt(self):
        files = self.selection("a", "nested/b")
        (self.folder / "unselected").write_text("keep")
        receipt = await self.delete(files)
        self.assertEqual(receipt["state"], "completed")
        self.assertEqual(receipt["workspace_revision"], 1)
        self.assertEqual(receipt["totals"], {"deleted": 2, "skipped": 0, "errors": 0, "deleted_bytes": 9})
        self.assertFalse((self.folder / "a").exists())
        self.assertEqual((self.folder / "unselected").read_text(), "keep")
        self.assertEqual(await self.delete(files, actor="another-owner"), receipt)
        receipt["results"].clear()
        stored = await self.service.get(self.binding.tenant_id, "cleanup", "request")
        self.assertEqual(len(stored["results"]), 2)
        self.assertEqual(self.service.preview_state(self.binding), {"workspace_revision": 1, "cleanup_pending": False})
        with self.assertRaises(CoreError) as error:
            await self.delete(list(reversed(files)))
        self.assertEqual(error.exception.code, "CLEANUP_REQUEST_CONFLICT")

    async def test_stale_changed_missing_and_empty_selection_do_not_delete(self):
        files = self.selection("changed", "missing")
        (self.folder / "changed").write_text("replacement")
        (self.folder / "missing").unlink()
        result = await self.delete(files)
        self.assertEqual([item["status"] for item in result["results"]], ["skipped", "skipped"])
        self.assertEqual([item["reason"] for item in result["results"]], ["identity_changed", "missing"])
        self.assertEqual(result["workspace_revision"], 0)
        self.assertEqual((self.folder / "changed").read_text(), "replacement")
        empty = await self.delete([], "empty")
        self.assertEqual(empty["results"], [])
        self.assertEqual(empty["workspace_revision"], 0)

    async def test_busy_wait_refuses_without_persisting_intent(self):
        files = self.selection("keep")
        self.agent.model = ScriptedModel([ModelResponse(tool_requests=(
            ToolRequest("ask", "core_ask_owner", {"question": "Confirm?"}),))])
        await self.submit("owner-a", "waiting", "cleanup")
        with self.assertRaises(CoreError) as error:
            await self.delete(files)
        self.assertEqual(error.exception.code, "CONTEXT_BUSY")
        with self.assertRaises(CoreError) as error:
            await self.service.get(self.binding.tenant_id, "cleanup", "request")
        self.assertEqual(error.exception.code, "FILE_CLEANUP_NOT_FOUND")
        self.assertTrue((self.folder / "keep").exists())

    async def test_crashes_after_committed_intent_and_capture_recover_same_operation(self):
        for phase in ("intent", "capture", "proof", "unlink", "commit"):
            with self.subTest(phase=phase):
                files = self.selection(phase)
                original_rename = __import__("core_agent.workspace_cleanup", fromlist=["_rename"])._rename
                original_marker = self.service._marker
                original_unlink = os.unlink
                original_save = self.service._save
                def renamed(*args):
                    original_rename(*args)
                    raise Crash()
                def marked(stage, name, value=None):
                    result = original_marker(stage, name, value)
                    if value is not None and value["phase"] == "delete_ready":
                        raise Crash()
                    return result
                def unlinked(path, *args, **kwargs):
                    original_unlink(path, *args, **kwargs)
                    if str(path).endswith(".file"):
                        raise Crash()
                def saved(operation, connection, **kwargs):
                    if not kwargs.get("insert"):
                        raise Crash()
                    return original_save(operation, connection, **kwargs)
                target = {"intent": patch.object(self.service, "_execute", side_effect=Crash),
                          "capture": patch("core_agent.workspace_cleanup._rename", side_effect=renamed),
                          "proof": patch.object(self.service, "_marker", side_effect=marked),
                          "unlink": patch("core_agent.workspace_cleanup.os.unlink", side_effect=unlinked),
                          "commit": patch.object(self.service, "_save", side_effect=saved)}[phase]
                with target, self.assertRaises(Crash):
                    await self.delete(files, phase)
                await self.assert_pending_blocks_new_admission()
                before = await self.service.get(self.binding.tenant_id, "cleanup", phase)
                self.restart()
                recovered = await self.delete(files, phase)
                self.assertEqual(recovered["operation_id"], before["operation_id"])
                self.assertEqual(recovered["state"], "completed")
                self.assertEqual(recovered["results"][0]["status"], "deleted")
                self.assertFalse((self.folder / phase).exists())
                self.assertFalse(self.service.preview_state(self.binding)["cleanup_pending"])

    async def test_same_inode_write_preserving_size_and_mtime_is_restored_not_deleted(self):
        files = self.selection("file")
        original = __import__("core_agent.workspace_cleanup", fromlist=["_rename"])._rename
        def replaced(source_fd, source, target_fd, target):
            if source == "file":
                previous = os.stat(source, dir_fd=source_fd)
                (self.folder / source).write_bytes(b"EDIT")
                os.utime(source, ns=(previous.st_atime_ns, previous.st_mtime_ns), dir_fd=source_fd)
            return original(source_fd, source, target_fd, target)
        with patch("core_agent.workspace_cleanup._rename", side_effect=replaced):
            receipt = await self.delete(files)
        self.assertEqual(receipt["results"], [{"path": "file", "status": "skipped", "reason": "identity_changed"}])
        self.assertEqual((self.folder / "file").read_bytes(), b"EDIT")
        self.assertEqual(receipt["workspace_revision"], 0)

    async def test_restore_conflict_preserves_both_files_and_recovery_barrier(self):
        files = self.selection("file")
        original = __import__("core_agent.workspace_cleanup", fromlist=["_rename"])._rename
        def replaced(source_fd, source, target_fd, target):
            if source == "file":
                (self.folder / "file").unlink()
                (self.folder / "file").write_bytes(b"captured replacement")
                original(source_fd, source, target_fd, target)
                (self.folder / "file").write_bytes(b"new unselected file")
                return None
            return original(source_fd, source, target_fd, target)
        with patch("core_agent.workspace_cleanup._rename", side_effect=replaced):
            receipt = await self.delete(files)
        self.assertEqual(receipt["state"], "reconciliation")
        self.assertEqual(receipt["results"][0]["reason"], "reconciliation_required")
        stage = self.workspaces.root / "private" / "cleanup" / receipt["operation_id"] / "0.file"
        self.assertEqual(stage.read_bytes(), b"captured replacement")
        self.assertEqual((self.folder / "file").read_bytes(), b"new unselected file")
        await self.assert_pending_blocks_new_admission()
        self.restart()
        await asyncio.to_thread(self.service.recover)
        self.assertTrue(stage.exists())
        (self.folder / "file").rename(self.folder / "unselected")
        await asyncio.to_thread(self.service.recover)
        receipt = await self.service.get(self.binding.tenant_id, "cleanup")
        self.assertEqual(receipt["state"], "completed")
        self.assertEqual(receipt["results"][0]["status"], "skipped")
        self.assertEqual((self.folder / "file").read_bytes(), b"captured replacement")
        self.assertEqual((self.folder / "unselected").read_bytes(), b"new unselected file")

    async def test_scope_validation_and_immutable_original_selection(self):
        files = self.selection("file")
        for payload in ({"request_id": "", "files": files}, {"request_id": "x\0", "files": files},
                        {"request_id": "x", "files": files * 2}, {"request_id": "x", "files": files, "age": 0},
                        {"request_id": "x", "files": [{"path": "../file", "identity_token": files[0]["identity_token"]}]}):
            with self.assertRaises(CoreError) as error:
                await self.service.delete(self.binding.tenant_id, "cleanup", "actor", payload)
            self.assertEqual(error.exception.code, "REQUEST_INVALID")
        with self.assertRaises(CoreError) as error:
            await self.service.delete("other-company", "cleanup", "actor", {"request_id": "x", "files": files})
        self.assertEqual(error.exception.code, "TASK_NOT_FOUND")
        with patch.object(self.service, "_execute", side_effect=Crash), self.assertRaises(Crash):
            await self.delete(files)
        receipt = await self.service.get(self.binding.tenant_id, "cleanup")
        self.assertEqual(receipt["files"], files)
        self.assertNotIn("sha256", str(receipt))
        changed = copy.deepcopy(files)
        changed[0]["identity_token"] = "v2:" + "0" * 64
        with self.assertRaises(CoreError) as error:
            await self.delete(changed)
        self.assertEqual(error.exception.code, "CLEANUP_REQUEST_CONFLICT")

    async def test_changed_parent_after_capture_restores_object_in_pinned_parent(self):
        files = self.selection("folder/file")
        original = __import__("core_agent.workspace_cleanup", fromlist=["_rename"])._rename
        def moved(source_fd, source, target_fd, target):
            result = original(source_fd, source, target_fd, target)
            if source == "file":
                (self.folder / "folder").rename(self.folder / "moved")
                (self.folder / "folder").mkdir()
                (self.folder / "folder" / "file").write_text("new unselected")
            return result
        with patch("core_agent.workspace_cleanup._rename", side_effect=moved):
            receipt = await self.delete(files)
        self.assertEqual(receipt["results"][0]["status"], "skipped")
        self.assertEqual((self.folder / "moved" / "file").read_text(), "folder/file")
        self.assertEqual((self.folder / "folder" / "file").read_text(), "new unselected")

    async def test_corrupt_private_deletion_proof_fails_closed_and_keeps_barrier(self):
        files = self.selection("file")
        unlink = os.unlink
        def unlinked(path, *args, **kwargs):
            unlink(path, *args, **kwargs)
            if str(path).endswith(".file"):
                raise Crash()
        with patch("core_agent.workspace_cleanup.os.unlink", side_effect=unlinked), self.assertRaises(Crash):
            await self.delete(files)
        receipt = await self.service.get(self.binding.tenant_id, "cleanup", "request")
        marker = self.workspaces.root / "private" / "cleanup" / receipt["operation_id"] / "0.json"
        data = json.loads(marker.read_text())
        data["captured"] = None
        marker.write_text(json.dumps(data))
        self.restart()
        with self.assertRaises(CoreError) as error:
            await self.delete(files)
        self.assertEqual(error.exception.code, "WORKSPACE_CLEANUP_INVALID")
        await self.assert_pending_blocks_new_admission()

    async def test_unsafe_and_protected_paths_skipped_partial_permission_error(self):
        files = self.selection("allowed", "denied", "link", "hardlink", "fifo")
        (self.folder / "link").unlink()
        (self.folder / "link").symlink_to("allowed")
        (self.folder / "hardlink").unlink()
        os.link(self.folder / "denied", self.folder / "hardlink")
        (self.folder / "fifo").unlink()
        os.mkfifo(self.folder / "fifo")
        batch = self.folder / "attachments" / str(uuid.uuid4())
        batch.mkdir(parents=True)
        (batch / ".manifest.json").write_text("control")
        files.append({"path": f"attachments/{batch.name}/.manifest.json", "identity_token": "v2:" + "0" * 64})
        result = await self.delete(files)
        statuses = {item["path"]: item for item in result["results"]}
        self.assertEqual(statuses["allowed"]["status"], "deleted")
        for path in ("denied", "link", "hardlink", "fifo"):
            self.assertEqual(statuses[path]["reason"], "unsafe_file")
        self.assertEqual(statuses[files[-1]["path"]]["reason"], "protected_file")
        self.assertTrue((batch / ".manifest.json").exists())
        files = self.selection("good", "error")
        original = self.service._hash
        def denied(parent, name):
            if name == "error":
                raise PermissionError("private absolute host path must not leak")
            return original(parent, name)
        with patch.object(self.service, "_hash", side_effect=denied):
            result = await self.delete(files, "partial")
        self.assertEqual(result["totals"], {"deleted": 1, "skipped": 0, "errors": 1, "deleted_bytes": 4})
        self.assertEqual(result["workspace_revision"], 2)
        self.assertNotIn("private absolute", str(result))
        self.assertTrue((self.folder / "error").exists())

    async def test_revision_invalidates_prior_cursor_and_tokens(self):
        files = self.selection("a", "b", "c")
        page = self.workspaces.preview(self.binding, limit=1, workspace_revision=0)
        await self.delete(files[:1])
        revision = self.service.preview_state(self.binding)["workspace_revision"]
        with self.assertRaises(CoreError) as error:
            self.workspaces.preview(self.binding, limit=1, cursor=page["next_cursor"], workspace_revision=revision)
        self.assertEqual(error.exception.code, "REQUEST_INVALID")
        result = await self.delete(files[1:], "stale")
        self.assertTrue(all(item["reason"] == "identity_changed" for item in result["results"]))
        self.assertTrue((self.folder / "b").exists())

    async def test_concurrent_duplicate_cleanup_has_one_intent_and_one_mutation(self):
        files = self.selection("file")
        first, second = await asyncio.gather(self.delete(files), self.delete(files, actor="another"))
        self.assertEqual(first, second)
        self.assertEqual(first["workspace_revision"], 1)
        self.assertEqual(first["totals"]["deleted"], 1)

    async def test_new_root_cannot_enter_between_intent_and_mutation(self):
        files = self.selection("file")
        entered, release = threading.Event(), threading.Event()
        execute = self.service._execute
        def paused(*args):
            entered.set()
            release.wait(3)
            return execute(*args)
        with patch.object(self.service, "_execute", side_effect=paused):
            deletion = asyncio.create_task(self.delete(files))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                await self.assert_pending_blocks_new_admission()
            finally:
                release.set()
            self.assertEqual((await deletion)["state"], "completed")
        task = await self.submit("owner-a", "after-cleanup", "cleanup")
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertFalse((self.folder / "file").exists())

    async def test_restore_crash_never_becomes_deleted_receipt(self):
        files = self.selection("file")
        original = __import__("core_agent.workspace_cleanup", fromlist=["_rename"])._rename
        def changed_and_restored(source_fd, source, target_fd, target):
            if source == "file":
                (self.folder / "file").write_bytes(b"changed")
            original(source_fd, source, target_fd, target)
            if source == "0.file":
                raise Crash()
        with patch("core_agent.workspace_cleanup._rename", side_effect=changed_and_restored), self.assertRaises(Crash):
            await self.delete(files)
        self.assertEqual((self.folder / "file").read_bytes(), b"changed")
        self.restart()
        result = await self.delete(files)
        self.assertEqual(result["results"][0]["status"], "skipped")
        self.assertEqual(result["totals"]["deleted"], 0)
        self.assertEqual(result["workspace_revision"], 0)

    async def test_missing_and_foreign_receipt_not_found_and_get_is_readonly(self):
        for tenant, context in (("foreign", "cleanup"), (self.binding.tenant_id, "missing")):
            with self.assertRaises(CoreError) as error:
                await self.service.get(tenant, context)
            self.assertEqual(error.exception.code, "FILE_CLEANUP_NOT_FOUND")
        files = self.selection("file")
        with patch.object(self.service, "_execute", side_effect=Crash), self.assertRaises(Crash):
            await self.delete(files)
        with patch.object(self.service, "_execute", side_effect=AssertionError("GET must not execute")):
            first = await self.service.get(self.binding.tenant_id, "cleanup", "request")
            second = await self.service.get(self.binding.tenant_id, "cleanup")
        self.assertEqual(first, second)
        self.assertTrue((self.folder / "file").exists())

    async def test_empty_selection_creates_no_private_stage(self):
        before = sorted(str(path) for path in self.workspaces.root.rglob("*"))
        result = await self.delete([])
        self.assertEqual(sorted(str(path) for path in self.workspaces.root.rglob("*")), before)
        self.assertEqual(result["state"], "completed")

    async def test_partial_unlink_crash_recovers_each_item_once_and_preserves_new_file(self):
        files = self.selection("a", "b")
        unlink = os.unlink
        def unlinked(path, *args, **kwargs):
            unlink(path, *args, **kwargs)
            if path == "0.file":
                (self.folder / "a").write_text("new unselected")
                raise Crash()
        with patch("core_agent.workspace_cleanup.os.unlink", side_effect=unlinked), self.assertRaises(Crash):
            await self.delete(files)
        self.restart()
        result = await self.delete(files)
        self.assertEqual(result["totals"], {"deleted": 2, "skipped": 0, "errors": 0, "deleted_bytes": 2})
        self.assertEqual((self.folder / "a").read_text(), "new unselected")
        self.assertFalse((self.folder / "b").exists())
        self.assertEqual(result["workspace_revision"], 1)

    async def test_unknown_operation_version_and_private_result_fields_fail_closed(self):
        files = self.selection("file")
        with patch.object(self.service, "_execute", side_effect=Crash), self.assertRaises(Crash):
            await self.delete(files)
        with self.service._locked(self.binding.tenant_id, "cleanup") as (binding, _, _, connection):
            original = self.service._load(binding, "request", connection)
        for mutate in (lambda row: row.update(storage_version=2),
                       lambda row: row["results"][0].update(private="not public"),
                       lambda row: row["results"][0].update(size="private data")):
            row = copy.deepcopy(original)
            mutate(row)
            load = patch.object(self.service, "_load", return_value=row)
            load.start()
            self.addCleanup(load.stop)
            with self.assertRaises(CoreError) as error:
                await self.service.get(self.binding.tenant_id, "cleanup", "request")
            self.assertEqual(error.exception.code, "WORKSPACE_CLEANUP_INVALID")
            with self.assertRaises(CoreError):
                await self.delete(files)
            self.assertTrue((self.folder / "file").exists())
            load.stop()
            await self.assert_pending_blocks_new_admission()

    async def test_each_selected_proof_is_validated_before_filesystem_io(self):
        files = self.selection("first", "second")
        with patch.object(self.service, "_execute", side_effect=Crash), self.assertRaises(Crash):
            await self.delete(files)
        with self.service._locked(self.binding.tenant_id, "cleanup") as (binding, _, _, connection):
            original = self.service._load(binding, "request", connection)
        proof = original["selection"][0]["proof"]
        malformed = [{}, {key: value for key, value in proof.items() if key != "stat"},
                     {**proof, "stat": None}, {**proof, "stat": proof["stat"][:6]},
                     {**proof, "stat": dict(enumerate(proof["stat"]))},
                     {**proof, "stat": [True, *proof["stat"][1:]]},
                     {**proof, "sha256": "not-a-sha256"}, {**proof, "sha256": None},
                     {**proof, "parents": None}, {**proof, "parents": []},
                     {**proof, "parents": [[1]]}, {**proof, "parents": ["12"]}]
        for bad in malformed:
            with self.subTest(proof=bad):
                row = copy.deepcopy(original)
                row["selection"][0]["proof"] = bad
                with patch.object(self.service, "_load", return_value=row), \
                        patch.object(self.service, "_stage", side_effect=AssertionError("invalid proof reached filesystem")):
                    with self.assertRaises(CoreError) as error:
                        await self.service.get(self.binding.tenant_id, "cleanup")
                    self.assertEqual(error.exception.code, "WORKSPACE_CLEANUP_INVALID")
                    with self.assertRaises(CoreError) as error:
                        await self.delete(files)
                    self.assertEqual(error.exception.code, "WORKSPACE_CLEANUP_INVALID")
                    self.assertEqual(await asyncio.to_thread(self.service.recover), 1)
                self.assertTrue((self.folder / "first").exists())
                self.assertTrue((self.folder / "second").exists())
                await self.assert_pending_blocks_new_admission()

    async def test_recovered_capture_fsyncs_both_directories_before_unlink(self):
        files = self.selection("nested/file")
        rename = __import__("core_agent.workspace_cleanup", fromlist=["_rename"])._rename
        def captured(*args):
            rename(*args)
            raise Crash()
        with patch("core_agent.workspace_cleanup._rename", side_effect=captured), self.assertRaises(Crash):
            await self.delete(files)
        receipt = await self.service.get(self.binding.tenant_id, "cleanup")
        source = os.stat(self.folder / "nested")
        staged = os.stat(self.workspaces.root / "private" / "cleanup" / receipt["operation_id"])
        expected = {(source.st_dev, source.st_ino), (staged.st_dev, staged.st_ino)}
        fsync, unlink, synced = os.fsync, os.unlink, set()
        def synced_directory(descriptor):
            fsync(descriptor)
            info = os.fstat(descriptor)
            synced.add((info.st_dev, info.st_ino))
        def deleted(path, *args, **kwargs):
            if path == "0.file":
                self.assertTrue(expected <= synced, "capture must be durable in both parents before unlink")
            return unlink(path, *args, **kwargs)
        self.restart()
        with patch("core_agent.workspace_cleanup.os.fsync", side_effect=synced_directory), \
                patch("core_agent.workspace_cleanup.os.unlink", side_effect=deleted):
            receipt = await self.delete(files)
        self.assertEqual(receipt["state"], "completed")
        self.assertEqual(receipt["results"][0]["status"], "deleted")

    async def test_nonobject_journal_keeps_barrier_and_other_chat_recovers(self):
        files = self.selection("file")
        unlink = os.unlink
        def deleted(path, *args, **kwargs):
            unlink(path, *args, **kwargs)
            if path == "0.file":
                raise Crash()
        with patch("core_agent.workspace_cleanup.os.unlink", side_effect=deleted), self.assertRaises(Crash):
            await self.delete(files)
        receipt = await self.service.get(self.binding.tenant_id, "cleanup")
        marker = self.workspaces.root / "private" / "cleanup" / receipt["operation_id"] / "0.json"
        original_marker = json.loads(marker.read_text())
        for malformed in ([], None, "invalid", False, {**original_marker, "phase": []}, {**original_marker, "phase": {}}):
            with self.subTest(malformed=malformed):
                marker.write_text(json.dumps(malformed))
                with self.assertRaises(CoreError) as error:
                    await self.delete(files)
                self.assertEqual(error.exception.code, "WORKSPACE_CLEANUP_INVALID")
                self.assertEqual(json.loads(marker.read_text()), malformed)
                await self.assert_pending_blocks_new_admission()
        task = await self.submit("external-a", "other-root", "other")
        record = self.agent.workflow_store.lookup_task(task["id"])
        other = WorkspaceBinding(record.tenant_id, record.owner_id, record.context_id)
        (self.workspaces.workspace(other) / "safe").write_text("safe")
        item = self.workspaces.preview(other)["files"][0]
        body = {"request_id": "other-request", "files": [{key: item[key] for key in ("path", "identity_token")}]}
        with patch.object(self.service, "_execute", side_effect=Crash), self.assertRaises(Crash):
            await self.service.delete(other.tenant_id, other.context_id, "actor", body)
        self.restart()
        self.assertEqual(await asyncio.to_thread(self.service.recover), 2)
        self.assertEqual((await self.service.get(other.tenant_id, "other"))["state"], "completed")
        self.assertFalse((self.workspaces.workspace(other) / "safe").exists())
        self.assertTrue(self.service.preview_state(self.binding)["cleanup_pending"])

    async def test_bounded_recovery_rotates_past_unrepairable_old_operation(self):
        files = self.selection("file")
        with patch("core_agent.workspace_cleanup.uuid.uuid4", return_value=uuid.UUID(int=1)), \
                patch.object(self.service, "_execute", side_effect=Crash), self.assertRaises(Crash):
            await self.delete(files)
        receipt = await self.service.get(self.binding.tenant_id, "cleanup")
        stage = self.workspaces.root / "private" / "cleanup" / receipt["operation_id"]
        stage.mkdir(parents=True)
        (stage / "0.json").write_text("[]")
        task = await self.submit("external-a", "other-root", "other")
        record = self.agent.workflow_store.lookup_task(task["id"])
        binding = WorkspaceBinding(record.tenant_id, record.owner_id, record.context_id)
        (self.workspaces.workspace(binding) / "file").write_text("other")
        item = self.workspaces.preview(binding)["files"][0]
        body = {"request_id": "other", "files": [{key: item[key] for key in ("path", "identity_token")}]}
        with patch("core_agent.workspace_cleanup.uuid.uuid4", return_value=uuid.UUID(int=2)), \
                patch.object(self.service, "_execute", side_effect=Crash), self.assertRaises(Crash):
            await self.service.delete(binding.tenant_id, "other", "actor", body)
        self.restart()
        self.assertEqual(await asyncio.to_thread(self.service.recover, 1), 1)
        self.assertEqual((await self.service.get(binding.tenant_id, "other"))["state"], "pending")
        self.assertEqual(await asyncio.to_thread(self.service.recover, 1), 1)
        self.assertEqual((await self.service.get(binding.tenant_id, "other"))["state"], "completed")
        self.assertEqual(await asyncio.to_thread(self.service.recover, 1), 1)
        self.assertTrue(self.service.preview_state(self.binding)["cleanup_pending"])

    async def test_impossible_completed_receipt_does_not_release_admission_barrier(self):
        files = self.selection("file")
        rename = __import__("core_agent.workspace_cleanup", fromlist=["_rename"])._rename
        def captured(*args):
            rename(*args)
            raise Crash()
        with patch("core_agent.workspace_cleanup._rename", side_effect=captured), self.assertRaises(Crash):
            await self.delete(files)
        with self.service._locked(self.binding.tenant_id, "cleanup") as (binding, _, _, connection):
            original = self.service._load(binding, "request", connection)
        corrupt = {**original, "state": "completed"}
        with patch.object(self.service, "_load", return_value=corrupt):
            with self.assertRaises(CoreError) as error:
                await self.service.get(self.binding.tenant_id, "cleanup")
            self.assertEqual(error.exception.code, "WORKSPACE_CLEANUP_INVALID")
        if self.service.database:
            # A corrupt on-disk row is represented at the read boundary; SQL separately
            # rejects creating this state even for an otherwise valid pending operation.
            original_ready = self.service.check_ready
            with self.service.database.transaction() as connection:
                connection.execute("UPDATE core_workspace_cleanups SET state='reconciliation' WHERE operation_id=%s",
                                   (original["operation_id"],))
            with patch.object(self.service, "_validate", side_effect=CoreError("WORKSPACE_CLEANUP_INVALID")):
                with self.assertRaises(CoreError) as error:
                    original_ready(self.binding)
                self.assertEqual(error.exception.code, "WORKSPACE_CLEANUP_PENDING")
        else:
            key = (self.binding.tenant_id, "cleanup", "request")
            self.admission._workspace_cleanups[key] = corrupt
            await self.assert_pending_blocks_new_admission()
            self.admission._workspace_cleanups[key] = original

    async def test_completed_receipt_revision_matches_actual_deleted_outcomes(self):
        files = self.selection("file")
        await self.delete(files)
        with self.service._locked(self.binding.tenant_id, "cleanup") as (binding, _, _, connection):
            original = self.service._load(binding, "request", connection)
        invalid = [dict(original, workspace_revision=0),
                   dict(original, state="pending"),
                   dict(original, results=[{"path": "file", "status": "error", "reason": "reconciliation_required"}])]
        for row in invalid:
            with patch.object(self.service, "_load", return_value=row):
                with self.assertRaises(CoreError) as error:
                    await self.service.get(self.binding.tenant_id, "cleanup")
                self.assertEqual(error.exception.code, "WORKSPACE_CLEANUP_INVALID")
                self.assertTrue(self.service.preview_state(self.binding)["cleanup_pending"])


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL not set")
class PostgresWorkspaceCleanupTests(WorkspaceCleanupTests):
    use_postgres = True

    async def test_database_intent_and_terminal_receipt_are_immutable(self):
        import psycopg
        files = self.selection("file")
        with patch.object(self.service, "_execute", side_effect=Crash), self.assertRaises(Crash):
            await self.delete(files)
        key = (self.binding.tenant_id, self.binding.context_id, "request")
        with self.assertRaises(psycopg.Error):
            with self.service.database.transaction() as connection:
                connection.execute("UPDATE core_workspace_cleanups SET selection='[]'::jsonb WHERE tenant_id=%s AND context_id=%s AND request_id=%s", key)
        with self.assertRaises(psycopg.Error):
            with self.service.database.transaction() as connection:
                connection.execute("UPDATE core_workspace_cleanups SET actor_id='different' WHERE tenant_id=%s AND context_id=%s AND request_id=%s", key)
        with self.assertRaises(psycopg.Error):
            with self.service.database.transaction() as connection:
                connection.execute("UPDATE core_workspace_cleanups SET state='completed' WHERE tenant_id=%s AND context_id=%s AND request_id=%s", key)
        receipt = await self.delete(files)
        with self.assertRaises(psycopg.Error):
            with self.service.database.transaction() as connection:
                connection.execute("UPDATE core_workspace_cleanups SET state='pending' WHERE tenant_id=%s AND context_id=%s AND request_id=%s", key)
        self.assertEqual(await self.service.get(self.binding.tenant_id, "cleanup", "request"), receipt)
