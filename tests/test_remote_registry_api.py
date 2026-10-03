"""Owner registry contract through the authenticated composition root."""

import asyncio
import base64
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from cryptography.fernet import Fernet

from tests.test_auth import AuthAppTestCase, TEST_DATABASE_URL


class RemoteRegistryAPITests(AuthAppTestCase):
    automatic_tools = False
    push_encryption_key = Fernet.generate_key().decode()
    metadata_fields = {"id", "name", "url", "description", "enabled", "header_name", "has_header_value", "revision"}
    values = {"name": "delivery", "url": "https://peer.example.test/a2a", "description": "Delivery agent",
              "enabled": True, "header_name": "Authorization", "header_value": "Bearer registry-test-value"}

    async def create_peer(self, *, token="owner-a", **values):
        response = await self.http.post("/api/remote-agents", headers=self.headers(token),
                                        json={**self.values, **values})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(set(response.json()), self.metadata_fields)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("registry-test-value", response.text)
        return response.json()

    def update_values(self, peer, **values):
        return {**{key: peer[key] for key in ("url", "description", "enabled", "header_name")},
                "expected_revision": peer["revision"], **values}

    async def update_peer(self, peer, *, token="owner-b", **values):
        return await self.http.put("/api/remote-agents/" + peer["id"], headers=self.headers(token),
                                   json=self.update_values(peer, **values))

    async def test_shared_owner_crud_never_returns_secret_and_preserves_pinned_revisions(self):
        peer = await self.create_peer()
        self.assertEqual(peer["revision"], 1)
        self.assertTrue(peer["has_header_value"])
        listed = await self.http.get("/api/remote-agents", headers=self.headers("owner-b"))
        self.assertEqual(listed.json(), {"agents": [peer], "next_cursor": None})
        self.assertEqual(listed.headers["cache-control"], "no-store")
        self.assertNotIn("registry-test-value", listed.text)
        changed = await self.update_peer(peer, header_name="X-API-Key", url="https://other.example.test/tasks")
        self.assertEqual(changed.status_code, 200, changed.text)
        current = changed.json()
        self.assertEqual(current["revision"], 2)
        self.assertEqual(current["name"], peer["name"])
        registry = self.app.state.remote_registry_store
        tenant = self.app.state.authenticator.settings.tenant
        self.assertEqual(registry.get_revision(tenant, peer["id"], 1), peer)
        self.assertEqual(registry.resolve_headers(tenant, peer["id"], 1),
                         {"Authorization": self.values["header_value"]})
        self.assertEqual(registry.resolve_headers(tenant, peer["id"], 2),
                         {"X-API-Key": self.values["header_value"]})
        cleared = await self.update_peer(current, header_value=None)
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertFalse(cleared.json()["has_header_value"])
        disabled = await self.http.request("DELETE", "/api/remote-agents/" + peer["id"],
            headers=self.headers("owner-a"), json={"expected_revision": 3})
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertEqual(disabled.json()["revision"], 4)
        self.assertFalse(disabled.json()["enabled"])
        self.assertEqual(registry.resolve_headers(tenant, peer["id"], 4), {})
        self.assertEqual(registry.get_revision(tenant, peer["id"], 1), peer)
        for response in (changed, cleared, disabled):
            self.assertEqual(set(response.json()), self.metadata_fields)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertNotIn("registry-test-value", response.text)
        self.assertFalse(self.model.calls)

    async def test_external_dual_role_and_unauthenticated_callers_cannot_read_or_mutate(self):
        self.tokens["dual-role"] = {**self.tokens["owner-a"],
            "realm_access": {"roles": ["agent-owner", "agent-external"]}}
        for token, status in ((None, 401), ("external-a", 403), ("external-b", 403), ("dual-role", 403)):
            for method, path, payload in (("GET", "/api/remote-agents", None),
                ("POST", "/api/remote-agents", self.values),
                ("PUT", "/api/remote-agents/missing", {}), ("DELETE", "/api/remote-agents/missing", {})):
                with self.subTest(token=token, method=method):
                    response = await self.http.request(method, path, headers=self.headers(token) if token else {},
                                                        json=payload)
                    self.assertEqual(response.status_code, status, response.text)
                    self.assertEqual(response.headers["cache-control"], "no-store")
        response = await self.http.get("/api/remote-agents", headers=self.headers("owner-a"))
        self.assertEqual(response.json(), {"agents": [], "next_cursor": None})
        self.assertFalse(self.model.calls)

    async def test_cas_conflicts_and_disabled_names_remain_reserved(self):
        peer = await self.create_peer()
        responses = await asyncio.gather(self.update_peer(peer, description="Owner A"),
                                          self.update_peer(peer, token="owner-a", description="Owner B"))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        conflict = next(response for response in responses if response.status_code == 409)
        self.assertEqual(conflict.json()["error"]["code"], "REMOTE_AGENT_CONFLICT")
        current = next(response.json() for response in responses if response.status_code == 200)
        renamed = await self.update_peer(current, name="replacement")
        self.assertEqual(renamed.status_code, 400, renamed.text)
        disabled = await self.http.request("DELETE", "/api/remote-agents/" + peer["id"],
            headers=self.headers("owner-a"), json={"expected_revision": 2})
        self.assertEqual(disabled.status_code, 200, disabled.text)
        stale = await self.http.request("DELETE", "/api/remote-agents/" + peer["id"],
            headers=self.headers("owner-b"), json={"expected_revision": 2})
        self.assertEqual(stale.status_code, 409, stale.text)
        duplicate = await self.http.post("/api/remote-agents", headers=self.headers("owner-b"), json=self.values)
        self.assertEqual(duplicate.status_code, 409, duplicate.text)
        self.assertNotIn("registry-test-value", conflict.text + duplicate.text)

    async def test_delete_connection_hides_shared_registration_and_reuses_name_without_old_identity(self):
        peer = await self.create_peer()
        path = "/api/remote-agents/" + peer["id"] + "/connection"
        for token, status in ((None, 401), ("external-a", 403), ("dual-role", 403)):
            if token == "dual-role":
                self.tokens[token] = {**self.tokens["owner-a"], "realm_access": {"roles": ["agent-owner", "agent-external"]}}
            response = await self.http.request("DELETE", path, headers=self.headers(token) if token else {},
                                               json={"expected_revision": 1})
            self.assertEqual(response.status_code, status, response.text)
        for payload in ({}, {"expected_revision": True}, {"expected_revision": 1, "extra": 1}):
            response = await self.http.request("DELETE", path, headers=self.headers("owner-a"), json=payload)
            self.assertEqual(response.status_code, 400, response.text)
        response = await self.http.request("DELETE", path, headers=self.headers("owner-a"), json={"expected_revision": 2})
        self.assertEqual(response.status_code, 409, response.text)
        response = await self.http.request("DELETE", path, headers=self.headers("owner-b"), json={"expected_revision": 1})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(set(response.json()), self.metadata_fields)
        self.assertFalse(response.json()["enabled"])
        self.assertFalse(response.json()["has_header_value"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("registry-test-value", response.text)
        for token in ("owner-a", "owner-b"):
            listed = await self.http.get("/api/remote-agents", headers=self.headers(token))
            self.assertEqual(listed.json(), {"agents": [], "next_cursor": None})
        self.assertEqual((await self.update_peer(peer, enabled=True)).status_code, 404)
        self.assertEqual((await self.http.request("DELETE", path, headers=self.headers("owner-a"), json={"expected_revision": 1})).status_code, 404)
        replacement = await self.create_peer()
        self.assertNotEqual(replacement["id"], peer["id"])
        self.assertEqual(replacement["revision"], 1)
        registry = self.app.state.remote_registry_store
        tenant = self.app.state.authenticator.settings.tenant
        self.assertEqual(registry.get_revision(tenant, peer["id"], 1), peer)
        self.assertEqual(registry.resolve_headers(tenant, peer["id"], 1), {"Authorization": self.values["header_value"]})
        self.assertFalse(self.model.calls)

    async def test_company_ids_and_cursors_do_not_cross_scope(self):
        peers = [await self.create_peer(name=name) for name in ("first", "second")]
        first = await self.http.get("/api/remote-agents?limit=1", headers=self.headers("owner-a"))
        cursor = first.json()["next_cursor"]
        authenticator = self.app.state.authenticator
        with patch.object(authenticator, "settings", replace(authenticator.settings, tenant="other-company")):
            other = await self.http.get("/api/remote-agents", headers=self.headers("owner-b"))
            self.assertEqual(other.json(), {"agents": [], "next_cursor": None})
            for response in (await self.update_peer(peers[0]),
                await self.http.request("DELETE", "/api/remote-agents/" + peers[0]["id"],
                    headers=self.headers("owner-a"), json={"expected_revision": 1})):
                self.assertEqual(response.status_code, 404, response.text)
                self.assertEqual(response.json()["error"]["code"], "REMOTE_AGENT_NOT_FOUND")
            for value in (cursor, base64.urlsafe_b64encode(json.dumps(
                    ["remote-agents", "other-company", peers[0]["id"]]).encode()).decode()):
                response = await self.http.get("/api/remote-agents", params={"cursor": value},
                                               headers=self.headers("owner-a"))
                self.assertEqual(response.status_code, 400, response.text)

    async def test_pagination_is_stable_across_updates_and_rejects_bad_queries(self):
        peers = sorted([await self.create_peer(name=name) for name in ("one", "two", "three")],
                       key=lambda peer: peer["id"])
        response = await self.http.get("/api/remote-agents?limit=1", headers=self.headers("owner-a"))
        self.assertEqual(response.json()["agents"], peers[:1])
        cursor = response.json()["next_cursor"]
        self.assertEqual((await self.update_peer(peers[0], enabled=False)).status_code, 200)
        second = await self.http.get("/api/remote-agents", params={"limit": "1", "cursor": cursor},
                                     headers=self.headers("owner-b"))
        self.assertEqual(second.json()["agents"], peers[1:2])
        third = await self.http.get("/api/remote-agents", params={"limit": "1", "cursor": second.json()["next_cursor"]},
                                    headers=self.headers("owner-a"))
        self.assertEqual(third.json(), {"agents": peers[2:], "next_cursor": None})
        for query in ("limit=0", "limit=101", "limit=true", "limit=1&limit=2", "cursor=invalid",
                      "cursor=", "tenant_id=other", "limit=-1", "limit=9999"):
            response = await self.http.get("/api/remote-agents?" + query, headers=self.headers("owner-a"))
            self.assertEqual(response.status_code, 400, response.text)
        tenant = self.app.state.authenticator.settings.tenant
        for namespace, value in (("chats", peers[0]["id"]), ("remote-agents", "\0"),
                                  ("remote-agents", "\ud800"), ("remote-agents", 1),
                                  ("remote-agents", "missing-peer")):
            cursor = base64.urlsafe_b64encode(json.dumps([namespace, tenant, value]).encode()).decode()
            response = await self.http.get("/api/remote-agents", params={"cursor": cursor},
                                           headers=self.headers("owner-a"))
            self.assertEqual(response.status_code, 400, response.text)
        self.assertFalse(self.model.calls)

    async def test_invalid_values_and_schema_are_rejected_without_secret_or_mutation(self):
        for values in ({"url": "http://peer.example.test/a2a"}, {"url": "https://user:password@peer.example.test"},
            {"url": "https://peer.example.test?token=secret"}, {"url": "https://peer.example.test#fragment"},
            {"url": "https://peer.example.test:0"}, {"name": "bad name"}, {"enabled": 1},
            {"description": "text\0tail"}, {"header_name": "Host"}, {"header_name": "A2A-Version"},
            {"header_value": "Bearer registry-test-value\r\nX-Injected: yes"}, {"header_value": ""},
            {"header_value": "\ud800"}):
            response = await self.http.post("/api/remote-agents", headers=self.headers("owner-a"),
                                            content=json.dumps({**self.values, **values}))
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(response.json()["error"]["code"], "REMOTE_AGENT_INVALID")
            self.assertNotIn("registry-test-value", response.text)
        for payload in ({}, {**self.values, "tenant_id": "other"}, {**self.values, "id": "chosen"}):
            response = await self.http.post("/api/remote-agents", headers=self.headers("owner-a"), json=payload)
            self.assertEqual(response.status_code, 400, response.text)
        for content in ('{"name":"one","name":"two"}', '{', '[]'):
            response = await self.http.post("/api/remote-agents", headers=self.headers("owner-a"), content=content)
            self.assertEqual(response.status_code, 400, response.text)
        peer = await self.create_peer()
        for method, path, payload in (("POST", "/api/remote-agents", {**self.values, "name": "new"}),
            ("PUT", "/api/remote-agents/" + peer["id"], self.update_values(peer)),
            ("DELETE", "/api/remote-agents/" + peer["id"], {"expected_revision": 1})):
            response = await self.http.request(method, path + "?tenant_id=other", headers=self.headers("owner-a"),
                                                json=payload)
            self.assertEqual(response.status_code, 400, response.text)
        for revision in (True, 0, 1.5, "1"):
            response = await self.update_peer(peer, expected_revision=revision)
            self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual((await self.http.get("/api/remote-agents", headers=self.headers("owner-a"))).json()["agents"],
                         [peer])

    async def test_malformed_path_ids_are_rejected_before_lookup(self):
        peer = await self.create_peer()
        for method, payload in (("PUT", self.update_values(peer)), ("DELETE", {"expected_revision": 1})):
            response = await self.http.request(method, "/api/remote-agents/%00", headers=self.headers("owner-a"),
                                               json=payload)
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(response.json()["error"]["code"], "REMOTE_AGENT_INVALID")
            self.assertEqual(response.headers["cache-control"], "no-store")
            missing = await self.http.request(method, "/api/remote-agents/missing-safe-id",
                                              headers=self.headers("owner-a"), json=payload)
            self.assertEqual(missing.status_code, 404, missing.text)
        self.assertEqual((await self.http.get("/api/remote-agents", headers=self.headers("owner-b"))).json()["agents"],
                         [peer])

    async def test_secret_operations_fail_closed_without_key_but_metadata_and_clear_work(self):
        peer = await self.create_peer()
        registry = self.app.state.remote_registry_store
        with patch.object(registry, "_cipher", None):
            for response in (await self.update_peer(peer), await self.update_peer(peer, header_value="replacement"),
                await self.http.request("DELETE", "/api/remote-agents/" + peer["id"],
                    headers=self.headers("owner-a"), json={"expected_revision": 1})):
                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(response.json()["error"]["code"], "REMOTE_AGENT_CREDENTIAL_UNAVAILABLE")
                self.assertNotIn("registry-test-value", response.text)
            metadata = await self.http.get("/api/remote-agents", headers=self.headers("owner-b"))
            self.assertEqual(metadata.json()["agents"], [peer])
            cleared = await self.update_peer(peer, header_value=None)
            self.assertEqual(cleared.status_code, 200, cleared.text)
            self.assertFalse(cleared.json()["has_header_value"])

    async def test_remote_settings_are_revisioned_and_legacy_updates_preserve_them(self):
        defaults = await self.http.get("/api/settings", headers=self.headers("owner-a"))
        self.assertEqual(defaults.json()["remote_timeout_seconds"], 86400)
        self.assertEqual(defaults.json()["remote_poll_interval_seconds"], 300)
        legacy = {"hitl_timeout_seconds": 31, "owner_answer_timeout_seconds": 32,
                  "guardrails_timeout_seconds": 33}
        updated = await self.http.put("/api/settings", headers=self.headers("owner-b"),
            json={**legacy, "expected_revision": 0, "remote_timeout_seconds": 1234, "remote_poll_interval_seconds": 50})
        self.assertEqual(updated.status_code, 200, updated.text)
        old_client = await self.http.put("/api/settings", headers=self.headers("owner-a"),
                                         json={**legacy, "expected_revision": 1})
        self.assertEqual(old_client.status_code, 200, old_client.text)
        self.assertEqual(old_client.json()["remote_timeout_seconds"], 1234)
        self.assertEqual(old_client.json()["remote_poll_interval_seconds"], 50)
        for values in ({"remote_timeout_seconds": True}, {"remote_timeout_seconds": 0},
                       {"remote_poll_interval_seconds": 2147483648}, {"remote_poll_interval_seconds": "300"}):
            response = await self.http.put("/api/settings", headers=self.headers("owner-a"),
                json={**legacy, "expected_revision": 2, **values})
            self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual((await self.http.get("/api/settings", headers=self.headers("owner-b"))).json()["revision"], 2)


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL is required")
class PostgresRemoteRegistryAPITests(RemoteRegistryAPITests):
    use_postgres = True
