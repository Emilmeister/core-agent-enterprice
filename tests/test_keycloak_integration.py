"""Real Keycloak smoke in an isolated local realm; no application-issued keys."""
import os
import unittest
import uuid
from urllib.parse import urlsplit

import httpx

from core_agent.auth import AuthSettings, KeycloakAuthenticator
from core_agent.errors import CoreError


KEYCLOAK_URL = os.environ.get("TEST_KEYCLOAK_URL", "").rstrip("/")


@unittest.skipUnless(KEYCLOAK_URL, "TEST_KEYCLOAK_URL is required")
class KeycloakIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_lived_service_token_rotation_and_revocation(self):
        self.assertIn(urlsplit(KEYCLOAK_URL).hostname, {"127.0.0.1", "localhost", "::1"})
        realm = "core-agent-test-" + uuid.uuid4().hex
        async with httpx.AsyncClient(base_url=KEYCLOAK_URL, timeout=20, trust_env=False) as http:
            login = await http.post("/realms/master/protocol/openid-connect/token", data={
                "client_id": "admin-cli", "grant_type": "password",
                "username": os.environ["TEST_KEYCLOAK_ADMIN"],
                "password": os.environ["TEST_KEYCLOAK_ADMIN_PASSWORD"],
            })
            self.assertEqual(login.status_code, 200)
            headers = {"Authorization": "Bearer " + login.json()["access_token"]}
            secret = "test-only-" + uuid.uuid4().hex
            role_mapper = {
                "name": "realm-roles", "protocol": "openid-connect",
                "protocolMapper": "oidc-usermodel-realm-role-mapper",
                "config": {"multivalued": "true", "claim.name": "realm_access.roles",
                           "jsonType.label": "String", "access.token.claim": "true",
                           "introspection.token.claim": "true"},
            }
            audience_mapper = {
                "name": "agent-audience", "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper",
                "config": {"included.custom.audience": "company-agent",
                           "access.token.claim": "true", "introspection.token.claim": "true"},
            }
            response = await http.post("/admin/realms", headers=headers, json={
                "realm": realm, "enabled": True, "accessTokenLifespan": 31536000,
                "ssoSessionMaxLifespan": 31536000,
                "roles": {"realm": [{"name": "agent-external"}]},
                "clients": [
                    {"clientId": "introspection", "enabled": True, "publicClient": False,
                     "secret": secret, "protocol": "openid-connect"},
                    {"clientId": "external", "enabled": True, "publicClient": False,
                     "secret": secret, "protocol": "openid-connect", "fullScopeAllowed": True,
                     "serviceAccountsEnabled": True, "protocolMappers": [role_mapper, audience_mapper]},
                ],
            })
            self.assertEqual(response.status_code, 201, response.text)
            admin = "/admin/realms/" + realm
            try:
                clients = await http.get(admin + "/clients", params={"clientId": "external"}, headers=headers)
                client = clients.json()[0]
                account = await http.get(admin + f"/clients/{client['id']}/service-account-user", headers=headers)
                role = await http.get(admin + "/roles/agent-external", headers=headers)
                mapped = await http.post(
                    admin + f"/users/{account.json()['id']}/role-mappings/realm",
                    headers=headers, json=[role.json()],
                )
                self.assertEqual(mapped.status_code, 204)
                issuer = KEYCLOAK_URL + "/realms/" + realm
                authenticator = KeycloakAuthenticator(AuthSettings(
                    issuer, "introspection", secret, "company-agent", "integration-company",
                ))
                identities = []
                for _ in range(2):
                    issued = await http.post(issuer + "/protocol/openid-connect/token", data={
                        "client_id": "external", "client_secret": secret,
                        "grant_type": "client_credentials",
                    })
                    self.assertEqual(issued.status_code, 200)
                    payload = issued.json()
                    self.assertGreaterEqual(payload["expires_in"], 30 * 24 * 3600)
                    self.assertLessEqual(payload["expires_in"], 366 * 24 * 3600)
                    token = payload["access_token"]
                    identity = await authenticator.authenticate(token)
                    self.assertTrue(identity.is_external)
                    self.assertFalse(identity.is_owner)
                    identities.append(identity.owner_id)
                self.assertEqual(identities[0], identities[1])
                client["enabled"] = False
                disabled = await http.put(admin + f"/clients/{client['id']}", headers=headers, json=client)
                self.assertEqual(disabled.status_code, 204)
                with self.assertRaises(CoreError) as caught:
                    await authenticator.authenticate(token)
                self.assertEqual(caught.exception.code, "AUTHENTICATION_REQUIRED")
            finally:
                removed = await http.delete(admin, headers=headers)
                self.assertEqual(removed.status_code, 204)
