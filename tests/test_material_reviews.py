import json
import os
import threading
import unittest
import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from a2a.types import Task, TaskState, TaskStatus

from core_agent.a2a_sdk import ScopedMemoryTaskStore
from core_agent.auth import AuthenticatedCallContext, ScopeUser
from core_agent.database import PostgresDatabase
from core_agent.errors import CoreError
from core_agent.guardrails import GuardrailClassifier
from core_agent.material_reviews import MemoryMaterialReviewStore, PostgresMaterialReviewStore
from core_agent.observability import RecordingExporter, Telemetry
from core_agent.workflow import InMemoryWorkflowStore, OutboxDispatcher, PostgresWorkflowStore, WorkflowRecord


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

    def diagnostics(self):
        if isinstance(self.workflow, InMemoryWorkflowStore):
            return [event for event in getattr(self.workflow, "_guardrail_events", ())
                    if event["run_id"] == self.record.run_id]
        with self.database.transaction() as connection:
            return connection.execute("""SELECT kind,data FROM core_events
                WHERE run_id=%s AND tenant_id=%s AND kind LIKE 'guardrail.%%'
                ORDER BY revision""", (self.record.run_id, self.record.tenant_id)).fetchall()

    def test_clear_diagnostic_is_private_committed_once_without_workflow_transition(self):
        row = self.create()
        version = self.current().version
        result = self.classify(row)
        events = self.diagnostics()
        self.assertEqual([event["kind"] for event in events], ["guardrail.classified"])
        data = events[0]["data"]
        self.assertEqual((data["verdict"], data["reason"]), ("clear", "clear"))
        self.assertEqual(data["review_id"], row["review_id"])
        self.assertEqual(data["source_id"], "input-1")
        self.assertEqual(data["source_kind"], "input")
        self.assertEqual(data["detector_deadline"], row["deadline"])
        self.assertEqual(data["attempts_used"], 1)
        self.assertEqual(data["input_tokens_used"], result["input_tokens_used"])
        self.assertEqual((data["prompt_tokens"], data["completion_tokens"]), (10, 5))
        self.assertGreaterEqual(data["elapsed_seconds"], 0)
        self.assertEqual(self.current().version, version)
        self.assertFalse({"payload", "sealed_ref", "completed_result_ref", "content_digest", "material_digest",
                          "attempt_token", "answer"} & data.keys())
        self.assertNotIn("secret raw material", json.dumps(data))
        self.restart()
        self.classify(row)
        self.assertEqual(self.diagnostics(), events)

    def test_pending_diagnostics_correlate_distinct_deadlines_and_deduplicate_lifecycle(self):
        row = self.classify(self.create(), "suspicious")
        wait = self.workflow.get_wait(row["wait_id"], tenant_id=self.record.tenant_id)
        events = self.diagnostics()
        self.assertEqual([event["kind"] for event in events], ["guardrail.classified", "guardrail.wait.entered"])
        classified, entered = [event["data"] for event in events]
        self.assertEqual(classified["review_id"], entered["review_id"])
        self.assertEqual(entered["wait_id"], row["wait_id"])
        self.assertEqual(entered["detector_deadline"], row["deadline"])
        self.assertEqual(entered["owner_deadline"], wait.deadline)
        self.assertAlmostEqual(wait.deadline - wait.created_at, 86400, delta=1)
        self.resolve(row, "allowed")
        self.resolve(row, "rejected")
        self.restart()
        self.get(row)
        events = self.diagnostics()
        self.assertEqual([event["kind"] for event in events].count("guardrail.wait.resolved"), 1)
        resolved = next(event["data"] for event in events if event["kind"] == "guardrail.wait.resolved")
        self.assertEqual(resolved["resolution_reason"], "allowed")
        self.assertEqual(resolved["review_id"], row["review_id"])
        self.assertEqual(resolved["wait_id"], row["wait_id"])
        current = self.current()
        self.token = self.lease()
        snapshot = {key: value for key, value in current.snapshot.items() if key not in {"wait_id", "wait_ready"}}
        self.workflow.transition(current.run_id, tenant_id=current.tenant_id, owner_id=current.owner_id,
            expected_version=current.version, state="RUNNING", snapshot=snapshot, lease_token=self.token,
            event_kind="wait.continued")
        self.assertEqual([event["kind"] for event in self.diagnostics()].count("guardrail.wait.applied"), 1)

    def test_rolled_back_pending_classification_does_not_leave_diagnostics(self):
        row = self.create()
        save = self.store._save

        def fail(decision, connection):
            save(decision, connection)
            if decision["state"] == "pending":
                raise RuntimeError("decision rollback")

        with patch.object(self.store, "_save", side_effect=fail), self.assertRaisesRegex(RuntimeError, "rollback"):
            self.classify(row, "suspicious")
        self.assertEqual(self.get(row)["state"], "checking")
        self.assertEqual(self.current().state, "RUNNING")
        self.assertEqual(self.diagnostics(), [])

    def test_detector_timeout_is_not_reported_as_owner_timeout(self):
        self.classify(self.create(deadline=self.now - 1))
        events = self.diagnostics()
        self.assertEqual([event["kind"] for event in events], ["guardrail.classified", "guardrail.wait.entered"])
        self.assertEqual(events[0]["data"]["reason"], "timeout")
        self.assertEqual(events[0]["data"]["attempts_used"], 0)
        self.assertEqual(events[1]["data"]["reason"], "timeout")
        self.assertGreater(events[1]["data"]["owner_deadline"], self.now)
        self.assertNotIn("resolution_reason", events[1]["data"])

    def test_diagnostic_export_failure_keeps_committed_review_and_stable_retry_identity(self):
        row = self.classify(self.create())
        drain = getattr(self.workflow, "drain_guardrail_events", None)
        self.assertTrue(callable(drain), "committed diagnostic export is not implemented")
        seen = []

        def failed(event):
            self.assertEqual(self.get(row)["state"], "clear")
            seen.append(event)
            return False

        self.assertEqual(drain(self.record, failed), 0)
        self.assertEqual(self.get(row)["state"], "clear")
        self.assertEqual(len(seen), 1)
        self.assertEqual(drain(self.record, failed), 0)
        self.assertEqual(len(seen), 1, "failed export was retried without backoff")
        if isinstance(self.workflow, InMemoryWorkflowStore):
            self.now += 2
        else:
            with self.database.transaction() as connection:
                connection.execute("""UPDATE core_outbox SET available_at=0 WHERE tenant_id=%s
                    AND aggregate_id=%s AND aggregate_type='diagnostic' AND published_at IS NULL""",
                    (self.record.tenant_id, self.record.run_id))
        self.assertEqual(drain(self.record, lambda event: seen.append(event) or True), 1)
        self.assertEqual(seen[0]["event_id"], seen[1]["event_id"])
        self.assertEqual(drain(self.record, lambda event: self.fail("accepted event was exported again")), 0)

    def test_diagnostic_persistence_failure_rolls_back_classification_and_wait(self):
        row = self.create()
        writer = getattr(self.workflow, "record_guardrail_event", None)
        self.assertTrue(callable(writer), "atomic diagnostic persistence is not implemented")
        with patch.object(self.workflow, "record_guardrail_event", side_effect=RuntimeError("journal rollback")):
            with self.assertRaisesRegex(RuntimeError, "journal rollback"):
                self.classify(row, "suspicious")
        self.assertEqual(self.current().state, "RUNNING")
        self.assertEqual(self.get(row)["state"], "checking")
        self.assertEqual(self.diagnostics(), [])

    def test_valid_non_ascii_scope_exports_with_safe_correlation_and_omits_unsafe_source(self):
        self.record = self.workflow.create(replace(self.record, run_id=str(uuid.uuid4()),
            task_id=f"Задача с пробелом {self.record.task_id}", tenant_id="Компания диагностики", context_id="Чат с пробелом", snapshot={}))
        self.token = self.lease()
        row = self.store.create(self.record, source_id="private source/unsafe?value", source_kind="unknown private kind",
            payload="secret source bytes", deadline=self.now + 60, lease_token=self.token)
        self.classify(row)
        data = self.diagnostics()[0]["data"]
        self.assertTrue({"run_id", "task_id", "tenant_id", "review_id"} <= data.keys())
        self.assertEqual(data["source_kind"], "other")
        self.assertNotIn("source_id", data)
        encoded = json.dumps(data, ensure_ascii=False)
        for private in (self.record.tenant_id, self.record.task_id, self.record.context_id,
                        "private source/unsafe?value", "unknown private kind", "secret source bytes"):
            self.assertNotIn(private, encoded)
        exporter = RecordingExporter()
        self.assertEqual(self.workflow.drain_guardrail_events(self.record, Telemetry(exporter).publish_diagnostic), 1)
        self.assertEqual(len(exporter.spans), 1)

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

    def test_five_occupied_slots_create_owner_wait_without_provider_attempt(self):
        row = self.create()
        model = Model()
        classifier = GuardrailClassifier(model, clock=self.workflow.current_time)
        acquired = 0
        try:
            for _ in range(5):
                self.assertTrue(classifier._slot.acquire(blocking=False))
                acquired += 1
            result = self.store.classify(
                self.record, row["review_id"], classifier, lease_token=self.token,
                continuation={"version": 1, "phase": "input"}, snapshot=self.current().snapshot,
            )
        finally:
            for _ in range(acquired):
                classifier._slot.release()
        self.assertEqual(result["classification"]["reason"], "detector_busy")
        self.assertEqual((result["state"], result["attempts_used"], result["input_tokens_used"]), ("pending", 0, 0))
        self.assertEqual(model.calls, [])
        self.assertEqual(self.current().state, "WAITING_INPUT")
        wait = self.workflow.get_wait(result["wait_id"], tenant_id=self.record.tenant_id)
        self.assertEqual(wait.kind, "guardrail")
        self.assertIsNone(self.lease_if_waiting())
        self.restart()
        self.assertEqual(self.get(row)["state"], "pending")

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

    def bind_canonical_observer(self):
        self.record = replace(self.current(), snapshot={**self.current().snapshot,
            "output_contract_version": 2, "public_messages": [], "published_artifacts": []})
        self.workflow._records[self.record.run_id] = self.record
        tasks = ScopedMemoryTaskStore(workflow_store=self.workflow)
        context = AuthenticatedCallContext(tenant=self.record.tenant_id, user=ScopeUser(self.record.owner_id))
        tasks._save_admission(Task(id=self.record.task_id, context_id=self.record.context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_WORKING)), context)
        self.workflow.public_output_observer = tasks.reconcile_run
        return tasks

    @staticmethod
    def canonical_aliases(tasks):
        return {owner: {task_id: task.SerializeToString(deterministic=True) for task_id, task in rows.items()}
                for owner, rows in tasks._impl.tasks.items()}

    def test_pending_rollback_never_publishes_phantom_canonical_task(self):
        for failure in ("review", "classified", "entered"):
            with self.subTest(failure=failure):
                tasks = self.bind_canonical_observer()
                before = self.canonical_aliases(tasks)
                row = self.create(payload="private fixture " + failure)
                save, write = self.store._save, self.workflow.record_guardrail_event

                def fail_save(decision, connection):
                    save(decision, connection)
                    if failure == "review" and decision["state"] == "pending":
                        raise RuntimeError("atomic review rollback")

                def fail_journal(record, kind, data, **options):
                    result = write(record, kind, data, **options)
                    if kind == "guardrail." + ("classified" if failure == "classified" else "wait.entered"):
                        raise RuntimeError("atomic journal rollback")
                    return result

                with patch.object(self.store, "_save", side_effect=fail_save), \
                        patch.object(self.workflow, "record_guardrail_event", side_effect=fail_journal):
                    with self.assertRaisesRegex(RuntimeError, "atomic"):
                        self.classify(row, "suspicious")
                self.assertEqual(self.current().state, "RUNNING")
                self.assertEqual(self.get(row)["state"], "checking")
                self.assertIsNone(self.get(row)["wait_id"])
                self.assertEqual(self.diagnostics(), [])
                self.assertFalse(self.workflow.pending_waits())
                self.assertEqual(self.canonical_aliases(tasks), before)

    def test_applied_journal_failure_never_changes_canonical_task_or_wait(self):
        tasks = self.bind_canonical_observer()
        row = self.classify(self.create(), "suspicious")
        self.resolve(row, "allowed")
        self.get(row)
        before_tasks = self.canonical_aliases(tasks)
        before_record = self.current()
        before_wait = self.workflow.get_wait(row["wait_id"], tenant_id=self.record.tenant_id)
        before_journal = self.diagnostics().copy()
        self.token = self.lease()
        snapshot = {key: value for key, value in before_record.snapshot.items() if key not in {"wait_id", "wait_ready"}}
        write = self.workflow.record_guardrail_event

        def fail(record, kind, data, **options):
            result = write(record, kind, data, **options)
            if kind == "guardrail.wait.applied":
                raise RuntimeError("applied rollback")
            return result

        with patch.object(self.workflow, "record_guardrail_event", side_effect=fail), \
                self.assertRaisesRegex(RuntimeError, "applied rollback"):
            self.workflow.transition(before_record.run_id, tenant_id=before_record.tenant_id,
                owner_id=before_record.owner_id, expected_version=before_record.version, state="RUNNING",
                snapshot=snapshot, lease_token=self.token)
        self.assertEqual(self.current(), before_record)
        self.assertEqual(self.workflow.get_wait(row["wait_id"], tenant_id=self.record.tenant_id), before_wait)
        self.assertEqual(self.diagnostics(), before_journal)
        self.assertEqual(self.get(row)["state"], "allowed")
        self.assertEqual(self.canonical_aliases(tasks), before_tasks)

    def test_nested_material_scope_publishes_once_after_review_wait_and_journal_commit(self):
        tasks = self.bind_canonical_observer()
        before = self.canonical_aliases(tasks)
        row = self.create()
        observed = []
        canonical = tasks.reconcile_run

        def observe(record, connection=None):
            private = self.store.rows[row["review_id"]]
            self.assertEqual(private["state"], "pending")
            self.assertEqual(self.current().snapshot["wait_id"], private["wait_id"])
            self.assertEqual([event["kind"] for event in self.diagnostics()],
                             ["guardrail.classified", "guardrail.wait.entered"])
            observed.append(record.version)
            return canonical(record, connection)

        self.workflow.public_output_observer = observe
        with self.store._scope(self.record, lease_token=self.token):
            self.classify(row, "suspicious")
            self.assertEqual(observed, [])
            self.assertEqual(self.canonical_aliases(tasks), before)
        self.assertEqual(observed, [self.current().version])
        self.assertNotEqual(self.canonical_aliases(tasks), before)

    def test_public_observer_error_rolls_back_pending_scope_and_next_commit_still_publishes(self):
        tasks = self.bind_canonical_observer()
        before = self.canonical_aliases(tasks)
        row = self.create()
        canonical = tasks.reconcile_run

        def fail(record, connection=None):
            raise RuntimeError("canonical observer unavailable")

        self.workflow.public_output_observer = fail
        with self.assertRaisesRegex(RuntimeError, "canonical observer"):
            self.classify(row, "suspicious")
        self.assertEqual(self.current().state, "RUNNING")
        self.assertEqual(self.get(row)["state"], "checking")
        self.assertIsNone(self.get(row)["wait_id"])
        self.assertFalse(self.workflow.pending_waits())
        self.assertEqual(self.diagnostics(), [])
        self.assertEqual(self.canonical_aliases(tasks), before)
        self.workflow.public_output_observer = canonical
        self.classify(row)
        self.assertEqual(self.current().state, "WAITING_INPUT")
        self.assertNotEqual(self.canonical_aliases(tasks), before)

    def test_legacy_wait_resolution_retains_safe_verdict_without_classification_journal(self):
        row = self.classify(self.create(), "suspicious")
        self.workflow._guardrail_events.clear()
        self.restart()
        self.resolve(row, "allowed")
        data = self.diagnostics()[0]["data"]
        self.assertEqual(data.get("verdict"), "suspicious")
        self.assertEqual(data.get("reason"), "suspicious")
        self.assertEqual(data["resolution_reason"], "allowed")
        self.assertEqual(data["review_id"], row["review_id"])
        self.assertNotIn("content_digest", data)

    def test_reading_and_classifying_own_review_never_clones_foreign_payloads(self):
        row = self.create()

        class ForeignPayload(dict):
            def __deepcopy__(self, memo):
                raise AssertionError("foreign private material was copied")

        foreign = dict(self.store.rows[row["review_id"]], review_id=str(uuid.uuid4()), tenant_id="foreign",
                       payload=ForeignPayload(private="foreign material"))
        self.store.rows[foreign["review_id"]] = foreign
        self.assertEqual(self.get(row)["state"], "checking")
        self.assertEqual(self.classify(row)["state"], "clear")

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

    def test_borrowed_classification_transaction_hides_and_rolls_back_private_events(self):
        row = self.create()
        finish = self.store.finish
        with self.assertRaisesRegex(RuntimeError, "outer rollback"):
            with self.database.transaction() as connection:
                def joined(*args, **kwargs):
                    return finish(*args, **kwargs, connection=connection)

                with patch.object(self.store, "finish", side_effect=joined):
                    self.classify(row, "suspicious")
                count = connection.execute("""SELECT count(*) AS n FROM core_outbox
                    WHERE aggregate_type='diagnostic' AND aggregate_id=%s""", (self.record.run_id,)).fetchone()["n"]
                self.assertEqual(count, 2)
                self.assertEqual(self.workflow.drain_guardrail_events(self.record,
                    lambda event: self.fail("uncommitted event was exported")), 0)
                raise RuntimeError("outer rollback")
        self.assertEqual(self.get(row)["state"], "checking")
        self.assertEqual(self.current().state, "RUNNING")
        self.assertEqual(self.diagnostics(), [])

    def test_restarted_terminal_diagnostic_export_is_scoped_and_uses_stable_event_id(self):
        self.classify(self.create())
        current = self.current()
        self.workflow.transition(current.run_id, tenant_id=current.tenant_id, owner_id=current.owner_id,
            expected_version=current.version, state="COMPLETED", snapshot=current.snapshot,
            event_kind="task.completed", lease_token=self.token)
        restarted = PostgresWorkflowStore(self.database)
        self.assertNotIn(current.run_id, [record.run_id for record in restarted.pending_guardrail_records(tenant_id="foreign")])
        records = restarted.pending_guardrail_records(tenant_id=current.tenant_id)
        self.assertEqual([record.run_id for record in records], [current.run_id])
        exporter = RecordingExporter()
        self.assertEqual(restarted.drain_guardrail_events(records[0], Telemetry(exporter).publish_diagnostic), 1)
        self.assertEqual(restarted.pending_guardrail_records(tenant_id=current.tenant_id), ())
        self.assertEqual(len(exporter.spans), 1)

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
            events = conn.execute("SELECT payload FROM core_outbox WHERE aggregate_id=%s AND aggregate_type='run'",
                                  (self.record.run_id,)).fetchall()
            private = conn.execute("""SELECT payload FROM core_outbox WHERE aggregate_id=%s
                AND aggregate_type='diagnostic' ORDER BY sequence""", (self.record.run_id,)).fetchall()
        self.assertNotIn("secret raw material", str(events))
        self.assertNotIn(row["review_id"], str(events))
        self.assertEqual(len(private), 2)
        self.assertEqual(private[0]["payload"]["data"]["review_id"], row["review_id"])
        self.assertNotIn("secret raw material", str(private))
        self.assertNotIn("content_digest", str(private))

    def test_generic_outbox_dispatcher_cannot_deliver_private_diagnostics(self):
        self.classify(self.create(), "suspicious")
        published = []
        OutboxDispatcher(self.database, published.append).drain_once(limit=100)
        self.assertFalse(any(event["aggregate_type"] == "diagnostic" for event in published))


if __name__ == "__main__":
    unittest.main()
