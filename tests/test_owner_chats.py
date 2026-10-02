import asyncio
import base64
import json
import unittest
import uuid
from dataclasses import replace
from unittest.mock import patch
from psycopg.conninfo import make_conninfo
from psycopg.sql import SQL, Identifier
from a2a.types import Message, Role, Task, TaskState, TaskStatus
from core_agent import database as database_module

from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
from core_agent.errors import CoreError
from core_agent.database import PostgresDatabase, PostgresTaskStore
from core_agent.admission import PostgresRootAdmission
from tests import test_admission as admission_tests
from tests.test_auth import AuthAppTestCase, TEST_DATABASE_URL


class OwnerChatAPITests(AuthAppTestCase):
    def legacy_rows(self, rows):
        return [{key: row[key] for key in ("context_id", "latest_task_id", "active")} for row in rows]

    async def test_titles_are_shared_cas_metadata_without_changing_task_or_workspace(self):
        response = await self.http.post("/a2a/owner/message:send", headers=self.headers("owner-a"), json={
            "message": {"messageId": "named-root", "contextId": "named-chat", "role": "ROLE_USER",
                        "parts": [{"text": "  Prepare\n the quarterly report  "}]}})
        self.assertEqual(response.status_code, 200, response.text)
        task = response.json()["task"]
        row = (await self.http.get("/api/chats", headers=self.headers("owner-b"))).json()["chats"][0]
        self.assertEqual(row["title"], "Prepare the quarterly report")
        self.assertEqual(row["title_revision"], 1)
        self.assertEqual(row["status"], task["status"]["state"])
        self.assertGreater(row["updated_at"], 0)
        agent = self.app.state.core_agent
        record = agent.workflow_store.lookup_task(task["id"])
        snapshot, calls = record.snapshot, len(self.model.calls)
        admission = agent.tool_runtime.environment_manager.validate_workspace_scope.__self__
        binding = await admission.workspace_scope(record.tenant_id, "named-chat")
        renamed = await self.http.put("/api/chats/named-chat/title", headers=self.headers("owner-b"),
                                     json={"title": "  Quarterly report  ", "expected_revision": 1})
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(renamed.json()["title"], "Quarterly report")
        self.assertEqual(renamed.json()["title_revision"], 2)
        self.assertEqual(renamed.json()["context_id"], "named-chat")
        self.assertGreaterEqual(renamed.json()["updated_at"], row["updated_at"])
        unchanged = agent.workflow_store.lookup_task(task["id"])
        self.assertEqual((unchanged.run_id, unchanged.owner_id, unchanged.context_id, unchanged.snapshot),
                         (record.run_id, record.owner_id, record.context_id, snapshot))
        self.assertEqual(await admission.workspace_scope(record.tenant_id, "named-chat"), binding)
        after = await self.http.get("/api/chats", headers=self.headers("owner-a"))
        again = await self.http.get("/api/chats", headers=self.headers("owner-b"))
        self.assertEqual(after.json(), again.json())
        self.assertEqual(after.json()["chats"][0]["title"], "Quarterly report")
        self.assertEqual(after.json()["chats"][0]["latest_task_id"], task["id"])
        self.assertEqual(len(self.model.calls), calls)
        if self.use_postgres:
            reopened = PostgresDatabase(TEST_DATABASE_URL)
            self.addCleanup(reopened.close)
            restored = PostgresRootAdmission(agent, PostgresTaskStore(reopened))
            persisted = await restored.list_chats(record.tenant_id, limit=10)
            self.assertEqual(persisted, after.json()["chats"])

    async def test_owner_followup_names_an_external_chat_only_after_processing(self):
        agent = self.app.state.core_agent
        agent.model = ScriptedModel([
            ModelResponse(tool_requests=(ToolRequest("owner-question", "core_ask_owner", {"question": "Continue?"}),)),
            ModelResponse(message="Done"),
        ])
        task = await self.submit("external-a", "external-root", "external-chat")
        before = (await self.http.get("/api/chats", headers=self.headers("owner-a"))).json()["chats"][0]
        self.assertEqual(before["title"], "")
        response = await self.http.post("/a2a/owner/message:send", headers=self.headers("owner-b"), json={
            "message": {"messageId": "owner-followup", "taskId": task["id"], "contextId": "external-chat",
                        "role": "ROLE_USER", "parts": [{"text": "Prepare corrected report"}]}})
        self.assertEqual(response.status_code, 200, response.text)
        queued = (await self.http.get("/api/chats", headers=self.headers("owner-a"))).json()["chats"][0]
        self.assertEqual((queued["title"], queued["title_revision"]), ("", 1))
        self.assertGreaterEqual(queued["updated_at"], before["updated_at"])
        record = agent.workflow_store.lookup_task(task["id"])
        agent.workflow_store.resolve_wait(record.snapshot["wait_id"], tenant_id=record.tenant_id,
                                         outcome={"reason": "answer", "answer": "yes"})
        await asyncio.to_thread(agent.resume_task, task["id"])
        after = (await self.http.get("/api/chats", headers=self.headers("owner-a"))).json()["chats"][0]
        self.assertEqual((after["title"], after["latest_task_id"]), ("Prepare corrected report", task["id"]))
        self.assertEqual(agent.workflow_store.lookup_task(task["id"]).owner_id, record.owner_id)

    async def test_legacy_name_uses_original_request_without_changing_old_text(self):
        task = await self.submit("owner-a", "legacy-root", "legacy-chat")
        agent = self.app.state.core_agent
        admission = agent.tool_runtime.environment_manager.validate_workspace_scope.__self__
        tenant = self.app.state.authenticator.settings.tenant
        if self.use_postgres:
            with admission.database.transaction() as connection:
                connection.execute("UPDATE core_chats SET title='',title_revision=0,title_source=NULL WHERE tenant_id=%s AND context_id=%s",
                                   (tenant, "legacy-chat"))
        else:
            admission.chats[(tenant, "legacy-chat")].update(title="", title_revision=0, title_source=None)
        before = agent.workflow_store.lookup_task(task["id"])
        response = await self.http.get("/api/chats", headers=self.headers("owner-b"))
        self.assertEqual((response.json()["chats"][0]["title"], response.json()["chats"][0]["title_revision"]),
                         ("Answer briefly", 0))
        self.assertEqual(agent.workflow_store.lookup_task(task["id"]), before)

    async def test_title_rename_validation_scope_and_concurrent_revision(self):
        await self.submit("owner-a", "rename-root", "rename-chat")
        self.tokens["dual-role"] = {**self.tokens["owner-a"],
                                   "realm_access": {"roles": ["agent-owner", "agent-external"]}}
        url = "/api/chats/rename-chat/title"
        for token in ("external-a", "dual-role"):
            result = await self.http.put(url, headers=self.headers(token), json={"title": "Denied", "expected_revision": 1})
            self.assertEqual(result.status_code, 403, result.text)
        for payload in ({"title": "", "expected_revision": 1}, {"title": " \n ", "expected_revision": 1},
                        {"title": "x" * 121, "expected_revision": 1}, {"title": "\0", "expected_revision": 1},
                        {"title": "\ud800", "expected_revision": 1}, {"title": 1, "expected_revision": 1},
                        {"title": "Valid", "expected_revision": True}, {"title": "Valid", "expected_revision": -1},
                        {"title": "Valid", "expected_revision": 1, "tenant_id": "other"}):
            result = await self.http.put(url, headers=self.headers("owner-a"), content=json.dumps(payload))
            self.assertEqual(result.status_code, 400, result.text)
        results = await asyncio.gather(*(self.http.put(url, headers=self.headers(token),
            json={"title": "  " + "Я" * 120 + "  ", "expected_revision": 1}) for token in ("owner-a", "owner-b")))
        self.assertEqual(sorted(result.status_code for result in results), [200, 409])
        self.assertEqual(next(result for result in results if result.status_code == 200).json()["title"], "Я" * 120)
        self.assertEqual(next(result for result in results if result.status_code == 409).json()["error"]["code"],
                         "CHAT_TITLE_CONFLICT")
        authenticator = self.app.state.authenticator
        with patch.object(authenticator, "settings", replace(authenticator.settings, tenant="another-company")):
            result = await self.http.put(url, headers=self.headers("owner-a"),
                                         json={"title": "Other company", "expected_revision": 2})
            self.assertEqual(result.status_code, 404, result.text)

    async def test_owner_attention_distinguishes_tool_approval_from_timer_wait(self):
        agent = self.app.state.core_agent
        tenant = self.app.state.authenticator.settings.tenant
        agent.interaction_store.update_policy(tenant, "core_task_list", "builtin:core_task_list",
            mode="require_hitl", guardrails_exempt=False, expected_revision=1, actor_id="test-owner")
        agent.model = ScriptedModel([
            ModelResponse(tool_requests=(ToolRequest("timer", "core_wait_until", {"until": "2099-01-01T00:00:00Z"}),)),
            ModelResponse(tool_requests=(ToolRequest("approval", "core_task_list", {}),)),
        ])
        await self.submit("external-a", "timer-root", "timer-chat")
        approval = await self.submit("external-a", "approval-root", "approval-chat")
        rows = (await self.http.get("/api/chats", headers=self.headers("owner-b"))).json()["chats"]
        self.assertEqual({row["context_id"]: row["needs_attention"] for row in rows},
                         {"approval-chat": True, "timer-chat": False})
        self.assertTrue(all(row["status"] == "TASK_STATE_WORKING" for row in rows))
        calls = len(agent.model.calls)
        record = agent.workflow_store.lookup_task(approval["id"])
        agent.workflow_store.resolve_wait(record.snapshot["wait_id"], tenant_id=tenant, outcome={"reason": "rejected"})
        settled = (await self.http.get("/api/chats", headers=self.headers("owner-a"))).json()["chats"]
        self.assertTrue(all(not row["needs_attention"] for row in settled))
        self.assertEqual(len(agent.model.calls), calls)

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
        self.assertEqual(self.legacy_rows(rows), [{"context_id": binding.context_id, "latest_task_id": None, "active": False}])
        self.assertEqual((rows[0]["title"], rows[0]["title_revision"], rows[0]["status"]), ("", 0, None))
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
        self.assertEqual(self.legacy_rows(first.json()["chats"]), [{
            "context_id": "a-chat", "latest_task_id": tasks[0]["id"], "active": False,
        }])
        cursor = first.json()["next_cursor"]
        self.assertIsInstance(cursor, str)
        second = await self.http.get("/api/chats", params={"limit": "1", "cursor": cursor},
                                     headers=self.headers("owner-a"))
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual({**second.json(), "chats": self.legacy_rows(second.json()["chats"])}, {"chats": [{
            "context_id": "z-chat", "latest_task_id": tasks[1]["id"], "active": False,
        }], "next_cursor": None})
        self.assertEqual(len(self.model.calls), calls)
        self.assertEqual(first.json()["chats"][0]["title"], "")
        self.assertEqual(second.json()["chats"][0]["title"], "Answer briefly")

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
        self.assertEqual({**response.json(), "chats": self.legacy_rows(response.json()["chats"])}, {"chats": [{
            "context_id": "waiting-chat", "latest_task_id": task["id"], "active": True,
        }], "next_cursor": None})
        self.assertEqual(len(agent.model.calls), calls)
        self.assertTrue(response.json()["chats"][0]["needs_attention"])
        self.assertEqual(response.json()["chats"][0]["status"], "TASK_STATE_WORKING")
        record = agent.workflow_store.lookup_task(task["id"])
        wait = agent.workflow_store.get_wait(record.snapshot["wait_id"], tenant_id=record.tenant_id)
        if self.use_postgres:
            with agent.workflow_store.database.transaction() as connection:
                connection.execute("UPDATE core_waits SET deadline=%s WHERE wait_id=%s", (agent.workflow_store.current_time()-1, wait.wait_id))
        else:
            agent.workflow_store._waits[wait.wait_id] = replace(wait, deadline=agent.workflow_store.current_time()-1)
        expired = await self.http.get("/api/chats", headers=self.headers("owner-a"))
        self.assertFalse(expired.json()["chats"][0]["needs_attention"])
        self.assertEqual(expired.json()["chats"][0]["status"], "TASK_STATE_WORKING")
        agent.workflow_store.resolve_wait(wait.wait_id, tenant_id=record.tenant_id, outcome={"reason": "answer", "answer": "yes"})
        resolved = await self.http.get("/api/chats", headers=self.headers("owner-a"))
        self.assertFalse(resolved.json()["chats"][0]["needs_attention"])
        self.assertEqual(len(agent.model.calls), calls)
        self.assertNotIn("Confirm the delivery address?", response.text + expired.text + resolved.text)

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
        self.assertEqual({**second.json(), "chats": self.legacy_rows(second.json()["chats"])}, {"chats": [{
            "context_id": "z-chat", "latest_task_id": last["id"], "active": False,
        }], "next_cursor": None})


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL not set")
class PostgresOwnerChatAPITests(OwnerChatAPITests):
    use_postgres = True

    async def test_schema24_upgrade_preserves_chat_binding_and_original_task_bytes(self):
        database = self.app.state.core_agent.workflow_store.database
        schema = "owner_title_upgrade_" + uuid.uuid4().hex
        with database.transaction() as connection:
            connection.execute(SQL("CREATE SCHEMA {}").format(Identifier(schema)))
        def remove_schema():
            with database.transaction() as connection:
                connection.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(schema)))
        self.addCleanup(remove_schema)
        upgraded = PostgresDatabase(make_conninfo(TEST_DATABASE_URL, options="-c search_path=" + schema))
        self.addCleanup(upgraded.close)
        previous = {version: sql for version, sql in database_module.MIGRATIONS.items() if version <= 24}
        with patch.object(database_module, "SCHEMA_VERSION", 24), patch.dict(database_module.MIGRATIONS, previous, clear=True):
            upgraded.migrate()
        task = Task(id=uuid.uuid4().hex, context_id="pre-upgrade", status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
                    history=[Message(message_id="original-message", role=Role.ROLE_USER, parts=[{"text": "Original ё request"}])])
        payload = task.SerializeToString()
        with upgraded.transaction() as connection:
            connection.execute("INSERT INTO core_a2a_tasks(task_id,owner,tenant,context_id,state,payload) VALUES(%s,%s,%s,%s,%s,%s)",
                               (task.id, "company-owners", "legacy-company", task.context_id, int(task.status.state), payload))
            connection.execute("INSERT INTO core_chats(tenant_id,context_id,owner_id) VALUES(%s,%s,%s)",
                               ("legacy-company", task.context_id, "company-owners"))
            connection.execute("""INSERT INTO core_root_messages(tenant_id,actor_id,message_id,request_digest,owner_id,context_id,task_id)
                                  VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                               ("legacy-company", "actor", "original-message", "original-digest", "company-owners", task.context_id, task.id))
            old_chat = connection.execute("SELECT * FROM core_chats").fetchone()
            old_input = connection.execute("SELECT * FROM core_root_messages").fetchone()
        upgraded.migrate()
        upgraded.migrate()
        with upgraded.transaction() as connection:
            chat = connection.execute("SELECT * FROM core_chats").fetchone()
            self.assertEqual((chat.pop("title"), chat.pop("title_revision"), chat.pop("title_source")), ("", 0, None))
            self.assertEqual(chat.pop("updated_at"), old_chat["created_at"])
            self.assertEqual(chat, old_chat)
            message = connection.execute("SELECT * FROM core_root_messages").fetchone()
            self.assertIsNone(message.pop("display_text"))
            self.assertEqual(message, old_input)
            self.assertEqual(bytes(connection.execute("SELECT payload FROM core_a2a_tasks").fetchone()["payload"]), payload)
