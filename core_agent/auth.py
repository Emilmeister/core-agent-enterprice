"""Keycloak request authentication; transport data never selects identity/scope."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from a2a.auth.user import User
from a2a.server.context import ServerCallContext
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.utils.errors import InvalidParamsError
from starlette.responses import JSONResponse

from .errors import CoreError


OWNER_SCOPE = "company-owners"


@dataclass(frozen=True)
class AuthSettings:
    issuer: str
    client_id: str
    client_secret: str = field(repr=False)
    audience: str
    tenant: str
    owner_role: str = "agent-owner"
    external_role: str = "agent-external"
    ui_client_id: str = ""

    @classmethod
    def from_environment(cls, get, *, production=False, allow_legacy=False):
        values = [get(name, "") for name in (
            "KEYCLOAK_ISSUER_URL", "KEYCLOAK_CLIENT_ID", "KEYCLOAK_CLIENT_SECRET",
            "KEYCLOAK_AUDIENCE", "CORE_AGENT_TENANT_ID",
        )]
        ui_client_id = get("KEYCLOAK_UI_CLIENT_ID", "")
        if not any(values) and not ui_client_id and allow_legacy and not production:
            return None
        if not all(isinstance(value, str) and value.strip() for value in values):
            raise CoreError("CONFIG_INVALID", "Complete Keycloak authentication configuration is required")
        if (not isinstance(ui_client_id, str) or len(ui_client_id) > 255
                or any(char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159
                       or 0xD800 <= ord(char) <= 0xDFFF for char in ui_client_id)
                or ui_client_id == values[1]):
            raise CoreError("CONFIG_INVALID", "KEYCLOAK_UI_CLIENT_ID must identify a separate public browser client")
        issuer = values[0].rstrip("/")
        try:
            parsed = urlsplit(issuer)
            valid = (
                parsed.hostname and parsed.port != 0
                and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment
                and not any(char.isspace() or ord(char) < 32 for char in issuer)
                and (parsed.scheme == "https" or (
                    not production and parsed.scheme == "http"
                    and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                ))
            )
        except ValueError:
            valid = False
        if not valid:
            raise CoreError("CONFIG_INVALID", "Keycloak issuer must be a trusted HTTPS realm URL")
        owner_role = get("KEYCLOAK_OWNER_ROLE", "agent-owner")
        external_role = get("KEYCLOAK_EXTERNAL_ROLE", "agent-external")
        if not owner_role or not external_role or owner_role == external_role:
            raise CoreError("CONFIG_INVALID", "Distinct owner and external Keycloak roles are required")
        return cls(issuer, *values[1:], owner_role, external_role, ui_client_id)


@dataclass(frozen=True)
class Principal:
    actor_id: str
    tenant: str
    is_owner: bool
    is_external: bool

    @property
    def owner_id(self):
        return OWNER_SCOPE if self.is_owner else "external-" + self.actor_id


@dataclass(frozen=True)
class ScopeUser(User):
    name: str

    @property
    def user_name(self):
        return self.name

    @property
    def is_authenticated(self):
        return True


def is_company_owner(context):
    actor = context.state.get("principal")
    return (
        isinstance(actor, Principal) and actor.is_owner
        and context.user.is_authenticated and context.tenant == actor.tenant
    )


class AuthenticatedCallContext(ServerCallContext):
    def __setattr__(self, name, value):
        # SDK dispatchers assign caller-selected tenant AFTER the context builder.
        # Keep the authenticated scope even when the SDK assigns its empty default.
        actor = self.state.get("principal") if name == "tenant" else None
        if isinstance(actor, Principal):
            if value and value != actor.tenant:
                raise InvalidParamsError("Tenant is fixed by authenticated deployment")
            value = actor.tenant
        super().__setattr__(name, value)


class AuthContextBuilder(DefaultServerCallContextBuilder):
    def build(self, request):
        actor = request.scope["principal"]
        context = super().build(request)
        # Incoming credentials must not reach remote-agent forwarding/checkpoints.
        context.state["headers"] = {
            name: value for name, value in context.state["headers"].items()
            if name.lower() not in {"authorization", "cookie", "proxy-authorization"}
        }
        context.state["principal"] = actor
        return AuthenticatedCallContext(
            user=ScopeUser(actor.owner_id), tenant=actor.tenant,
            state=context.state, requested_extensions=context.requested_extensions,
        )


class KeycloakAuthenticator:
    def __init__(self, settings, *, transport=None, clock=time.time):
        self.settings = settings
        self.transport = transport
        self.clock = clock

    async def authenticate(self, token):
        settings = self.settings
        # Per-request clients avoid sharing an async connection pool across ASGI
        # event loops. No credential-bearing redirects, proxies or cached verdicts.
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=10, follow_redirects=False, trust_env=False,
            ) as client:
                async with client.stream(
                    "POST", settings.issuer + "/protocol/openid-connect/token/introspect",
                    auth=(settings.client_id, settings.client_secret),
                    data={"token": token, "token_type_hint": "access_token"},
                    headers={"Accept": "application/json"},
                ) as response:
                    if response.status_code != 200:
                        raise CoreError("AUTHENTICATION_UNAVAILABLE")
                    content = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        content.extend(chunk)
                        if len(content) > 65536:
                            raise CoreError("AUTHENTICATION_UNAVAILABLE")
                    claims = json.loads(content)
        except (httpx.HTTPError, ValueError, UnicodeError):
            raise CoreError("AUTHENTICATION_UNAVAILABLE") from None
        if not isinstance(claims, dict):
            raise CoreError("AUTHENTICATION_UNAVAILABLE")
        now = self.clock()
        expiry, not_before = claims.get("exp"), claims.get("nbf", 0)
        audience = claims.get("aud")
        audience = [audience] if isinstance(audience, str) else audience
        subject = claims.get("sub")
        if not (
            claims.get("active") is True and claims.get("iss") == settings.issuer
            and isinstance(audience, list) and settings.audience in audience
            and all(isinstance(item, str) for item in audience)
            and isinstance(subject, str) and 0 < len(subject) <= 1024
            and isinstance(claims.get("token_type"), str)
            and claims["token_type"].lower() == "bearer"
            and all(type(value) is int or (type(value) is float and math.isfinite(value)) for value in (expiry, not_before))
            and not_before <= now < expiry
        ):
            raise CoreError("AUTHENTICATION_REQUIRED")
        realm = claims.get("realm_access", {})
        resources = claims.get("resource_access", {})
        resource = resources.get(settings.audience, {}) if isinstance(resources, dict) else None
        if not isinstance(realm, dict) or not isinstance(resource, dict):
            raise CoreError("AUTHENTICATION_REQUIRED")
        groups = [realm.get("roles", []), resource.get("roles", [])]
        if not all(isinstance(group, list) and all(isinstance(role, str) for role in group) for group in groups):
            raise CoreError("AUTHENTICATION_REQUIRED")
        roles = set(groups[0]) | set(groups[1])
        external = settings.external_role in roles
        owner = settings.owner_role in roles and not external
        actor_id = hashlib.sha256((settings.issuer + "\0" + subject).encode()).hexdigest()
        return Principal(actor_id, settings.tenant, owner, external)


class AuthenticationMiddleware:
    def __init__(self, app, *, authenticator, public_paths=()):
        self.app = app
        self.authenticator = authenticator
        self.public_paths = frozenset(public_paths)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if (path in {"/health/live", "/health/ready"}
                or (path == "/ui/config" and self.authenticator.settings.ui_client_id)
                or (scope["method"] in {"GET", "HEAD"} and path in self.public_paths)):
            await self.app(scope, receive, send)
            return
        owner_path = path == "/a2a/owner" or path.startswith(("/a2a/owner/", "/api/"))
        external_path = path == "/a2a/external" or path.startswith("/a2a/external/")
        if not owner_path and not external_path:
            await JSONResponse({"error": {"code": "NOT_FOUND"}}, status_code=404)(scope, receive, send)
            return
        try:
            values = [value for name, value in scope["headers"] if name.lower() == b"authorization"]
            match = re.fullmatch(rb"(?i:Bearer) ([A-Za-z0-9._~+/-]+=*)", values[0]) if len(values) == 1 else None
            if match is None or len(values[0]) > 16384:
                raise CoreError("AUTHENTICATION_REQUIRED")
            actor = await self.authenticator.authenticate(match[1].decode("ascii"))
            if not (actor.is_owner if owner_path else actor.is_external):
                raise CoreError("ACCESS_DENIED")
        except CoreError as error:
            code = error.code
            status = {"AUTHENTICATION_REQUIRED": 401, "ACCESS_DENIED": 403}.get(code, 503)
            headers = {"Cache-Control": "no-store"}
            if status == 401:
                headers["WWW-Authenticate"] = "Bearer"
            await JSONResponse({"error": {"code": code}}, status_code=status, headers=headers)(scope, receive, send)
            return
        scope["principal"] = actor
        await self.app(scope, receive, send)
