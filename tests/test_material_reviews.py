import os
import threading
import unittest
import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from core_agent.database import PostgresDatabase
from core_agent.errors import CoreError
from core_agent.guardrails import GuardrailClassifier
from core_agent.material_reviews import MemoryMaterialReviewStore, PostgresMaterialReviewStore
from core_agent.workflow import InMemoryWorkflowStore, PostgresWorkflowStore, WorkflowRecord


class Model:
    context_window = 16384
    max_tokens = 512

    def __init__(self, verdict="clear"):
        self.verdict = verdict
        self.calls = []

    @staticmethod
    def count_tokens(text):
        return len(text)

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(message='{"verdict":"' + self.verdict + '"}', finish_reason="stop",
                               tool_requests=(), prompt_tokens=10, completion_tokens=5)


class MaterialReviewContract:
    def setup_run(self):
        self.record = self.workflow.create(WorkflowRecord(
            str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()),
            "owner", None, "RUNNING", 1, {"prompt": "private"}, {},
        ))
        self.token = self.lease()
        self.now = self.workflow.current_time()

    def lease(self):
        return self.workflow.acquire_lease(self.record.run_id, tenant_id=self.record.tenant_id,
                                           owner_id=self.record.owner_id, worker_id="worker", ttl=100)

    def create(self, **kwargs):
        return self.store.create(self.record, source_id="input-1", source_kind="input",
                                 payload=kwargs.pop("payload", "secret raw material"),
                                 deadline=kwargs.pop("deadline", self.now + 60),
                                 lease_token=self.token, **kwargs)

    def current(self):
        return self.workflow.get(self.record.run_id, tenant_id=self.record.tenant_id)

    def classify(self, row, verdict="clear", **kwargs):
        self.model = Model(verdict)
        classifier = GuardrailClassifier(self.model, clock=self.workflow.current_time)
        return self.store.classify(self.record, row["review_id"], classifier,
                                   lease_token=self.token, continuation={"version": 1, "phase": "input"},
                                   snapshot=self.current().snapshot, **kwargs)

    def get(self, row):
        return self.store.get(self.record, row["review_id"])

    def read(self, row):
        return self.store.read_payload(self.record, row["review_id"], lease_token=self.token)

    def resolve(self, row, reason):
        return self.workflow.resolve_wait(row["wait_id"], tenant_id=self.record.tenant_id,
                                         outcome={"reason": reason}, actor_id="company-owner")

    def restart(self):
        if isinstance(self.store, MemoryMaterialReviewStore):
            restarted = MemoryMaterialReviewStore(self.workflow)
            restarted.rows = self.store.rows
        else:
            restarted = PostgresMaterialReviewStore(self.database, PostgresWorkflowStore(self.database))
        self.store = restarted

    def test_private_identity_exact_replay_changed_content_and_scope(self):
        row = self.create(completed_result_ref={"call_id": "call-1", "status": "completed"})
        same = self.create(deadline=self.now + 999, max_calls=1,
                           completed_result_ref={"call_id": "call-1", "status": "completed"})
        self.assertEqual(row, same)
        self.assertNotIn("payload", row)
        self.assertNotIn("completed_result_ref", row)
        changed = self.create(payload="changed bytes")
        self.assertNotEqual(row["review_id"], changed["review_id"])
        with self.assertRaises(CoreError):
            self.create(payload="changed bytes", content_digest=row["content_digest"])
        for field, value in (("tenant_id", "other"), ("owner_id", "other"), ("owner_id", None),
                             ("owner_id", ""), ("tenant_id", None),
                             ("context_id", "other"), ("task_id", "other")):
            with self.subTest(field=field), self.assertRaises(CoreError):
                self.store.owner_read_payload(replace(self.record, **{field: value}), row["review_id"])
        other = self.workflow.create(replace(self.record, run_id=str(uuid.uuid4()), task_id=str(uuid.uuid4())))
        with self.assertRaises(CoreError):
            self.store.owner_read_payload(other, row["review_id"])
        self.restart()
        private = self.store.owner_read_payload(self.record, row["review_id"])
        self.assertEqual(private["payload"], "secret raw material")
        self.assertEqual(private["completed_result_ref"]["status"], "completed")

    def test_clear_charged_before_provider_readable_after_restart_no_workflow_bump(self):
        row = self.create()
        version = self.current().version
        model = Model()
        generate = model.generate

        def checked(**kwargs):
            charged = self.get(row)
            self.assertEqual(charged["attempts_used"], 1)
            self.assertGreater(charged["input_tokens_used"], 0)
            return generate(**kwargs)

        with patch.object(model, "generate", side_effect=checked):
            result = self.store.classify(self.record, row["review_id"], GuardrailClassifier(model, clock=self.workflow.current_time),
                lease_token=self.token, continuation={"version": 1, "phase": "input"}, snapshot=self.current().snapshot)
        self.assertEqual(result["state"], "clear")
        self.assertEqual(result["classification"]["prompt_tokens"], 10)
        self.assertEqual(self.current().version, version)
        self.restart()
        self.assertEqual(self.read(row)["payload"], "secret raw material")
        self.assertEqual(self.classify(row)["state"], "clear")
        self.assertEqual(self.model.calls, [])

    def test_suspicious_wait_is_atomic_private_and_owner_authoritative(self):
        row = self.classify(self.create(), "suspicious", owner_timeout_seconds=30)
        self.assertEqual(row["state"], "pending")
        wait = self.workflow.get_wait(row["wait_id"], tenant_id=self.record.tenant_id)
        self.assertEqual(wait.kind, "guardrail")
        self.assertEqual(wait.subject["content_digest"], row["content_digest"])
        self.assertNotIn("secret raw", str(wait.subject))
        self.assertEqual(wait.continuation, {"version": 1, "phase": "input"})
        self.assertEqual(self.current().snapshot["wait_id"], row["wait_id"])
        self.assertEqual(self.current().state, "WAITING_INPUT")
        self.assertIsNone(self.lease_if_waiting())
        self.assertAlmostEqual(wait.deadline - wait.created_at, 30, delta=1)
        self.resolve(row, "allowed")
        self.resolve(row, "rejected")
        self.restart()
        self.assertEqual(self.get(row)["state"], "allowed")
        self.token = self.lease()
        self.assertEqual(self.read(row)["payload"], "secret raw material")

    def lease_if_waiting(self):
        if isinstance(self.workflow, InMemoryWorkflowStore):
            return self.workflow._leases.get(self.record.run_id)
        with self.database.transaction() as conn:
            return conn.execute("SELECT lease_token FROM core_runs WHERE run_id=%s", (self.record.run_id,)).fetchone()["lease_token"]

    def test_reject_persists_and_never_rechecks_same_version(self):
        row = self.classify(self.create(), "suspicious")
        self.resolve(row, "rejected")
        self.assertEqual(self.get(row)["state"], "rejected")
        self.token = self.lease()
        with self.assertRaises(CoreError) as error:
            self.read(row)
        self.assertEqual(error.exception.code, "MATERIAL_REVIEW_REQUIRED")
        self.assertEqual(self.classify(row)["state"], "rejected")
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.create()["review_id"], row["review_id"])

    def test_interrupted_charged_attempt_is_not_replayed_or_refunded(self):
        row = self.create()

        class Interrupted:
            @staticmethod
            def classify(documents, *, record_attempt, **kwargs):
                record_attempt(123)
                raise RuntimeError("process interrupted")

        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.store.classify(self.record, row["review_id"], Interrupted(), lease_token=self.token,
                                continuation={"version": 1, "phase": "input"}, snapshot=self.current().snapshot)
        self.restart()
        result = self.classify(row)
        self.assertEqual(result["classification"]["reason"], "interrupted")
        self.assertEqual((result["attempts_used"], result["input_tokens_used"]), (1, 123))
        self.assertEqual(result["state"], "pending")
        self.assertEqual(self.model.calls, [])

    def test_storage_failure_in_charge_prevents_provider(self):
        row = self.create()
        save = self.store._save

        def fail(row, conn):
            if row["attempts_used"]:
                raise RuntimeError("storage unavailable")
            return save(row, conn)

        with patch.object(self.store, "_save", side_effect=fail), self.assertRaisesRegex(RuntimeError, "storage"):
            self.classify(row)
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.get(row)["attempts_used"], 0)
        self.assertEqual(self.get(row)["state"], "checking")

    def test_atomic_pending_failure_does_not_leave_wait_or_release_lease(self):
        row = self.create()
        save = self.store._save

        def fail(row, conn):
            if row["state"] == "pending":
                raise RuntimeError("failed to persist decision")
            return save(row, conn)

        with patch.object(self.store, "_save", side_effect=fail), self.assertRaisesRegex(RuntimeError, "persist"):
            self.classify(row, "suspicious")
        self.assertEqual(self.current().state, "RUNNING")
        self.assertNotIn("wait_id", self.current().snapshot)
        self.assertEqual(self.get(row)["state"], "checking")
        self.assertEqual(self.get(row)["attempts_used"], 1)
        self.assertEqual(self.lease_if_waiting()[1] if isinstance(self.workflow, InMemoryWorkflowStore)
                         else self.lease_if_waiting(), self.token)
        self.assertFalse([wait for wait in self.workflow.pending_waits() if wait.run_id == self.record.run_id])

    def test_frozen_budget_and_expired_detector_deadline_do_not_dispatch(self):
        row = self.create(max_input_tokens=1)
        result = self.classify(row)
        self.assertEqual(result["classification"]["reason"], "budget_exhausted")
        self.assertEqual(result["attempts_used"], 0)
        self.assertEqual(self.model.calls, [])

    def test_detector_deadline_does_not_restart(self):
        row = self.create(deadline=self.now - 1)
        result = self.classify(row)
        self.assertEqual(result["classification"]["reason"], "timeout")
        self.assertEqual(result["attempts_used"], 0)
        self.assertEqual(self.model.calls, [])

    def test_persisted_call_cap_keeps_observed_usage_after_first_chunk(self):
        row = self.create(payload="A" * 16000, max_calls=1)
        result = self.classify(row)
        self.assertEqual(result["classification"]["reason"], "budget_exhausted")
        self.assertEqual((result["attempts_used"], result["input_tokens_used"]), (1, 8192))
        self.assertEqual((result["classification"]["calls"], result["classification"]["input_tokens"]), (1, 8192))
        self.assertEqual((result["classification"]["prompt_tokens"], result["classification"]["completion_tokens"]), (10, 5))
        self.assertEqual(len(self.model.calls), 1)

    def test_callback_deadline_race_persists_prior_chunk_usage(self):
        row = self.create(payload="A" * 16000)
        charge = self.store.record_attempt
        calls = []

        def raced(*args, **kwargs):
            if calls:
                raise CoreError("MATERIAL_REVIEW_DEADLINE")
            calls.append(kwargs["input_tokens"])
            return charge(*args, **kwargs)

        with patch.object(self.store, "record_attempt", side_effect=raced):
            result = self.classify(row)
        self.assertEqual(result["classification"]["reason"], "timeout")
        self.assertEqual((result["attempts_used"], result["input_tokens_used"]), (1, 8192))
        self.assertEqual((result["classification"]["calls"], result["classification"]["input_tokens"]), (1, 8192))
        self.assertEqual((result["classification"]["prompt_tokens"], result["classification"]["completion_tokens"]), (10, 5))
        self.assertEqual(len(self.model.calls), 1)

    def test_cancel_and_terminal_fence_attempts(self):
        row = self.create()
        self.workflow.request_cancel(self.record.run_id, tenant_id=self.record.tenant_id, owner_id=self.record.owner_id)
        with self.assertRaises(CoreError) as error:
            self.classify(row)
        self.assertEqual(error.exception.code, "CANCEL_REQUESTED")
        self.assertEqual(self.get(row)["attempts_used"], 0)
        self.assertEqual(self.model.calls, [])

    def test_terminal_fences_classifier_and_clear_payload_read(self):
        row = self.classify(self.create())
        current = self.current()
        self.workflow.transition(current.run_id, tenant_id=current.tenant_id, owner_id=current.owner_id,
                                 expected_version=current.version, state="COMPLETED", snapshot=current.snapshot,
                                 event_kind="task.completed", lease_token=self.token)
        for operation in (lambda: self.classify(row), lambda: self.read(row)):
            with self.assertRaises(CoreError):
                operation()

    def test_wrong_and_missing_lease_prevent_provider(self):
        row = self.create()
        for token in ("wrong", "", None):
            self.token = token
            with self.subTest(token=token), self.assertRaises(CoreError) as error:
                self.classify(row)
            self.assertEqual(error.exception.code, "LEASE_LOST")
            self.assertEqual(self.get(row)["attempts_used"], 0)
            self.assertEqual(self.model.calls, [])

    def test_sealed_reference_has_no_path_and_cannot_change_kind_or_version(self):
        options = dict(source_id="file-1", source_kind="file", sealed_ref={"batch_id": "batch", "index": 0},
                       content_digest="a" * 64, deadline=self.now + 60, lease_token=self.token)
        row = self.store.create(self.record, **options)
        self.assertEqual(self.store.owner_read_payload(self.record, row["review_id"])["sealed_ref"], options["sealed_ref"])
        for updates in ({"source_kind": "input"}, {"sealed_ref": {"batch_id": "other", "index": 0}},
                        {"sealed_ref": {"path": "/private/secret"}}):
            with self.assertRaises(CoreError):
                self.store.create(self.record, **(options | updates))
        result = self.classify(row, documents=None, complete=False)
        self.assertEqual(result["classification"]["reason"], "incomplete_extraction")
        self.assertEqual(self.model.calls, [])

    def test_concurrent_creation_returns_one_review(self):
        barrier = threading.Barrier(2)
        rows, errors = [], []

        def create():
            try:
                barrier.wait(timeout=5)
                rows.append(self.create())
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=create) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(rows[0], rows[1])

    def test_active_wait_collision_leaves_review_checking_and_preserves_old_wait(self):
        row = self.create()
        wait = self.workflow.enter_wait(self.current(), kind="owner_question", source_id="question",
            subject={"question": "choose"}, continuation={"version": 1, "phase": "tool_wait"},
            deadline=self.now + 30, snapshot=self.current().snapshot, lease_token=self.token)
        # A misordered runtime continuation must not overwrite this unresolved wait.
        self.token = self.lease()
        with self.assertRaises(CoreError):
            self.classify(row, "suspicious")
        self.assertEqual(self.current().snapshot["wait_id"], wait.wait_id)
        self.assertEqual(self.get(row)["state"], "checking")
        self.assertIsNone(self.get(row)["wait_id"])
        self.assertEqual(len([w for w in self.workflow.pending_waits() if w.run_id == self.record.run_id]), 1)

    def test_competing_owner_decisions_have_one_authoritative_outcome(self):
        row = self.classify(self.create(), "suspicious")
        barrier = threading.Barrier(2)
        states, errors = [], []

        def decide(reason):
            try:
                barrier.wait(timeout=5)
                self.resolve(row, reason)
                states.append(self.get(row)["state"])
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=decide, args=(reason,)) for reason in ("allowed", "rejected")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(set(states)), 1)
        self.assertIn(states[0], {"allowed", "rejected"})

    def test_provider_failure_is_safe_unverified_with_charged_attempt(self):
        row = self.create()
        with patch.object(Model, "generate", side_effect=RuntimeError("secret provider diagnostic")):
            result = self.classify(row)
        self.assertEqual(result["classification"]["reason"], "provider_error")
        self.assertEqual(result["attempts_used"], 1)
        self.assertNotIn("secret", str(result))


