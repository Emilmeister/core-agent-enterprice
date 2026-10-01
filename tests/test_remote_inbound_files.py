import base64
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from a2a.types import Task, TaskState, TaskStatus

from core_agent.chat_files import ChatFileService, MemoryChatFileStore, PostgresChatFileStore
from core_agent.database import PostgresDatabase
from core_agent.errors import CoreError
from core_agent.postgres_tasks import PostgresTaskScheduler
from core_agent.remote_agents import RemoteEvent
from core_agent.remote_operations import RemoteA2AExecutor
from core_agent.remote_registry import InMemoryRemoteRegistry
from core_agent.tasks import REMOTE_TASK_PENDING, TaskScheduler
from core_agent.workflow import InMemoryWorkflowStore, PostgresWorkflowStore, WorkflowRecord
from core_agent.workspace import ChatWorkspaces, WorkspaceBinding


class RemoteInboundContract:
    def setup_remote(self):
        self.now = time.time() if hasattr(self, "database") else 1000.0
        self.tenant, self.person, self.context = uuid.uuid4().hex, "source-person", uuid.uuid4().hex
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspaces = ChatWorkspaces(self.directory.name)
        self.binding = WorkspaceBinding(self.tenant, self.person, self.context)
        self.root = self.workflow.create(WorkflowRecord(uuid.uuid4().hex, uuid.uuid4().hex,
            self.context, self.tenant, self.person, None, "RUNNING", 1, {"prompt": "root"}, {}))
        self.source = self.root
        if hasattr(self, "database"):
            public = Task(id=self.root.task_id, context_id=self.context,
                          status=TaskStatus(state=TaskState.TASK_STATE_WORKING))
            with self.database.transaction() as conn:
                conn.execute("INSERT INTO core_chats(tenant_id,context_id,owner_id) VALUES (%s,%s,%s)",
                             (self.tenant, self.context, self.person))
                conn.execute("""INSERT INTO core_a2a_tasks(task_id,owner,tenant,context_id,state,status_timestamp,payload)
                    VALUES (%s,%s,%s,%s,%s,0,%s)""", (self.root.task_id, self.person, self.tenant,
                    self.context, TaskState.TASK_STATE_WORKING, public.SerializeToString()))
        self.service = ChatFileService(self.store, self.workspaces, clock=lambda: self.now)
        self.addCleanup(self.service.close)
        self.scheduler.chat_file_service = self.service
        self.registry = InMemoryRemoteRegistry()
        self.peer = self.registry.create(self.tenant, {"name": "delivery", "url": "https://peer.example/a2a",
            "description": "Delivery", "enabled": True, "header_name": "Authorization",
            "header_value": "Bearer PRIVATE-credential"}, actor_id=self.person)
        self.responses, self.calls = [], []
        self.executor = RemoteA2AExecutor(self.scheduler, self.registry, None, self.service)
        self.scheduler.register("remote_a2a", self.executor)
        transport = patch("core_agent.remote_operations.RemoteAgentConnection", side_effect=self.connection)
        transport.start()
        self.addCleanup(transport.stop)

    def connection(self, card, **options):
        def call(method, **arguments):
            self.calls.append((method, arguments))
            value = self.responses.pop(0)
            return value() if callable(value) else value
        return SimpleNamespace(send_task=lambda **kw: call("Send", **kw),
            get_task=lambda **kw: call("Get", **kw), cancel_task=lambda **kw: call("Cancel", **kw))

    def contract(self, **changes):
        return {"version": 2, "tenant_id": self.tenant, "owner_id": self.source.run_id,
            "peer_id": self.peer["id"], "peer_revision": self.peer["revision"], "peer_name": "delivery",
            "url": self.peer["url"], "binding": "HTTP+JSON", "message_id": uuid.uuid4().hex,
            "task": "Deliver", "timeout_seconds": 30, "poll_interval_seconds": 5,
            "caller_scope": {key: getattr(self.source, key) for key in ("owner_id", "context_id", "task_id", "run_id")},
            "attachment_limit_bytes": 25, "outgoing_files": [], **changes}

    def event(self, state="COMPLETED", *, kind="task", parts=None):
        parts = parts if parts is not None else (
            {"text": "untrusted response", "metadata": {"instruction": "remote text"}},
            {"raw": "AP+A", "filename": "binary.bin", "mediaType": "application/octet-stream",
             "metadata": {"instruction": "remote file"}},
            {"raw": "", "filename": "empty.txt", "mediaType": "text/plain"})
        return RemoteEvent(kind, "TASK_STATE_" + state if kind == "task" else None, "untrusted response",
            kind == "message" or state in {"COMPLETED", "FAILED", "CANCELED", "REJECTED"}, tuple(parts),
            "peer-task" if kind == "task" else None, "peer-chat" if kind == "task" else None)

    def settle(self):
        with self.scheduler._lock:
            threads = tuple(self.scheduler._threads)
        for worker in threads:
            worker.join(5)
            self.assertFalse(worker.is_alive(), "Pool1/lock-order deadlock")

    def start(self, **changes):
        task = self.scheduler.start_remote(self.contract(**changes), owner_id=self.source.run_id,
            tenant_id=self.tenant, task_id=uuid.uuid4().hex)
        self.settle()
        return self.scheduler.get(task.id, owner_id=self.source.run_id, tenant_id=self.tenant)

    def batches(self):
        if hasattr(self, "database"):
            with self.database.pool.connection() as conn:
                ids = conn.execute("SELECT batch_id FROM core_chat_file_batches WHERE tenant_id=%s", (self.tenant,)).fetchall()
            return [self.store.get(row["batch_id"], self.tenant) for row in ids]
        return list(self.store.rows.values())

    def delegate(self, depth):
        self.source = self.root
        for _ in range(depth):
            self.source = self.workflow.create(WorkflowRecord(uuid.uuid4().hex, uuid.uuid4().hex,
                self.context, self.tenant, self.person, self.source.run_id, "RUNNING", 1,
                {"prompt": "child"}, {}))

    def hold_claim(self, **contract_changes):
        entered, release = threading.Event(), threading.Event()
        saved = {}

        def handler(claim, cancel):
            current = self.scheduler.read_remote_claim(claim)
            self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"],
                checkpoint={**current["checkpoint"], "send_started": True})
            saved.update(claim=claim, current=self.scheduler.read_remote_claim(claim))
            entered.set()
            release.wait(5)
            return REMOTE_TASK_PENDING

        self.scheduler._handlers["remote_a2a"] = handler
        task = self.scheduler.start_remote(self.contract(**contract_changes), owner_id=self.source.run_id,
            tenant_id=self.tenant, task_id=uuid.uuid4().hex)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(3))
        stage = self.service.prepare([{"raw": b"one", "name": "one.txt"}], tenant_id=self.tenant,
            actor_id=self.person, message_id=task.id, request_digest="response-digest", source="remote")
        prepared = {key: stage[key] for key in ("batch_id", "lease_token", "actor_id", "message_id", "request_digest")}
        return task, saved["claim"], saved["current"], prepared, release

    def commit_files(self, claim, current, prepared, **changes):
        return self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"],
            checkpoint=current["checkpoint"], outcome=("completed", {
                "agent_name": "delivery", "remote_state": "TASK_STATE_COMPLETED", "text": "done"}, None),
            prepared_file_batch=prepared, **changes)

    def reject_stage(self, prepared):
        self.service.reject(prepared["batch_id"], self.tenant, prepared["lease_token"])

    def test_notification_failure_rolls_back_batch_terminal_and_mailbox_then_loser_cleans(self):
        task, claim, current, prepared, release = self.hold_claim()
        method = "_remote_finish_locked" if hasattr(self, "database") else "_finish_remote_locked"
        original = getattr(self.scheduler, method)

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise CoreError("INJECTED_NOTIFICATION_FAILURE")

        with patch.object(self.scheduler, method, side_effect=fail), self.assertRaises(CoreError) as caught:
            self.commit_files(claim, current, prepared)
        self.assertEqual(caught.exception.code, "INJECTED_NOTIFICATION_FAILURE")
        saved = self.scheduler.get(task.id, owner_id=self.source.run_id, tenant_id=self.tenant)
        self.assertEqual((saved.state, saved.revision, saved.result), ("working", current["revision"], None))
        self.assertEqual(self.store.get(prepared["batch_id"], self.tenant)["state"], "staging")
        self.assertEqual(self.scheduler.mailbox(self.source.run_id, self.tenant).poll(), ())
        if hasattr(self, "database"):
            with self.database.pool.connection() as conn:
                self.assertEqual(conn.execute("SELECT count(*) AS n FROM core_outbox WHERE aggregate_id=%s AND aggregate_type='background_task'", (task.id,)).fetchone()["n"], 0)
        self.reject_stage(prepared)
        self.assertEqual(os.listdir(self.service.uploads), [])
        release.set()
        self.settle()

    def test_bind_failure_after_acceptance_rolls_back_and_leaves_no_terminal_notification(self):
        task, claim, current, prepared, release = self.hold_claim()
        original = self.service.bind

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise CoreError("INJECTED_BIND_FAILURE")

        with patch.object(self.service, "bind", side_effect=fail), self.assertRaises(CoreError):
            self.commit_files(claim, current, prepared)
        self.assertEqual(self.store.get(prepared["batch_id"], self.tenant)["state"], "staging")
        self.assertEqual(self.scheduler.get(task.id, owner_id=self.source.run_id, tenant_id=self.tenant).state, "working")
        self.assertEqual(self.scheduler.mailbox(self.source.run_id, self.tenant).poll(), ())
        self.reject_stage(prepared)
        release.set()
        self.settle()

    def test_late_duplicate_commit_cannot_accept_second_stage(self):
        task, claim, current, prepared, release = self.hold_claim()
        committed = self.commit_files(claim, current, prepared)
        another = self.service.prepare([{"raw": b"other"}], tenant_id=self.tenant, actor_id=self.person,
            message_id=task.id, request_digest="other-digest", source="remote")
        descriptor = {key: another[key] for key in prepared}
        late = self.commit_files(claim, current, descriptor)
        self.assertEqual(late.result, committed.result)
        self.assertEqual(self.store.get(another["batch_id"], self.tenant)["state"], "staging")
        self.assertEqual(len(self.scheduler.mailbox(self.source.run_id, self.tenant).poll()), 1)
        self.reject_stage(descriptor)
        release.set()
        self.settle()

    def test_reflected_credentials_in_any_file_text_or_metadata_reject_whole_before_stage(self):
        cases = (
            ({"raw": "b2s=", "filename": "ok"}, {"raw": base64.b64encode(b"PRIVATE-credential").decode()}),
            ({"raw": "b2s=", "metadata": {"nested": ["PRIVATE-credential"]}},),
            ({"text": "PRIVATE-credential"}, {"raw": "b2s="}),
        )
        for parts in cases:
            with self.subTest(parts=parts):
                self.responses.append(self.event(parts=parts))
                task = self.start()
                self.assertEqual((task.state, task.error.code), ("failed", "REMOTE_AGENT_PROTOCOL_ERROR"))
                self.assertNotIn("PRIVATE-credential", str(task.result))
                self.assertEqual(self.batches(), [])

    def test_bad_last_part_or_aggregate_never_creates_stage_or_partial_result(self):
        for last in ({"raw": "Zh=="}, {"raw": "eA==", "filename": 7},
                     {"raw": base64.b64encode(b"x" * 26).decode()}, {"url": "https://peer/file"}):
            with self.subTest(last=last):
                self.responses.append(self.event(parts=({"raw": "b2s="}, last)))
                task = self.start()
                self.assertEqual(task.state, "failed")
                self.assertNotIn("file_batch_id", task.result)
                self.assertEqual(self.batches(), [])

    def test_latin1_private_header_reflected_as_actual_wire_bytes_never_stages(self):
        self.registry = InMemoryRemoteRegistry()
        self.peer = self.registry.create(self.tenant, {"name": "delivery", "url": "https://peer.example/a2a",
            "description": "Delivery", "enabled": True, "header_name": "X-Key",
            "header_value": "PRIVATE-é"}, actor_id=self.person)
        self.executor.registry = self.registry
        for encoding in ("latin-1", "utf-8"):
            with self.subTest(encoding=encoding):
                self.responses.append(self.event(parts=({"raw": "b2s="}, {
                    "raw": base64.b64encode("PRIVATE-é".encode(encoding)).decode(), "filename": "latin.bin"})))
                task = self.start()
                self.assertEqual(self.calls[-1][1]["headers"], {"X-Key": "PRIVATE-é"})
                self.assertEqual((task.state, getattr(task.error, "code", None)), ("failed", "REMOTE_AGENT_PROTOCOL_ERROR"))
                self.assertEqual(self.batches(), [])
                self.assertNotIn("PRIVATE", str(task.result))

    def test_early_and_failed_or_cancelled_replies_never_accept_preview(self):
        for state in ("WORKING", "INPUT_REQUIRED", "AUTH_REQUIRED", "FAILED", "CANCELED", "REJECTED"):
            with self.subTest(state=state):
                self.responses.append(self.event(state))
                task = self.start()
                self.assertEqual(task.state, "working" if state in {"WORKING", "INPUT_REQUIRED", "AUTH_REQUIRED"} else "failed")
                self.assertNotIn("file_batch_id", task.result or {})
                self.assertEqual(self.batches(), [])

    def test_foreign_source_chain_or_closed_ancestor_rejects_prepared_batch(self):
        self.delegate(2)
        task, claim, current, prepared, release = self.hold_claim()
        if hasattr(self, "database"):
            with self.database.transaction() as conn:
                conn.execute("UPDATE core_runs SET cancel_requested=true WHERE run_id=%s", (self.root.run_id,))
        else:
            self.workflow._records[self.root.run_id] = replace(self.root, cancel_requested=True)
        with self.assertRaises(CoreError):
            self.commit_files(claim, current, prepared)
        self.assertEqual(self.store.get(prepared["batch_id"], self.tenant)["state"], "staging")
        self.assertEqual(self.scheduler.mailbox(self.source.run_id, self.tenant).poll(), ())
        self.reject_stage(prepared)
        release.set()
        self.settle()

    def test_completed_and_immediate_message_accept_private_ordered_batch_once(self):
        for kind in ("task", "message"):
            with self.subTest(kind=kind):
                self.responses.append(self.event(kind=kind))
                task = self.start()
                self.assertEqual(task.state, "completed", getattr(task.error, "code", None))
                batch = self.store.get(task.result["file_batch_id"], self.tenant)
                self.assertEqual((batch["state"], batch["task_id"], batch["run_id"]),
                                 ("accepted_quarantine", self.root.task_id, self.source.run_id))
                self.assertEqual([entry["size_bytes"] for entry in batch["manifest"]["entries"]], [3, 0])
                self.assertEqual(batch["manifest"]["source"], "remote")
                self.assertEqual(batch["manifest"]["metadata"]["text"], "untrusted response")
                self.assertEqual(batch["manifest"]["metadata"]["parts"][0]["metadata"], {"instruction": "remote text"})
                self.assertEqual(batch["manifest"]["entries"][0]["metadata"]["metadata"], {"instruction": "remote file"})
                normalized = {"kind": kind, "state": "TASK_STATE_COMPLETED" if kind == "task" else None,
                    "text": "untrusted response", "final": True, "task_id": "peer-task" if kind == "task" else None,
                    "context_id": "peer-chat" if kind == "task" else None, "parts": list(self.event(kind=kind).parts)}
                digest = hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=True,
                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()
                self.assertEqual(batch["request_digest"], digest)
                self.assertFalse((self.workspaces.workspace(self.binding) / "attachments" / batch["batch_id"]).exists())
                mailbox = self.scheduler.mailbox(self.source.run_id, self.tenant).poll()
                visible = json.dumps([item.payload for item in mailbox])
                for private in (batch["batch_id"], batch["lease_token"], batch["request_digest"], "file_batch_id", "AP+A"):
                    self.assertNotIn(private, visible)
                if hasattr(self, "database"):
                    with self.database.pool.connection() as conn:
                        outbox = conn.execute("SELECT payload FROM core_outbox WHERE aggregate_type='background_task' AND aggregate_id=%s", (task.id,)).fetchall()
                    self.assertEqual(len(outbox), 1)
                    self.assertNotIn("file_batch_id", str(outbox))
                    self.assertNotIn(batch["batch_id"], str(outbox))
        self.assertEqual([method for method, _ in self.calls], ["Send", "Send"])

    def test_child_and_grandchild_bind_canonical_root_without_public_child_rows(self):
        for depth in (1, 2):
            with self.subTest(depth=depth):
                self.delegate(depth)
                self.responses.append(self.event())
                task = self.start()
                self.assertEqual(task.state, "completed", getattr(task.error, "code", None))
                batch = self.store.get(task.result["file_batch_id"], self.tenant)
                self.assertEqual((batch["task_id"], batch["run_id"]), (self.root.task_id, self.source.run_id))
                if hasattr(self, "database"):
                    with self.database.pool.connection() as conn:
                        self.assertIsNone(conn.execute("SELECT task_id FROM core_a2a_tasks WHERE task_id=%s", (self.source.task_id,)).fetchone())


    def test_slow_bind_crossing_absolute_deadline_rolls_back_before_terminal(self):
        task, claim, current, prepared, release = self.hold_claim(timeout_seconds=1)
        original = self.service.bind

        def slow(*args, **kwargs):
            result = original(*args, **kwargs)
            if hasattr(self, "database"):
                time.sleep(1.1)
            else:
                self.now += 2
            return result

        with patch.object(self.service, "bind", side_effect=slow), self.assertRaises(CoreError) as caught:
            self.commit_files(claim, current, prepared)
        self.assertEqual(caught.exception.code, "LEASE_LOST")
        self.assertEqual(self.store.get(prepared["batch_id"], self.tenant)["state"], "staging")
        self.assertEqual(self.scheduler.mailbox(self.source.run_id, self.tenant).poll(), ())
        with self.assertRaises(CoreError):
            self.scheduler.read_remote_claim(claim)
        timed_out = self.scheduler.get(task.id, owner_id=self.source.run_id, tenant_id=self.tenant)
        self.assertEqual(timed_out.error.code, "REMOTE_OPERATION_TIMEOUT")
        self.assertNotIn("file_batch_id", timed_out.result)
        self.reject_stage(prepared)
        release.set()
        self.settle()

    def test_cancel_after_prepare_never_accepts_late_complete_batch(self):
        task, claim, current, prepared, release = self.hold_claim()
        self.scheduler.cancel(task.id, owner_id=self.source.run_id, tenant_id=self.tenant)
        with self.assertRaises(CoreError):
            self.commit_files(claim, current, prepared)
        self.assertEqual(self.store.get(prepared["batch_id"], self.tenant)["state"], "staging")
        self.assertEqual(self.scheduler.mailbox(self.source.run_id, self.tenant).poll(), ())
        self.reject_stage(prepared)
        release.set()
        self.settle()

    def test_forged_stage_scope_extra_field_and_unbound_private_marker_fail_closed(self):
        task, claim, current, prepared, release = self.hold_claim()
        for forged in ({**prepared, "actor_id": "foreign"}, {**prepared, "message_id": "peer-task"},
                       {**prepared, "run_id": self.root.run_id}):
            with self.subTest(forged=forged), self.assertRaises(CoreError):
                self.commit_files(claim, current, forged)
        with self.assertRaises(CoreError):
            self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"],
                checkpoint=current["checkpoint"], outcome=("completed", {"file_batch_id": prepared["batch_id"]}, None))
        self.assertEqual(self.store.get(prepared["batch_id"], self.tenant)["state"], "staging")
        self.reject_stage(prepared)
        release.set()
        self.settle()


