"""Cloud.ru owner sessions and proxy external-scope acceptance proofs."""
import asyncio
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx

from core_agent.auth import AuthContextBuilder, AuthSettings, Principal
from core_agent.app import _state
from core_agent.database import PostgresDatabase, PostgresTaskStore
from core_agent.errors import CoreError
from core_agent.model import ModelResponse, ScriptedModel
from tests.app_support import UnsandboxedTestLauncher, create_app
from starlette.requests import Request


ORIGIN = "https://agent.example.test"
PASSWORD = "owner-password-for-test-only"
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


class CloudAuthConfigurationTests(unittest.TestCase):
    def test_cloud_development_defaults_to_required_postgres_without_injected_database(self):
        with patch.dict(os.environ, {"CORE_AGENT_ENVIRONMENT": "development", "CORE_AGENT_AUTH_MODE": "cloudru"}, clear=True):
            with self.assertRaises(CoreError) as caught:
                _state()
        self.assertEqual(caught.exception.code, "DATABASE_URL_REQUIRED")

    def test_cloud_development_rejects_either_in_memory_store_before_opening_database(self):
        for setting in ("SESSION_STORAGE_TYPE", "TASK_STORAGE_TYPE"):
            with self.subTest(setting=setting), patch.dict(os.environ, {
                    "CORE_AGENT_ENVIRONMENT": "development", "CORE_AGENT_AUTH_MODE": "cloudru",
                    setting: "in-memory"}, clear=True), patch("core_agent.app.PostgresDatabase.from_environment") as open_database:
                with self.assertRaises(CoreError) as caught:
                    _state()
                self.assertEqual(caught.exception.code, "CONFIG_INVALID")
                open_database.assert_not_called()

    def test_cloud_context_preserves_protocol_without_forwarding_credentials(self):
        actor = Principal("cloudru-platform-proxy", "company", False, True, auth_mode="cloudru")
        request = Request({"type": "http", "headers": [(b"authorization", b"Bearer secret"),
            (b"cookie", b"owner-secret"), (b"x-api-key", b"platform-secret"),
            (b"a2a-version", b"1.0"), (b"traceparent", b"trace")], "principal": actor})
        context = AuthContextBuilder().build(request)
        self.assertEqual(context.state["headers"], {"a2a-version": "1.0", "traceparent": "trace"})
        self.assertEqual(context.tenant, "company")

    def test_cloud_profile_requires_explicit_valid_mode_password_tenant_and_https_url(self):
        base = {"CORE_AGENT_AUTH_MODE": "cloudru", "CORE_AGENT_TENANT_ID": "company",
                "CORE_AGENT_OWNER_PASSWORD": PASSWORD, "AGENT_URL": ORIGIN}
        settings = AuthSettings.from_environment(base.get)
        self.assertEqual(settings.mode, "cloudru")
        self.assertNotIn(PASSWORD, repr(settings))
        alias = AuthSettings.from_environment({**base, "AGENT_URL": "", "URL_AGENT": ORIGIN}.get)
        self.assertEqual(alias.origin, ORIGIN)
        with self.assertRaises(CoreError) as production:
            AuthSettings.from_environment(base.get, production=True)
        self.assertEqual(production.exception.code, "CONFIG_INVALID")
        for name, value in (("CORE_AGENT_AUTH_MODE", "unknown"), ("CORE_AGENT_TENANT_ID", ""),
                            ("CORE_AGENT_OWNER_PASSWORD", "short"), ("AGENT_URL", "http://agent.example.test"),
                            ("AGENT_URL", ORIGIN + "/path"), ("AGENT_URL", ORIGIN + "?key=secret"),
                            ("AGENT_URL", "https://secret@agent.example.test"), ("AGENT_URL", ORIGIN + "\n")):
            with self.subTest(name=name, value=value), self.assertRaises(CoreError) as caught:
                AuthSettings.from_environment({**base, name: value}.get)
            self.assertEqual(caught.exception.code, "CONFIG_INVALID")
            self.assertNotIn(PASSWORD, str(caught.exception))

    def test_cloud_production_rejects_in_memory_tasks_before_allocating_runtime(self):
        values = {"CORE_AGENT_ENVIRONMENT": "production", "CORE_AGENT_AUTH_MODE": "cloudru",
                  "CORE_AGENT_OWNER_PASSWORD": PASSWORD, "CORE_AGENT_TENANT_ID": "company",
                  "AGENT_URL": ORIGIN, "CHAT_WORKSPACE_ROOT": "/unused",
                  "SESSION_STORAGE_TYPE": "postgres", "TASK_STORAGE_TYPE": "in-memory"}
        with patch.dict(os.environ, values, clear=True), patch("core_agent.app._state") as allocate:
            with self.assertRaises(CoreError) as caught:
                create_app(model=ScriptedModel([]))
            self.assertEqual(caught.exception.code, "CONFIG_INVALID")
            allocate.assert_not_called()