class MemoryMaterialReviewTests(MaterialReviewContract, unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.workflow = InMemoryWorkflowStore(clock=lambda: self.now)
        self.store = MemoryMaterialReviewStore(self.workflow)
        self.setup_run()

    def test_timeout_wins_late_owner_decision_and_read_commits_timeout(self):
        row = self.classify(self.create(), "suspicious", owner_timeout_seconds=10)
        self.now += 10
        self.token = self.lease()
        with self.assertRaises(CoreError):
            self.read(row)
        self.assertEqual(self.store.rows[row["review_id"]]["state"], "timed_out")
        self.resolve(row, "allowed")
        self.assertEqual(self.get(row)["state"], "timed_out")

    def test_expired_lease_cannot_charge(self):
        row = self.create()
        self.now += 101
        with self.assertRaises(CoreError) as error:
            self.classify(row)
        self.assertEqual(error.exception.code, "LEASE_LOST")
        self.assertEqual(self.get(row)["attempts_used"], 0)


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "set TEST_DATABASE_URL for material review persistence proofs")
class PostgresMaterialReviewTests(MaterialReviewContract, unittest.TestCase):
    def setUp(self):
        self.database = PostgresDatabase(os.environ["TEST_DATABASE_URL"], min_size=0, max_size=5)
        self.addCleanup(self.database.close)
        self.database.migrate()
        self.workflow = PostgresWorkflowStore(self.database)
        self.store = PostgresMaterialReviewStore(self.database, self.workflow)
        self.setup_run()

    def test_creation_joins_completed_outcome_transaction(self):
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.database.transaction() as conn:
                row = self.create(connection=conn)
                conn.execute("UPDATE core_runs SET snapshot=snapshot || '{\"completed_ref\":\"result-1\"}' WHERE run_id=%s",
                             (self.record.run_id,))
                raise RuntimeError("rollback")
        with self.assertRaises(CoreError):
            self.get(row)
        self.assertNotIn("completed_ref", self.current().snapshot)

    def test_database_rejects_identity_mutation_and_reopening(self):
        from psycopg import Error
        row = self.classify(self.create())
        for assignment in ("payload='\"changed\"'", "content_digest=repeat('b',64)", "state='checking', classification=NULL"):
            with self.assertRaises(Error), self.database.transaction() as conn:
                conn.execute("UPDATE core_material_reviews SET " + assignment + ",revision=revision+1 WHERE review_id=%s",
                             (row["review_id"],))

    def test_public_outbox_omits_review_payload_and_reference(self):
        row = self.classify(self.create(), "suspicious")
        with self.database.transaction() as conn:
            events = conn.execute("SELECT payload FROM core_outbox WHERE aggregate_id=%s", (self.record.run_id,)).fetchall()
        self.assertNotIn("secret raw material", str(events))
        self.assertNotIn(row["review_id"], str(events))


if __name__ == "__main__":
    unittest.main()
