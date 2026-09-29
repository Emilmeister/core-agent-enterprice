import asyncio
import hashlib
import os
import tempfile
import threading
import time
import unittest
import uuid
from urllib.parse import parse_qs
from unittest.mock import patch

import httpx

from core_agent.app import create_app
from core_agent.errors import CoreError
from core_agent.database import PostgresDatabase
from core_agent.model import ModelResponse, ScriptedModel


ISSUER = "https://identity.example.test/realms/company"
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


class AuthBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """ENT-AC-01..05/67: exercise HTTP admission and real A2A task storage."""

    use_postgres = False

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(
            os.environ,
            {
                "KEYCLOAK_ISSUER_URL": ISSUER,
                "CORE_AGENT_ENVIRONMENT": "development",
                "KEYCLOAK_CLIENT_ID": "agent-introspection",
                "KEYCLOAK_CLIENT_SECRET": "test-only-client-secret",
                "KEYCLOAK_AUDIENCE": "company-agent",
                "CORE_AGENT_TENANT_ID": "auth-test-" + uuid.uuid4().hex,
                "CORE_AGENT_MEMORY": "disabled",
                "LOCAL_WORKSPACE_ROOT": self.temp.name,
            },
            clear=True,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.temp.cleanup)
        self.tokens = {}
        for token, subject, role in (
            ("owner-a", "alice", "agent-owner"),
            ("owner-b", "bob", "agent-owner"),
            ("external-a", "service-a", "agent-external"),
            ("external-a-replaced", "service-a", "agent-external"),
            ("external-b", "service-b", "agent-external"),
        ):
            self.tokens[token] = {
                "active": True,
                "iss": ISSUER,
                "sub": subject,
                "aud": ["company-agent"],
                "exp": int(time.time()) + 3600,
                "token_type": "Bearer",
                "realm_access": {"roles": [role]},
            }
        self.checks = []
        self.keycloak_status = 200
        self.model = ScriptedModel([ModelResponse(message="verified") for _ in range(8)])
        self.model.model = "auth-test-model"
        database = PostgresDatabase(TEST_DATABASE_URL) if self.use_postgres else None
        self.app = create_app(
            model=self.model,
            base_url="https://agent.example.test",
            auth_transport=httpx.MockTransport(self.introspect),
            database=database,
        )
        self.addCleanup(self.app.state.close)
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="https://agent.example.test",
        )
        self.addAsyncCleanup(self.http.aclose)

    def introspect(self, request):
        self.assertEqual(str(request.url), ISSUER + "/protocol/openid-connect/token/introspect")
        self.assertEqual(request.method, "POST")
        form = parse_qs(request.content.decode())
        token = form["token"][0]
        self.checks.append(token)
        return httpx.Response(
            self.keycloak_status, json=self.tokens.get(token, {"active": False})
        )

    @staticmethod
    def headers(token):
        return {"Authorization": "Bearer " + token, "A2A-Version": "1.0"}

    async def submit(self, token, message_id, context_id):
        kind = "owner" if token.startswith("owner") else "external"
        result = await self.http.post(
            f"/a2a/{kind}/message:send",
            headers=self.headers(token),
            json={
                "message": {
                    "messageId": message_id,
                    "contextId": context_id,
                    "role": "ROLE_USER",
                    "parts": [{"text": "Answer briefly"}],
                }
            },
        )
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()["task"]

    async def test_missing_bearer_and_legacy_route_never_reach_model(self):
        response = await self.http.get("/api/identity")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.checks, [])
        legacy = await self.http.post("/message:send", json={})
        self.assertEqual(legacy.status_code, 404)
        self.assertEqual((await self.http.get("/health/live")).status_code, 200)

    async def test_roles_cards_and_conflicting_roles(self):
        owner = await self.http.get("/api/identity", headers=self.headers("owner-a"))
        self.assertEqual(owner.status_code, 200, owner.text)
        self.assertEqual(owner.json()["role"], "owner")
        denied = await self.http.get("/api/identity", headers=self.headers("external-a"))
        self.assertEqual(denied.status_code, 403)
        for kind, token in (("owner", "owner-a"), ("external", "external-a")):
            card = await self.http.get(
                f"/a2a/{kind}/.well-known/agent-card.json", headers=self.headers(token)
            )
            self.assertEqual(card.status_code, 200, card.text)
            self.assertTrue(card.json()["securitySchemes"])
            self.assertTrue(all(
                item["url"] == f"https://agent.example.test/a2a/{kind}"
                for item in card.json()["supportedInterfaces"]
            ))
        self.tokens["external-a"]["realm_access"]["roles"].append("agent-owner")
        denied = await self.http.get("/api/identity", headers=self.headers("external-a"))
        self.assertEqual(denied.status_code, 403)

    async def test_owner_shared_tasks_and_external_isolation_survive_token_replacement(self):
        external = await self.submit("external-a", "external-message", "external-chat")
        shared = await self.submit("owner-a", "owner-message", "owner-chat")
        agent = self.app.state.core_agent
        record = agent.workflow_store.lookup_task(shared["id"])
        scope = {"tenant_id": record.tenant_id} if self.use_postgres else {}
        admitted = next(item for item in agent.audit_log.records(record.run_id, **scope) if item.kind == "task.admitted")
        self.assertEqual(admitted.data.get("actor_id"), hashlib.sha256((ISSUER + "\0alice").encode()).hexdigest())
        for token in ("external-a", "external-a-replaced", "owner-a", "owner-b"):
            kind = "owner" if token.startswith("owner") else "external"
            response = await self.http.get(
                f"/a2a/{kind}/tasks/{external['id']}", headers=self.headers(token)
            )
            self.assertEqual(response.status_code, 200, response.text)
        for task in (external, shared):
            response = await self.http.get(
                f"/a2a/external/tasks/{task['id']}", headers=self.headers("external-b")
            )
            self.assertEqual(response.status_code, 404, response.text)
        owners = await self.http.get("/a2a/owner/tasks", headers=self.headers("owner-b"))
        self.assertEqual({item["id"] for item in owners.json()["tasks"]}, {external["id"], shared["id"]})
        foreign = await self.http.get("/a2a/external/tasks", headers=self.headers("external-b"))
        self.assertEqual(foreign.json().get("tasks", []), [])

    async def test_credentials_do_not_enter_runtime_forwarding_or_persistence(self):
        agent = self.app.state.core_agent
        with patch.object(agent, "attach_stream", wraps=agent.attach_stream) as attached:
            task = await self.submit("external-a", "private-headers", "private-headers-chat")
        forwarded = attached.call_args.args[2]
        self.assertNotIn("authorization", {name.lower() for name in forwarded})
        record = agent.workflow_store.lookup_task(task["id"])
        scope = {"tenant_id": record.tenant_id} if self.use_postgres else {}
        persisted = repr((record, agent.audit_log.records(record.run_id, **scope), self.model.calls))
        for secret in ("Bearer external-a", "test-only-client-secret"):
            self.assertNotIn(secret, persisted)

    async def test_every_request_is_checked_and_invalid_claims_fail_closed(self):
        headers = self.headers("owner-a")
        self.assertEqual((await self.http.get("/api/identity", headers=headers)).status_code, 200)
        original = dict(self.tokens["owner-a"])
        for change in (
            {"active": False}, {"active": 1}, {"exp": 1}, {"exp": True},
            {"exp": "Infinity"}, {"nbf": time.time() + 600},
            {"iss": "https://other.example.test"}, {"aud": ["other"]},
            {"sub": ""}, {"token_type": "Refresh"},
        ):
            with self.subTest(change=change):
                self.tokens["owner-a"] = original | change
                result = await self.http.get("/api/identity", headers=headers)
                self.assertEqual(result.status_code, 401, result.text)
                self.assertNotIn("test-only-client-secret", result.text)
        self.tokens["owner-a"] = original
        self.keycloak_status = 503
        result = await self.http.get("/api/identity", headers=headers)
        self.assertEqual(result.status_code, 503)
        self.assertEqual(len(self.checks), 12)

    async def test_caller_cannot_select_another_company(self):
        valid = await self.http.post(
            "/a2a/external/", headers=self.headers("external-a"),
            json={"jsonrpc": "2.0", "id": 0, "method": "ListTasks", "params": {}},
        )
        self.assertIn("result", valid.json(), valid.text)
        response = await self.http.get(
            "/a2a/external/other-company/tasks", headers=self.headers("external-a")
        )
        self.assertIn(response.status_code, (400, 404))
        response = await self.http.post(
            "/a2a/external/", headers=self.headers("external-a"),
            json={"jsonrpc": "2.0", "id": 1, "method": "ListTasks", "params": {"tenant": "other-company"}},
        )
        self.assertIn("error", response.json())

    async def test_foreign_active_cancel_is_denied_before_any_effect(self):
        started, release = threading.Event(), threading.Event()
        generate = self.model.generate

        def waiting_model(**kwargs):
            started.set()
            release.wait(10)
            return generate(**kwargs)

        self.model.generate = waiting_model
        self.addCleanup(release.set)
        result = await self.http.post(
            "/a2a/external/message:send", headers=self.headers("external-a"),
            json={"configuration": {"returnImmediately": True}, "message": {
                "messageId": "active-cancel", "role": "ROLE_USER",
                "parts": [{"text": "private-external-request"}],
            }},
        )
        task_id = result.json()["task"]["id"]
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        try:
            denied = await self.http.post(
                f"/a2a/external/tasks/{task_id}:cancel", headers=self.headers("external-b"),
            )
            self.assertEqual(denied.status_code, 404, denied.text)
            self.assertNotIn("private-external-request", denied.text)
            subscription = await self.http.post(
                f"/a2a/external/tasks/{task_id}:subscribe", headers=self.headers("external-b"),
            )
            self.assertEqual(subscription.status_code, 404, subscription.text)
            self.assertNotIn("private-external-request", subscription.text)
            record = self.app.state.core_agent.workflow_store.lookup_task(task_id)
            self.assertNotIn(record.state, {"CANCELLED", "FAILED"})
            agent = self.app.state.core_agent
            signal_cancel = agent.signal_task_cancel

            def release_on_cancellation(task_id):
                signal_cancel(task_id)
                release.set()

            with patch.object(agent, "signal_task_cancel", side_effect=release_on_cancellation):
                allowed = await self.http.post(
                    f"/a2a/owner/tasks/{task_id}:cancel", headers=self.headers("owner-b"),
                )
            self.assertEqual(allowed.status_code, 200, allowed.text)
            own = await self.http.get(
                f"/a2a/external/tasks/{task_id}", headers=self.headers("external-a"),
            )
            self.assertEqual(own.status_code, 200, own.text)
        finally:
            release.set()

    async def test_stream_authenticates_once_and_reconnect_checks_again(self):
        before = len(self.checks)
        generate = self.model.generate

        def expire_token_during_generation(**kwargs):
            self.app.state.authenticator.clock = lambda: self.tokens["external-a"]["exp"] + 1
            return generate(**kwargs)

        self.model.generate = expire_token_during_generation
        result = await self.http.post(
            "/a2a/external/message:stream", headers=self.headers("external-a"),
            json={"message": {"messageId": "stream-message", "role": "ROLE_USER", "parts": [{"text": "Answer briefly"}]}},
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertGreater(result.text.count("data:"), 1)
        self.assertEqual(len(self.checks), before + 1)
        denied = await self.http.get("/a2a/external/tasks", headers=self.headers("external-a"))
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(len(self.checks), before + 2)


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL is required")
class PostgresAuthBoundaryTests(AuthBoundaryTests):
    use_postgres = True


class AuthenticationConfigurationTests(unittest.TestCase):
    def test_anonymous_mode_requires_an_explicit_valid_development_profile(self):
        model = ScriptedModel([])
        model.model = "auth-configuration-test"
        for mode in (None, "staging", "Production", "production"):
            with self.subTest(mode=mode):
                values = {"CORE_AGENT_ENVIRONMENT": mode} if mode else {}
                with patch.dict(os.environ, values, clear=True):
                    with self.assertRaises(CoreError) as caught:
                        create_app(model=model)
                self.assertEqual(caught.exception.code, "CONFIG_INVALID")

    def test_partial_configuration_fails_before_allocating_runtime(self):
        model = ScriptedModel([])
        model.model = "auth-configuration-test"
        with patch.dict(os.environ, {
            "CORE_AGENT_ENVIRONMENT": "development", "KEYCLOAK_ISSUER_URL": ISSUER,
        }, clear=True):
            with self.assertRaises(CoreError) as caught:
                create_app(model=model)
        self.assertEqual(caught.exception.code, "CONFIG_INVALID")
