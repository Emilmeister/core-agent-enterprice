import base64
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
from core_agent.errors import CoreError
from tests import test_admission as admission_tests
from tests.test_auth import AuthAppTestCase, TEST_DATABASE_URL


class OwnerChatAPITests(AuthAppTestCase):
    async def test_empty_canonical_chat_lists_and_opens_workspace_without_task(self):
        agent = self.app.state.core_agent
        admission = agent.tool_runtime.environment_manager.validate_workspace_scope.__self__
        context = admission_tests.AuthAdmissionTests.context(self, "owner-a")
        if self.use_postgres:
            with admission.database.transaction() as connection:
                binding = admission.ensure_chat(context, connection=connection)
        else:
            binding = await admission.transaction(context, lambda _: admission.ensure_chat(context))
        before = sorted(str(path) for path in agent.tool_runtime.environment_manager.backend.chats.root.rglob("*"))
        rows = await admission.list_chats(context.tenant, limit=10)
        self.assertEqual(rows, [{"context_id": binding.context_id, "latest_task_id": None, "active": False}])
        self.assertEqual(await admission.workspace_scope(context.tenant, binding.context_id), (binding, False))
        self.assertEqual(await admission.list_chats(context.tenant, limit=10, after=[2, binding.context_id]), [])
        self.assertFalse(self.model.calls)
        self.assertEqual(sorted(str(path) for path in agent.tool_runtime.environment_manager.backend.chats.root.rglob("*")), before)
        with self.assertRaises(CoreError) as error:
            await admission.workspace_scope("foreign", binding.context_id)
        self.assertEqual(error.exception.code, "TASK_NOT_FOUND")
        task = await self.submit("owner-b", "first-in-empty", binding.context_id)
        self.assertEqual(task["contextId"], binding.context_id)
        self.assertEqual(agent.workflow_store.lookup_task(task["id"]).owner_id, binding.owner_id)

    async def test_company_list_is_shared_paginated_and_read_only(self):
        tasks = [await self.submit(token, "message-" + context, context)
                 for token, context in (("external-a", "a-chat"), ("owner-a", "z-chat"))]
        calls = len(self.model.calls)
        first = await self.http.get("/api/chats?limit=1", headers=self.headers("owner-b"))
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.headers["cache-control"], "no-store")
        self.assertEqual(first.json()["chats"], [{
            "context_id": "a-chat", "latest_task_id": tasks[0]["id"], "active": False,
        }])
        cursor = first.json()["next_cursor"]
        self.assertIsInstance(cursor, str)
        second = await self.http.get("/api/chats", params={"limit": "1", "cursor": cursor},
                                     headers=self.headers("owner-a"))
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json(), {"chats": [{
            "context_id": "z-chat", "latest_task_id": tasks[1]["id"], "active": False,
        }], "next_cursor": None})
        self.assertEqual(len(self.model.calls), calls)
        self.assertNotIn("Answer briefly", first.text + second.text)

    async def test_external_dual_role_and_malformed_query_rejected(self):
        self.tokens["dual-role"] = {**self.tokens["owner-a"],
            "realm_access": {"roles": ["agent-owner", "agent-external"]}}
        for token in ("external-a", "external-b", "dual-role"):
            denied = await self.http.get("/api/chats", headers=self.headers(token))
            self.assertEqual(denied.status_code, 403, denied.text)
        for query in ("limit=0", "limit=101", "limit=true", "limit=1&limit=2",
                      "cursor=invalid", "tenant_id=other", "limit=-1"):
            response = await self.http.get("/api/chats?" + query, headers=self.headers("owner-a"))
            self.assertEqual(response.status_code, 400, response.text)
        tenant = self.app.state.authenticator.settings.tenant
        for value in ("\0", "\ud800", 1):
            cursor = base64.urlsafe_b64encode(json.dumps(["chats", tenant, value]).encode()).decode()
            response = await self.http.get("/api/chats", params={"cursor": cursor}, headers=self.headers("owner-a"))
            self.assertEqual(response.status_code, 400, response.text)
        empty = await self.http.get("/api/chats", headers=self.headers("owner-a"))
        self.assertEqual(empty.json(), {"chats": [], "next_cursor": None})
        self.assertFalse(self.model.calls)

    async def test_company_scope_and_cursor_cannot_be_reused_across_companies(self):
        await self.submit("owner-a", "one", "a-chat")
        await self.submit("owner-a", "two", "z-chat")
        first = await self.http.get("/api/chats?limit=1", headers=self.headers("owner-a"))
        cursor = first.json()["next_cursor"]
        authenticator = self.app.state.authenticator
        with patch.object(authenticator, "settings", replace(authenticator.settings, tenant="another-company")):
            other = await self.http.get("/api/chats", headers=self.headers("owner-a"))
            self.assertEqual(other.json(), {"chats": [], "next_cursor": None})
            stale = await self.http.get("/api/chats", params={"cursor": cursor}, headers=self.headers("owner-a"))
            self.assertEqual(stale.status_code, 400)

    async def test_waiting_root_remains_active_and_busy_attempt_does_not_replace_it(self):
        agent = self.app.state.core_agent
        agent.model = ScriptedModel([ModelResponse(tool_requests=(
            ToolRequest("owner-question", "core_ask_owner", {"question": "Confirm the delivery address?"}),
        ))])
        task = await self.submit("external-a", "waiting", "waiting-chat")
        self.assertEqual(task["status"]["state"], "TASK_STATE_WORKING")
        busy = await self.submit("owner-b", "busy", "waiting-chat")
        self.assertEqual(busy["status"]["state"], "TASK_STATE_FAILED")
        calls = len(agent.model.calls)
        response = await self.http.get("/api/chats", headers=self.headers("owner-b"))
        self.assertEqual(response.json(), {"chats": [{
            "context_id": "waiting-chat", "latest_task_id": task["id"], "active": True,
        }], "next_cursor": None})
        self.assertEqual(len(agent.model.calls), calls)

    async def test_cursor_for_long_context_survives_a_newer_root_in_that_chat(self):
        context_id = "a-" + "漢" * 1_500
        await self.submit("owner-a", "long-context", context_id)
        last = await self.submit("owner-b", "last", "z-chat")
        first = await self.http.get("/api/chats?limit=1", headers=self.headers("owner-a"))
        self.assertEqual(first.json()["chats"][0]["context_id"], context_id)
        cursor = first.json()["next_cursor"]
        self.assertLess(len(cursor), 4096)
        await self.submit("owner-a", "newer-root", context_id)
        second = await self.http.get("/api/chats", params={"cursor": cursor}, headers=self.headers("owner-b"))
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json(), {"chats": [{
            "context_id": "z-chat", "latest_task_id": last["id"], "active": False,
        }], "next_cursor": None})


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL not set")
class PostgresOwnerChatAPITests(OwnerChatAPITests):
    use_postgres = True
