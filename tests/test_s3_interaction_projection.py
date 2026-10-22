import os
import unittest
import uuid
from dataclasses import replace
from unittest.mock import patch

from psycopg.types.json import Jsonb

from core_agent.a2a import workflow_public_output
from core_agent.guardrails import GuardrailClassifier
from core_agent.interactions import PostgresInteractionStore
from core_agent.material_reviews import PostgresMaterialReviewStore
from core_agent.postgres_tasks import PostgresTaskScheduler
from core_agent.response_artifacts import ResponseArtifactService
from core_agent.response_files import ResponseFileService
from core_agent.workflow import PostgresWorkflowStore, WorkflowRecord
from core_agent.workspace import ChatWorkspaces, WorkspaceBinding
from core_agent.tasks import _initial_remote_checkpoint
from tests import test_s3_storage
from tests.test_material_reviews import Model


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL") and os.getenv("TEST_S3_ENDPOINT"),
                     "set TEST_DATABASE_URL and TEST_S3_ENDPOINT for cold interaction projections")
class ColdInteractionProjectionTests(unittest.TestCase):
    def setUp(self):
        test_s3_storage.S3PostgresArtifactTests.setUp(self)
        self.files = ResponseFileService(ChatWorkspaces(self.directory.name), self.store)
        self.database.response_files_service = self.files
        self.workflow = PostgresWorkflowStore(self.database)
        self.workflow.public_output_observer = lambda record, connection=None: workflow_public_output(
            record, self.files, connection=connection)
        self.reviews = PostgresMaterialReviewStore(self.database, self.workflow)
        self.record = WorkflowRecord(uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex,
            self.tenant, "owner", None, "RUNNING", 1, {"prompt": "private"}, {})
        binding = WorkspaceBinding(self.tenant, "owner", self.record.context_id)
        manifests = ResponseArtifactService(self.files).prepare(binding,
            [{"parts": [{"type": "text", "text": "published"}]}], task_id=self.record.task_id,
            run_id=self.record.run_id, publication_id="published", limit_bytes=1024)
        self.record = self.workflow.create(replace(self.record,
            snapshot={"output_contract_version": 2, "published_artifacts": list(manifests)}))
        self.token = self.workflow.acquire_lease(self.record.run_id, tenant_id=self.tenant,
            owner_id="owner", worker_id="proof", ttl=100)
        self.downloads = []
        original_get = self.blobs.get

        def download(digest, size):
            # max_size=1 makes retaining the projection transaction observable.
            with self.database.pool.connection(timeout=1) as connection:
                connection.execute("SELECT 1")
            self.downloads.append(digest)
            return original_get(digest, size)

        self.patch_get = patch.object(self.blobs, "get", side_effect=download)
        self.patch_get.start()
        self.addCleanup(self.patch_get.stop)

    def cold(self):
        with self.store._cache_lock:
            self.store._cache.clear()
            self.store._cache_bytes = 0
        self.downloads.clear()

    def review(self):
        return self.reviews.create(self.record, source_id="material", source_kind="input",
            payload="private material", deadline=self.workflow.current_time() + 60, lease_token=self.token)

    def test_policy_denial_retries_whole_transaction_and_publishes_once(self):
        wait = self.workflow.enter_wait(self.record, kind="tool_approval", source_id="tool",
            subject={"tool_name": "tool", "origin": "builtin:tool"}, continuation={"version": 1, "phase": "tool_gate"},
            deadline=self.workflow.current_time() + 60, snapshot=self.record.snapshot, lease_token=self.token)
        interactions = PostgresInteractionStore(self.database, self.workflow)
        self.cold()
        policy = interactions.update_policy(self.tenant, "tool", "builtin:tool", mode="deny",
            guardrails_exempt=False, expected_revision=0, actor_id="owner")
        self.assertEqual(policy.revision, 1)
        self.assertEqual(self.workflow.get_wait(wait.wait_id, tenant_id=self.tenant).outcome["reason"], "policy_denied")
        self.assertEqual(len(self.downloads), 1)

    def test_pending_callback_and_detector_run_once_after_cache_loss(self):
        review = self.review()
        model = Model("suspicious")
        callback_calls = []

        def before_pending():
            callback_calls.append(1)
            self.cold()  # Simulates unrelated eviction after the interpreter was stopped.
            return self.record.snapshot, {"version": 1, "phase": "input"}

        result = self.reviews.classify(self.record, review["review_id"],
            GuardrailClassifier(model, clock=self.workflow.current_time), lease_token=self.token,
            continuation={"version": 1, "phase": "input"}, snapshot=self.record.snapshot,
            before_pending=before_pending)
        self.assertEqual(result["state"], "pending")
        self.assertEqual(len(callback_calls), 1)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(len(self.downloads), 1)
        with self.database.pool.connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM core_waits WHERE run_id=%s",
                (self.record.run_id,)).fetchone()["n"], 1)

    def test_existing_expired_review_is_resolved_before_repeating_detector(self):
        review = self.review()
        model = Model("suspicious")
        classifier = GuardrailClassifier(model, clock=self.workflow.current_time)
        result = self.reviews.classify(self.record, review["review_id"], classifier,
            lease_token=self.token, continuation={"version": 1, "phase": "input"}, snapshot=self.record.snapshot)
        with self.database.transaction() as connection:
            connection.execute("UPDATE core_waits SET deadline=0 WHERE wait_id=%s", (result["wait_id"],))
        self.cold()
        refreshed = self.reviews.get(self.record, review["review_id"])
        self.assertEqual(refreshed["state"], "timed_out")
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(len(self.downloads), 1)

    def remote_contract(self):
        return {"version": 3, "tenant_id": self.tenant, "owner_id": self.record.run_id,
            "peer_id": "peer", "peer_revision": 1, "peer_name": "peer",
            "url": "https://peer.example/a2a", "binding": "HTTP+JSON", "message_id": uuid.uuid4().hex,
            "task": "request", "timeout_seconds": 60, "poll_interval_seconds": 5,
            "caller_scope": {"owner_id": self.record.owner_id, "context_id": self.record.context_id,
                             "task_id": self.record.task_id, "run_id": self.record.run_id},
            "attachment_limit_bytes": 1024, "outgoing_files": []}

    def admit(self, connection):
        return self.workflow.transition(self.record.run_id, tenant_id=self.tenant, owner_id="owner",
            expected_version=self.record.version, state="RUNNING", snapshot=self.record.snapshot,
            event_kind="test.admitted", event_data={}, lease_token=self.token, connection=connection)

    def test_scheduler_hydrates_before_launching_background_or_remote_worker_once(self):
        scheduler = PostgresTaskScheduler(self.database)
        self.addCleanup(scheduler.close)
        scheduler.register("remote_a2a", lambda *_: None)
        for remote in (False, True):
            with self.subTest(remote=remote), patch.object(scheduler, "_launch") as launch:
                self.cold()
                if remote:
                    task = scheduler.start_remote(self.remote_contract(), owner_id=self.record.run_id,
                        tenant_id=self.tenant, task_id=uuid.uuid4().hex, admission=self.admit)
                else:
                    task = scheduler.start(lambda: None, owner_id=self.record.run_id, tenant_id=self.tenant,
                        kind="background_tool", contract={}, admission=self.admit)
                self.assertEqual(launch.call_count, 1)
                self.assertEqual(len(self.downloads), 1)
                self.assertEqual(scheduler.get(task.id, owner_id=self.record.run_id,
                    tenant_id=self.tenant).state, "working")
                previous = self.record.version
                self.record = self.workflow.get(self.record.run_id, tenant_id=self.tenant, owner_id="owner")
                self.assertEqual(self.record.version, previous + 1)

    def test_scheduler_reply_hydrates_before_notifying_once(self):
        scheduler = PostgresTaskScheduler(self.database)
        self.addCleanup(scheduler.close)
        scheduler.register("remote_a2a", lambda *_: None)
        contract = self.remote_contract()
        with patch.object(scheduler, "_launch"):
            task = scheduler.start_remote(contract, owner_id=self.record.run_id,
                tenant_id=self.tenant, task_id=uuid.uuid4().hex)
        checkpoint = {**_initial_remote_checkpoint(contract), "send_started": True,
            "deadline": self.workflow.current_time() + 60, "remote_task_id": "remote",
            "remote_context_id": "remote-context", "next_poll_at": self.workflow.current_time() + 5,
            "input_request": {"input_id": "a" * 64, "question": "Question?"}}
        with self.database.transaction() as connection:
            connection.execute("UPDATE core_background_tasks SET checkpoint=%s WHERE id=%s",
                (Jsonb(checkpoint), task.id))
        before = scheduler.get(task.id, owner_id=self.record.run_id, tenant_id=self.tenant).revision
        self.cold()
        with patch.object(scheduler, "_notify_remote") as notify:
            result = scheduler.queue_remote_reply(task.id, {"input_id": "a" * 64,
                "message_id": uuid.uuid4().hex, "task": "reply", "outgoing_files": [],
                "request_provenance": {"version": 1, "sources": {}}},
                owner_id=self.record.run_id, tenant_id=self.tenant, admission=self.admit)
        self.assertEqual(notify.call_count, 1)
        self.assertEqual(result.revision, before + 1)
        self.assertEqual(len(self.downloads), 1)
