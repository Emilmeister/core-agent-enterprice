"""Cloud.ru proxy entrance and opaque, database-backed owner browser sessions."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Match

from .auth import Principal
from .errors import CoreError


COOKIE_NAME = "__Host-core-agent-owner"
SESSION_SECONDS = 8 * 3600
LOGIN_WINDOW = 300
LOGIN_ATTEMPTS = 10
LOGIN_BODY_LIMIT = 4096


@dataclass(frozen=True)
class CloudAuthSettings:
    tenant: str
    password: str = field(repr=False)
    origin: str
    mode: str = field(default="cloudru", init=False)
    ui_client_id: str = field(default="", init=False)

    @classmethod
    def from_environment(cls, get):
        tenant = get("CORE_AGENT_TENANT_ID", "")
        password = get("CORE_AGENT_OWNER_PASSWORD", "")
        url = get("AGENT_URL", "") or get("URL_AGENT", "")
        if not isinstance(tenant, str) or not tenant.strip() or len(tenant) > 255 or any(ord(c) < 32 for c in tenant):
            raise CoreError("CONFIG_INVALID", "Cloud.ru requires CORE_AGENT_TENANT_ID")
        if not isinstance(password, str) or not 16 <= len(password) <= 1024:
            raise CoreError("CONFIG_INVALID", "CORE_AGENT_OWNER_PASSWORD must contain 16 to 1024 characters")
        try:
            password.encode("utf-8")
        except UnicodeError:
            raise CoreError("CONFIG_INVALID", "CORE_AGENT_OWNER_PASSWORD must contain valid UTF-8") from None
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme == "https" and parsed.hostname and parsed.port != 0
                and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
                and not parsed.username and not parsed.password and not parsed.netloc.endswith(":")
                and not any(c.isspace() or ord(c) < 32 or c == "\\" for c in url))
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise CoreError("CONFIG_INVALID", "Cloud.ru AGENT_URL must be a public HTTPS origin")
        hostname = parsed.hostname.lower()
        hostname = "[" + hostname + "]" if ":" in hostname else hostname
        authority = hostname + (":" + str(parsed.port) if parsed.port not in {None, 443} else "")
        return cls(tenant, password, "https://" + authority)

    @property
    def password_version(self):
        return hashlib.sha256(("core-agent-owner\0" + self.tenant + "\0" + self.password).encode()).hexdigest()


class OwnerSessionStore:
    """The shared PostgreSQL transaction owns both attempt limits and issuance.

    The small memory branch is only for explicit development/test deployments.
    """
    def __init__(self, database=None):
        self.database = database
        self._lock = threading.Lock()
        self._versions = {}
        self._seen_versions = set()
        self._attempts = {}
        self._sessions = {}

    def register(self, tenant, version):
        if self.database is None:
            with self._lock:
                if self._versions.get(tenant) != version:
                    if (tenant, version) in self._seen_versions:
                        raise CoreError("CONFIG_INVALID", "CORE_AGENT_OWNER_PASSWORD was retired; deploy the current secret")
                    self._sessions = {key: value for key, value in self._sessions.items() if key[0] != tenant}
                self._versions[tenant] = version
                self._seen_versions.add((tenant, version))
            return
        with self.database.transaction() as connection:
            connection.execute("""INSERT INTO core_owner_auth (tenant_id, password_version)
                VALUES (%s,%s) ON CONFLICT (tenant_id) DO NOTHING""", (tenant, version))
            row = connection.execute("SELECT password_version FROM core_owner_auth WHERE tenant_id=%s FOR UPDATE", (tenant,)).fetchone()
            if row["password_version"] != version:
                if connection.execute("SELECT 1 FROM core_owner_auth_versions WHERE tenant_id=%s AND password_version=%s", (tenant, version)).fetchone():
                    raise CoreError("CONFIG_INVALID", "CORE_AGENT_OWNER_PASSWORD was retired; deploy the current secret")
                connection.execute("UPDATE core_owner_auth SET password_version=%s WHERE tenant_id=%s", (version, tenant))
                connection.execute("DELETE FROM core_owner_sessions WHERE tenant_id=%s", (tenant,))
            connection.execute("INSERT INTO core_owner_auth_versions (tenant_id,password_version) VALUES (%s,%s) ON CONFLICT DO NOTHING", (tenant, version))

    def login(self, tenant, version, digest, password_matches, now):
        if self.database is None:
            with self._lock:
                start, attempts = self._attempts.get(tenant, (now, 0))
                if now >= start + LOGIN_WINDOW:
                    start, attempts = now, 0
                if attempts >= LOGIN_ATTEMPTS:
                    return "AUTH_RATE_LIMITED"
                self._attempts[tenant] = (start, attempts + 1)
                if not password_matches or self._versions.get(tenant) != version:
                    return "AUTHENTICATION_REQUIRED"
                self._sessions = {key: value for key, value in self._sessions.items() if value[1] > now}
                self._sessions[(tenant, digest)] = (version, now + SESSION_SECONDS)
                return None
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM core_owner_auth WHERE tenant_id=%s FOR UPDATE", (tenant,)).fetchone()
            start, attempts = row["window_started_at"], row["attempts"]
            if now >= start + LOGIN_WINDOW:
                start, attempts = now, 0
            if attempts >= LOGIN_ATTEMPTS:
                return "AUTH_RATE_LIMITED"
            connection.execute("UPDATE core_owner_auth SET window_started_at=%s, attempts=%s WHERE tenant_id=%s",
                               (start, attempts + 1, tenant))
            if not password_matches or row["password_version"] != version:
                return "AUTHENTICATION_REQUIRED"
            connection.execute("DELETE FROM core_owner_sessions WHERE tenant_id=%s AND expires_at<=%s", (tenant, now))
            connection.execute("""INSERT INTO core_owner_sessions (tenant_id, token_digest, password_version, expires_at)
                VALUES (%s,%s,%s,%s)""", (tenant, digest, version, now + SESSION_SECONDS))
            return None

    def valid(self, tenant, version, digest, now):
        if self.database is None:
            with self._lock:
                session = self._sessions.get((tenant, digest))
                return bool(session and session[0] == version == self._versions.get(tenant) and session[1] > now)
        with self.database.transaction() as connection:
            return connection.execute("""SELECT 1 FROM core_owner_sessions session
                JOIN core_owner_auth auth USING (tenant_id)
                WHERE session.tenant_id=%s AND session.token_digest=%s
                  AND session.password_version=%s AND auth.password_version=%s AND session.expires_at>%s""",
                (tenant, digest, version, version, now)).fetchone() is not None

    def revoke(self, tenant, digest):
        if self.database is None:
            with self._lock:
                self._sessions.pop((tenant, digest), None)
            return
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM core_owner_sessions WHERE tenant_id=%s AND token_digest=%s", (tenant, digest))


class CloudAuthenticator:
    def __init__(self, settings, database=None, *, clock=time.time):
        self.settings = settings
        self.clock = clock
        self.store = OwnerSessionStore(database)
        self.store.register(settings.tenant, settings.password_version)

    async def _call(self, method, *args):
        try:
            return await asyncio.to_thread(method, *args)
        except Exception:
            raise CoreError("AUTHENTICATION_UNAVAILABLE") from None

    @staticmethod
    def _digest(token):
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def _cookie(self, request):
        token = request.cookies.get(COOKIE_NAME, "")
        return token if re.fullmatch(r"[A-Za-z0-9_-]{43}", token) else ""

    def check_origin(self, request):
        if request.headers.getlist("origin") != [self.settings.origin]:
            raise CoreError("ACCESS_DENIED")

    async def authenticate(self, request):
        token = self._cookie(request)
        if not token or not await self._call(self.store.valid, self.settings.tenant,
                self.settings.password_version, self._digest(token), self.clock()):
            raise CoreError("AUTHENTICATION_REQUIRED")
        return Principal("cloudru-owner", self.settings.tenant, True, False, True, auth_mode="cloudru")

    def external(self):
        # Only the platform proxy may reach this route; it forwards no trusted ID.
        return Principal("cloudru-platform-proxy", self.settings.tenant, False, True, auth_mode="cloudru")

    async def login(self, request):
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > LOGIN_BODY_LIMIT:
                raise CoreError("REQUEST_TOO_LARGE")
            raw.extend(chunk)
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or set(payload) != {"password"} or not isinstance(payload["password"], str):
                raise ValueError()
            candidate = payload["password"].encode("utf-8")
        except (ValueError, UnicodeError, RecursionError):
            raise CoreError("REQUEST_INVALID") from None
        token = secrets.token_urlsafe(32)
        matches = hmac.compare_digest(hashlib.sha256(candidate).digest(),
                                      hashlib.sha256(self.settings.password.encode("utf-8")).digest())
        error = await self._call(self.store.login, self.settings.tenant, self.settings.password_version,
                                self._digest(token), matches, self.clock())
        if error:
            raise CoreError(error)
        response = JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})
        response.set_cookie(COOKIE_NAME, token, max_age=SESSION_SECONDS, secure=True, httponly=True, samesite="lax", path="/")
        return response

    async def logout(self, request):
        token = self._cookie(request)
        if token:
            await self._call(self.store.revoke, self.settings.tenant, self._digest(token))
        response = JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})
        response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax")
        return response


class CloudAuthenticationMiddleware:
    def __init__(self, app, *, authenticator, public_paths=(), external_routes=()):
        self.app = app
        self.authenticator = authenticator
        self.public_paths = frozenset(public_paths)
        self.external_routes = tuple(external_routes)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path, method = scope["path"], scope["method"]
        if method in {"GET", "HEAD"} and path in {"/health/live", "/health/ready", "/ui/config", *self.public_paths}:
            await self.app(scope, receive, send)
            return
        owner = path == "/a2a/owner" or path.startswith(("/a2a/owner/", "/api/"))
        external = (path == "/a2a/external" or path.startswith("/a2a/external/")
                    or (not owner and any(route.matches(scope)[0] != Match.NONE
                                          for route in self.external_routes)))
        if not owner and not external:
            await JSONResponse({"error": {"code": "NOT_FOUND"}}, status_code=404)(scope, receive, send)
            return
        request = Request(scope, receive)
        try:
            if owner:
                if method not in {"GET", "HEAD", "OPTIONS"}:
                    self.authenticator.check_origin(request)
                if method == "POST" and path in {"/api/auth/login", "/api/auth/logout"}:
                    response = await (self.authenticator.login(request) if path.endswith("/login") else self.authenticator.logout(request))
                    await response(scope, receive, send)
                    return
                actor = await self.authenticator.authenticate(request)
            else:
                actor = self.authenticator.external()
        except CoreError as error:
            status = {"AUTHENTICATION_REQUIRED": 401, "ACCESS_DENIED": 403, "AUTH_RATE_LIMITED": 429,
                      "REQUEST_INVALID": 400, "REQUEST_TOO_LARGE": 413}.get(error.code, 503)
            headers = {"Cache-Control": "no-store"}
            if status == 429:
                headers["Retry-After"] = str(LOGIN_WINDOW)
            await JSONResponse({"error": {"code": error.code}}, status_code=status, headers=headers)(scope, receive, send)
            return
        scope["principal"] = actor
        await self.app(scope, receive, send)