class CloudAuthTests(unittest.IsolatedAsyncioTestCase):
    use_postgres = False

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tenant = "cloud-auth-" + uuid.uuid4().hex
        environment = patch.dict(os.environ, {
            "CORE_AGENT_ENVIRONMENT": "development" if self.use_postgres else "test", "CORE_AGENT_AUTH_MODE": "cloudru",
            "CORE_AGENT_TENANT_ID": self.tenant, "CORE_AGENT_OWNER_PASSWORD": PASSWORD,
            "AGENT_URL": ORIGIN, "CORE_AGENT_MEMORY": "disabled",
            "LOCAL_WORKSPACE_ROOT": self.temp.name + "/scratch",
            "CHAT_WORKSPACE_ROOT": self.temp.name + "/chats",
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.app = self.make_app()
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=ORIGIN)
        self.addAsyncCleanup(self.http.aclose)

    def make_app(self):
        model = ScriptedModel([ModelResponse(message="verified") for _ in range(8)])
        model.model = "cloud-auth-test"
        app = create_app(model=model, database=PostgresDatabase(TEST_DATABASE_URL) if self.use_postgres else None)
        self.addCleanup(app.state.close)
        return app

    async def login(self, **kwargs):
        return await self.http.post("/api/auth/login", headers={"Origin": ORIGIN},
                                    json={"password": kwargs.get("password", PASSWORD)})

    async def submit(self, kind, context_id):
        response = await self.http.post(f"/a2a/{kind}/message:send",
            headers={"Origin": ORIGIN, "A2A-Version": "1.0", "X-Tenant": "attacker"},
            json={"message": {"messageId": str(uuid.uuid4()), "contextId": context_id,
                  "role": "ROLE_USER", "parts": [{"text": "Answer briefly"}]}})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["task"]

    async def test_login_cookie_identity_logout_and_expiry(self):
        self.assertEqual((await self.http.get("/api/identity")).status_code, 401)
        response = await self.login()
        self.assertEqual(response.status_code, 200, response.text)
        cookie = response.headers["set-cookie"]
        for attribute in ("__Host-core-agent-owner=", "HttpOnly", "Secure", "Path=/", "SameSite=lax", "Max-Age=28800"):
            self.assertIn(attribute, cookie)
        self.assertNotIn("Domain=", cookie)
        token = self.http.cookies.get("__Host-core-agent-owner")
        identity = (await self.http.get("/api/identity")).json()
        self.assertEqual(identity["role"], "owner")
        self.assertEqual(identity["tenant"], self.tenant)
        self.assertEqual(identity["auth_mode"], "cloudru")
        self.assertNotIn(PASSWORD, str(identity))
        response = await self.http.post("/api/auth/logout", headers={"Origin": ORIGIN})
        self.assertEqual(response.status_code, 200, response.text)
        self.http.cookies.set("__Host-core-agent-owner", token)
        self.assertEqual((await self.http.get("/api/identity")).status_code, 401)
        self.http.cookies.clear()
        await self.login()
        self.app.state.authenticator.clock = lambda: time.time() + 28801
        self.assertEqual((await self.http.get("/api/identity")).status_code, 401)

    async def test_origin_is_exact_and_required_for_every_owner_mutation(self):
        for origin in (None, "null", ORIGIN + ".evil.test", "https://sibling.example.test", ORIGIN + "/"):
            headers = {"Origin": origin} if origin is not None else {}
            response = await self.http.post("/api/auth/login", headers=headers, json={"password": PASSWORD})
            self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual((await self.login()).status_code, 200)
        for path in ("/api/auth/logout", "/api/settings", "/a2a/owner/message:send"):
            response = await self.http.post(path, json={})
            self.assertEqual(response.status_code, 403, (path, response.text))
        response = await self.http.post("/api/auth/login", headers=[("Origin", ORIGIN), ("Origin", ORIGIN)],
                                        json={"password": PASSWORD})
        self.assertEqual(response.status_code, 403)

    async def test_bounded_login_and_tenant_wide_rate_limit(self):
        too_large = await self.http.post("/api/auth/login", headers={"Origin": ORIGIN}, content=b"x" * 4097)
        self.assertEqual(too_large.status_code, 413, too_large.text)
        for _ in range(10):
            wrong = await self.login(password="wrong-password")
            self.assertEqual(wrong.status_code, 401, wrong.text)
        limited = await self.login()
        self.assertEqual(limited.status_code, 429, limited.text)
        self.assertIn("retry-after", limited.headers)
        self.app.state.authenticator.clock = lambda: time.time() + 301
        self.assertEqual((await self.login()).status_code, 200)

    async def test_public_config_and_legacy_routes_and_external_management(self):
        config = await self.http.get("/ui/config")
        self.assertEqual(config.status_code, 200, config.text)
        self.assertEqual(config.json(), {"auth_mode": "cloudru", "external_a2a_url": ORIGIN + "/"})
        self.assertEqual((await self.http.get("/ui/config?auth_mode=keycloak")).status_code, 400)
        for path in ("/unknown", "/.well-known/unknown", "/ui/anything"):
            self.assertEqual((await self.http.get(path)).status_code, 404, path)
        await self.login()
        self.assertEqual((await self.http.get("/api/external-access")).status_code, 404)

    async def test_root_get_and_head_use_real_readiness(self):
        launcher = self.app.state.core_agent.tool_runtime.environment_manager.backend.launcher
        for unhealthy, expected in ((False, 200), (True, 503)):
            launcher._unhealthy = unhealthy
            for path in ("/", "/health/ready"):
                for method in ("GET", "HEAD"):
                    response = await self.http.request(method, path)
                    self.assertEqual(response.status_code, expected, (path, response.text))
                    if method == "HEAD":
                        self.assertEqual(response.content, b"")
        launcher._unhealthy = False
        if self.app.state.database is not None:
            with patch.object(self.app.state.database, "check", side_effect=CoreError("DATABASE_UNAVAILABLE")):
                for path in ("/", "/health/ready"):
                    self.assertEqual((await self.http.get(path)).status_code, 503)

    async def test_root_rpc_and_rest_share_external_task_and_dedup_with_alias(self):
        message = {"messageId": str(uuid.uuid4()), "contextId": "root-chat", "role": "ROLE_USER",
                   "parts": [{"text": "Answer briefly"}]}
        headers = {"A2A-Version": "1.0"}
        response = await self.http.post("/", headers=headers,
            json={"jsonrpc": "2.0", "id": "root", "method": "SendMessage", "params": {"message": message}})
        self.assertEqual(response.status_code, 200, response.text)
        task = response.json()["result"]["task"]
        for prefix in ("", "/a2a/external"):
            duplicate = await self.http.post(prefix + "/message:send", headers=headers, json={"message": message})
            self.assertEqual(duplicate.status_code, 200, duplicate.text)
            self.assertEqual(duplicate.json()["task"]["id"], task["id"])
            known = await self.http.get(prefix + "/tasks/" + task["id"], headers=headers)
            self.assertEqual(known.status_code, 200, known.text)
        self.assertEqual(len(self.app.state.core_agent.model.calls), 1)

    async def test_root_discovery_uses_trusted_origin_and_external_authority(self):
        await self.login()
        for name in ("agent-card.json", "agent.json"):
            card = await self.http.get("/.well-known/" + name, headers={
                "Host": "attacker.invalid", "X-Forwarded-Host": "attacker.invalid",
                "X-Forwarded-Proto": "http"})
            self.assertEqual(card.status_code, 200, card.text)
            self.assertEqual({item["url"] for item in card.json()["supportedInterfaces"]}, {ORIGIN})
            self.assertEqual(set(card.json()["securitySchemes"]), {"cloudruPlatform"})
            self.assertNotIn("attacker.invalid", card.text)

    async def test_platform_mcp_urls_and_headers_never_seed_connections_or_owner_settings(self):
        for headers in ('{"Authorization":"Bearer platform-mcp-secret"}', "not-valid-json"):
            with self.subTest(headers=headers), patch.dict(os.environ, {
                    "MCP_URL": "https://platform.invalid/mcp?key=platform-url-secret",
                    "MCP_HEADERS_JSON": headers, "ENTITY_ID": "platform-entity",
                    "MCP_ALLOWED_SERVERS": "owner-added", "MCP_ALLOWED_TOOLS": "owner-added.search"}), \
                    patch("core_agent.mcp.StreamableHttpMcpConnector.connect", side_effect=AssertionError("unexpected platform connection")) as connect:
                app = self.make_app()
                agent = app.state.core_agent
                self.assertEqual(agent.platform_mcp, ())
                self.assertEqual(agent.mcp_connector.headers, {})
                self.assertIn("owner-added", agent.platform_config.allowed_mcp_servers)
                settings = agent.agent_settings_store.public(agent.agent_settings_store.get(self.tenant), agent)
                self.assertEqual(settings["mcp_servers"], [])
                self.assertNotIn("platform-mcp-secret", str(settings))
                self.assertNotIn("platform-url-secret", str(settings))
                connect.assert_not_called()

    async def test_cloud_public_ui_serves_only_enumerated_build_assets(self):
        build = Path(self.temp.name) / "ui-build"
        build.mkdir()
        (build / "index.html").write_text("<!doctype html><title>Owner login</title>")
        (build / "assets").mkdir()
        (build / "assets/app-Abcd1234.js").write_text("export const fixture = true;")
        (build / "secret.txt").write_text("private")
        with patch("core_agent.ui.UI_DIST", build):
            app = self.make_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as http:
            shell = await http.get("/ui/")
            self.assertEqual(shell.status_code, 200)
            self.assertIn("frame-ancestors 'none'", shell.headers["content-security-policy"])
            self.assertEqual((await http.get("/ui/assets/app-Abcd1234.js")).status_code, 200)
            self.assertEqual((await http.get("/ui/secret.txt")).status_code, 404)
            self.assertEqual((await http.get("/api/identity")).status_code, 401)

    async def test_agent_cards_advertise_owner_cookie_and_platform_bearer(self):
        await self.login()
        for path in ("agent-card.json", "agent.json"):
            owner = await self.http.get("/a2a/owner/.well-known/" + path)
            external = await self.http.get("/a2a/external/.well-known/" + path)
            self.assertEqual(owner.status_code, 200, owner.text)
            self.assertEqual(external.status_code, 200, external.text)
            self.assertNotIn("keycloak", owner.text.lower())
            self.assertNotIn("keycloak", external.text.lower())
            owner_scheme = owner.json()["securitySchemes"]["cloudruOwner"]["apiKeySecurityScheme"]
            self.assertEqual(owner_scheme["location"], "cookie")
            self.assertEqual(owner_scheme["name"], "__Host-core-agent-owner")
            external_scheme = external.json()["securitySchemes"]["cloudruPlatform"]["httpAuthSecurityScheme"]
            self.assertEqual(external_scheme["scheme"], "bearer")
            self.assertEqual(set(owner.json()["securityRequirements"][0]["schemes"]), {"cloudruOwner"})
            self.assertEqual(set(external.json()["securityRequirements"][0]["schemes"]), {"cloudruPlatform"})

    async def test_external_cookie_never_elevates_and_task_enumeration_is_disabled(self):
        await self.login()
        owner = await self.submit("owner", "owner-chat")
        external = await self.submit("external", "external-chat")
        for prefix in ("", "/a2a/external"):
            denied = await self.http.get(prefix + "/tasks/" + owner["id"], headers={"A2A-Version": "1.0"})
            self.assertNotEqual(denied.status_code, 200, denied.text)
            listed = await self.http.get(prefix + "/tasks", headers={"A2A-Version": "1.0"})
            self.assertNotEqual(listed.status_code, 200, listed.text)
            listed_rpc = await self.http.post(prefix + "/", headers={"A2A-Version": "1.0"},
                json={"jsonrpc": "2.0", "id": 1, "method": "ListTasks", "params": {}})
            self.assertNotIn("result", listed_rpc.json())
            self.assertIn("enumeration is disabled", listed_rpc.text)
        self.http.cookies.clear()
        known = await self.http.get("/a2a/external/tasks/" + external["id"], headers={"A2A-Version": "1.0"})
        self.assertEqual(known.status_code, 200, known.text)
        self.assertEqual((await self.http.get("/api/identity", headers={"Authorization": "Bearer arbitrary"})).status_code, 401)


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL is required")
class PostgresCloudAuthTests(CloudAuthTests):
    use_postgres = True

    async def test_failed_recovery_closes_database_and_sandbox_without_atexit_registration(self):
        database = PostgresDatabase(TEST_DATABASE_URL)
        launcher = UnsandboxedTestLauncher()
        model = ScriptedModel([])
        model.model = "cloud-recovery-failure"
        deferred_cleanup = []
        try:
            with patch("core_agent.app.atexit.register", side_effect=deferred_cleanup.append), \
                    patch("core_agent.runtime.CoreAgent.recover_durable_tasks", side_effect=CoreError("RECOVERY_FAILED")):
                with self.assertRaises(CoreError) as caught:
                    create_app(model=model, database=database, sandbox_launcher=launcher)
            self.assertEqual(caught.exception.code, "RECOVERY_FAILED")
            self.assertEqual((database._closed, launcher._closed, len(deferred_cleanup)), (True, True, 0))
        finally:
            for cleanup in deferred_cleanup:
                cleanup()
            database.close()
            launcher.close()

    async def test_cloud_development_opens_real_postgres_from_environment_with_no_injected_database(self):
        with patch.dict(os.environ, {"SESSION_DATABASE_URL": TEST_DATABASE_URL}):
            state = _state()
        try:
            self.assertIsInstance(state["database"], PostgresDatabase)
            self.assertIsInstance(state["tasks"], PostgresTaskStore)
            self.assertIs(state["tasks"].database, state["database"])
            state["database"].verify_schema()
        finally:
            state["database"].close()

    async def test_sessions_and_rate_limit_survive_restart_and_password_rotation_fences_replicas(self):
        await self.login()
        token = self.http.cookies.get("__Host-core-agent-owner")
        second = self.make_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=second), base_url=ORIGIN,
                cookies={"__Host-core-agent-owner": token}) as http:
            self.assertEqual((await http.get("/api/identity")).status_code, 200)
        with self.app.state.database.transaction() as connection:
            rows = connection.execute("SELECT * FROM core_owner_sessions WHERE tenant_id=%s", (self.tenant,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotIn(token, str(rows))
        self.assertNotIn(PASSWORD, str(rows))
        with patch.dict(os.environ, {"CORE_AGENT_OWNER_PASSWORD": PASSWORD + "-rotated"}):
            third = self.make_app()
        self.assertEqual((await self.http.get("/api/identity")).status_code, 401)
        self.assertEqual((await self.login()).status_code, 401)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=third), base_url=ORIGIN) as http:
            response = await http.post("/api/auth/login", headers={"Origin": ORIGIN}, json={"password": PASSWORD + "-rotated"})
            self.assertEqual(response.status_code, 200, response.text)
            with self.assertRaises(CoreError) as stale:
                self.make_app()
            self.assertEqual(stale.exception.code, "CONFIG_INVALID")
            self.assertEqual((await http.get("/api/identity")).status_code, 200)
            for _ in range(8):
                await http.post("/api/auth/login", headers={"Origin": ORIGIN}, json={"password": "wrong"})
            with patch.dict(os.environ, {"CORE_AGENT_OWNER_PASSWORD": PASSWORD + "-rotated"}):
                replacement = self.make_app()
            self.assertEqual((await self.http.get("/api/identity")).status_code, 401)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=replacement), base_url=ORIGIN) as restarted:
                limited = await restarted.post("/api/auth/login", headers={"Origin": ORIGIN}, json={"password": PASSWORD + "-rotated"})
                self.assertEqual(limited.status_code, 429, limited.text)

    async def test_tenant_login_limit_is_atomic_across_replicas(self):
        other = self.make_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=other), base_url=ORIGIN) as http:
            async def wrong(client):
                return await client.post("/api/auth/login", headers={"Origin": ORIGIN}, json={"password": "wrong"})
            responses = await asyncio.gather(*(wrong(self.http if i % 2 else http) for i in range(20)))
        self.assertEqual(sum(response.status_code == 401 for response in responses), 10)
        self.assertEqual(sum(response.status_code == 429 for response in responses), 10)
