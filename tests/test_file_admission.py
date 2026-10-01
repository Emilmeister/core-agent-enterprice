"""Canonical admission with real private storage; HTTP binary gate stays closed."""
import asyncio
import copy
import os
import threading
import unittest

import httpx
from unittest.mock import patch

from a2a.types import ListTasksRequest, Message, Part, Role, TaskState
from a2a.utils.errors import InvalidParamsError, TaskNotFoundError

from core_agent.a2a_sdk import CoreAgentExecutor
from core_agent.admission import fingerprint, prepare_files, reject_files
from core_agent.a2a import parse_run_request
from core_agent.errors import CoreError
from core_agent.database import PostgresDatabase
from tests.app_support import create_app
from core_agent.workspace import WorkspaceBinding
from core_agent.workflow import SuspendedRun
from tests import test_admission as admission_tests
from tests.test_auth import AuthAppTestCase, TEST_DATABASE_URL


class FileAdmissionContract:
    context = admission_tests.AuthAdmissionTests.context

    async def asyncSetUp(self):
        # These fixtures resume their own continuations explicitly. Retained
        # synthetic workflows must not consume another test's model script.
        with patch("core_agent.runtime.CoreAgent.recover_workflows"):
            await super().asyncSetUp()

    @property
    def agent(self):
        return self.app.state.core_agent

    @property
    def handler(self):
        return self.app.state.a2a_request_handler

    def message(self, message_id="files", context_id="chat", *, raw=b"report", name="report.txt", task_id=None):
        return Message(message_id=message_id, context_id=context_id, task_id=task_id or "", role=Role.ROLE_USER,
                       parts=[{"text": "Read these files"}, {"raw": raw, "filename": name, "media_type": "text/plain"}])

    async def admit(self, message=None, token="external-a"):
        return await self.handler.admission_handler(message or self.message(), self.context(token))

    async def followup(self, task, message, token="external-a"):
        return await asyncio.to_thread(self.handler.followup_handler,
            CoreAgentExecutor._from_sdk_message(message, context_id=task.context_id), task,
            self.context(token), original_message=message)

    def batch(self, admitted):
        return self.agent.chat_file_service.store.get(dict(admitted.task.metadata)["file_batch_id"], self.context().tenant)

    def target(self, batch):
        service = self.agent.chat_file_service
        binding = WorkspaceBinding(batch["tenant_id"], batch["owner_id"], batch["context_id"])
        return service.workspaces.workspace(binding) / "attachments" / batch["batch_id"]

    async def release(self, admitted):
        await asyncio.to_thread(self.agent.workflow_store.release_lease, admitted.run_id,
            tenant_id=self.context().tenant, worker_id=self.agent._worker_id, token=admitted.lease_token)

    async def test_root_binds_original_fingerprint_and_private_complete_batch(self):
        message = self.message()
        message.parts.append(Part(raw=b"", filename="report.txt", media_type="text/plain"))
        digest = fingerprint(message)
        admitted = await self.admit(message)
        batch = self.batch(admitted)
        record = self.agent.workflow_store.lookup_task(admitted.task.id)
        self.assertEqual(batch["request_digest"], digest)
        self.assertEqual(record.snapshot["file_batch_id"], batch["batch_id"])
        self.assertEqual(batch["state"], "accepted_quarantine")
        self.assertEqual([entry["actual_name"] for entry in batch["manifest"]["entries"]], ["report.txt", "report_2.txt"])
        self.assertFalse(self.target(batch).exists())
        self.assertNotIn("Read these files", str(record.snapshot["context"]))
        self.assertEqual(self.model.calls, ())
        repeated = await self.admit(message)
        self.assertEqual(repeated.task.id, admitted.task.id)
        self.assertEqual(dict(repeated.task.metadata)["file_receipt"], dict(admitted.task.metadata)["file_receipt"])
        await self.release(admitted)
        result = await asyncio.to_thread(self.agent.resume_task, admitted.task.id)
        self.assertIsInstance(result, SuspendedRun)  # Empty text needs an explicit owner decision.
        self.assertFalse(self.target(batch).exists())
        self.agent.workflow_store.resolve_wait(result.wait_id, tenant_id=self.context().tenant,
            outcome={"reason": "allowed"}, actor_id=self.context("owner-a").state["principal"].actor_id)
        result = await asyncio.to_thread(self.agent.resume_task, admitted.task.id)
        self.assertEqual(result.terminal_state, "completed")
        self.assertTrue(self.target(batch).is_dir())
        self.assertEqual((self.target(batch) / "report.txt").read_bytes(), b"report")
        self.assertIn("/workspace/attachments/" + batch["batch_id"], self.model.calls[-1].context)

    async def test_duplicate_does_not_restage_and_changed_input_conflicts(self):
        first = await self.admit()
        with patch.object(self.agent.chat_file_service, "prepare", wraps=self.agent.chat_file_service.prepare) as prepare:
            repeated = await self.admit()
            self.assertEqual(repeated.task.id, first.task.id)
            for message in (self.message(raw=b"changed"), self.message(name="changed.txt"), self.message(context_id="other")):
                with self.assertRaises(InvalidParamsError):
                    await self.admit(message)
            prepare.assert_not_called()

    async def test_original_file_metadata_is_retained_outside_safe_receipt(self):
        original_name = "report\0.txt"
        message = self.message(name=original_name)
        message.parts[1].metadata.update({"\0key": "value\0"})
        first = await self.admit(message)
        initial = self.batch(first)["manifest"]["entries"][0]
        self.assertEqual(initial["original_name"], original_name)
        self.assertEqual(initial["metadata"], {"\0key": "value\0"})
        followup = self.message("follow-nul", task_id=first.task.id, name=original_name)
        followup.parts[1].metadata.update({"\0key": "value\0"})
        receipt = await self.followup(first.task, followup)
        batch = self.agent.chat_file_service.store.get(
            receipt["provenance"]["file_batch_id"], self.context().tenant)
        self.assertEqual(batch["manifest"]["entries"][0]["original_name"], original_name)
        self.assertEqual(batch["manifest"]["entries"][0]["metadata"], {"\0key": "value\0"})
        for file_receipt in (dict(first.task.metadata)["file_receipt"], receipt["provenance"]["file_receipt"]):
            entry = file_receipt["entries"][0]
            self.assertNotIn("original_name", entry)
            self.assertNotIn("metadata", entry)
            self.assertNotIn("\0", entry["actual_name"])

    async def test_authorization_and_schema_precede_staging(self):
        await self.admit()
        with patch.object(self.agent.chat_file_service, "prepare", wraps=self.agent.chat_file_service.prepare) as prepare:
            with self.assertRaises(TaskNotFoundError):
                await self.admit(self.message("foreign"), "external-b")
            message = self.message("invalid", "other")
            message.parts.append(Part(url="https://private.example/file"))
            with self.assertRaises((CoreError, InvalidParamsError)):
                await self.admit(message)
            prepare.assert_not_called()

    async def test_full_stage_limit_failure_leaves_no_workflow_or_files(self):
        settings = self.agent.interaction_store.get_settings(self.context().tenant)
        self.agent.interaction_store.update_settings(self.context().tenant, {"hitl_timeout_seconds": 86400, "owner_answer_timeout_seconds": 86400, "guardrails_timeout_seconds": 86400, "attachment_limit_bytes": 7}, settings.revision)
        message = self.message(raw=b"123456")
        message.parts.append(Part(raw=b"89", filename="second.txt", media_type="text/plain"))
        with self.assertRaises(CoreError) as caught:
            await self.admit(message)
        self.assertEqual(caught.exception.code, "ATTACHMENTS_TOO_LARGE")
        self.assertEqual(caught.exception.data, {"allowed_bytes": 7, "actual_bytes": 8})
        self.assertEqual(os.listdir(self.agent.chat_file_service.uploads), [])
        tasks = await self.handler.task_store.inner.list(ListTasksRequest(context_id="chat"), self.context())
        self.assertFalse(tasks.tasks)
        # Failed preparation must not claim the chat or reserve the dedup key.
        accepted = await self.admit(self.message(raw=b"123"))
        self.assertIsNotNone(accepted.run_id)

    async def test_bind_failure_rolls_back_root_and_accepts_retry(self):
        service = self.agent.chat_file_service
        bind = service.bind
        def failed(*args, **kwargs):
            bind(*args, **kwargs)
            raise CoreError("TEST_BIND_FAILURE")
        with patch.object(service, "bind", side_effect=failed):
            with self.assertRaisesRegex(CoreError, "TEST_BIND_FAILURE"):
                await self.admit()
        tasks = await self.handler.task_store.inner.list(ListTasksRequest(context_id="chat"), self.context())
        self.assertFalse(tasks.tasks)
        self.assertEqual(os.listdir(service.uploads), [])
        accepted = await self.admit()
        self.assertIsNotNone(accepted.run_id)
        self.assertFalse(self.target(self.batch(accepted)).exists())

    async def test_busy_root_retains_failed_task_without_staging(self):
        first = await self.admit()
        with patch.object(self.agent.chat_file_service, "prepare", wraps=self.agent.chat_file_service.prepare) as prepare:
            busy = await self.admit(self.message("busy"))
            prepare.assert_not_called()
        self.assertIsNone(busy.run_id)
        self.assertEqual(busy.task.status.state, TaskState.TASK_STATE_FAILED)
        self.assertNotIn("file_batch_id", busy.task.metadata)
        self.assertEqual((await self.admit(self.message("busy"))).task.id, busy.task.id)
        self.assertNotEqual(busy.task.id, first.task.id)

    async def test_owner_files_keep_external_execution_scope_and_frozen_limit(self):
        first = await self.admit()
        batch = self.batch(first)
        settings = self.agent.interaction_store.get_settings(self.context().tenant)
        self.agent.interaction_store.update_settings(self.context().tenant, {"hitl_timeout_seconds": 86400, "owner_answer_timeout_seconds": 86400, "guardrails_timeout_seconds": 86400, "attachment_limit_bytes": 1}, settings.revision)
        message = self.message("owner-follow", raw=b"x", task_id=first.task.id)
        receipt = await self.followup(first.task, message, "owner-a")
        follow = self.agent.chat_file_service.store.get(receipt["provenance"]["file_batch_id"], self.context().tenant)
        self.assertEqual(follow["owner_id"], batch["owner_id"])
        self.assertEqual(follow["actor_id"], self.context("owner-a").state["principal"].actor_id)
        self.assertEqual(self.batch(first)["manifest"], batch["manifest"])

    async def test_followup_during_terminal_cleanup_is_accepted_but_not_published(self):
        first = await self.admit()
        workflow = self.agent.workflow_store
        record = workflow.lookup_task(first.task.id)
        service = self.agent.chat_file_service
        binding = WorkspaceBinding(record.tenant_id, record.owner_id, record.context_id)
        initial = self.batch(first)
        service.record_decision(initial["batch_id"], binding, decision_ref="approved-before-cleanup",
                                allow=True, lease_token=first.lease_token)
        workflow.begin_terminal(record, {"id": "closing-test", "state": "COMPLETED"},
                                lease_token=first.lease_token)
        receipt = await self.followup(first.task, self.message("closing-followup", task_id=first.task.id))
        batch = self.agent.chat_file_service.store.get(receipt["provenance"]["file_batch_id"], record.tenant_id)
        self.assertEqual(batch["state"], "accepted_quarantine")
        self.assertEqual([item["message_id"] for item in workflow.pending_inbound(record)], ["closing-followup"])
        with self.assertRaises(CoreError) as caught:
            service.publish(initial["batch_id"], binding, lease_token=first.lease_token)
        self.assertEqual(caught.exception.code, "FILE_BATCH_TASK_CLOSED")
        self.assertFalse(self.target(initial).exists())
        self.assertFalse(self.target(batch).exists())

    async def test_followup_sequence_duplicate_and_changed_input_are_atomic(self):
        first = await self.admit()
        message = self.message("follow", task_id=first.task.id)
        receipts = await asyncio.gather(self.followup(first.task, message), self.followup(first.task, message))
        self.assertEqual(receipts[0], receipts[1])
        receipt = receipts[0]
        batch = self.agent.chat_file_service.store.get(receipt["provenance"]["file_batch_id"], self.context().tenant)
        self.assertEqual(batch["sequence"], 1)
        self.assertEqual(batch["run_id"], first.run_id)
        self.assertFalse(self.target(batch).exists())
        changed = copy.deepcopy(message)
        changed.parts[1].raw = b"changed"
        with self.assertRaises(CoreError) as caught:
            await self.followup(first.task, changed)
        self.assertEqual(caught.exception.code, "MESSAGE_ID_CONFLICT")
        record = self.agent.workflow_store.lookup_task(first.task.id)
        self.assertEqual(len(self.agent.workflow_store.pending_inbound(record)), 1)

    async def test_followup_bind_failure_rolls_back_inbox_and_batch(self):
        first = await self.admit()
        service = self.agent.chat_file_service
        bind = service.bind
        def failed(*args, **kwargs):
            bind(*args, **kwargs)
            raise CoreError("TEST_BIND_FAILURE")
        message = self.message("follow", task_id=first.task.id)
        with patch.object(service, "bind", side_effect=failed):
            with self.assertRaisesRegex(CoreError, "TEST_BIND_FAILURE"):
                await self.followup(first.task, message)
        record = self.agent.workflow_store.lookup_task(first.task.id)
        self.assertEqual(self.agent.workflow_store.pending_inbound(record), ())
        self.assertEqual(os.listdir(service.uploads), [self.batch(first)["batch_id"]])
        self.assertEqual((await self.followup(first.task, message))["sequence"], 1)

    async def test_cancel_fence_rejects_followup_before_staging(self):
        first = await self.admit()
        self.agent.workflow_store.request_cancel(first.run_id, tenant_id=self.context().tenant,
                                                 owner_id=self.context().user.user_name)
        with patch.object(self.agent.chat_file_service, "prepare", wraps=self.agent.chat_file_service.prepare) as prepare:
            with self.assertRaisesRegex(CoreError, "TASK_TERMINAL"):
                await self.followup(first.task, self.message("late", task_id=first.task.id))
            prepare.assert_not_called()
        record = self.agent.workflow_store.lookup_task(first.task.id)
        self.assertEqual(self.agent.workflow_store.pending_inbound(record), ())

    async def test_cancel_orders_after_inflight_followup_binding(self):
        first = await self.admit()
        entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
        service = self.agent.chat_file_service
        bind = service.bind
        def blocked(*args, **kwargs):
            batch = bind(*args, **kwargs)
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release stage")
            return batch
        def cancel():
            self.agent.workflow_store.request_cancel(first.run_id, tenant_id=self.context().tenant,
                owner_id=self.context().user.user_name)
            cancelled.set()
        with patch.object(service, "bind", side_effect=blocked):
            incoming = asyncio.create_task(self.followup(first.task, self.message("ordered", task_id=first.task.id)))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            cancellation = asyncio.create_task(asyncio.to_thread(cancel))
            try:
                self.assertFalse(await asyncio.to_thread(cancelled.wait, 0.05))
                self.assertEqual(len(os.listdir(service.uploads)), 2)
                binding = WorkspaceBinding(self.context().tenant, self.context().user.user_name, "chat")
                self.assertEqual(os.listdir(service.workspaces.workspace(binding) / "attachments"), [])
            finally:
                release.set()
            receipt, _ = await asyncio.gather(incoming, cancellation)
        self.assertEqual(receipt["sequence"], 1)
        record = self.agent.workflow_store.lookup_task(first.task.id)
        self.assertTrue(record.cancel_requested)
        self.assertEqual(len(self.agent.workflow_store.pending_inbound(record)), 1)
        batch = service.store.get(receipt["provenance"]["file_batch_id"], self.context().tenant)
        with self.assertRaisesRegex(CoreError, "FILE_BATCH_TASK_CLOSED"):
            service.record_decision(batch["batch_id"], binding, decision_ref="stale", allow=True)
        self.assertFalse(self.target(batch).exists())

    async def test_accepted_publication_failure_stays_pending_and_recovers(self):
        from core_agent import chat_files
        first = await self.admit()
        await self.release(first)
        service = self.agent.chat_file_service
        rename = chat_files._rename
        def unavailable(source_fd, source, target_fd, target):
            if target_fd != service.quarantine:
                raise OSError("temporary storage outage")
            return rename(source_fd, source, target_fd, target)
        with patch("core_agent.chat_files._rename", side_effect=unavailable):
            result = await asyncio.to_thread(self.agent.resume_task, first.task.id)
        self.assertIsInstance(result, SuspendedRun)
        batch = self.batch(first)
        self.assertEqual(batch["state"], "accepted_ready")
        record = self.agent.workflow_store.lookup_task(first.task.id)
        self.assertEqual(record.state, "RUNNING")
        self.assertEqual(record.snapshot["file_delivery_pending"]["batch_id"], batch["batch_id"])
        self.assertEqual(self.model.calls, ())
        repeated = await self.admit()
        self.assertEqual(repeated.task.id, first.task.id)
        with patch.object(self.agent.guardrail_classifier, "classify", side_effect=AssertionError("must reuse reviews")):
            result = await asyncio.to_thread(self.agent.resume_task, first.task.id)
        self.assertEqual(result.terminal_state, "completed")
        self.assertTrue(self.target(batch).exists())
        self.assertNotIn("file_delivery_pending", self.agent.workflow_store.lookup_task(first.task.id).snapshot)

    async def test_files_only_input_reaches_guarded_runtime(self):
        message = self.message()
        del message.parts[0]
        first = await self.admit(message)
        await self.release(first)
        await asyncio.to_thread(self.agent.resume_task, first.task.id)
        self.assertTrue(self.target(self.batch(first)).exists())
        self.assertIn("attachments", self.model.calls[-1].context)

    async def test_concurrent_root_dedup_and_busy_share_one_batch(self):
        admitted = await asyncio.gather(self.admit(), self.admit(), self.admit(self.message("busy")))
        self.assertEqual(admitted[0].task.id, admitted[1].task.id)
        self.assertEqual(sum(item.run_id is not None for item in admitted), 1)
        self.assertEqual(len(os.listdir(self.agent.chat_file_service.uploads)), 1)
        unique = {item.task.id: item.task for item in admitted}
        self.assertEqual(len(unique), 2)
        self.assertEqual(sum(task.status.state == TaskState.TASK_STATE_FAILED for task in unique.values()), 1)


