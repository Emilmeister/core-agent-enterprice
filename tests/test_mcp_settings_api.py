"""Owner HTTP boundary and actual PostgreSQL persistence of MCP identities."""

import base64
import json
import unittest
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12

from core_agent.agent_settings import AgentSettingsStore
from tests.test_auth import AuthAppTestCase, TEST_DATABASE_URL


class McpSettingsAPITests(AuthAppTestCase):
    automatic_tools = False
    push_encryption_key = "gLgFI-gLRDHqUBOd7HGWKfdBLZuYX-m3t_k78ohw0Aw="

    async def test_encrypted_identity_roundtrip_and_pinned_restart(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "test-client")])
        now = datetime.now(timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
            .sign(key, hashes.SHA256()))
        bundle = pkcs12.serialize_key_and_certificates(b"test", key, certificate, None,
            serialization.BestAvailableEncryption(b"upload-password-canary"))
        server = {"name": "private_mcp", "url": "https://mcp.example.test/", "enabled": True,
            "header_name": "Authorization", "header_value": "Api-Key literal-canary",
            "proxy_url": "https://proxy.example.test:8443", "proxy_username": "proxy-user-canary",
            "proxy_password": "proxy-password-canary",
            "client_certificate": {"format": "pkcs12", "data": base64.b64encode(bundle).decode(),
                                   "password": "upload-password-canary"}}
        endpoint = "/api/agent-settings"
        denied = await self.http.put(endpoint, headers=self.headers("external-a"),
            json={"expected_revision": 0, "mcp_servers": [server]})
        self.assertEqual(denied.status_code, 403)
        saved = await self.http.put(endpoint, headers=self.headers("owner-a"),
            json={"expected_revision": 0, "mcp_servers": [server]})
        self.assertEqual(saved.status_code, 200, saved.text)
        public = saved.json()["mcp_servers"][0]
        self.assertTrue(public["has_client_certificate"])
        self.assertTrue(public["has_proxy_credentials"])
        self.assertEqual(public["certificate_info"]["sha256"], certificate.fingerprint(hashes.SHA256()).hex())
        for secret in ("literal-canary", "proxy-user-canary", "proxy-password-canary", "upload-password-canary", server["client_certificate"]["data"]):
            self.assertNotIn(secret, saved.text)
        agent = self.app.state.core_agent
        tenant = self.app.state.authenticator.settings.tenant
        store = agent.agent_settings_store
        row = store.get(tenant)
        self.assertNotIn("canary", json.dumps(row["config"]))
        self.assertNotIn(b"canary", bytes(row["encrypted_payload"]))
        if self.use_postgres:
            store = AgentSettingsStore(agent.workflow_store, self.push_encryption_key, database=store.database)
        raw = agent.agent_config.to_dict()
        old = store.configure(store.get(tenant, 1), raw, agent.platform_config, agent.platform_mcp, credentials=True)[2][0]
        self.assertEqual(old["headers"], {"Authorization": "Api-Key literal-canary"})
        self.assertEqual(old["proxy_auth"]["password"], "proxy-password-canary")
        identity = old["client_certificate"]
        self.assertEqual(x509.load_pem_x509_certificate(identity["certificate_pem"].encode()), certificate)
        self.assertNotIn("upload-password-canary", json.dumps(identity))
        unchanged = {key: server[key] for key in ("name", "url", "enabled", "header_name")}
        preserved = await self.http.put(endpoint, headers=self.headers("owner-a"),
            json={"expected_revision": 1, "mcp_servers": [unchanged]})
        self.assertEqual(preserved.status_code, 200, preserved.text)
        self.assertEqual(preserved.json()["mcp_servers"], [public])
        invalid = await self.http.put(endpoint, headers=self.headers("owner-a"),
            json={"expected_revision": 2, "mcp_servers": [{**unchanged, "url": "https://other.test/"}]})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(store.get(tenant)["revision"], 2)
        cleared = await self.http.put(endpoint, headers=self.headers("owner-a"),
            json={"expected_revision": 2, "mcp_servers": [{**unchanged, "proxy_url": None,
                "client_certificate": None, "header_value": None}]})
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertFalse(cleared.json()["mcp_servers"][0]["has_client_certificate"])
        self.assertFalse(cleared.json()["mcp_servers"][0]["has_proxy_credentials"])
        restored = store.configure(store.get(tenant, 1), raw, agent.platform_config, agent.platform_mcp, credentials=True)[2][0]
        self.assertEqual(restored, old)


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL is required")
class PostgresMcpSettingsAPITests(McpSettingsAPITests):
    use_postgres = True