class MemoryRemoteInboundTests(RemoteInboundContract, unittest.TestCase):
    def setUp(self):
        self.workflow = InMemoryWorkflowStore()
        self.store = MemoryChatFileStore(self.workflow, lambda binding: None)
        self.scheduler = TaskScheduler(clock=lambda: self.now)
        self.addCleanup(self.scheduler.close)
        self.setup_remote()


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is required")
class PostgresRemoteInboundTests(RemoteInboundContract, unittest.TestCase):
    def setUp(self):
        self.database = PostgresDatabase(os.environ["TEST_DATABASE_URL"], min_size=0, max_size=1)
        self.addCleanup(self.database.close)
        self.workflow = PostgresWorkflowStore(self.database)
        self.store = PostgresChatFileStore(self.database, self.workflow)
        self.scheduler = PostgresTaskScheduler(self.database)
        self.addCleanup(self.scheduler.close)
        self.setup_remote()
        self.addCleanup(self.cleanup_rows)

    def cleanup_rows(self):
        self.scheduler.close()
        for batch in self.batches():
            if batch["state"] in {"staging", "rejected"} and batch["cleaned_at"] is None:
                self.service.reject(batch["batch_id"], self.tenant, batch["lease_token"])
        with self.database.transaction() as conn:
            conn.execute("DELETE FROM core_notifications WHERE tenant_id=%s", (self.tenant,))
            conn.execute("DELETE FROM core_outbox WHERE tenant_id=%s AND aggregate_type='background_task'", (self.tenant,))
            conn.execute("DELETE FROM core_background_tasks WHERE tenant_id=%s", (self.tenant,))

    def test_same_borrowed_connection_and_fresh_claim_fence_after_bind(self):
        task, claim, current, prepared, release = self.hold_claim()
        original = self.service.bind
        connections = []

        def expired(*args, **kwargs):
            connection = kwargs["connection"]
            connections.append(connection)
            original(*args, **kwargs)
            connection.execute("UPDATE core_background_tasks SET claim_expires_at=EXTRACT(EPOCH FROM clock_timestamp())-1 WHERE id=%s", (task.id,))

        with patch.object(self.service, "bind", side_effect=expired), self.assertRaises(CoreError) as caught:
            self.commit_files(claim, current, prepared)
        self.assertEqual(caught.exception.code, "LEASE_LOST")
        self.assertEqual(len(connections), 1)
        self.assertEqual(self.database.pool.max_size, 1)
        self.assertEqual(self.store.get(prepared["batch_id"], self.tenant)["state"], "staging")
        self.assertEqual(self.scheduler.get(task.id, owner_id=self.source.run_id, tenant_id=self.tenant).state, "working")
        self.reject_stage(prepared)
        release.set()
        self.settle()

    def test_restart_preserves_accepted_child_batch_without_resending_or_publication(self):
        self.delegate(2)
        self.responses.append(self.event())
        task = self.start()
        self.assertEqual(task.state, "completed", getattr(task.error, "code", None))
        batch_id = task.result["file_batch_id"]
        self.scheduler.close()
        restarted_db = PostgresDatabase(os.environ["TEST_DATABASE_URL"], min_size=0, max_size=1)
        self.addCleanup(restarted_db.close)
        restarted = PostgresTaskScheduler(restarted_db)
        self.addCleanup(restarted.close)
        store = PostgresChatFileStore(restarted_db, PostgresWorkflowStore(restarted_db))
        service = ChatFileService(store, self.workspaces)
        self.addCleanup(service.close)
        restarted.chat_file_service = service
        restarted.register("remote_a2a", RemoteA2AExecutor(restarted, self.registry, None, service))
        self.assertEqual(restarted.recover(), 0)
        saved = restarted.get(task.id, owner_id=self.source.run_id, tenant_id=self.tenant)
        self.assertEqual(saved.result, task.result)
        batch = store.get(batch_id, self.tenant)
        self.assertEqual((batch["state"], batch["task_id"], batch["run_id"]),
                         ("accepted_quarantine", self.root.task_id, self.source.run_id))
        read = service.owner_download(batch_id, self.binding, 0, task_id=self.source.task_id, run_id=self.source.run_id)
        self.assertEqual(read["content"], b"\0\xff\x80")
        self.assertEqual(len(self.calls), 1)
        self.assertFalse((self.workspaces.workspace(self.binding) / "attachments" / batch_id).exists())
