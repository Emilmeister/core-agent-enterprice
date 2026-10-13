"""Owner history and downloads use committed typed publications in any Task state."""
import copy
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from psycopg.types.json import Jsonb

from core_agent.response_artifacts import ResponseArtifactService
from core_agent.workspace import WorkspaceBinding
from tests.test_auth import AuthAppTestCase, TEST_DATABASE_URL


class PublicOutputOwnerHistoryTests(AuthAppTestCase):
    durable_blobs = True

    async def asyncSetUp(self):
        with patch("core_agent.runtime.CoreAgent.recover_workflows"):
            await super().asyncSetUp()

    async def fixture(self, *, state="RUNNING"):
        task = await self.submit("owner-a", "initial", "public-output")
        agent = self.app.state.core_agent
        record = agent.workflow_store.lookup_task(task["id"])
        binding = WorkspaceBinding(record.tenant_id, record.owner_id, record.context_id)
        (agent.response_files_service.workspaces.workspace(binding) / "report.bin").write_bytes(b"\0\xff")
        manifests = ResponseArtifactService(agent.response_files_service).prepare(binding, [{"name": "Report", "parts": [
            {"type": "text", "text": "Artifact text"}, {"type": "data", "data": {"count": 2}},
            {"type": "file", "path": "report.bin"}]}],
            task_id=record.task_id, run_id=record.run_id, publication_id="publish", limit_bytes=25_000_000)
        snapshot = copy.deepcopy(record.snapshot)
        snapshot.update(output_contract_version=2, published_artifacts=list(manifests), public_messages=[
            {"message_id": record.task_id + ":message:message", "text": "Durable working message"}])
        snapshot["context"]["transcript"].extend([
            {"kind": "tool_result", "tokens": 1, "content": json.dumps({"tool_call_id": "message", "tool_name": "core_message_send",
                "status": "succeeded", "output": {"message_id": record.task_id + ":message:message"}})},
            {"kind": "tool_result", "tokens": 1, "content": json.dumps({"tool_call_id": "publish", "tool_name": "core_response_artifacts",
                "status": "succeeded", "output": {"artifacts": list(ResponseArtifactService.receipts(manifests))}})},
        ])
        snapshot["context"]["sequence_range"][1] = len(snapshot["context"]["transcript"])
        self.save(record, snapshot, state)
        return record, manifests, snapshot

    def save(self, record, snapshot, state):
        store = self.app.state.core_agent.workflow_store
        if self.use_postgres:
            with store.database.transaction() as connection:
                connection.execute("UPDATE core_runs SET snapshot=%s,state=%s WHERE run_id=%s", (Jsonb(snapshot), state, record.run_id))
        else:
            store._records[record.run_id] = replace(record, snapshot=snapshot, state=state)

    async def test_working_history_and_failed_downloads_use_only_committed_safe_output(self):
        record, manifests, snapshot = await self.fixture()
        history = await self.http.get("/api/chats/public-output/history", headers=self.headers("owner-b"))
        self.assertEqual(history.status_code, 200, history.text)
        message = next(item for item in history.json()["items"] if item.get("public_message"))
        self.assertEqual(message["public_message"], {"message_id": record.task_id + ":message:message", "text": "Durable working message"})
        artifact = next(item for item in history.json()["items"] if item.get("response_artifacts"))["response_artifacts"][0]
        self.assertEqual(artifact, ResponseArtifactService.receipts(manifests)[0])
        self.assertNotIn("blob_id", history.text)
        self.assertNotIn("Artifact text", history.text)
        file_id = manifests[0]["parts"][1]["ref"]["file_id"]
        route = f"/api/chats/public-output/tasks/{record.task_id}/files/{file_id}"
        for state in ("RUNNING", "FAILED", "CANCELLED"):
            self.save(record, snapshot, state)
            download = await self.http.get(route, headers=self.headers("owner-b"))
            self.assertEqual(download.status_code, 200, download.text)
            self.assertEqual(download.content, b'{"count":2.0}')
        foreign = await self.http.get(route.replace("public-output", "foreign"), headers=self.headers("owner-b"))
        self.assertEqual(foreign.status_code, 404)
        external = await self.http.get(route, headers=self.headers("external-a"))
        self.assertEqual(external.status_code, 403)
        snapshot["published_artifacts"][0]["parts"][0]["ref"]["owner_id"] = "foreign"
        self.save(record, snapshot, "RUNNING")
        corrupt = await self.http.get(route, headers=self.headers("owner-b"))
        self.assertEqual(corrupt.status_code, 409, corrupt.text)
        self.assertEqual(corrupt.json()["error"]["code"], "ARTIFACT_INTEGRITY_FAILED")

    async def test_uncommitted_tool_payload_cannot_publish_a_message_or_artifact(self):
        record, _, snapshot = await self.fixture()
        snapshot["public_messages"] = []
        snapshot["published_artifacts"] = []
        self.save(record, snapshot, "RUNNING")
        history = await self.http.get("/api/chats/public-output/history", headers=self.headers("owner-b"))
        self.assertEqual(history.status_code, 200, history.text)
        self.assertTrue(all("public_message" not in row and "response_artifacts" not in row for row in history.json()["items"]))

    async def test_current_rejected_source_hides_cards_and_refuses_explicit_download(self):
        from tests.test_owner_history import OwnerHistoryTests

        record, manifests, snapshot = await self.fixture(state="COMPLETED")
        OwnerHistoryTests.review_material(self, record, {"material_kind": "json",
            "material_digest": OwnerHistoryTests.digest("Answer briefly")})
        history = await self.http.get("/api/chats/public-output/history", headers=self.headers("owner-b"))
        self.assertEqual(history.status_code, 200, history.text)
        rows = history.json()["items"]
        self.assertTrue(all("response_artifacts" not in item and "public_message" not in item for item in rows))
        final = next(item for item in rows if item.get("outcome"))
        self.assertEqual(final["public_message_id"], record.task_id + ":final")
        file_id = manifests[0]["parts"][0]["ref"]["file_id"]
        download = await self.http.get(f"/api/chats/public-output/tasks/{record.task_id}/files/{file_id}",
            headers=self.headers("owner-b"))
        self.assertEqual(download.status_code, 404, download.text)

    async def test_invalid_sibling_json_prevents_a_valid_first_part_download(self):
        record, manifests, snapshot = await self.fixture()
        files = self.app.state.core_agent.response_files_service
        binding = WorkspaceBinding(record.tenant_id, record.owner_id, record.context_id)
        invalid = files.prepare_contents(binding, [("part-2.json", b'{"count":NaN}', "application/json")],
            task_id=record.task_id, run_id=record.run_id, limit_bytes=25_000_000)[0]
        snapshot["published_artifacts"][0]["parts"][1]["ref"] = invalid
        self.save(record, snapshot, "RUNNING")
        file_id = manifests[0]["parts"][0]["ref"]["file_id"]
        response = await self.http.get(f"/api/chats/public-output/tasks/{record.task_id}/files/{file_id}",
            headers=self.headers("owner-b"))
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "ARTIFACT_INTEGRITY_FAILED")
        self.assertNotIn("Artifact text", response.text)

    async def test_later_negative_typed_part_hides_its_whole_publication(self):
        from tests.test_owner_history import OwnerHistoryTests

        record, manifests, _ = await self.fixture(state="COMPLETED")
        sibling = manifests[0]["parts"][1]["ref"]
        OwnerHistoryTests.review_material(self, record, {"material_kind": "file_sha256", "material_digest": sibling["sha256"]},
            source_kind="file_attachment")
        history = await self.http.get("/api/chats/public-output/history", headers=self.headers("owner-b"))
        self.assertEqual(history.status_code, 200, history.text)
        self.assertTrue(all("response_artifacts" not in item for item in history.json()["items"]))
        first_id = manifests[0]["parts"][0]["ref"]["file_id"]
        response = await self.http.get(f"/api/chats/public-output/tasks/{record.task_id}/files/{first_id}",
            headers=self.headers("owner-b"))
        self.assertEqual(response.status_code, 404, response.text)

    async def test_later_exact_text_and_data_negative_hide_published_receipts(self):
        from tests.test_owner_history import OwnerHistoryTests

        record, _, _ = await self.fixture()
        OwnerHistoryTests.review_material(self, record, {"material_kind": "json",
            "material_digest": OwnerHistoryTests.digest("Artifact text")})
        history = await self.http.get("/api/chats/public-output/history", headers=self.headers("owner-b"))
        self.assertEqual(history.status_code, 200, history.text)
        self.assertTrue(all("response_artifacts" not in item for item in history.json()["items"]))

    async def test_later_exact_structured_json_negative_hides_published_receipts(self):
        from tests.test_owner_history import OwnerHistoryTests

        record, _, _ = await self.fixture()
        OwnerHistoryTests.review_material(self, record, {"material_kind": "json",
            "material_digest": OwnerHistoryTests.digest({"count": 2.0})})
        history = await self.http.get("/api/chats/public-output/history", headers=self.headers("owner-b"))
        self.assertEqual(history.status_code, 200, history.text)
        self.assertTrue(all("response_artifacts" not in item for item in history.json()["items"]))

    async def test_later_exact_binary_file_negative_blocks_other_typed_parts(self):
        from tests.test_owner_history import OwnerHistoryTests

        record, manifests, _ = await self.fixture()
        OwnerHistoryTests.review_material(self, record, {"material_kind": "file_sha256",
            "material_digest": manifests[0]["parts"][2]["ref"]["sha256"]}, source_kind="file_attachment")
        first_id = manifests[0]["parts"][0]["ref"]["file_id"]
        response = await self.http.get(f"/api/chats/public-output/tasks/{record.task_id}/files/{first_id}",
            headers=self.headers("owner-b"))
        self.assertEqual(response.status_code, 404, response.text)


@unittest.skipUnless(TEST_DATABASE_URL, "set TEST_DATABASE_URL for durable owner public-output proofs")
class PostgresPublicOutputOwnerHistoryTests(PublicOutputOwnerHistoryTests):
    use_postgres = True

    async def test_all_typed_reads_use_the_borrowed_download_transaction(self):
        record, manifests, _ = await self.fixture()
        database = self.app.state.core_agent.workflow_store.database
        original = ResponseArtifactService.load
        connections = []

        def borrowed(service, *args, **kwargs):
            connections.append(kwargs.get("connection"))
            self.assertIsNotNone(connections[-1])
            with patch.object(database.pool, "connection", side_effect=AssertionError("nested checkout")):
                return original(service, *args, **kwargs)

        file_id = manifests[0]["parts"][1]["ref"]["file_id"]
        with patch.object(ResponseArtifactService, "load", borrowed):
            response = await self.http.get(f"/api/chats/public-output/tasks/{record.task_id}/files/{file_id}",
                headers=self.headers("owner-b"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, b'{"count":2.0}')
        self.assertEqual(len(connections), 2)
