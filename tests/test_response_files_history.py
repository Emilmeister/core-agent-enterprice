"""Final owner-history receipts validate trusted manifests without retrieving blobs."""
import copy
import base64
import json
import unittest
import uuid
from dataclasses import replace
from urllib.parse import quote
from unittest.mock import patch

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from core_agent.history import read_history
from core_agent.interactions import SETTINGS_KEYS
from core_agent.workspace import WorkspaceBinding
from tests import test_owner_history as history_tests
from tests.test_auth import AuthAppTestCase, TEST_DATABASE_URL


class ResponseFilesHistoryTests(AuthAppTestCase):
    durable_blobs = True
    digest = staticmethod(history_tests.OwnerHistoryTests.digest)
    review_material = history_tests.OwnerHistoryTests.review_material

    async def asyncSetUp(self):
        # These history fixtures own their synthetic retained material reviews.
        with patch("core_agent.runtime.CoreAgent.recover_workflows"):
            await super().asyncSetUp()

    async def history(self, chat="outgoing-history", *, token="owner-b", **params):
        return await self.http.get("/api/chats/" + quote(chat, safe="") + "/history",
                                   headers=self.headers(token), params=params)

    def update_result(self, record, result, *, state="COMPLETED", parent_run_id=None):
        store = self.app.state.core_agent.workflow_store
        if self.use_postgres:
            with store.database.transaction() as connection:
                connection.execute("UPDATE core_runs SET state=%s,result=%s,parent_run_id=%s WHERE run_id=%s",
                                   (state, Jsonb(result), parent_run_id, record.run_id))
        else:
            store._records[record.run_id] = replace(record, state=state, result=copy.deepcopy(result),
                                                   parent_run_id=parent_run_id)

    async def prepared_result(self):
        task = await self.submit("external-a", uuid.uuid4().hex, "outgoing-history")
        agent = self.app.state.core_agent
        record = agent.workflow_store.lookup_task(task["id"])
        binding = WorkspaceBinding(record.tenant_id, record.owner_id, record.context_id)
        manager = agent.tool_runtime.environment_manager.backend.chats
        folder = manager.workspace(binding)
        (folder / "report.pdf").write_bytes(b"PDF")
        (folder / "empty.txt").write_bytes(b"")
        refs = agent.response_files_service.prepare(binding, ["report.pdf", "empty.txt"],
            task_id=record.task_id, run_id=record.run_id, limit_bytes=3)
        result = {**record.result, "outgoing_files": refs, "provider_replay": "PRIVATE_FULL_RESULT"}
        self.update_result(record, result)
        return agent, record, binding, refs, result

    def public(self, refs):
        return [{key: ref[key] for key in ("file_id", "name", "media_type", "size_bytes", "sha256")} for ref in refs]

    async def test_only_available_final_root_entry_has_safe_ordered_receipts(self):
        agent, record, binding, refs, _result = await self.prepared_result()
        folder = agent.tool_runtime.environment_manager.backend.chats.workspace(binding)
        (folder / "report.pdf").unlink()
        agent.artifact_store.delete(record.tenant_id, refs[0]["blob_id"])
        before = agent.workflow_store.lookup_task(record.task_id)
        with patch.object(agent.artifact_store, "get", side_effect=AssertionError("history reads no blobs")), \
                patch.object(agent.response_files_service, "load", side_effect=AssertionError("history reads no bytes")), \
                patch.object(agent.interaction_store, "get_settings", side_effect=AssertionError("history rereads no settings")), \
                patch.object(agent.model, "generate", side_effect=AssertionError("history executes no model")):
            response = await self.history()
            repeated = await self.history(token="owner-a")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(repeated.json(), response.json())
        items = response.json()["items"]
        final = next(item for item in items if item["id"] == record.task_id + "/result")
        self.assertEqual(final["response_files"], self.public(refs))
        self.assertEqual(final["kind"], "result")
        self.assertTrue(all("response_files" not in item for item in items if item is not final))
        for key in ("schema_version", "limit_bytes", "tenant_id", "owner_id", "context_id", "run_id", "blob_id", "raw", "provider_replay"):
            self.assertNotIn(key, json.dumps(final["response_files"]))
        self.assertNotIn("PRIVATE_FULL_RESULT", response.text)
        self.assertEqual(agent.workflow_store.lookup_task(record.task_id), before)

    async def test_historical_pin_survives_current_limit_reduction_and_partial_completion(self):
        agent, record, _binding, refs, result = await self.prepared_result()
        result.update(complete=False, completion_reason="budget_exhausted")
        self.update_result(record, result)
        settings = agent.interaction_store.get_settings(record.tenant_id)
        values = {key: getattr(settings, key) for key in SETTINGS_KEYS}
        values["attachment_limit_bytes"] = 1
        agent.interaction_store.update_settings(record.tenant_id, values, settings.revision)
        with patch.object(agent.interaction_store, "get_settings", side_effect=AssertionError("use stored pin")):
            response = await self.history(limit="1")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["items"]), 1)
        final = response.json()["items"][0]
        self.assertEqual(final["response_files"], self.public(refs))
        self.assertEqual(final["outcome"], {"state": "COMPLETED", "complete": False, "completion_reason": "budget_exhausted"})

    async def test_failed_cancelled_and_guardrail_placeholder_never_publish_receipts(self):
        agent, record, _binding, _refs, result = await self.prepared_result()
        for state in ("FAILED", "CANCELLED", "ABORTED", "REJECTED"):
            self.update_result(record, result, state=state)
            response = await self.history()
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(all("response_files" not in item for item in response.json()["items"]))
        self.update_result(record, result)
        self.review_material(record, {"material_kind": "json", "material_digest": self.digest(result["message"])})
        with patch.object(agent.response_files_service, "validate_refs", side_effect=AssertionError("hidden final has no file projection")):
            response = await self.history()
        self.assertEqual(response.status_code, 200, response.text)
        final = next(item for item in response.json()["items"] if item["id"].endswith("/result"))
        self.assertEqual((final["kind"], final["status"]), ("placeholder", "rejected"))
        self.assertNotIn("response_files", final)

    async def test_legacy_absent_and_explicit_empty_selection_have_no_file_field(self):
        _agent, record, _binding, _refs, result = await self.prepared_result()
        for missing in (True, False):
            old = {**result}
            if missing:
                old.pop("outgoing_files")
            else:
                old["outgoing_files"] = []
            self.update_result(record, old)
            response = await self.history()
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(all("response_files" not in item for item in response.json()["items"]))

    async def test_present_invalid_manifest_and_null_unknown_fields_are_not_silently_removed(self):
        _agent, record, _binding, refs, result = await self.prepared_result()
        for invalid in (None, False, "", {}, [None], [{**refs[0], "unknown_field": None}]):
            with self.subTest(manifest=invalid):
                self.update_result(record, {**result, "outgoing_files": invalid})
                response = await self.history()
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["error"]["code"], "ARTIFACT_INTEGRITY_FAILED")
                self.assertNotIn(refs[0]["file_id"], response.text)

    async def test_nested_colon_basename_survives_history_gettask_and_authorized_download(self):
        agent, record, binding, _refs, result = await self.prepared_result()
        manager = agent.tool_runtime.environment_manager.backend.chats
        folder = manager.workspace(binding) / "reports"
        folder.mkdir()
        (folder / "result:final.txt").write_bytes(b"frozen")
        refs = agent.response_files_service.prepare(binding, ["reports/result:final.txt"],
            task_id=record.task_id, run_id=record.run_id, limit_bytes=6)
        self.update_result(record, {**result, "outgoing_files": refs})
        response = await self.history()
        self.assertEqual(response.status_code, 200, response.text)
        final = next(item for item in response.json()["items"] if item["id"].endswith("/result"))
        self.assertEqual(final["response_files"], self.public(refs))
        task = await self.http.get(f"/a2a/owner/tasks/{record.task_id}", headers=self.headers("owner-b"))
        self.assertEqual(task.status_code, 200, task.text)
        files = [part for artifact in task.json()["artifacts"] for part in artifact["parts"] if "raw" in part]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["filename"], "result:final.txt")
        self.assertEqual(base64.b64decode(files[0]["raw"]), b"frozen")
        download = await self.http.get(f"/api/chats/{record.context_id}/tasks/{record.task_id}/files/{refs[0]['file_id']}",
                                       headers=self.headers("owner-b"))
        self.assertEqual(download.status_code, 200, download.text)
        self.assertEqual(download.content, b"frozen")

    async def test_whole_manifest_scope_and_integrity_validate_before_any_receipt(self):
        agent, record, _binding, refs, result = await self.prepared_result()
        for key in ("tenant_id", "owner_id", "context_id", "task_id", "run_id"):
            forged = [refs[0], {**refs[1], key: "foreign-child"}]
            self.update_result(record, {**result, "outgoing_files": forged})
            response = await self.history()
            self.assertEqual(response.status_code, 404, response.text)
            self.assertEqual(response.json()["error"]["code"], "FILE_NOT_FOUND")
            self.assertNotIn(refs[0]["file_id"], response.text)
        for forged, code, status in (([refs[0], {**refs[1], "schema_version": 2}], "ARTIFACT_INTEGRITY_FAILED", 409),
                                     ([refs[0], {**refs[1], "limit_bytes": 4}], "ARTIFACT_INTEGRITY_FAILED", 409),
                                     ([refs[0], {**refs[1], "sha256": "bad"}], "ARTIFACT_INTEGRITY_FAILED", 409),
                                     ([refs[0], {**refs[1], "size_bytes": 4}], "ATTACHMENTS_TOO_LARGE", 400)):
            self.update_result(record, {**result, "outgoing_files": forged})
            response = await self.history()
            self.assertEqual(response.status_code, status, response.text)
            self.assertEqual(response.json()["error"]["code"], code)
        self.update_result(record, result, parent_run_id=str(uuid.uuid4()))
        with patch.object(agent.response_files_service, "validate_refs", side_effect=AssertionError("child root rejected first")):
            response = await self.history()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "CHECKPOINT_INVALID")

    async def test_fresh_owner_authorization_precedes_manifest_validation(self):
        agent, _record, _binding, _refs, _result = await self.prepared_result()
        self.tokens["dual"] = {**self.tokens["owner-a"], "realm_access": {"roles": ["agent-owner", "agent-external"]}}
        with patch.object(agent.response_files_service, "validate_refs", side_effect=AssertionError("authorize first")):
            for token in ("external-a", "external-b", "dual"):
                self.assertEqual((await self.history(token=token)).status_code, 403)
            auth = self.app.state.authenticator
            with patch.object(auth, "settings", replace(auth.settings, tenant="foreign-company")):
                self.assertEqual((await self.history()).status_code, 404)


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL required")
class PostgresResponseFilesHistoryTests(ResponseFilesHistoryTests):
    use_postgres = True

    async def test_minimal_pg_result_fields_and_borrowed_history_connection(self):
        agent, record, _binding, refs, result = await self.prepared_result()
        result["PRIVATE_UNSELECTED_RESULT"] = "x" * 2_000_000
        self.update_result(record, result)
        admission = agent.tool_runtime.environment_manager.validate_workspace_scope.__self__
        database = agent.workflow_store.database
        observed = []

        def collect(cursor):
            build = dict_row(cursor)

            def row(values):
                value = build(values)
                observed.append(value)
                return value

            return row

        with database.transaction() as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            original = connection.row_factory
            connection.row_factory = collect
            try:
                with patch.object(database.pool, "connection", side_effect=AssertionError("nested pool checkout")), \
                        patch.object(agent.artifact_store, "get", side_effect=AssertionError("history retrieves no blobs")):
                    rows = read_history(admission, record.tenant_id, record.context_id, limit=1, connection=connection)
            finally:
                connection.row_factory = original
        self.assertEqual(rows[0][0]["response_files"], self.public(refs))
        selected = [row["result"] for row in observed if "result" in row]
        self.assertEqual(len(selected), 1)
        self.assertEqual(set(selected[0]), {"message", "complete", "completion_reason", "outgoing_files"})
        encoded = json.dumps(observed, default=str)
        self.assertNotIn("PRIVATE_UNSELECTED_RESULT", encoded)
        self.assertLess(len(encoded), 20_000)
