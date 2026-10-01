"""ENT-AC-64: live and recovered final files share scoped canonical projection."""
import asyncio
import base64
import hashlib
import json
import unittest
import uuid
from dataclasses import replace
from unittest.mock import patch

import httpx
import psycopg
from a2a.types import Task, TaskState
from google.protobuf.json_format import MessageToDict
from psycopg.types.json import Jsonb

from core_agent.a2a import Artifact, Part
from core_agent.app import create_app as production_create_app
from core_agent.database import PostgresDatabase
from core_agent.errors import CoreError
from core_agent.interactions import SETTINGS_KEYS
from core_agent.model import ModelResponse, ToolRequest
from core_agent.workspace import WorkspaceBinding
from tests import test_admission as admission_tests
from tests.app_support import UnsandboxedTestLauncher
from tests.test_auth import AuthAppTestCase, TEST_DATABASE_URL


class TypedArtifactTests(unittest.TestCase):
    def test_binary_size_and_digest_use_actual_bytes(self):
        artifact = Artifact("result", (Part.text("é"), Part.file(b"\0\xff", filename="image.bin")))
        self.assertEqual(artifact.size, 4)
        self.assertEqual(artifact.digest, "sha256:" + hashlib.sha256("é".encode() + b"\0\0\xff").hexdigest())