class MemoryFileAdmissionTests(FileAdmissionContract, AuthAppTestCase):
    async def test_outer_callback_failure_rolls_back_bound_batch_and_sdk_task(self):
        admission = self.agent.tool_runtime.environment_manager.validate_workspace_scope.__self__
        from core_agent.config import RunRequest
        def callback(admit):
            accepted = admit(self.message(), RunRequest(prompt="Read these files"), self.context())
            self.assertIsNotNone(accepted.run_id)
            self.assertEqual(self.batch(accepted)["state"], "accepted_quarantine")
            raise CoreError("TEST_OUTER_CALLBACK_FAILED")
        with self.assertRaisesRegex(CoreError, "TEST_OUTER_CALLBACK_FAILED"):
            await admission.transaction(self.context(), callback)
        self.assertFalse(admission.messages)
        self.assertFalse(admission.chats)
        self.assertFalse(self.agent.workflow_store._records)
        self.assertFalse(self.handler.task_store.inner._owners)
        self.assertEqual(os.listdir(self.agent.chat_file_service.uploads), [])
        self.assertIsNotNone((await self.admit()).run_id)

    async def test_task_save_failure_rolls_back_memory_workflow_and_batch(self):
        save = self.handler.task_store.inner._save_admission
        def fail(task, context):
            save(task, context)
            raise CoreError("TEST_SAVE_FAILURE")
        with patch.object(self.handler.task_store.inner, "_save_admission", side_effect=fail):
            with self.assertRaisesRegex(CoreError, "TEST_SAVE_FAILURE"):
                await self.admit()
        self.assertEqual(self.agent.workflow_store._records, {})
        self.assertEqual(self.agent.workflow_store._leases, {})
        self.assertEqual(self.agent.workflow_store._budgets, {})
        self.assertEqual(self.agent.event_store._events, {})
        self.assertEqual(self.agent.checkpoint_store._values, {})
        self.assertEqual(self.agent.audit_log._records, {})
        self.assertEqual(os.listdir(self.agent.chat_file_service.uploads), [])
        tasks = await self.handler.task_store.inner.list(ListTasksRequest(context_id="chat"), self.context())
        self.assertFalse(tasks.tasks)
        self.assertIsNotNone((await self.admit()).run_id)

    async def test_cleanup_outage_preserves_primary_error_and_original_age_sweep(self):
        service = self.agent.chat_file_service
        with patch.object(service, "bind", side_effect=CoreError("TEST_BIND_FAILURE")), patch.object(
            service, "reject", side_effect=OSError("cleanup temporarily unavailable")
        ):
            with self.assertRaisesRegex(CoreError, "TEST_BIND_FAILURE"):
                await self.admit()
        self.assertEqual(self.agent.workflow_store._records, {})
        rows = list(service.store.rows.values())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "staging")
        self.assertIsNone(rows[0]["run_id"])
        service.clock = lambda: rows[0]["created_at"] + 25 * 3600
        self.agent._file_sweep_startup, self.agent._file_sweep_due = True, 0
        self.agent._recover_workflows_once()
        self.assertEqual(os.listdir(service.uploads), [])
        self.assertIsNotNone(service.store.get(rows[0]["batch_id"], self.context().tenant)["cleaned_at"])

    async def test_waiting_for_task_store_lock_exposes_no_partial_workflow(self):
        store = self.handler.task_store.inner
        await store._impl.lock.acquire()
        pending = asyncio.create_task(self.admit())
        try:
            await asyncio.sleep(0)
            self.assertFalse(pending.done())
            self.assertEqual(self.agent.workflow_store._records, {})
            self.assertEqual(store._owners, {})
            rows = list(self.agent.chat_file_service.store.rows.values())
            self.assertEqual(rows, [])
        finally:
            store._impl.lock.release()
        self.assertIsNotNone((await pending).run_id)

    async def test_recovery_really_cleans_expired_uploads_but_retains_accepted_files(self):
        first = await self.admit()
        service = self.agent.chat_file_service
        now = service.clock()
        for message_id in ("abandoned-one", "abandoned-two"):
            service.prepare([{"raw": b"stale"}], tenant_id=self.context().tenant,
                actor_id=self.context().state["principal"].actor_id,
                message_id=message_id, request_digest=message_id, source="test")
        service.clock = lambda: now + 25 * 3600
        self.agent._file_sweep_startup, self.agent._file_sweep_due = True, 0
        sweep = service.sweep
        with patch.object(service, "sweep", side_effect=lambda **kw: sweep(**kw, limit=1)):
            self.agent._recover_workflows_once()
        self.assertEqual(os.listdir(service.uploads), [self.batch(first)["batch_id"]])
        self.assertEqual(self.batch(first)["state"], "accepted_quarantine")

    async def test_recovery_sweeps_startup_hourly_and_immediate_backlog(self):
        agent = self.agent
        agent._file_sweep_startup, agent._file_sweep_due = True, 0
        with patch.object(agent.chat_file_service, "sweep", side_effect=[
            {"has_more": True}, {"has_more": False}, {"has_more": False},
        ]) as sweep:
            agent._recover_workflows_once()
            self.assertEqual(sweep.call_count, 2)
            self.assertTrue(all(call.kwargs["startup"] for call in sweep.call_args_list))
            agent._recover_workflows_once()
            self.assertEqual(sweep.call_count, 2)
            agent._file_sweep_due = 0
            agent._recover_workflows_once()
            self.assertFalse(sweep.call_args.kwargs["startup"])


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL is required")
class PostgresFileAdmissionTests(FileAdmissionContract, AuthAppTestCase):
    use_postgres = True

    async def test_borrowed_transaction_rolls_back_prepared_file_binding_with_root(self):
        message, context = self.message(), self.context()
        batch = prepare_files(self.agent, message, context, fingerprint(message))
        database = self.app.state.database
        admission = self.agent.workspace_cleanup.admission
        request = parse_run_request(CoreAgentExecutor._from_sdk_message(message))
        try:
            with self.assertRaisesRegex(CoreError, "TEST_OUTER_ROLLBACK"):
                with database.transaction() as connection:
                    accepted = admission._admit_transaction(message, request, context, batch, connection=connection)
                    bound = self.agent.chat_file_service.store.get(batch["batch_id"], context.tenant, connection=connection)
                    self.assertEqual(bound["state"], "accepted_quarantine")
                    self.assertEqual(bound["run_id"], accepted.run_id)
                    with database.pool.connection() as observer:
                        self.assertIsNone(observer.execute("SELECT 1 FROM core_runs WHERE run_id=%s", (accepted.run_id,)).fetchone())
                    raise CoreError("TEST_OUTER_ROLLBACK")
            restored = self.agent.chat_file_service.store.get(batch["batch_id"], context.tenant)
            self.assertEqual(restored["state"], batch["state"])
            self.assertEqual(restored["run_id"], batch["run_id"])
            with database.pool.connection() as connection:
                self.assertIsNone(connection.execute("SELECT 1 FROM core_chats WHERE tenant_id=%s AND context_id=%s",
                    (context.tenant, message.context_id)).fetchone())
            self.assertFalse(self.target(dict(batch, owner_id=context.user.user_name, context_id=message.context_id)).exists())
            self.assertEqual(self.model.calls, ())
        finally:
            reject_files(self.agent, batch)
        self.assertEqual(os.listdir(self.agent.chat_file_service.uploads), [])

    async def test_concurrent_duplicate_private_stages_have_one_accepted_batch(self):
        service = self.agent.chat_file_service
        prepare = service.prepare
        barrier = threading.Barrier(2)
        def simultaneous(*args, **kwargs):
            batch = prepare(*args, **kwargs)
            barrier.wait(timeout=5)
            return batch
        with patch.object(service, "prepare", side_effect=simultaneous) as staged:
            roots = await asyncio.gather(self.admit(), self.admit())
        self.assertEqual(staged.call_count, 2)  # Private losing copy is allowed, publication is not.
        self.assertEqual(roots[0].task.id, roots[1].task.id)
        self.assertEqual(sum(root.run_id is not None for root in roots), 1)
        self.assertEqual(len(os.listdir(service.uploads)), 1)
        message = self.message("follow", task_id=roots[0].task.id)
        with patch.object(service, "prepare", side_effect=simultaneous):
            receipts = await asyncio.gather(self.followup(roots[0].task, message), self.followup(roots[0].task, message))
        self.assertEqual(receipts[0], receipts[1])
        with self.app.state.database.transaction() as connection:
            rows = connection.execute("SELECT state, cleaned_at, run_id FROM core_chat_file_batches WHERE tenant_id=%s",
                                      (self.context().tenant,)).fetchall()
        self.assertEqual(sum(row["state"] == "accepted_quarantine" for row in rows), 2)
        rejected = [row for row in rows if row["state"] == "rejected"]
        self.assertEqual(len(rejected), 2)
        self.assertTrue(all(row["cleaned_at"] is not None and row["run_id"] is None for row in rejected))
        self.assertEqual(len(os.listdir(service.uploads)), 2)
        self.assertEqual(self.model.calls, ())

    async def test_cancel_between_preflight_and_bind_rejects_and_cleans_stage(self):
        first = await self.admit()
        service = self.agent.chat_file_service
        prepare = service.prepare
        staged, release = threading.Event(), threading.Event()
        def blocked(*args, **kwargs):
            batch = prepare(*args, **kwargs)
            staged.set()
            if not release.wait(5):
                raise AssertionError("test did not release stage")
            return batch
        with patch.object(service, "prepare", side_effect=blocked):
            incoming = asyncio.create_task(self.followup(first.task, self.message("late", task_id=first.task.id)))
            self.assertTrue(await asyncio.to_thread(staged.wait, 2))
            try:
                await asyncio.to_thread(self.agent.workflow_store.request_cancel, first.run_id,
                    tenant_id=self.context().tenant, owner_id=self.context().user.user_name)
            finally:
                release.set()
            with self.assertRaisesRegex(CoreError, "TASK_TERMINAL"):
                await incoming
        self.assertEqual(os.listdir(service.uploads), [self.batch(first)["batch_id"]])
        record = self.agent.workflow_store.lookup_task(first.task.id)
        self.assertEqual(self.agent.workflow_store.pending_inbound(record), ())

    async def test_single_connection_pool_handles_concurrent_independent_file_admissions(self):
        database = PostgresDatabase(TEST_DATABASE_URL, min_size=1, max_size=1, timeout=1)
        self.app = create_app(model=self.model, database=database,
            base_url="https://agent.example.test", auth_transport=httpx.MockTransport(self.introspect))
        self.addCleanup(self.app.state.close)
        # Real PostgreSQL operations, including settings, fsynced prepare,
        # canonical bind and duplicate receipt lookup, share one pooled slot.
        async with asyncio.timeout(10):
            roots = await asyncio.gather(*(self.admit(self.message(f"root-{i}", f"chat-{i}")) for i in range(4)))
            self.assertEqual(len({root.task.id for root in roots}), 4)
            self.assertTrue(all(root.run_id for root in roots))
            messages = [self.message(f"follow-{i}", f"chat-{i}", task_id=root.task.id) for i, root in enumerate(roots)]
            receipts = await asyncio.gather(*(self.followup(root.task, message) for root, message in zip(roots, messages)))
            repeated = await asyncio.gather(*(self.followup(root.task, message) for root, message in zip(roots, messages)))
        self.assertEqual(receipts, repeated)
        self.assertTrue(all(receipt["sequence"] == 1 for receipt in receipts))
        self.assertEqual(len(os.listdir(self.agent.chat_file_service.uploads)), 8)

    async def test_task_save_failure_rolls_back_postgres_workflow_and_batch(self):
        save = self.handler.task_store.inner._save
        def fail(*args, **kwargs):
            save(*args, **kwargs)
            raise CoreError("TEST_SAVE_FAILURE")
        with patch.object(self.handler.task_store.inner, "_save", side_effect=fail):
            with self.assertRaisesRegex(CoreError, "TEST_SAVE_FAILURE"):
                await self.admit()
        with self.app.state.database.transaction() as connection:
            for table in ("core_runs", "core_chats", "core_root_messages"):
                count = connection.execute(f"SELECT count(*) AS count FROM {table} WHERE tenant_id=%s", (self.context().tenant,)).fetchone()
                self.assertEqual(count["count"], 0)
        self.assertEqual(os.listdir(self.agent.chat_file_service.uploads), [])
        self.assertIsNotNone((await self.admit()).run_id)
