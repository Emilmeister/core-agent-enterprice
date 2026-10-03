"""Owner-session Keycloak administration; issued credentials are never persisted."""
import asyncio
import hashlib
import json
import math
import time
import uuid
from urllib.parse import quote

import httpx

from .auth import KeycloakAuthenticator
from .errors import CoreError


PREFIX = "agent.access."


class ExternalAccess:
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.transport = transport
        self.authenticator = KeycloakAuthenticator(settings, transport=transport)
        origin, separator, realm = settings.issuer.rpartition("/realms/")
        if not separator or not realm or "/" in realm:
            raise CoreError("CONFIG_INVALID", "Keycloak issuer must identify a realm")
        self.admin_url = origin + "/admin/realms/" + realm
        # ponytail: one mutation lock per Pod; owner provisioning has low throughput.
        self.lock = asyncio.Lock()

    async def _request(self, http, method, path, *, allowed=(200,), **kwargs):
        try:
            async with http.stream(method, path, **kwargs) as response:
                if response.status_code in {401, 403}:
                    raise CoreError("KEYCLOAK_ADMIN_ACCESS_DENIED")
                if response.status_code not in allowed:
                    raise CoreError("KEYCLOAK_ADMIN_UNAVAILABLE")
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    body.extend(chunk)
                    if len(body) > 2 * 1024 * 1024:
                        raise CoreError("KEYCLOAK_ADMIN_UNAVAILABLE")
                value = json.loads(body) if body else None
                if value is not None and not isinstance(value, (dict, list)):
                    raise CoreError("KEYCLOAK_ADMIN_UNAVAILABLE")
                return response.status_code, value
        except (httpx.HTTPError, ValueError, UnicodeError, RecursionError):
            raise CoreError("KEYCLOAK_ADMIN_UNAVAILABLE") from None

    async def _admin(self, http, method, path, **kwargs):
        return (await self._request(http, method, self.admin_url + path, **kwargs))[1]

    @staticmethod
    def _attributes(client):
        value = client.get("attributes", {})
        if not isinstance(value, dict):
            raise CoreError("KEYCLOAK_ADMIN_UNAVAILABLE")
        return value

    def _public(self, client):
        attrs = self._attributes(client)
        def timestamp(key):
            value = attrs.get(PREFIX + key)
            try:
                number = float(value)
                return number if math.isfinite(number) and number >= 0 else None
            except (ValueError, TypeError):
                return None
        expiry = timestamp("expires_at")
        state = attrs.get(PREFIX + "state")
        status = ("revoked" if not client.get("enabled", True) else
                  "pending" if state == "pending" else
                  "expired" if expiry is not None and expiry <= time.time() else "active")
        return {"id": client["id"], "name": client.get("name") or client["clientId"],
                "status": status, "created_at": timestamp("created_at"),
                "issued_at": timestamp("issued_at"), "expires_at": expiry}

    async def _eligible(self, http, client):
        if (not isinstance(client, dict) or not isinstance(client.get("id"), str)
                or not isinstance(client.get("clientId"), str)
                or not isinstance(client.get("name", ""), str)):
            raise CoreError("KEYCLOAK_ADMIN_UNAVAILABLE")
        attrs = self._attributes(client)
        marked = attrs.get(PREFIX + "tenant")
        if marked is not None:
            return (marked == self.settings.tenant
                    and attrs.get(PREFIX + "audience") == self.settings.audience
                    and client.get("serviceAccountsEnabled") is True)
        if client.get("serviceAccountsEnabled") is not True:
            return False
        scopes = await self._admin(http, "GET", "/clients/" + quote(client["id"], safe="") + "/default-client-scopes")
        if not isinstance(scopes, list):
            raise CoreError("KEYCLOAK_ADMIN_UNAVAILABLE")
        mappers = client.get("protocolMappers", [])
        if not isinstance(mappers, list):
            raise CoreError("KEYCLOAK_ADMIN_UNAVAILABLE")
        mappers = list(mappers)
        for scope in scopes:
            if not isinstance(scope, dict) or not isinstance(scope.get("id"), str):
                raise CoreError("KEYCLOAK_ADMIN_UNAVAILABLE")
            scope_mappers = await self._admin(http, "GET", "/client-scopes/" + quote(scope["id"], safe="") + "/protocol-mappers/models")
            if not isinstance(scope_mappers, list):
                raise CoreError("KEYCLOAK_ADMIN_UNAVAILABLE")
            mappers.extend(scope_mappers)
        audience_matches = False
        for mapper in mappers:
            if not isinstance(mapper, dict) or not isinstance(mapper.get("config", {}), dict):
                raise CoreError("KEYCLOAK_ADMIN_UNAVAILABLE")
            config = mapper.get("config", {})
            audience_matches |= mapper.get("protocolMapper") == "oidc-audience-mapper" and (
                config.get("included.client.audience") == self.settings.audience
                or config.get("included.custom.audience") == self.settings.audience)
        if not audience_matches:
            return False
        user = await self._admin(http, "GET", "/clients/" + quote(client["id"], safe="") + "/service-account-user")
        if not isinstance(user, dict) or not isinstance(user.get("id"), str):
            raise CoreError("KEYCLOAK_ADMIN_UNAVAILABLE")
        roles = await self._admin(http, "GET", "/users/" + quote(user["id"], safe="") + "/role-mappings/realm/composite")
        names = self._role_names(roles)
        audience_clients = await self._admin(http, "GET", "/clients", params={"clientId": self.settings.audience})
        if not isinstance(audience_clients, list) or len(audience_clients) > 1:
            raise CoreError("KEYCLOAK_ADMIN_UNAVAILABLE")
        if audience_clients:
            audience_client = audience_clients[0]
            if not isinstance(audience_client, dict) or not isinstance(audience_client.get("id"), str):
                raise CoreError("KEYCLOAK_ADMIN_UNAVAILABLE")
            resource_roles = await self._admin(http, "GET", "/users/" + quote(user["id"], safe="")
                + "/role-mappings/clients/" + quote(audience_client["id"], safe="") + "/composite")
            names.update(self._role_names(resource_roles))
        return self.settings.external_role in names and self.settings.owner_role not in names

    @staticmethod
    def _role_names(roles):
        if not isinstance(roles, list) or not all(
            isinstance(role, dict) and isinstance(role.get("name"), str) for role in roles
        ):
            raise CoreError("KEYCLOAK_ADMIN_UNAVAILABLE")
        return {role["name"] for role in roles}

    async def _client(self, http, identifier):
        try:
            if str(uuid.UUID(identifier)) != identifier:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise CoreError("EXTERNAL_ACCESS_NOT_FOUND") from None
        status, client = await self._request(http, "GET", self.admin_url + "/clients/" + identifier,
                                        allowed=(200, 404))
        if status == 404 or client is None or not await self._eligible(http, client):
            raise CoreError("EXTERNAL_ACCESS_NOT_FOUND")
        return client

    async def _list(self, http):
        result = []
        for first in range(0, 10000, 100):
            rows = await self._admin(http, "GET", "/clients", params={"first": first, "max": 100})
            if not isinstance(rows, list):
                raise CoreError("KEYCLOAK_ADMIN_UNAVAILABLE")
            for client in rows:
                if await self._eligible(http, client):
                    result.append(self._public(client))
            if len(rows) < 100:
                return {"accounts": sorted(result, key=lambda row: row["name"].casefold()), "total": len(result)}
        raise CoreError("KEYCLOAK_ADMIN_UNAVAILABLE")

    def _validate(self, payload, *, creating):
        if type(payload.get("days")) is not int or not 1 <= payload["days"] <= 365:
            raise CoreError("REQUEST_INVALID")
        if not creating:
            return
        name = payload.get("name")
        if (not isinstance(name, str) or not name.strip() or len(name) > 100
                or any(ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF for char in name)):
            raise CoreError("REQUEST_INVALID")
        try:
            if str(uuid.UUID(payload["request_id"])) != payload["request_id"]:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise CoreError("REQUEST_INVALID") from None

    async def _create(self, http, payload):
        suffix = hashlib.sha256(self.settings.tenant.encode()).hexdigest()[:16]
        client_id = "external-" + suffix + "-" + payload["request_id"]
        clients = await self._admin(http, "GET", "/clients", params={"clientId": client_id})
        if not isinstance(clients, list):
            raise CoreError("KEYCLOAK_ADMIN_UNAVAILABLE")
        if not clients:
            await self._request(http, "POST", self.admin_url + "/clients", allowed=(201, 409), json={
                "clientId": client_id, "name": payload["name"].strip(), "enabled": True,
                "protocol": "openid-connect", "publicClient": False, "serviceAccountsEnabled": True,
                "standardFlowEnabled": False, "directAccessGrantsEnabled": False,
                "implicitFlowEnabled": False, "fullScopeAllowed": False,
                "defaultClientScopes": ["basic", "roles"], "optionalClientScopes": ["offline_access"],
                "attributes": {PREFIX + "tenant": self.settings.tenant, PREFIX + "audience": self.settings.audience,
                    PREFIX + "request_id": payload["request_id"],
                    PREFIX + "days": str(payload["days"]), PREFIX + "state": "pending",
                    PREFIX + "created_at": str(time.time())},
                "protocolMappers": [{
                    "name": "external-api-audience", "protocol": "openid-connect",
                    "protocolMapper": "oidc-audience-mapper", "config": {
                        "included.custom.audience": self.settings.audience, "access.token.claim": "true",
                        "introspection.token.claim": "true"}}, {
                    "name": "external-realm-roles", "protocol": "openid-connect",
                    "protocolMapper": "oidc-usermodel-realm-role-mapper", "config": {
                        "multivalued": "true", "claim.name": "realm_access.roles", "jsonType.label": "String",
                        "access.token.claim": "true", "introspection.token.claim": "true"}}],
            })
            clients = await self._admin(http, "GET", "/clients", params={"clientId": client_id})
        if not isinstance(clients, list):
            raise CoreError("KEYCLOAK_ADMIN_UNAVAILABLE")
        if len(clients) != 1 or not await self._eligible(http, clients[0]):
            raise CoreError("EXTERNAL_ACCESS_CONFLICT")
        client = clients[0]
        attrs = self._attributes(client)
        if client.get("name") != payload["name"].strip() or attrs.get(PREFIX + "days") != str(payload["days"]):
            raise CoreError("EXTERNAL_ACCESS_CONFLICT")
        if attrs.get(PREFIX + "state") != "pending":
            raise CoreError("EXTERNAL_ACCESS_ALREADY_ISSUED")
        return client

    async def _issue(self, http, client, days):
        path = "/clients/" + quote(client["id"], safe="")
        user = await self._admin(http, "GET", path + "/service-account-user")
        if not isinstance(user, dict) or not isinstance(user.get("id"), str):
            raise CoreError("KEYCLOAK_ADMIN_UNAVAILABLE")
        roles = [await self._admin(http, "GET", "/roles/" + quote(name, safe=""))
                 for name in (self.settings.external_role, "offline_access")]
        if not all(isinstance(role, dict) and isinstance(role.get("id"), str) for role in roles):
            raise CoreError("KEYCLOAK_ADMIN_UNAVAILABLE")
        await self._admin(http, "POST", path + "/scope-mappings/realm", allowed=(204,), json=roles)
        await self._admin(http, "POST", "/users/" + quote(user["id"], safe="") + "/role-mappings/realm",
                          allowed=(204,), json=roles)
        attrs = self._attributes(client)
        # Advance at least one second: old and new tokens must not share an iat
        # at the notBefore boundary. Disabling then re-enabling cannot revive old tokens.
        cutoff = int(time.time()) + 1
        client.update(enabled=True, notBefore=cutoff)
        attrs.update({"access.token.lifespan": str(days * 86400), "client_credentials.use_refresh_token": "false", PREFIX + "state": "pending",
                      PREFIX + "tenant": self.settings.tenant, PREFIX + "audience": self.settings.audience})
        client["attributes"] = attrs
        await self._admin(http, "PUT", path, allowed=(204,), json=client)
        await asyncio.sleep(max(0, cutoff - time.time()) + 0.05)
        # Some Keycloak versions combine nonzero realm/client notBefore using
        # min(). The independent user revocation timestamp closes that gap.
        await self._admin(http, "POST", "/users/" + quote(user["id"], safe="") + "/logout", allowed=(204,))
        secret = await self._admin(http, "GET", path + "/client-secret")
        if not isinstance(secret, dict) or not isinstance(secret.get("value"), str) or not secret["value"]:
            raise CoreError("KEYCLOAK_ADMIN_UNAVAILABLE")
        _, issued = await self._request(http, "POST", self.settings.issuer + "/protocol/openid-connect/token",
            headers={"Authorization": ""}, data={"grant_type": "client_credentials",
                "client_id": client["clientId"], "client_secret": secret["value"], "scope": "offline_access"})
        if not isinstance(issued, dict) or not isinstance(issued.get("access_token"), str):
            raise CoreError("KEYCLOAK_ADMIN_UNAVAILABLE")
        token = issued["access_token"]
        if (not 1 <= len(token) <= 16377 or not isinstance(issued.get("token_type"), str)
                or issued["token_type"].lower() != "bearer"):
            raise CoreError("KEYCLOAK_ADMIN_UNAVAILABLE")
        try:
            actor, claims = await self.authenticator.authenticate(token, include_claims=True)
        except CoreError:
            raise CoreError("KEYCLOAK_ADMIN_UNAVAILABLE") from None
        duration = issued.get("expires_in")
        iat = claims.get("iat")
        realm_roles = claims.get("realm_access", {}).get("roles", [])
        resource_roles = claims.get("resource_access", {}).get(self.settings.audience, {}).get("roles", [])
        if (not actor.is_external or actor.is_owner or self.settings.owner_role in realm_roles + resource_roles
                or claims["sub"] != user["id"]
                or type(duration) is not int or not days * 86400 - 10 <= duration <= days * 86400
                or type(iat) is not int or iat < cutoff
                or not days * 86400 - 10 <= claims["exp"] - iat <= days * 86400):
            raise CoreError("KEYCLOAK_ADMIN_UNAVAILABLE")
        attrs.update({PREFIX + "state": "issued", PREFIX + "issued_at": str(iat),
                      PREFIX + "expires_at": str(claims["exp"]), PREFIX + "days": str(days)})
        await self._admin(http, "PUT", path, allowed=(204,), json=client)
        return {"account": self._public(client), "access_token": token, "token_type": "Bearer", "expires_in": duration}

    async def execute(self, actor, bearer, method, identifier=None, payload=None):
        if not actor.is_owner or actor.is_external or actor.tenant != self.settings.tenant:
            raise CoreError("ACCESS_DENIED")
        if method == "POST":
            self._validate(payload, creating=identifier is None)
        try:
            async with asyncio.timeout(60):
                async with httpx.AsyncClient(transport=self.transport, timeout=10, trust_env=False,
                    follow_redirects=False, headers={"Authorization": bearer, "Accept": "application/json"}) as http:
                    if method == "GET":
                        return await self._list(http)
                    async with self.lock:
                        client = await self._create(http, payload) if identifier is None else await self._client(http, identifier)
                        if method == "POST":
                            return await self._issue(http, client, payload["days"])
                        client["enabled"] = False
                        await self._admin(http, "PUT", "/clients/" + quote(client["id"], safe=""), allowed=(204,), json=client)
                        return {"account": self._public(client)}
        except TimeoutError:
            raise CoreError("KEYCLOAK_ADMIN_UNAVAILABLE") from None