class ResponseFileProjectionTests(AuthAppTestCase):
    durable_blobs = True

    async def files_result(self, *, text="Files ready"):
        seed = await self.submit("external-a", uuid.uuid4().hex, "projection")
        agent = self.app.state.core_agent
        seed_record = agent.workflow_store.lookup_task(seed["id"])
        binding = WorkspaceBinding(seed_record.tenant_id, seed_record.owner_id, seed_record.context_id)
        folder = agent.tool_runtime.environment_manager.backend.chats.workspace(binding)
        (folder / "binary.bin").write_bytes(b"\0\xff\x80")
        (folder / "empty.txt").write_bytes(b"")
        self.model._responses = [ModelResponse(tool_requests=(ToolRequest("choose", "core_response_files",
            {"paths": ["binary.bin", "empty.txt"]}),)), ModelResponse(message=text)]
        task = await self.submit("external-a", uuid.uuid4().hex, "projection")
        async with asyncio.timeout(3):
            while True:
                record = agent.workflow_store.lookup_task(task["id"])
                if record.state in {"COMPLETED", "FAILED", "ABORTED"}:
                    break
                await asyncio.sleep(.01)
        self.assertEqual(record.state, "COMPLETED", record.error_code)
        self.assertEqual(len(record.result["outgoing_files"]), 2)
        return task, record, folder

    def assert_files(self, task, record):
        artifacts = task.get("artifacts", [])
        self.assertEqual(len(artifacts), 1)
        artifact = artifacts[0]
        parts = artifact["parts"]
        files = [part for part in parts if "raw" in part]
        self.assertEqual([part.get("filename") for part in files], ["binary.bin", "empty.txt"])
        self.assertEqual([base64.b64decode(part["raw"]) for part in files], [b"\0\xff\x80", b""])
        self.assertEqual([part["mediaType"] for part in files], ["application/octet-stream", "text/plain"])
        public_keys = {"file_id", "name", "media_type", "size_bytes", "sha256"}
        receipts = artifact["metadata"]["provenance"]["outgoingFiles"]
        self.assertEqual(receipts, [{key: ref[key] for key in public_keys} for ref in record.result["outgoing_files"]])
        self.assertTrue(all(set(receipt) == public_keys for receipt in receipts))
        self.assertFalse(any("raw" in part for part in task.get("status", {}).get("message", {}).get("parts", [])))
        self.assertNotIn("blob_id", json.dumps(artifact))
        self.assertNotIn("limit_bytes", json.dumps(artifact))
        return artifact

    def cached_projection(self, task_id, record, replacement=None):
        store = self.app.state.a2a_request_handler.task_store
        store = getattr(store, "inner", store)
        if self.use_postgres:
            with store.database.transaction() as connection:
                if replacement is not None:
                    connection.execute("UPDATE core_a2a_tasks SET state=%s,payload=%s WHERE task_id=%s AND tenant=%s",
                        (int(replacement.status.state), replacement.SerializeToString(), task_id, record.tenant_id))
                row = connection.execute("SELECT payload FROM core_a2a_tasks WHERE task_id=%s AND tenant=%s",
                    (task_id, record.tenant_id)).fetchone()
                return Task.FromString(bytes(row["payload"]))
        cached = None
        for rows in store._impl.tasks.values():
            if task_id in rows:
                if replacement is not None:
                    rows[task_id].CopyFrom(replacement)
                cached = rows[task_id]
        self.assertIsNotNone(cached)
        copied = Task()
        copied.CopyFrom(cached)
        return copied

    async def test_cached_terminal_saves_validate_whole_manifest_without_replacing_terminal_task(self):
        task, record, _folder = await self.files_result()
        store = self.app.state.a2a_request_handler.task_store
        context = admission_tests.AuthAdmissionTests.context(self, "external-a")
        cached = self.cached_projection(task["id"], record)
        incoming = Task()
        incoming.CopyFrom(cached)
        incoming.status.state = TaskState.TASK_STATE_WORKING
        del incoming.artifacts[:]
        service = self.app.state.core_agent.response_files_service
        original = service.artifact_store.get

        def corrupt_last(tenant, blob_id, **kwargs):
            metadata, content = original(tenant, blob_id, **kwargs)
            return metadata, b"corrupt" if blob_id == record.result["outgoing_files"][-1]["blob_id"] else content

        with patch.object(service.artifact_store, "get", side_effect=corrupt_last):
            with self.assertRaises(CoreError) as error:
                await store.save(incoming, context)
            self.assertEqual(error.exception.code, "ARTIFACT_INTEGRITY_FAILED")
        self.assertEqual(self.cached_projection(task["id"], record), cached)
        underlying = getattr(store, "inner", store)
        with patch.object(underlying, "response_files_service", None):
            with self.assertRaises(CoreError) as error:
                await store.save(incoming, context)
            self.assertEqual(error.exception.code, "ARTIFACT_INTEGRITY_FAILED")
        await store.save(incoming, context)
        self.assertEqual(self.cached_projection(task["id"], record), cached)

    async def test_equal_version_saves_validate_and_replace_supplied_parts_from_canonical_manifest(self):
        task, record, _folder = await self.files_result()
        store = self.app.state.a2a_request_handler.task_store
        context = admission_tests.AuthAdmissionTests.context(self, "external-a")
        canonical = self.cached_projection(task["id"], record)
        self.addCleanup(self.cached_projection, task["id"], record, canonical)
        incoming = Task()
        incoming.CopyFrom(canonical)
        incoming.status.state = TaskState.TASK_STATE_WORKING
        incoming.metadata["core_agent_workflow_version"] = record.version
        del incoming.artifacts[:]
        incoming.artifacts.add(artifact_id="supplied").parts.add(raw=b"untrusted", filename="forged.bin")
        working = self.cached_projection(task["id"], record, incoming)
        service = self.app.state.core_agent.response_files_service
        original = service.artifact_store.get
        if self.use_postgres:
            underlying = getattr(store, "inner", store)
            underlying.database.pool.resize(1, 1)
            underlying.database.pool.timeout = 2

        def corrupt_last(tenant, blob_id, **kwargs):
            if self.use_postgres:
                self.assertIsNotNone(kwargs.get("connection"), "save must borrow its transaction")
            metadata, content = original(tenant, blob_id, **kwargs)
            return metadata, b"corrupt" if blob_id == record.result["outgoing_files"][-1]["blob_id"] else content

        with patch.object(service.artifact_store, "get", side_effect=corrupt_last):
            with self.assertRaises(CoreError) as error:
                await store.save(incoming, context)
            self.assertEqual(error.exception.code, "ARTIFACT_INTEGRITY_FAILED")
        self.assertEqual(self.cached_projection(task["id"], record), working)
        await store.save(incoming, context)
        projected = self.cached_projection(task["id"], record)
        self.assertEqual(projected.status.state, TaskState.TASK_STATE_COMPLETED)
        self.assert_files(MessageToDict(projected), record)

    async def test_live_gettask_and_passive_subscription_preserve_order_ids_and_bytes(self):
        task, record, folder = await self.files_result()
        live = self.assert_files(task, record)
        (folder / "binary.bin").unlink()
        (folder / "empty.txt").write_bytes(b"changed")
        for kind, token in (("external", "external-a"), ("owner", "owner-b")):
            response = await self.http.get(f"/a2a/{kind}/tasks/{task['id']}", headers=self.headers(token))
            self.assertEqual(response.status_code, 200, response.text)
            recovered = self.assert_files(response.json(), record)
            self.assertEqual(recovered["artifactId"], live["artifactId"])
            self.assertEqual(recovered["metadata"]["digest"], live["metadata"]["digest"])
        denied = await self.http.get(f"/a2a/external/tasks/{task['id']}", headers=self.headers("external-b"))
        self.assertEqual(denied.status_code, 404)
        stream = await self.http.post(f"/a2a/external/tasks/{task['id']}:subscribe", headers=self.headers("external-a"))
        self.assertEqual(stream.status_code, 200, stream.text)
        events = [json.loads(line[5:].strip()) for line in stream.text.splitlines() if line.startswith("data:")]
        self.assertEqual(len(events), 1)
        self.assert_files(events[0]["task"], record)

    async def test_pinned_selection_limit_survives_later_company_shrink(self):
        task, record, _folder = await self.files_result()
        agent = self.app.state.core_agent
        settings = agent.interaction_store.get_settings(record.tenant_id)
        values = {key: getattr(settings, key) for key in SETTINGS_KEYS}
        values["attachment_limit_bytes"] = 1
        agent.interaction_store.update_settings(record.tenant_id, values, settings.revision)
        response = await self.http.get(f"/a2a/external/tasks/{task['id']}", headers=self.headers("external-a"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_files(response.json(), record)

    async def test_cached_terminal_reads_revalidate_blobs_and_fail_closed_without_service(self):
        task, record, _folder = await self.files_result()
        store = self.app.state.a2a_request_handler.task_store
        context = admission_tests.AuthAdmissionTests.context(self, "external-a")
        service = self.app.state.core_agent.response_files_service
        with patch.object(service.artifact_store, "get", side_effect=CoreError("ARTIFACT_INTEGRITY_FAILED")):
            with self.assertRaises(CoreError) as error:
                await store.get(task["id"], context)
            self.assertEqual(error.exception.code, "ARTIFACT_INTEGRITY_FAILED")
        underlying = getattr(store, "inner", store)
        with patch.object(underlying, "response_files_service", None):
            with self.assertRaises(CoreError) as error:
                await store.get(task["id"], context)
            self.assertEqual(error.exception.code, "ARTIFACT_INTEGRITY_FAILED")

    async def test_invalid_present_manifest_cannot_bypass_cached_terminal_validation(self):
        task, record, _folder = await self.files_result()
        agent = self.app.state.core_agent
        store = self.app.state.a2a_request_handler.task_store
        context = admission_tests.AuthAdmissionTests.context(self, "external-a")
        for invalid in (None, False, "", {}):
            with self.subTest(manifest=invalid):
                result = {**record.result, "outgoing_files": invalid}
                if self.use_postgres:
                    with agent.workflow_store.database.transaction() as connection:
                        connection.execute("UPDATE core_runs SET result=%s WHERE run_id=%s AND tenant_id=%s",
                            (Jsonb(result), record.run_id, record.tenant_id))
                else:
                    agent.workflow_store._records[record.run_id] = replace(record, result=result)
                with self.assertRaises(CoreError) as error:
                    await store.get(task["id"], context)
                self.assertEqual(error.exception.code, "ARTIFACT_INTEGRITY_FAILED")

    async def test_canonical_helper_checks_whole_batch_scope_and_preserves_partial_provenance(self):
        from core_agent.a2a import workflow_result_artifact

        _task, record, _folder = await self.files_result()
        service = self.app.state.core_agent.response_files_service
        result = {**record.result, "complete": False, "completion_reason": "budget_exhausted",
            "exhausted_dimension": "tool_calls", "shared_budget": {"tool_calls": 2}, "pending_tasks": ["child"]}
        partial = replace(record, result=result)
        projected = workflow_result_artifact(partial, service)
        self.assertFalse(projected.provenance["complete"])
        self.assertEqual(projected.provenance["pending_tasks"], ["child"])
        self.assertEqual(projected.provenance["shared_budget"], {"tool_calls": 2})
        self.assertEqual([part.kind for part in projected.parts], ["text", "file", "file"])
        altered = [dict(ref) for ref in result["outgoing_files"]]
        altered[1]["context_id"] = "another-chat"
        with self.assertRaises(CoreError) as error:
            workflow_result_artifact(replace(partial, result={**result, "outgoing_files": altered}), service)
        self.assertEqual(error.exception.code, "FILE_NOT_FOUND")
        original = service.artifact_store.get
        def corrupt_second(tenant, blob, **kwargs):
            metadata, content = original(tenant, blob, **kwargs)
            return metadata, b"changed" if blob == result["outgoing_files"][1]["blob_id"] else content
        with patch.object(service.artifact_store, "get", side_effect=corrupt_second):
            with self.assertRaises(CoreError) as error:
                workflow_result_artifact(partial, service)
            self.assertEqual(error.exception.code, "ARTIFACT_INTEGRITY_FAILED")

    async def test_files_only_final_has_artifact_without_terminal_file_message(self):
        task, record, _folder = await self.files_result(text="")
        self.assert_files(task, record)

    async def test_live_stream_sends_each_raw_file_once_in_artifact_frames(self):
        _task, _record, _folder = await self.files_result()
        self.model._responses = [ModelResponse(tool_requests=(ToolRequest("stream-files", "core_response_files",
            {"paths": ["binary.bin", "empty.txt"]}),)), ModelResponse(message="Stream files ready")]
        response = await self.http.post("/a2a/external/message:stream", headers=self.headers("external-a"), json={
            "message": {"messageId": uuid.uuid4().hex, "contextId": "projection", "role": "ROLE_USER",
                        "parts": [{"text": "Return the files"}]}})
        self.assertEqual(response.status_code, 200, response.text)
        events = [json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith("data:")]
        frames = [event["artifactUpdate"] for event in events if "artifactUpdate" in event]
        raw_parts = [part for frame in frames for part in frame["artifact"]["parts"] if "raw" in part]
        self.assertEqual([base64.b64decode(part["raw"]) for part in raw_parts], [b"\0\xff\x80", b""])
        self.assertEqual(len({frame["artifact"]["artifactId"] for frame in frames}), 1)
        self.assertTrue(frames[-1]["lastChunk"])
        for event in events:
            if "statusUpdate" in event:
                self.assertFalse(any("raw" in part for part in event["statusUpdate"].get("status", {}).get("message", {}).get("parts", [])))

    async def test_reconciliation_rebuilds_interrupted_publication_from_frozen_manifest(self):
        task, record, _folder = await self.files_result()
        store = getattr(self.app.state.a2a_request_handler.task_store, "inner", self.app.state.a2a_request_handler.task_store)
        if self.use_postgres:
            context = admission_tests.AuthAdmissionTests.context(self, "external-a")
            sdk_task = await store.get(task["id"], context)
            sdk_task.status.state = TaskState.TASK_STATE_WORKING
            del sdk_task.artifacts[:]
            with store.database.transaction() as connection:
                connection.execute("UPDATE core_a2a_tasks SET state=%s,payload=%s WHERE task_id=%s AND tenant=%s",
                    (int(sdk_task.status.state), sdk_task.SerializeToString(), task["id"], record.tenant_id))
            store.database.pool.resize(1, 1)
            store.database.pool.timeout = 2
            self.assertEqual(await asyncio.to_thread(store.reconcile_from_workflows), 1)
        else:
            for rows in store._impl.tasks.values():
                if task["id"] in rows:
                    rows[task["id"]].status.state = TaskState.TASK_STATE_WORKING
                    del rows[task["id"]].artifacts[:]
        response = await self.http.get(f"/a2a/external/tasks/{task['id']}", headers=self.headers("external-a"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_files(response.json(), record)


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL not set")
class PostgresResponseFileProjectionTests(ResponseFileProjectionTests):
    use_postgres = True

    async def test_actual_restart_reconciles_interrupted_files_before_gettask_without_model_calls(self):
        task, record, folder = await self.files_result()
        canonical = self.cached_projection(task["id"], record)
        interrupted = Task()
        interrupted.CopyFrom(canonical)
        interrupted.status.state = TaskState.TASK_STATE_WORKING
        del interrupted.artifacts[:]
        self.cached_projection(task["id"], record, interrupted)
        (folder / "binary.bin").unlink()
        (folder / "empty.txt").write_bytes(b"changed source")
        classifier = self.app.state.core_agent.guardrail_classifier
        model_calls = len(self.model.calls)
        await self.http.aclose()
        self.app.state.close()
        database = PostgresDatabase(TEST_DATABASE_URL, min_size=0, max_size=1)
        try:
            restarted = production_create_app(
                model=self.model, base_url="https://agent.example.test", database=database,
                auth_transport=httpx.MockTransport(self.introspect),
                sandbox_launcher=UnsandboxedTestLauncher(), guardrail_classifier=classifier,
            )
        except BaseException:
            # A failed startup must not poison the next tenant fixture's recovery.
            with psycopg.connect(TEST_DATABASE_URL) as connection:
                connection.execute("UPDATE core_a2a_tasks SET state=%s,payload=%s WHERE task_id=%s AND tenant=%s",
                    (int(canonical.status.state), canonical.SerializeToString(), task["id"], record.tenant_id))
            raise
        self.app = restarted
        self.addCleanup(restarted.state.close)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url="https://agent.example.test")
        self.addAsyncCleanup(self.http.aclose)
        persisted = self.cached_projection(task["id"], record)
        self.assertEqual(persisted.status.state, TaskState.TASK_STATE_COMPLETED)
        self.assert_files(MessageToDict(persisted), record)
        response = await self.http.get(f"/a2a/external/tasks/{task['id']}", headers=self.headers("external-a"))
        self.assertEqual(response.status_code, 200, response.text)
        rebuilt = self.assert_files(response.json(), record)
        self.assertEqual(rebuilt["artifactId"], task["artifacts"][0]["artifactId"])
        self.assertEqual(len(self.model.calls), model_calls)
