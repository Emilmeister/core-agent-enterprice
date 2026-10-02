"""Canonical child file ownership without child public A2A admission rows."""
import os
import unittest
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from unittest.mock import patch

from a2a.types import Task, TaskState, TaskStatus

from core_agent.chat_files import MemoryChatFileStore, PostgresChatFileStore
from core_agent.database import PostgresDatabase
from core_agent.errors import CoreError
from core_agent.workflow import InMemoryWorkflowStore, PostgresWorkflowStore, WorkflowRecord
from tests.test_chat_files import ChatFileContract


class ChildFileContract:
    prepare = ChatFileContract.prepare
    target = ChatFileContract.target

    def setup_files(self):
        ChatFileContract.setup_files(self)
        self.child = self.child_of(self.record)
        self.grandchild = self.child_of(self.child)
        self.sibling = self.child_of(self.record)
        self.addCleanup(self.cleanup_staging)

    def child_of(self, parent):
        return self.workflow.create(WorkflowRecord(str(uuid.uuid4()), str(uuid.uuid4()),
            parent.context_id, parent.tenant_id, parent.owner_id, parent.run_id,
            "RUNNING", 1, {"prompt": "child"}, {}))

    def cleanup_staging(self):
        if hasattr(self, "database"):
            with self.database.transaction() as connection:
                rows = connection.execute("SELECT batch_id,lease_token FROM core_chat_file_batches "
                    "WHERE tenant_id=%s AND state IN ('staging','rejected') AND cleaned_at IS NULL",
                    (self.binding.tenant_id,)).fetchall()
        else:
            rows = [row for row in self.store.rows.values() if row["state"] in {"staging", "rejected"}]
        for row in rows:
            self.service.reject(row["batch_id"], self.binding.tenant_id, row["lease_token"])

    def bind_source(self, batch, source, *, sequence=None, connection=None):
        transaction = (self.database.transaction() if hasattr(self, "database") and connection is None
                       else nullcontext(connection))
        with transaction as conn:
            return self.service.bind(batch["batch_id"], self.binding, task_id=source.task_id,
                run_id=source.run_id, actor_id="actor", message_id="message",
                request_digest=self.options["request_digest"], lease_token=batch["lease_token"],
                sequence=sequence, connection=conn)

    def assert_code(self, code, callback):
        with self.assertRaises(CoreError) as error:
            callback()
        self.assertEqual(error.exception.code, code)

    def change(self, record, **fields):
        if hasattr(self, "database"):
            from psycopg import sql
            from psycopg.types.json import Jsonb

            with self.database.transaction() as connection:
                connection.execute(sql.SQL("UPDATE core_runs SET {} WHERE run_id=%s").format(
                    sql.SQL(",").join(sql.SQL("{}=%s").format(sql.Identifier(key)) for key in fields)),
                    (*[Jsonb(value) if key == "snapshot" else value for key, value in fields.items()], record.run_id))
        else:
            self.workflow._records[record.run_id] = replace(record, **fields)

    def lease(self, source):
        return self.workflow.acquire_lease(source.run_id, tenant_id=source.tenant_id,
            owner_id=source.owner_id, worker_id="worker", ttl=60)

    def expire(self, source):
        if hasattr(self, "database"):
            with (nullcontext(self.current_connection) if self.current_connection is not None
                  else self.database.transaction()) as connection:
                connection.execute("UPDATE core_runs SET lease_expires_at=0 WHERE run_id=%s", (source.run_id,))
        else:
            worker, token, _ = self.workflow._leases[source.run_id]
            self.workflow._leases[source.run_id] = (worker, token, 0)

    def test_child_and_grandchild_bind_to_root_and_historical_read_preserves_source(self):
        for source in (self.child, self.grandchild):
            with self.subTest(depth=source.parent_run_id):
                batch = self.prepare()
                row = self.bind_source(batch, source)
                self.assertEqual((row["run_id"], row["task_id"]), (source.run_id, self.record.task_id))
                material = self.service.review_material(batch["batch_id"], self.binding,
                    task_id=source.task_id, run_id=source.run_id)
                self.assertEqual(material["manifest"]["total_bytes"], 3)
                for invalid_task in (None, "", True):
                    self.assert_code("FILE_BATCH_NOT_FOUND", lambda: self.service.owner_download(
                        batch["batch_id"], self.binding, 0, run_id=source.run_id, task_id=invalid_task))
                for wrong in (self.record, self.sibling):
                    self.assert_code("FILE_BATCH_NOT_FOUND", lambda: self.service.owner_download(
                        batch["batch_id"], self.binding, 0, run_id=wrong.run_id, task_id=wrong.task_id))
                self.change(source, state="COMPLETED")
                self.assertEqual(self.service.owner_download(batch["batch_id"], self.binding, 0,
                    run_id=source.run_id, task_id=source.task_id)["content"], b"one")
                self.change(source, state="RUNNING")
        self.change(self.child, state="COMPLETED")
        self.change(self.grandchild, state="COMPLETED")
        self.change(self.record, state="COMPLETED")
        self.assertEqual(self.service.review_material(batch["batch_id"], self.binding,
            task_id=self.grandchild.task_id, run_id=self.grandchild.run_id)["state"], "accepted_quarantine")

    def test_owner_review_uses_source_run_and_root_chat_without_external_approval_access(self):
        import hashlib
        import json
        from types import SimpleNamespace
        from unittest.mock import Mock

        from starlette.datastructures import QueryParams

        from core_agent.auth import Principal
        from core_agent.guardrails import GuardrailClassifier
        from core_agent.material_reviews import MemoryMaterialReviewStore, PostgresMaterialReviewStore
        from core_agent.model import ModelResponse
        from core_agent.owner_api import list_interactions, read_guardrail_file, read_guardrail_material

        reviews = (PostgresMaterialReviewStore(self.database, self.workflow) if hasattr(self, "database")
                   else MemoryMaterialReviewStore(self.workflow))
        agent = SimpleNamespace(workflow_store=self.workflow, material_review_store=reviews,
                                chat_file_service=self.service)
        owners = [Principal(name, self.binding.tenant_id, True, False) for name in ("alice", "bob")]
        for source in (self.child, self.grandchild):
            with self.subTest(source=source.run_id):
                batch = self.prepare()
                self.bind_source(batch, source)
                payload = {"manifest": batch["manifest"], "index": 0}
                encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                token = self.lease(source)
                review = reviews.create(source, source_id="file:" + batch["batch_id"] + ":0",
                    source_kind="file_attachment", deadline=self.workflow.current_time() + 60,
                    lease_token=token, sealed_ref={"batch_id": batch["batch_id"], "index": 0},
                    content_digest=hashlib.sha256(encoded.encode()).hexdigest())
                detector = Mock(context_window=128000, max_tokens=512)
                detector.count_tokens.side_effect = lambda text: max(1, len(text) // 4)
                detector.generate.return_value = ModelResponse(message='{"verdict":"suspicious"}', finish_reason="stop")
                current = self.workflow.get(source.run_id, tenant_id=source.tenant_id)
                review = reviews.classify(current, review["review_id"],
                    GuardrailClassifier(detector, clock=self.workflow.current_time), lease_token=token,
                    continuation={"version": 1, "phase": "tool_result", "call_id": "remote-result"},
                    snapshot=current.snapshot, documents=[encoded, "one"])
                self.assertEqual(review["state"], "pending")
                for actor in owners:
                    listed = list_interactions(self.workflow, actor, QueryParams({"task_id": self.record.task_id}))
                    self.assertIn(review["wait_id"], [item["wait_id"] for item in listed["interactions"]])
                    material = read_guardrail_material(agent, actor, review["wait_id"], include_file_metadata=True)
                    self.assertEqual(material["review"]["run_id"], source.run_id)
                    self.assertEqual(material["material"]["file"]["entry"]["sha256"],
                                     hashlib.sha256(b"one").hexdigest())
                    self.assertEqual(read_guardrail_file(agent, actor, review["wait_id"])["content"], b"one")
                for actor in (Principal("external", self.binding.tenant_id, False, True),
                              Principal("dual-role", self.binding.tenant_id, True, True)):
                    self.assert_code("ACCESS_DENIED", lambda: read_guardrail_file(agent, actor, review["wait_id"]))
                    self.assert_code("ACCESS_DENIED", lambda: list_interactions(
                        self.workflow, actor, QueryParams({"task_id": self.record.task_id})))
                self.assertFalse(self.target(batch).exists())
        self.change(self.child, state="COMPLETED")
        self.change(self.grandchild, state="COMPLETED")
        self.change(self.record, state="COMPLETED")
        self.assertEqual(read_guardrail_file(agent, owners[1], review["wait_id"])["content"], b"one")

    def test_source_lease_only_and_released_root_lease_allow_child_publication(self):
        root_token = self.lease(self.record)
        self.workflow.release_lease(self.record.run_id, tenant_id=self.record.tenant_id,
            worker_id="worker", token=root_token)
        source_token = self.lease(self.grandchild)
        batch = self.prepare()
        self.bind_source(batch, self.grandchild)
        self.assert_code("LEASE_LOST", lambda: self.service.record_decision(batch["batch_id"], self.binding,
            decision_ref="allow", allow=True, lease_token="stale"))
        self.service.record_decision(batch["batch_id"], self.binding,
            decision_ref="allow", allow=True, lease_token=source_token)
        self.assert_code("LEASE_LOST", lambda: self.service.publish(batch["batch_id"], self.binding, lease_token=root_token))
        self.service.publish(batch["batch_id"], self.binding, lease_token=source_token)
        self.assertEqual((self.target(batch) / "report.pdf").read_bytes(), b"one")

    def test_cancelled_or_closing_ancestors_block_bind_decision_and_publish(self):
        for ancestor in (self.record, self.child):
            for fields in ({"cancel_requested": True}, {"snapshot": {**ancestor.snapshot, "terminal_intent": "COMPLETED"}}):
                with self.subTest(ancestor=ancestor.run_id, fields=fields):
                    batch = self.prepare()
                    self.bind_source(batch, self.grandchild)
                    self.change(ancestor, **fields)
                    self.assert_code("FILE_BATCH_TASK_CLOSED", lambda: self.bind_source(self.prepare(), self.grandchild))
                    self.assert_code("FILE_BATCH_TASK_CLOSED", lambda: self.service.record_decision(
                        batch["batch_id"], self.binding, decision_ref="allow", allow=True))
                    self.assertEqual(self.store.get(batch["batch_id"], self.binding.tenant_id)["state"], "accepted_quarantine")
                    self.change(ancestor, cancel_requested=False, snapshot=ancestor.snapshot)
                    self.service.record_decision(batch["batch_id"], self.binding, decision_ref="allow", allow=True)
                    self.change(ancestor, **fields)
                    self.assert_code("FILE_BATCH_TASK_CLOSED", lambda: self.service.publish(batch["batch_id"], self.binding))
                    self.assertFalse(self.target(batch).exists())
                    self.change(ancestor, cancel_requested=False, snapshot=ancestor.snapshot)

    def test_root_followup_exception_does_not_allow_child_under_closing_root(self):
        self.change(self.record, snapshot={**self.record.snapshot, "terminal_intent": "COMPLETED"})
        self.assert_code("FILE_BATCH_TASK_CLOSED", lambda: self.bind_source(self.prepare(), self.record))
        self.bind_source(self.prepare(), self.record, sequence=1)
        self.assert_code("FILE_BATCH_TASK_CLOSED", lambda: self.bind_source(self.prepare(), self.child, sequence=1))

    def test_ancestry_ignores_budget_hint_and_rejects_foreign_context_cycles_and_overdepth(self):
        overdepth = self.child_of(self.grandchild)
        self.assert_code("FILE_BATCH_NOT_FOUND", lambda: self.bind_source(self.prepare(), overdepth))
        self.change(self.grandchild, snapshot={**self.grandchild.snapshot, "budget_root_id": "untrusted-root"})
        batch = self.prepare()
        row = self.bind_source(batch, self.grandchild)
        self.assertEqual(row["task_id"], self.record.task_id)
        for fields in ({"context_id": "foreign-chat"}, {"owner_id": "foreign-owner"},
                       {"tenant_id": str(uuid.uuid4())}, {"parent_run_id": self.grandchild.run_id}):
            with self.subTest(fields=fields):
                self.change(self.child, **fields)
                self.assert_code("FILE_BATCH_NOT_FOUND", lambda: self.bind_source(self.prepare(), self.grandchild))
                self.assert_code("FILE_BATCH_NOT_FOUND", lambda: self.service.review_material(
                    batch["batch_id"], self.binding, task_id=self.grandchild.task_id, run_id=self.grandchild.run_id))
                self.change(self.child, context_id=self.child.context_id, owner_id=self.child.owner_id,
                    tenant_id=self.child.tenant_id, parent_run_id=self.child.parent_run_id)

    def test_lease_expiry_during_file_work_never_commits_allow_or_published(self):
        batch = self.prepare()
        self.bind_source(batch, self.child)
        token = self.lease(self.child)
        from core_agent.chat_files import _verify
        original_chat = self.store.chat
        self.current_connection = None

        @contextmanager
        def chat(*args, **kwargs):
            with original_chat(*args, **kwargs) as connection:
                self.current_connection = connection
                try:
                    yield connection
                finally:
                    self.current_connection = None

        def expire_after_verify(*args):
            _verify(*args)
            self.expire(self.child)

        with patch.object(self.store, "chat", side_effect=chat), \
                patch("core_agent.chat_files._verify", side_effect=expire_after_verify):
            self.assert_code("LEASE_LOST", lambda: self.service.record_decision(
                batch["batch_id"], self.binding, decision_ref="allow", allow=True, lease_token=token))
        self.assertEqual(self.store.get(batch["batch_id"], self.binding.tenant_id)["state"], "accepted_quarantine")
        self.expire(self.child)  # PostgreSQL rolled back the injected in-transaction expiry.
        token = self.lease(self.child)
        self.service.record_decision(batch["batch_id"], self.binding, decision_ref="allow", allow=True, lease_token=token)
        with patch.object(self.store, "chat", side_effect=chat), \
                patch("core_agent.chat_files._verify", side_effect=expire_after_verify):
            self.assert_code("LEASE_LOST", lambda: self.service.publish(batch["batch_id"], self.binding, lease_token=token))
        self.assertEqual(self.store.get(batch["batch_id"], self.binding.tenant_id)["state"], "accepted_ready")
        self.assertFalse(self.target(batch).exists())

    def test_slow_bind_rechecks_ancestor_liveness_before_acceptance(self):
        batch = self.prepare()
        from core_agent.chat_files import _manifest

        transaction = self.database.transaction() if hasattr(self, "database") else nullcontext()
        with self.assertRaises(CoreError) as error:
            with transaction as connection:
                def close_after_manifest(*args):
                    _manifest(*args)
                    if connection is not None:
                        connection.execute("UPDATE core_runs SET cancel_requested=true WHERE run_id=%s", (self.child.run_id,))
                    else:
                        self.change(self.child, cancel_requested=True)

                with patch("core_agent.chat_files._manifest", side_effect=close_after_manifest):
                    self.bind_source(batch, self.grandchild, connection=connection)
        self.assertEqual(error.exception.code, "FILE_BATCH_TASK_CLOSED")
        self.assertEqual(self.store.get(batch["batch_id"], self.binding.tenant_id)["state"], "staging")


class MemoryChildFileTests(ChildFileContract, unittest.TestCase):
    def setUp(self):
        self.workflow = InMemoryWorkflowStore()
        self.store = MemoryChatFileStore(self.workflow, lambda binding: None)
        self.setup_files()


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "TEST_DATABASE_URL required for canonical child file FK proofs")
class PostgresChildFileTests(ChildFileContract, unittest.TestCase):
    def setUp(self):
        self.database = PostgresDatabase(os.environ["TEST_DATABASE_URL"], min_size=0, max_size=1)
        self.addCleanup(self.database.close)
        self.workflow = PostgresWorkflowStore(self.database)
        self.store = PostgresChatFileStore(self.database, self.workflow)
        self.setup_files()
        task = Task(id=self.record.task_id, context_id=self.binding.context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING))
        with self.database.transaction() as connection:
            connection.execute("INSERT INTO core_chats (tenant_id,context_id,owner_id) VALUES (%s,%s,%s)",
                (self.binding.tenant_id, self.binding.context_id, self.binding.owner_id))
            connection.execute("INSERT INTO core_a2a_tasks (task_id,owner,tenant,context_id,state,status_timestamp,payload) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)", (self.record.task_id, self.binding.owner_id,
                    self.binding.tenant_id, self.binding.context_id, TaskState.TASK_STATE_WORKING, 0, task.SerializeToString()))

    def test_borrowed_pool_one_locks_root_first_and_keeps_both_real_foreign_keys(self):
        batch = self.prepare()
        original = self.workflow.get
        locks = []

        def get(run_id, **kwargs):
            if kwargs.get("lock"):
                locks.append(run_id)
            return original(run_id, **kwargs)

        with self.database.transaction() as connection:
            with patch.object(self.database.pool, "connection", side_effect=AssertionError("nested checkout")), \
                    patch.object(self.workflow, "get", side_effect=get):
                with self.service.caller_scope(self.binding, task_id=self.grandchild.task_id,
                        run_id=self.grandchild.run_id, connection=connection) as (source, root, borrowed):
                    self.assertIs(borrowed, connection)
                    self.assertEqual((source.run_id, root.task_id), (self.grandchild.run_id, self.record.task_id))
                    self.assertEqual(locks[:3], [self.record.run_id, self.child.run_id, self.grandchild.run_id])
                    row = self.bind_source(batch, source, connection=connection)
                    self.change_in_transaction(connection, self.child.run_id)
                    self.assert_code("FILE_BATCH_TASK_CLOSED", lambda: self.enter_scope(connection))
                    connection.execute("UPDATE core_runs SET cancel_requested=false WHERE run_id=%s", (self.child.run_id,))
                self.assertEqual(connection.execute("SELECT 1 AS value").fetchone()["value"], 1)
            count = connection.execute("SELECT count(*) AS value FROM core_a2a_tasks WHERE tenant=%s",
                (self.binding.tenant_id,)).fetchone()["value"]
            self.assertEqual(count, 1)
            self.assertEqual((row["run_id"], row["task_id"]), (self.grandchild.run_id, self.record.task_id))

    def enter_scope(self, connection):
        with self.service.caller_scope(self.binding, task_id=self.grandchild.task_id,
                run_id=self.grandchild.run_id, connection=connection):
            self.fail("cancelled intermediate admitted")

    @staticmethod
    def change_in_transaction(connection, run_id):
        connection.execute("UPDATE core_runs SET cancel_requested=true WHERE run_id=%s", (run_id,))
