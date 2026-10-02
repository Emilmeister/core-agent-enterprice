"""Synthetic build fixtures prove routing/security, not an actual SPA build."""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from core_agent.auth import AuthenticationMiddleware, AuthSettings, Principal
from core_agent.ui import ui_routes
from tests.test_auth import AuthAppTestCase


class UIStaticTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "build"
        self.root.mkdir()
        (self.root / "assets").mkdir()
        (self.root / "index.html").write_text("<!doctype html><title>Fixture</title>")
        (self.root / "assets/app-Abcd1234.js").write_text("export const fixture = true;")
        (self.root / "assets/app-Abcd1234.css").write_text("body { color: black; }")
        (self.root / "assets/logo.svg").write_text("<svg></svg>")
        (self.root / "secret.txt").write_text("never public")
        self.settings = AuthSettings("https://identity.test:8443/realms/company", "server", "secret", "aud", "company", ui_client_id="browser")
        self.checks = []

    async def client(self, *, settings=None, root=None):
        settings = settings or self.settings
        routes, paths = ui_routes(settings, directory=self.root if root is None else root)

        async def authenticate(token):
            self.checks.append(token)
            return Principal(token, "company", token == "owner", token == "external")

        async def private(_request):
            return JSONResponse({"private": True})

        app = Starlette(routes=[*routes, Route("/api/private", private), Route("/a2a/external/private", private)])
        app.add_middleware(AuthenticationMiddleware, authenticator=SimpleNamespace(settings=settings, authenticate=authenticate), public_paths=paths)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://agent.test")
        self.addAsyncCleanup(client.aclose)
        return client, paths

    async def test_shell_callback_head_redirect_and_security_headers(self):
        client, paths = await self.client()
        self.assertIn("/ui/index.html", paths)
        for path in ("/ui/", "/ui/index.html", "/ui/?code=oauth&issuer=https://evil.test"):
            response = await client.get(path)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("Fixture", response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")
            self.assertEqual(response.headers["referrer-policy"], "no-referrer")
            directives = dict(item.strip().split(" ", 1) for item in response.headers["content-security-policy"].split(";") if item.strip())
            self.assertEqual(directives["connect-src"], "'self' https://identity.test:8443")
            for name in ("script-src", "style-src"):
                self.assertEqual(directives[name], "'self'")
            self.assertEqual(directives["img-src"], "'self' blob:")
            for name in ("frame-ancestors", "object-src", "base-uri"):
                self.assertEqual(directives[name], "'none'")
        head = await client.head("/ui/")
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.content, b"")
        redirect = await client.get("/ui?code=ignored")
        self.assertEqual(redirect.status_code, 307)
        self.assertEqual(redirect.headers["location"], "/ui/")
        self.assertEqual(redirect.headers["cache-control"], "no-store")
        self.assertEqual(self.checks, [])

    async def test_enumerated_assets_only_and_cache_requires_hash_filename(self):
        client, paths = await self.client()
        for path in ("/ui/assets/app-Abcd1234.js", "/ui/assets/app-Abcd1234.css"):
            response = await client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn("immutable", response.headers["cache-control"])
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        response = await client.get("/ui/assets/logo.svg")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        (self.root / "assets/late-Abcd1234.js").write_text("late")
        self.assertNotIn("/ui/assets/late-Abcd1234.js", paths)
        self.assertEqual((await client.get("/ui/assets/late-Abcd1234.js")).status_code, 404)

    async def test_unknown_traversal_methods_and_private_routes_never_get_shell(self):
        client, _ = await self.client()
        for headers in ({}, {"Authorization": "Bearer owner"}):
            for path in ("/ui/missing", "/ui/secret.txt", "/ui/assets/missing.js", "/ui/assets/%2e%2e/secret.txt", "/ui/assets/%2fetc%2fpasswd", "/ui/assets/%5c..%5csecret.txt"):
                response = await client.get(path, headers=headers)
                self.assertEqual(response.status_code, 404, path)
                self.assertNotIn("Fixture", response.text)
        self.assertEqual((await client.post("/ui/")).status_code, 404)
        for path in ("/api/private", "/a2a/external/private"):
            self.assertEqual((await client.get(path)).status_code, 401)
        self.assertEqual((await client.get("/api/private", headers={"Authorization": "Bearer external"})).status_code, 403)
        self.assertEqual((await client.get("/api/private", headers={"Authorization": "Bearer owner"})).status_code, 200)
        self.assertEqual((await client.get("/api/missing", headers={"Authorization": "Bearer owner"})).status_code, 404)

    async def test_disabled_missing_and_symlink_builds_are_not_public(self):
        for settings, root in ((replace(self.settings, ui_client_id=""), self.root), (self.settings, self.root / "missing"), (None, self.root)):
            routes, paths = ui_routes(settings, directory=root)
            self.assertEqual(routes, [])
            self.assertEqual(paths, frozenset())
        link = self.root.parent / "build-link"
        link.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(ui_routes(self.settings, directory=link), ([], frozenset()))
        (self.root / "index.html").unlink()
        (self.root / "index.html").symlink_to(self.root / "secret.txt")
        self.assertEqual(ui_routes(self.settings, directory=self.root), ([], frozenset()))

    async def test_symlink_assets_and_replacements_never_escape_build(self):
        outside = self.root.parent / "outside.js"
        outside.write_text("private bytes")
        (self.root / "assets/link-Abcd1234.js").symlink_to(outside)
        client, paths = await self.client()
        self.assertNotIn("/ui/assets/link-Abcd1234.js", paths)
        self.assertEqual((await client.get("/ui/assets/link-Abcd1234.js")).status_code, 404)
        target = self.root / "assets/app-Abcd1234.js"
        target.unlink()
        target.symlink_to(outside)
        response = await client.get("/ui/assets/app-Abcd1234.js")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("private bytes", response.text)


class UIStaticCompositionTests(AuthAppTestCase):
    ui_client_id = "browser"

    async def asyncSetUp(self):
        build = tempfile.TemporaryDirectory()
        self.addCleanup(build.cleanup)
        root = Path(build.name)
        (root / "index.html").write_text("<!doctype html><title>Composed fixture</title>")
        (root / "assets").mkdir()
        (root / "assets/app-Abcd1234.js").write_text("export const fixture = true;")
        fixture = patch("core_agent.ui.UI_DIST", root)
        fixture.start()
        self.addCleanup(fixture.stop)
        await super().asyncSetUp()

    async def test_real_composition_exposes_build_without_opening_owner_api(self):
        for path in ("/ui/", "/ui/index.html", "/ui/assets/app-Abcd1234.js"):
            response = await self.http.get(path)
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((await self.http.head("/ui/")).status_code, 200)
        self.assertEqual((await self.http.get("/ui")).headers["location"], "/ui/")
        self.assertEqual((await self.http.get("/ui/config")).status_code, 200)
        self.assertEqual(self.checks, [])
        self.assertFalse(self.model.calls)
        for path in ("/ui/unknown", "/ui/assets/missing.js"):
            self.assertEqual((await self.http.get(path)).status_code, 404)
        for path in ("/api/identity", "/api/settings", "/a2a/owner/tasks", "/a2a/external/tasks"):
            self.assertEqual((await self.http.get(path)).status_code, 401)
        self.assertEqual((await self.http.get("/api/identity", headers=self.headers("external-a"))).status_code, 403)
