"""Owner schedule controls use authenticated scope and ordinary root admission."""
import asyncio
import copy
import threading
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from core_agent.errors import CoreError
from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
from tests.test_auth import AuthAppTestCase, TEST_DATABASE_URL


class CronAPITests(AuthAppTestCase):
    automatic_tools = False
    values = {"request_id": "create", "prompt": "Check deliveries", "expression": "0 18 * * *"}

    async def create_schedule(self, **values):
        response = await self.http.post("/api/schedules", json={**self.values, **values},
                                        headers=self.headers("owner-a"))
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()["schedule"]

    async def test_owner_create_empty_chat_shared_list_and_stable_retry(self):
        row = await self.create_schedule()
        self.assertEqual(row["timezone"], "Europe/Moscow")
        retry = await self.http.post("/api/schedules", json=self.values, headers=self.headers("owner-b"))
        self.assertEqual(retry.status_code, 201, retry.text)
        self.assertEqual(retry.json()["schedule"], row)
        read = await self.http.get("/api/schedules", headers=self.headers("owner-b"))
        self.assertEqual(read.json(), {"schedules": [row], "next_cursor": None})
        chats = await self.http.get("/api/chats", headers=self.headers("owner-b"))
        self.assertIn({"context_id": row["context_id"], "latest_task_id": None, "active": False}, chats.json()["chats"])
        history = await self.http.get(f"/api/chats/{row['context_id']}/history", headers=self.headers("owner-a"))
        self.assertEqual(history.json(), {"items": [], "next_cursor": None})
        self.assertFalse(self.model.calls)

    async def test_external_dual_role_and_company_scope(self):
        row = await self.create_schedule()
        path = "/api/schedules/" + row["id"]
        self.tokens["dual"] = copy.deepcopy(self.tokens["external-a"])
        self.tokens["dual"]["realm_access"]["roles"].append("agent-owner")
        for token in ("external-a", "dual"):
            for method, url, body in (("GET", "/api/schedules", None), ("GET", path, None),
                                      ("POST", "/api/schedules", self.values),
                                      ("POST", path + "/run-now", {"request_id": "run", "expected_revision": 1}),
                                      ("DELETE", path, {"expected_revision": 1})):
                response = await self.http.request(method, url, json=body, headers=self.headers(token))
                self.assertEqual(response.status_code, 403, response.text)
        with patch.object(self.app.state.authenticator, "settings",
                          replace(self.app.state.authenticator.settings, tenant="other-company")):
            response = await self.http.get(path, headers=self.headers("owner-a"))
            self.assertEqual(response.status_code, 404, response.text)
            response = await self.http.get("/api/schedules", headers=self.headers("owner-a"))
            self.assertEqual(response.json(), {"schedules": [], "next_cursor": None})

    async def test_validation_has_no_chat_or_model_effect(self):
        for changes in ({"expression": "@daily"}, {"timezone": "not/a/timezone"},
                        {"request_id": "bad\0id"}, {"prompt": ""}, {"owner_id": "external-a"}):
            response = await self.http.post("/api/schedules", json={**self.values, **changes}, headers=self.headers("owner-a"))
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(response.json()["error"]["code"], "CRON_INVALID")
        for body in ('{"prompt":"a","prompt":"b"}', '[]', '{'):
            response = await self.http.post("/api/schedules", content=body, headers=self.headers("owner-a"))
            self.assertEqual(response.status_code, 400, response.text)
        chats = await self.http.get("/api/chats", headers=self.headers("owner-a"))
        self.assertEqual(chats.json()["chats"], [])
        self.assertFalse(self.model.calls)

    async def test_pagination_cas_disable_delete_and_stale_update(self):
        row = await self.create_schedule()
        second = await self.create_schedule(request_id="another", prompt="Second")
        response = await self.http.get("/api/schedules?limit=1", headers=self.headers("owner-a"))
        first = response.json()
        self.assertIsNotNone(first["next_cursor"])
        response = await self.http.get("/api/schedules", params={"cursor": first["next_cursor"]}, headers=self.headers("owner-b"))
        self.assertEqual({first["schedules"][0]["id"], response.json()["schedules"][0]["id"]}, {row["id"], second["id"]})
        path = "/api/schedules/" + row["id"]
        values = {key: row[key] for key in ("prompt", "expression", "timezone", "enabled")}
        response = await self.http.put(path, json={**values, "enabled": False, "expected_revision": 1}, headers=self.headers("owner-b"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["schedule"]["next_due_at"])
        stale = await self.http.put(path, json={**values, "expected_revision": 1}, headers=self.headers("owner-a"))
        self.assertEqual(stale.status_code, 409, stale.text)
        disabled = await self.http.post(path + "/run-now", json={"request_id": "run", "expected_revision": 2}, headers=self.headers("owner-a"))
        self.assertEqual(disabled.status_code, 409, disabled.text)
        removed = await self.http.request("DELETE", path, json={"expected_revision": 2}, headers=self.headers("owner-a"))
        self.assertEqual(removed.status_code, 200, removed.text)
        again = await self.http.request("DELETE", path, json={"expected_revision": 2}, headers=self.headers("owner-b"))
        self.assertEqual(again.json(), removed.json())
        self.assertEqual((await self.http.get(path, headers=self.headers("owner-a"))).status_code, 404)
        self.assertEqual(await self.create_schedule(), row)

    async def test_run_now_uses_same_chat_and_task_retry_after_schedule_edit(self):
        row = await self.create_schedule()
        path = "/api/schedules/" + row["id"]
        payload = {"request_id": "run", "expected_revision": 1}
        response = await self.http.post(path + "/run-now", json=payload, headers=self.headers("owner-a"))
        self.assertEqual(response.status_code, 200, response.text)
        task = response.json()["task"]
        self.assertEqual(task["contextId"], row["context_id"])
        record = self.app.state.core_agent.workflow_store.lookup_task(task["id"])
        self.assertEqual(record.snapshot["cron_origin"]["source"], "manual")
        values = {key: row[key] for key in ("prompt", "expression", "timezone", "enabled")}
        changed = await self.http.put(path, json={**values, "prompt": "New prompt", "expected_revision": 1}, headers=self.headers("owner-a"))
        self.assertEqual(changed.status_code, 200, changed.text)
        repeat = await self.http.post(path + "/run-now", json=payload, headers=self.headers("owner-b"))
        self.assertEqual(repeat.status_code, 200, repeat.text)
        self.assertEqual(repeat.json()["task"]["id"], task["id"])
        self.assertEqual(self.app.state.core_agent.workflow_store.lookup_task(task["id"]).request["prompt"], row["prompt"])

    async def test_busy_manual_attempt_is_failed_task_without_queue(self):
        self.app.state.core_agent.model = ScriptedModel([ModelResponse(tool_requests=(
            ToolRequest("ask", "core_ask_owner", {"question": "Confirm?"}),))])
        active = await self.submit("owner-a", "active", "same-chat")
        active_state = self.app.state.core_agent.workflow_store.lookup_task(active["id"]).state
        row = await self.create_schedule(context_id="same-chat")
        response = await self.http.post("/api/schedules/" + row["id"] + "/run-now",
                                        json={"request_id": "busy", "expected_revision": 1}, headers=self.headers("owner-b"))
        self.assertEqual(response.status_code, 200, response.text)
        task = response.json()["task"]
        self.assertNotEqual(task["id"], active["id"])
        self.assertEqual(task["status"]["state"], "TASK_STATE_FAILED")
        self.assertEqual(task["metadata"]["error"]["code"], "CONTEXT_BUSY")
        current = self.app.state.core_agent.workflow_store.lookup_task(active["id"])
        self.assertEqual(current.state, active_state)

    async def test_asgi_lifespan_runs_thread_cron_on_main_loop_and_preserves_admission(self):
        if self.use_postgres:
            self.skipTest("Memory loop ownership; PostgreSQL leader coverage is in coordinator tests")
        agent = self.app.state.core_agent
        coordinator = agent.cron_coordinator
        store = agent.cron_store
        tenant = self.app.state.authenticator.settings.tenant
        loop = asyncio.get_running_loop()
        main_thread = threading.get_ident()
        now = datetime.now(UTC)
        store.clock = lambda: now
        row = await self.create_schedule(expression="* * * * *")
        store.admission._cron_schedules[(tenant, row["id"])]["next_due_at"] = now - timedelta(seconds=1)
        passes = asyncio.Queue()
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        tick_threads = []
        occur, tick, close = store.occur_memory, coordinator.tick, agent.close

        async def observed_occurrence(*args, **kwargs):
            self.assertIs(asyncio.get_running_loop(), loop)
            self.assertEqual(threading.get_ident(), main_thread)
            accepted = await occur(*args, **kwargs)
            passes.put_nowait(accepted)
            return accepted

        def observed_tick():
            tick_threads.append(threading.get_ident())
            tick()

        def observed_close():
            self.assertTrue(coordinator._closed)
            self.assertIsNone(coordinator._task)
            close()

        # Leave the admitted root ready for normal recovery; no provider worker
        # is needed to prove lifecycle ownership and shutdown preservation.
        with patch.object(store, "occur_memory", side_effect=observed_occurrence), \
                patch.object(coordinator, "tick", side_effect=observed_tick), \
                patch.object(agent, "_launch_recovery", return_value=False), \
                patch.object(agent, "close", side_effect=observed_close) as closing:
            lifespan = asyncio.create_task(self.app(
                {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}, "state": {}},
                incoming.get, outgoing.put))
            try:
                await incoming.put({"type": "lifespan.startup"})
                self.assertEqual(await asyncio.wait_for(outgoing.get(), 5), {"type": "lifespan.startup.complete"})
                self.assertIs(coordinator._loop, loop)
                self.assertIsNone(await asyncio.wait_for(passes.get(), 5))
                self.assertEqual(store.events(tenant)[-1]["reason"], "service_unavailable")
                now = datetime.fromisoformat(store.get(tenant, row["id"])["next_due_at"])
                accepted = await asyncio.wait_for(passes.get(), 5)
                self.assertIsNotNone(accepted)
                original = copy.deepcopy(agent.workflow_store.lookup_task(accepted.task.id))
                self.assertEqual(original.snapshot["cron_origin"]["source"], "automatic")
                self.assertTrue(tick_threads)
                self.assertTrue(all(value != main_thread for value in tick_threads))
                await incoming.put({"type": "lifespan.shutdown"})
                self.assertEqual(await asyncio.wait_for(outgoing.get(), 5), {"type": "lifespan.shutdown.complete"})
                await asyncio.wait_for(lifespan, 5)
                closing.assert_called_once()
                self.assertEqual(agent.workflow_store.lookup_task(accepted.task.id), original)
                self.assertEqual([event["kind"] for event in store.events(tenant)], ["created", "skipped", "started"])
                self.assertFalse(self.model.calls)
            finally:
                if not lifespan.done():
                    lifespan.cancel()
                    await asyncio.gather(lifespan, return_exceptions=True)

    async def test_cleanup_pre_admission_barrier_and_recovery_composition(self):
        row = await self.create_schedule()
        agent = self.app.state.core_agent
        self.assertIsNotNone(agent.cron_coordinator)
        before = copy.deepcopy(agent.cron_store.events(self.app.state.authenticator.settings.tenant))
        with patch.object(agent.workspace_cleanup, "check_ready", side_effect=CoreError("WORKSPACE_CLEANUP_PENDING")):
            response = await self.http.post("/api/schedules/" + row["id"] + "/run-now",
                json={"request_id": "blocked", "expected_revision": 1}, headers=self.headers("owner-a"))
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(agent.cron_store.events(self.app.state.authenticator.settings.tenant), before)
        self.assertFalse(self.model.calls)


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL not set")
class PostgresCronAPITests(CronAPITests):
    use_postgres = True
