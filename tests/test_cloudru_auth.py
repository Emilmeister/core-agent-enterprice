"""Cloud.ru owner sessions and proxy external-scope acceptance proofs."""
import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from a2a.compat.v0_3.types import AgentCard as LegacyAgentCard, Task as LegacyTask
from a2a.types import AgentCard, Task
from google.protobuf.json_format import ParseDict

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
    def test_cloud_defaults_to_required_postgres_without_injected_database(self):
        for environment in ("development", "production"):
            with self.subTest(environment=environment), patch.dict(os.environ, {
                    "CORE_AGENT_ENVIRONMENT": environment, "CORE_AGENT_AUTH_MODE": "cloudru"}, clear=True):
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
        self.assertEqual(AuthSettings.from_environment(base.get, production=True), settings)
        for name, value in (("CORE_AGENT_AUTH_MODE", "unknown"), ("CORE_AGENT_TENANT_ID", ""),
                            ("CORE_AGENT_OWNER_PASSWORD", "short"), ("AGENT_URL", "http://agent.example.test"),
                            ("AGENT_URL", ORIGIN + "/path"), ("AGENT_URL", ORIGIN + "?key=secret"),
                            ("AGENT_URL", "https://secret@agent.example.test"), ("AGENT_URL", ORIGIN + "\n")):
            with self.subTest(name=name, value=value), self.assertRaises(CoreError) as caught:
                AuthSettings.from_environment({**base, name: value}.get)
            self.assertEqual(caught.exception.code, "CONFIG_INVALID")
            self.assertNotIn(PASSWORD, str(caught.exception))

    def test_cloud_production_rejects_ephemeral_stores_before_opening_database(self):
        for setting in ("SESSION_STORAGE_TYPE", "TASK_STORAGE_TYPE"):
            with self.subTest(setting=setting), patch.dict(os.environ, {
                    "CORE_AGENT_ENVIRONMENT": "production", "CORE_AGENT_AUTH_MODE": "cloudru",
                    setting: "in-memory"}, clear=True), patch("core_agent.app.PostgresDatabase.from_environment") as open_database:
                with self.assertRaises(CoreError) as caught:
                    _state()
                self.assertEqual(caught.exception.code, "CONFIG_INVALID")
                open_database.assert_not_called()

    def test_cloud_production_verifies_schema_without_automatic_migration(self):
        for auto_migrate in (None, "false", "true"):
            database = Mock()
            values = {"CORE_AGENT_ENVIRONMENT": "production", "CORE_AGENT_AUTH_MODE": "cloudru"}
            if auto_migrate is not None:
                values["DATABASE_AUTO_MIGRATE"] = auto_migrate
            with self.subTest(auto_migrate=auto_migrate), patch.dict(os.environ, values, clear=True):
                if auto_migrate == "true":
                    with self.assertRaises(CoreError) as caught:
                        _state(database)
                    self.assertEqual(caught.exception.code, "PRODUCTION_AUTO_MIGRATE_FORBIDDEN")
                    database.close.assert_called_once()
                    database.verify_schema.assert_not_called()
                else:
                    state = _state(database)
                    self.assertIs(state["database"], database)
                    self.assertIsInstance(state["tasks"], PostgresTaskStore)
                    database.verify_schema.assert_called_once()
                database.migrate.assert_not_called()

    def test_cloud_production_schema_failure_closes_database(self):
        database = Mock()
        database.verify_schema.side_effect = CoreError("SCHEMA_VERSION_MISMATCH")
        with patch.dict(os.environ, {"CORE_AGENT_ENVIRONMENT": "production", "CORE_AGENT_AUTH_MODE": "cloudru"}, clear=True):
            with self.assertRaises(CoreError) as caught:
                _state(database)
        self.assertEqual(caught.exception.code, "SCHEMA_VERSION_MISMATCH")
        database.close.assert_called_once()
        database.migrate.assert_not_called()


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
                "A2A-Version": "1.0",
                "Host": "attacker.invalid", "X-Forwarded-Host": "attacker.invalid",
                "X-Forwarded-Proto": "http"})
            self.assertEqual(card.status_code, 200, card.text)
            self.assertEqual({item["url"] for item in card.json()["supportedInterfaces"]}, {ORIGIN})
            self.assertEqual(set(card.json()["securitySchemes"]), {"cloudruPlatform"})
            self.assertNotIn("attacker.invalid", card.text)

    async def test_external_cards_keep_version_schemas_separate(self):
        await self.login()
        for prefix in ("", "/a2a/external"):
            canonical = (await self.http.get(prefix + "/.well-known/agent-card.json")).json()
            ParseDict(canonical, AgentCard(), ignore_unknown_fields=False)
            self.assertEqual({(item["protocolBinding"], item["protocolVersion"])
                              for item in canonical["supportedInterfaces"]},
                             {("HTTP+JSON", "1.0"), ("JSONRPC", "1.0")})
            for field in ("url", "preferredTransport", "protocolVersion", "additionalInterfaces"):
                self.assertNotIn(field, canonical)
            modern = await self.http.get(prefix + "/.well-known/agent.json", headers={"A2A-Version": "1.0"})
            self.assertEqual(modern.json(), canonical)
            modern_patch = await self.http.get(prefix + "/.well-known/agent.json", headers={"A2A-Version": "1.0.0"})
            self.assertEqual(modern_patch.json(), canonical)
            for headers in ({}, {"A2A-Version": "0.3"}):
                legacy = await self.http.get(prefix + "/.well-known/agent.json?agentId=platform-agent", headers=headers)
                self.assertEqual(legacy.status_code, 200, legacy.text)
                payload = legacy.json()
                LegacyAgentCard.model_validate(payload, extra="forbid")
                self.assertIn(payload["protocolVersion"], {"0.3", "0.3.0"})
                self.assertEqual(payload["url"], ORIGIN + prefix)
                self.assertEqual(payload["preferredTransport"], "JSONRPC")
                self.assertEqual(set(payload["securitySchemes"]), {"cloudruPlatform"})
                self.assertEqual(payload["securitySchemes"]["cloudruPlatform"]["type"], "http")
                self.assertEqual(payload["security"], [{"cloudruPlatform": []}])
                self.assertNotIn("supportedInterfaces", payload)
                self.assertNotIn("securityRequirements", payload)
                self.assertFalse(any(item["transport"] != "JSONRPC" for item in payload.get("additionalInterfaces", [])))
            unsupported = await self.http.get(prefix + "/.well-known/agent.json", headers={"A2A-Version": "9.0"})
            self.assertGreaterEqual(unsupported.status_code, 400, unsupported.text)
        owner = await self.http.get("/a2a/owner/.well-known/agent.json")
        ParseDict(owner.json(), AgentCard(), ignore_unknown_fields=False)
        self.assertNotIn("protocolVersion", owner.json())

    async def test_legacy_send_get_share_modern_dedup_and_context(self):
        message_id = uuid.uuid4().hex
        legacy_message = {"kind": "message", "messageId": message_id, "contextId": "compat-chat",
                          "role": "user", "parts": [{"kind": "text", "text": "Remember compat-token-31"}]}
        response = await self.http.post("/", json={"jsonrpc": "2.0", "id": "legacy", "method": "message/send",
                                                  "params": {"message": legacy_message}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("result", response.json(), response.text)
        task = response.json()["result"]
        LegacyTask.model_validate(task, extra="forbid")
        self.assertEqual((task["kind"], task["status"]["state"]), ("task", "completed"))
        modern_message = {"messageId": message_id, "contextId": "compat-chat", "role": "ROLE_USER",
                          "parts": [{"text": "Remember compat-token-31"}]}
        duplicate = await self.http.post("/a2a/external/", headers={"A2A-Version": "1.0"},
            json={"jsonrpc": "2.0", "id": "modern", "method": "SendMessage", "params": {"message": modern_message}})
        self.assertEqual(duplicate.json()["result"]["task"]["id"], task["id"], duplicate.text)
        self.assertEqual(len(self.app.state.core_agent.model.calls), 1)
        for prefix in ("", "/a2a/external"):
            known = await self.http.post(prefix + "/", headers={"A2A-Version": "0.3"},
                json={"jsonrpc": "2.0", "id": "get", "method": "tasks/get", "params": {"id": task["id"]}})
            self.assertEqual(known.json()["result"]["id"], task["id"], known.text)
            LegacyTask.model_validate(known.json()["result"], extra="forbid")
        modern_message.update(messageId=uuid.uuid4().hex, parts=[{"text": "Continue the same chat"}])
        followup = await self.http.post("/message:send", headers={"A2A-Version": "1.0"}, json={"message": modern_message})
        self.assertEqual(followup.status_code, 200, followup.text)
        self.assertEqual(followup.json()["task"]["contextId"], task["contextId"])
        self.assertNotEqual(followup.json()["task"]["id"], task["id"])
        self.assertIn("compat-token-31", str(self.app.state.core_agent.model.calls[-1].context))

    async def test_legacy_and_modern_streams_keep_distinct_wire_shapes(self):
        for prefix, method, version, role, parts in (
                ("", "message/stream", None, "user", [{"kind": "text", "text": "Reply"}]),
                ("/a2a/external", "message/stream", "0.3", "user", [{"kind": "text", "text": "Reply"}]),
                ("", "SendStreamingMessage", "1.0", "ROLE_USER", [{"text": "Reply"}])):
            response = await self.http.post(prefix + "/", headers={"A2A-Version": version} if version else {},
                json={"jsonrpc": "2.0", "id": "stream", "method": method,
                      "params": {"message": {"messageId": uuid.uuid4().hex, "role": role, "parts": parts}}})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("text/event-stream", response.headers["content-type"], response.text)
            events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
            self.assertTrue(events, response.text)
            self.assertFalse(any("error" in event for event in events), response.text)
            results = [event["result"] for event in events]
            if version == "1.0":
                tasks = [result["task"] for result in results if "task" in result]
                self.assertTrue(tasks, response.text)
                ParseDict(tasks[0], Task(), ignore_unknown_fields=False)
                self.assertTrue(all("kind" not in result for result in results))
            else:
                tasks = [result for result in results if result.get("kind") == "task"]
                self.assertTrue(tasks, response.text)
                LegacyTask.model_validate(tasks[0], extra="forbid")
                self.assertTrue(all("task" not in result for result in results))
            self.assertIn("verified", response.text)

    async def test_legacy_calls_keep_external_scope_and_do_not_enable_owner_compatibility(self):
        await self.login()
        owner = await self.submit("owner", "private-owner-chat")
        for prefix in ("", "/a2a/external"):
            for method in ("tasks/get", "tasks/cancel", "tasks/resubscribe"):
                denied = await self.http.post(prefix + "/", json={"jsonrpc": "2.0", "id": "private",
                    "method": method, "params": {"id": owner["id"]}})
                payload = (json.loads(next(line[5:] for line in denied.text.splitlines() if line.startswith("data:")))
                           if method == "tasks/resubscribe" else denied.json())
                self.assertEqual(payload["error"]["code"], -32001, denied.text)
                self.assertNotIn("verified", denied.text)
                self.assertNotIn("private-owner-chat", denied.text)
        rejected = await self.http.post("/a2a/owner/", headers={"Origin": ORIGIN, "A2A-Version": "0.3"},
            json={"jsonrpc": "2.0", "id": "owner", "method": "tasks/get", "params": {"id": owner["id"]}})
        self.assertEqual(rejected.json()["error"]["code"], -32601, rejected.text)
        self.assertEqual((await self.http.get("/api/identity")).json()["role"], "owner")
        rest = await self.http.get("/tasks/" + owner["id"], headers={"A2A-Version": "0.3"})
        self.assertGreaterEqual(rest.status_code, 400, rest.text)
        self.http.cookies.clear()
        self.assertEqual((await self.http.get("/api/identity", headers={"A2A-Version": "0.3"})).status_code, 401)

    async def test_legacy_cancel_stops_active_task_and_modern_get_agrees(self):
        started, release = threading.Event(), threading.Event()
        agent = self.app.state.core_agent
        generate = agent.model.generate

        def waiting_model(**kwargs):
            started.set()
            release.wait(10)
            return generate(**kwargs)

        agent.model.generate = waiting_model
        self.addCleanup(release.set)
        created = await self.http.post("/", json={"jsonrpc": "2.0", "id": "start", "method": "message/send",
            "params": {"configuration": {"blocking": False}, "message": {"messageId": uuid.uuid4().hex,
                "contextId": "cancel-compat-chat", "role": "user", "parts": [{"kind": "text", "text": "Begin work"}]}}})
        task = created.json()["result"]
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            signal_cancel = agent.signal_task_cancel

            def release_on_cancellation(task_id):
                signal_cancel(task_id)
                release.set()

            with patch.object(agent, "signal_task_cancel", side_effect=release_on_cancellation):
                canceled = await self.http.post("/a2a/external/", json={"jsonrpc": "2.0", "id": "cancel",
                    "method": "tasks/cancel", "params": {"id": task["id"]}})
            result = canceled.json()["result"]
            LegacyTask.model_validate(result, extra="forbid")
            self.assertEqual((result["id"], result["contextId"], result["status"]["state"]),
                             (task["id"], task["contextId"], "canceled"), canceled.text)
            modern = await self.http.get("/tasks/" + task["id"], headers={"A2A-Version": "1.0"})
            self.assertEqual(modern.status_code, 200, modern.text)
            self.assertEqual((modern.json()["id"], modern.json()["contextId"], modern.json()["status"]["state"]),
                             (task["id"], task["contextId"], "TASK_STATE_CANCELED"), modern.text)
        finally:
            release.set()

    async def test_legacy_files_are_validated_before_decoding_or_admission(self):
        for prefix in ("", "/a2a/external"):
            for method in ("message/send", "message/stream"):
                for file in ({"bytes": "YR==", "name": "private-name.txt"},
                             {"uri": "https://private.example/secret", "name": "private-name.txt"}):
                    response = await self.http.post(prefix + "/", json={"jsonrpc": "2.0", "id": "file",
                        "method": method, "params": {"message": {"kind": "message", "messageId": uuid.uuid4().hex,
                            "role": "user", "parts": [{"kind": "file", "file": file}]}}})
                    self.assertIn("error", response.json(), response.text)
                    self.assertEqual(response.json()["error"]["code"], -32602 if "bytes" in file else -32005, response.text)
                    if "bytes" in file:
                        self.assertIn("INVALID_FILE_ENCODING", response.text)
                    self.assertNotIn("private-name", response.text)
                    self.assertNotIn("private.example", response.text)
        self.assertEqual(len(self.app.state.core_agent.model.calls), 0)

    async def test_legacy_request_body_keeps_deployment_limit(self):
        with patch.dict(os.environ, {"A2A_MAX_REQUEST_BYTES": "524288"}):
            app = self.make_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as http:
            for prefix in ("", "/a2a/external"):
                response = await http.post(prefix + "/", json={"jsonrpc": "2.0", "id": "oversized",
                    "method": "message/send", "params": {"message": {"messageId": uuid.uuid4().hex,
                        "role": "user", "parts": [{"kind": "text", "text": "x" * 524288}]}}})
                self.assertEqual(response.json()["error"]["code"], -32602, response.text)
                self.assertIn("REQUEST_TOO_LARGE", response.text)
        self.assertEqual(len(app.state.core_agent.model.calls), 0)

    async def test_legacy_explicit_version_rejection_and_patch_version_support(self):
        request = {"jsonrpc": "2.0", "id": "version", "method": "message/send", "params": {
            "message": {"messageId": uuid.uuid4().hex, "role": "user", "parts": [{"kind": "text", "text": "Reply"}]}}}
        for headers in ({"A2A-Version": "1.0"}, {"A2A-Version": "0.4"},
                        [("A2A-Version", "0.3"), ("A2A-Version", "0.3")]):
            rejected = await self.http.post("/", headers=headers, json=request)
            self.assertIn("error", rejected.json(), rejected.text)
            self.assertEqual(rejected.json()["error"]["code"], -32009, rejected.text)
        self.assertEqual(len(self.app.state.core_agent.model.calls), 0)
        accepted = await self.http.post("/", headers={"A2A-Version": "0.3.0"}, json=request)
        self.assertEqual(accepted.json()["result"]["status"]["state"], "completed", accepted.text)
        self.assertEqual(len(self.app.state.core_agent.model.calls), 1)

    async def test_legacy_schema_errors_do_not_echo_input_in_response_or_logs(self):
        secret = "private-legacy-schema-sentinel"
        for method in ("message/send", "message/stream"):
            with self.assertLogs(level="DEBUG") as logs:
                response = await self.http.post("/", json={"jsonrpc": "2.0", "id": "invalid", "method": method,
                    "params": {"message": {"messageId": uuid.uuid4().hex, "role": secret,
                        "parts": [{"kind": "text", "text": secret}]}}})
            self.assertEqual(response.json()["error"]["code"], -32602, response.text)
            self.assertNotIn(secret, response.text)
            self.assertNotIn(secret, "\n".join(logs.output))
        self.assertEqual(len(self.app.state.core_agent.model.calls), 0)

    async def test_legacy_unexpected_errors_are_safe_for_json_and_sse(self):
        secret = "private-legacy-error-sentinel"
        handler = self.app.state.a2a_request_handler

        async def broken_stream(*_args, **_kwargs):
            raise RuntimeError(secret)
            yield  # An async iterator that fails before its first event.

        for method in ("tasks/get", "tasks/resubscribe"):
            target = "on_get_task" if method == "tasks/get" else "on_subscribe_to_task"
            failure = {"side_effect": RuntimeError(secret)} if method == "tasks/get" else {"new": broken_stream}
            with patch.object(handler, target, **failure), self.assertLogs(level="DEBUG") as logs:
                response = await self.http.post("/", json={"jsonrpc": "2.0", "id": "failure",
                    "method": method, "params": {"id": "missing"}})
            payload = (json.loads(next(line[5:] for line in response.text.splitlines() if line.startswith("data:")))
                       if method == "tasks/resubscribe" else response.json())
            self.assertEqual(payload["error"]["code"], -32603, response.text)
            self.assertEqual(payload["error"]["message"], "Internal error", response.text)
            self.assertNotIn(secret, response.text)
            self.assertNotIn(secret, "\n".join(logs.output))

    async def test_legacy_raw_file_shares_modern_admission_and_dedup(self):
        message_id = uuid.uuid4().hex
        legacy = await self.http.post("/", json={"jsonrpc": "2.0", "id": "file", "method": "message/send", "params": {
            "message": {"messageId": message_id, "role": "user", "contextId": "file-chat", "parts": [
                {"kind": "text", "text": "Keep attached file"},
                {"kind": "file", "file": {"name": "hello.txt", "mimeType": "text/plain", "bytes": "aGVsbG8="}}]}}})
        self.assertIn("result", legacy.json(), legacy.text)
        task = legacy.json()["result"]
        modern = await self.http.post("/message:send", headers={"A2A-Version": "1.0"}, json={
            "message": {"messageId": message_id, "role": "ROLE_USER", "contextId": "file-chat", "parts": [
                {"text": "Keep attached file"}, {"filename": "hello.txt", "mediaType": "text/plain", "raw": "aGVsbG8="}]}})
        self.assertEqual(modern.json()["task"]["id"], task["id"], modern.text)
        files = list(Path(self.temp.name, "chats").rglob("hello.txt"))
        self.assertTrue(files)
        self.assertTrue(all(file.read_bytes() == b"hello" for file in files))

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
            external = await self.http.get("/a2a/external/.well-known/" + path, headers={"A2A-Version": "1.0"})
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
