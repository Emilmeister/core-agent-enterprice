"""Owner HTTP cleanup preserves exact confirmed intent and durable recovery."""
import asyncio
import json
import unittest
from dataclasses import replace
from urllib.parse import quote
from unittest.mock import patch

from core_agent.errors import CoreError
from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
from core_agent.workspace import WorkspaceBinding
from tests.test_auth import AuthAppTestCase, TEST_DATABASE_URL


class WorkspaceCleanupAPITests(AuthAppTestCase):
    @property
    def agent(self):
        return self.app.state.core_agent

    @property
    def service(self):
        return self.agent.workspace_cleanup

    @property
    def manager(self):
        return self.agent.tool_runtime.environment_manager.backend.chats

    def url(self, context="cleanup"):
        return "/api/chats/" + quote(context, safe="") + "/files/delete"

    async def prepare(self, context="cleanup"):
        task = await self.submit("external-a", "root-" + context, context)
        record = self.agent.workflow_store.lookup_task(task["id"])
        binding = WorkspaceBinding(record.tenant_id, record.owner_id, record.context_id)
        directory = self.manager.workspace(binding)
        (directory / "a.txt").write_bytes(b"alpha")
        (directory / "b.txt").write_bytes(b"beta")
        page = await self.http.get(self.url(context).removesuffix("/delete"), headers=self.headers("owner-a"))
        self.assertEqual(page.status_code, 200, page.text)
        files = [{key: item[key] for key in ("path", "identity_token")} for item in page.json()["files"]]
        return task, binding, directory, page.json(), {"request_id": "confirmed-" + context, "files": files}

    async def delete(self, body, context="cleanup", token="owner-a", **kwargs):
        return await self.http.post(self.url(context), headers=self.headers(token), json=body, **kwargs)

    async def test_completed_exact_receipt_shared_owners_retry_and_revision(self):
        task, binding, directory, page, body = await self.prepare()
        before = self.agent.workflow_store.lookup_task(task["id"])
        calls = len(self.model.calls)
        response = await self.delete(body)
        self.assertEqual(response.status_code, 200, response.text)
        receipt = response.json()
        self.assertEqual(set(receipt), {"request_id", "operation_id", "state", "workspace_revision", "files", "results", "totals"})
        self.assertEqual(receipt["state"], "completed")
        self.assertEqual(receipt["files"], body["files"])
        self.assertEqual(receipt["workspace_revision"], 1)
        self.assertEqual(receipt["totals"], {"deleted": 2, "skipped": 0, "errors": 0, "deleted_bytes": 9})
        self.assertEqual([item["status"] for item in receipt["results"]], ["deleted", "deleted"])
        self.assertFalse((directory / "a.txt").exists())
        self.assertFalse((directory / "b.txt").exists())
        repeated = await self.delete(body, token="owner-b")
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json(), receipt)
        latest = await self.http.get(self.url(), headers=self.headers("owner-b"))
        self.assertEqual(latest.json(), receipt)
        exact = await self.http.get(self.url(), params={"request_id": body["request_id"]}, headers=self.headers("owner-b"))
        self.assertEqual(exact.json(), receipt)
        self.assertEqual(latest.headers["cache-control"], "no-store")
        self.assertEqual(self.agent.workflow_store.lookup_task(task["id"]), before)
        self.assertEqual(len(self.model.calls), calls)
        changed = await self.delete({**body, "files": list(reversed(body["files"]))})
        self.assertEqual(changed.status_code, 409, changed.text)
        self.assertEqual(changed.json()["error"]["code"], "CLEANUP_REQUEST_CONFLICT")
        refreshed = await self.http.get(self.url().removesuffix("/delete"), headers=self.headers("owner-b"))
        self.assertEqual(refreshed.json()["workspace_revision"], 1)
        self.assertEqual(refreshed.json()["files"], [])

    async def test_owner_auth_scope_and_revocation_precede_mutation(self):
        _, _, directory, _, body = await self.prepare()
        self.tokens["dual"] = {**self.tokens["owner-a"], "realm_access": {"roles": ["agent-owner", "agent-external"]}}
        for token in ("external-a", "external-b", "dual"):
            self.assertEqual((await self.delete(body, token=token)).status_code, 403)
            self.assertEqual((await self.http.get(self.url(), headers=self.headers(token))).status_code, 403)
        self.assertEqual((await self.delete(body, context="missing")).status_code, 404)
        self.assertEqual((await self.http.get(self.url("missing"), headers=self.headers("owner-b"))).status_code, 404)
        auth = self.app.state.authenticator
        with patch.object(auth, "settings", replace(auth.settings, tenant="foreign-company")):
            self.assertEqual((await self.delete(body)).status_code, 404)
            self.assertEqual((await self.http.get(self.url(), headers=self.headers("owner-b"))).status_code, 404)
        self.keycloak_status = 503
        self.assertEqual((await self.delete(body)).status_code, 503)
        self.keycloak_status = 200
        self.tokens["owner-a"]["active"] = False
        self.assertEqual((await self.delete(body)).status_code, 401)
        self.assertEqual((directory / "a.txt").read_bytes(), b"alpha")
        missing = await self.http.get(self.url(), headers=self.headers("owner-b"))
        self.assertEqual(missing.status_code, 404, missing.text)
        self.assertEqual(missing.json()["error"]["code"], "FILE_CLEANUP_NOT_FOUND")

    async def test_strict_request_queries_and_empty_selection(self):
        _, _, directory, _, body = await self.prepare()
        invalid = ({}, {**body, "extra": True}, {**body, "request_id": ""},
                   {**body, "request_id": "x" * 257}, {**body, "files": body["files"] * 501},
                   {**body, "files": [body["files"][0], body["files"][0]]},
                   {**body, "files": [{**body["files"][0], "path": "../a.txt"}]},
                   {**body, "files": [{**body["files"][0], "identity_token": "forged"}]})
        for payload in invalid:
            response = await self.delete(payload)
            self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual((await self.delete(body, params={"request_id": "other"})).status_code, 400)
        for params in ({"request_id": ""}, {"tenant": "foreign"}, [("request_id", "a"), ("request_id", "b")]):
            response = await self.http.get(self.url(), headers=self.headers("owner-a"), params=params)
            self.assertEqual(response.status_code, 400, response.text)
        duplicated = json.dumps(body)[:-1] + ',"request_id":"other"}'
        response = await self.http.post(self.url(), headers=self.headers("owner-a"), content=duplicated)
        self.assertEqual(response.status_code, 400, response.text)
        response = await self.delete({"request_id": "empty-selection", "files": []})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["files"], [])
        self.assertEqual(response.json()["results"], [])
        self.assertEqual(response.json()["workspace_revision"], 0)
        self.assertEqual((directory / "a.txt").read_bytes(), b"alpha")

    async def test_active_root_refuses_intent_and_never_queues_cleanup(self):
        self.agent.model = ScriptedModel([ModelResponse(tool_requests=(
            ToolRequest("ask", "core_ask_owner", {"question": "Proceed?"}),))])
        _, _, directory, _, body = await self.prepare()
        response = await self.delete(body)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "CONTEXT_BUSY")
        lookup = await self.http.get(self.url(), headers=self.headers("owner-b"))
        self.assertEqual(lookup.status_code, 404, lookup.text)
        self.assertEqual((directory / "a.txt").read_bytes(), b"alpha")

    async def test_stale_identity_only_skips_changed_selected_file(self):
        _, _, directory, _, body = await self.prepare()
        (directory / "a.txt").write_bytes(b"new owner content")
        response = await self.delete(body)
        self.assertEqual(response.status_code, 200, response.text)
        receipt = response.json()
        self.assertEqual(receipt["results"][0], {"path": "a.txt", "status": "skipped", "reason": "identity_changed"})
        self.assertEqual(receipt["results"][1]["status"], "deleted")
        self.assertEqual(receipt["totals"], {"deleted": 1, "skipped": 1, "errors": 0, "deleted_bytes": 4})
        self.assertEqual((directory / "a.txt").read_bytes(), b"new owner content")

    async def test_committed_intent_reads_are_passive_and_coordinator_resumes(self):
        _, binding, directory, _, body = await self.prepare()
        with patch.object(self.service, "_execute", side_effect=CoreError("WORKSPACE_CLEANUP_INVALID")):
            started = await self.delete(body)
        self.assertEqual(started.status_code, 409, started.text)
        with patch.object(self.service, "_execute", side_effect=AssertionError("GET must be passive")):
            status = await self.http.get(self.url(), headers=self.headers("owner-b"))
            self.assertEqual(status.status_code, 202, status.text)
            self.assertEqual(status.json()["state"], "pending")
            self.assertEqual(status.json()["files"], body["files"])
            preview = await self.http.get(self.url().removesuffix("/delete"), headers=self.headers("owner-b"))
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertEqual(preview.json()["cleanup_block_reason"], "WORKSPACE_CLEANUP_PENDING")
            self.assertEqual(preview.json()["workspace_revision"], 0)
            self.assertEqual((directory / "a.txt").read_bytes(), b"alpha")
        with patch.object(self.service, "recover", wraps=self.service.recover) as recover:
            await asyncio.to_thread(self.agent._recover_workflows_once)
            recover.assert_called_once_with(limit=100, tenant_id=binding.tenant_id)
        self.assertFalse((directory / "a.txt").exists())
        self.assertFalse(self.service.preview_state(binding)["cleanup_pending"])
        final = await self.http.get(self.url(), headers=self.headers("owner-b"))
        self.assertEqual(final.status_code, 200, final.text)
        self.assertEqual(final.json()["state"], "completed")

    async def test_slash_context_and_cursors_revision_invalidation(self):
        context = "company/отчёты/cleanup"
        _, _, directory, _, body = await self.prepare(context)
        base = self.url(context).removesuffix("/delete")
        first = await self.http.get(base, params={"limit": 1}, headers=self.headers("owner-a"))
        selected = {**body, "files": body["files"][:1]}
        response = await self.delete(selected, context)
        self.assertEqual(response.status_code, 200, response.text)
        stale = await self.http.get(base, params={"cursor": first.json()["next_cursor"]}, headers=self.headers("owner-a"))
        self.assertEqual(stale.status_code, 400, stale.text)
        retry_old = await self.delete({"request_id": "stale-revision", "files": body["files"][1:]}, context)
        self.assertEqual(retry_old.status_code, 200, retry_old.text)
        self.assertEqual(retry_old.json()["results"][0]["reason"], "identity_changed")
        self.assertTrue((directory / "b.txt").exists())

    async def test_pending_cleanup_refuses_new_a2a_root_with_retryable_safe_error(self):
        task, _, directory, _, body = await self.prepare()
        with patch.object(self.service, "_execute", side_effect=CoreError("WORKSPACE_CLEANUP_INVALID")):
            self.assertEqual((await self.delete(body)).status_code, 409)
        calls = len(self.model.calls)
        original = self.agent.workflow_store.lookup_task(task["id"])
        for method in ("message:send", "message:stream"):
            response = await self.http.post("/a2a/external/" + method, headers=self.headers("external-a"), json={
                "message": {"messageId": "new-" + method, "contextId": "cleanup", "role": "ROLE_USER",
                            "parts": [{"text": "Try a new task"}]}})
            self.assertEqual(response.status_code, 503, response.text)
            error = response.json()["error"]
            self.assertEqual(error["status"], "UNAVAILABLE")
            self.assertTrue(any(item.get("reason") == "WORKSPACE_CLEANUP_PENDING" for item in error["details"]))
            self.assertNotIn(str(directory), response.text)
        response = await self.http.post("/a2a/external/", headers=self.headers("external-a"), json={
            "jsonrpc": "2.0", "id": "blocked-jsonrpc", "method": "SendMessage", "params": {
                "message": {"messageId": "new-jsonrpc", "contextId": "cleanup", "role": "ROLE_USER",
                            "parts": [{"text": "Try a new task"}]}}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["id"], "blocked-jsonrpc")
        self.assertEqual(response.json()["error"]["code"], -32000)
        self.assertTrue(any(item.get("reason") == "WORKSPACE_CLEANUP_PENDING" and
                            item.get("metadata") == {"code": "WORKSPACE_CLEANUP_PENDING", "retryable": "true"}
                            for item in response.json()["error"]["data"]))
        duplicate = await self.submit("external-a", "root-cleanup", "cleanup")
        self.assertEqual(duplicate["id"], task["id"])
        self.assertEqual(self.agent.workflow_store.lookup_task(task["id"]), original)
        self.assertEqual(len(self.model.calls), calls)
        self.assertTrue((directory / "a.txt").exists())


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL not set")
class PostgresWorkspaceCleanupAPITests(WorkspaceCleanupAPITests):
    use_postgres = True
