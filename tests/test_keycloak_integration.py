"""Real Keycloak smoke in an isolated local realm; no application-issued keys."""
import asyncio
import os
import unittest
import uuid
from urllib.parse import urlsplit

import httpx

from core_agent.auth import AuthSettings, KeycloakAuthenticator, Principal
from core_agent.external_access import ExternalAccess
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
                    {"clientId": "company-agent", "enabled": True, "protocol": "openid-connect"},
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
                # Prove provisioning and 30-day expiry despite a shorter online
                # SSO lifetime, using actual Keycloak rather than a fake token.
                import time
                updated = await http.put(admin, headers=headers, json={"ssoSessionMaxLifespan": 36000, "notBefore": int(time.time()) - 120})
                self.assertEqual(updated.status_code, 204)
                access = ExternalAccess(authenticator.settings)
                owner = Principal("fixture-owner", "integration-company", True, False)
                request = {"name": "External integration", "days": 30, "request_id": str(uuid.uuid4())}
                granted = await access.execute(owner, headers["Authorization"], "POST", payload=request)
                self.assertEqual(granted["expires_in"], 30 * 86400)
                external = await authenticator.authenticate(granted["access_token"])
                self.assertTrue(external.is_external)
                self.assertFalse(external.is_owner)
                listing = await access.execute(owner, headers["Authorization"], "GET")
                self.assertEqual(listing["total"], 2)  # Includes the manually created external account.
                self.assertNotIn(granted["access_token"], str(listing))
                # Discovery must also handle audience mappers in default scopes
                # and external/owner roles granted through the API client.
                scope = "legacy-audience"
                created_scope = await http.post(admin + "/client-scopes", headers=headers, json={
                    "name": scope, "protocol": "openid-connect", "protocolMappers": [audience_mapper]})
                self.assertEqual(created_scope.status_code, 201)
                scope_id = created_scope.headers["location"].rsplit("/", 1)[1]
                self.assertEqual((await http.put(admin + f"/clients/{client['id']}/default-client-scopes/{scope_id}", headers=headers)).status_code, 204)
                client["protocolMappers"] = [role_mapper]
                self.assertEqual((await http.put(admin + f"/clients/{client['id']}", headers=headers, json=client)).status_code, 204)
                api_client = (await http.get(admin + "/clients", params={"clientId": "company-agent"}, headers=headers)).json()[0]
                user_path = admin + f"/users/{account.json()['id']}/role-mappings"
                api_path = admin + f"/clients/{api_client['id']}/roles"
                for name in ("agent-external", "agent-owner"):
                    self.assertEqual((await http.post(api_path, headers=headers, json={"name": name})).status_code, 201)
                external_role = (await http.get(api_path + "/agent-external", headers=headers)).json()
                owner_role = (await http.get(api_path + "/agent-owner", headers=headers)).json()
                resource_path = user_path + f"/clients/{api_client['id']}"
                self.assertEqual((await http.request("DELETE", user_path + "/realm", headers=headers, json=[role.json()])).status_code, 204)
                self.assertEqual((await http.post(resource_path, headers=headers, json=[external_role])).status_code, 204)
                self.assertEqual((await access.execute(owner, headers["Authorization"], "GET"))["total"], 2)
                self.assertEqual((await http.post(resource_path, headers=headers, json=[owner_role])).status_code, 204)
                self.assertEqual((await access.execute(owner, headers["Authorization"], "GET"))["total"], 1)
                self.assertEqual((await http.request("DELETE", resource_path, headers=headers, json=[owner_role])).status_code, 204)
                with self.assertRaises(CoreError) as repeated:
                    await access.execute(owner, headers["Authorization"], "POST", payload=request)
                self.assertEqual(repeated.exception.code, "EXTERNAL_ACCESS_ALREADY_ISSUED")
                replaced = await access.execute(owner, headers["Authorization"], "POST", granted["account"]["id"], {"days": 1})
                replacement = await authenticator.authenticate(replaced["access_token"])
                self.assertEqual(external.owner_id, replacement.owner_id)
                with self.assertRaises(CoreError):
                    await authenticator.authenticate(granted["access_token"])
                await access.execute(owner, headers["Authorization"], "DELETE", granted["account"]["id"])
                with self.assertRaises(CoreError):
                    await authenticator.authenticate(replaced["access_token"])
                # Legacy clients may opt into persisted refresh sessions. A
                # one-time token must survive their shorter offline idle limit.
                updated = await http.put(admin, headers=headers, json={"offlineSessionIdleTimeout": 3})
                self.assertEqual(updated.status_code, 204)
                client["attributes"] = {"client_credentials.use_refresh_token": "true"}
                updated = await http.put(admin + f"/clients/{client['id']}", headers=headers, json=client)
                self.assertEqual(updated.status_code, 204)
                legacy = await access.execute(owner, headers["Authorization"], "POST", client["id"], {"days": 1})
                await asyncio.sleep(4)
                self.assertTrue((await authenticator.authenticate(legacy["access_token"])).is_external)
                client = (await http.get(admin + f"/clients/{client['id']}", headers=headers)).json()
                self.assertEqual(client["attributes"]["client_credentials.use_refresh_token"], "false")
                client["enabled"] = False
                disabled = await http.put(admin + f"/clients/{client['id']}", headers=headers, json=client)
                self.assertEqual(disabled.status_code, 204)
                with self.assertRaises(CoreError) as caught:
                    await authenticator.authenticate(token)
                self.assertEqual(caught.exception.code, "AUTHENTICATION_REQUIRED")
            finally:
                removed = await http.delete(admin, headers=headers)
                self.assertEqual(removed.status_code, 204)
