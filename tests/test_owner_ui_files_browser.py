"""Actual Chromium, Keycloak, PostgreSQL and native Pod file composer gate.

CORE_AGENT_REQUIRE_BROWSER_TESTS=1 requires every fixture and prohibits skips.
The Pod copies the existing native sandbox profile; only controlled models and
a transparent loopback service relay are fixtures. No auth/store/launcher mock.
"""
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import psycopg
from cryptography.fernet import Fernet
from psycopg import sql


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@unittest.skipUnless(os.environ.get("CORE_AGENT_REQUIRE_BROWSER_TESTS") == "1", "Dedicated actual-browser/native Pod fixture is required")
class OwnerUIFilesBrowserTests(unittest.TestCase):
    def command(self, argv, *, content=None, timeout=60, env=None):
        result = subprocess.run(argv, input=content, text=True, capture_output=True,
                                timeout=timeout, env=env, check=False)
        self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr[-4000:])
        return result.stdout

    def kube(self, *args, content=None, timeout=60):
        return self.command(self.kubectl + list(args), content=content, timeout=timeout)

    def apply(self, resource):
        self.kube("apply", "--validate=false", "-f", "-", content=json.dumps(resource))

    def setUp(self):
        self.root = Path.cwd()
        self.evidence = Path(os.environ.get("OWNER_UI_BROWSER_EVIDENCE", ".local-evidence/owner-browser-e2e"))
        self.evidence.mkdir(parents=True, exist_ok=True)
        required = ("KUBECONFIG", "OWNER_UI_BROWSER_IMAGE", "BROWSER_KEYCLOAK_TARGET",
                    "BROWSER_POSTGRES_TARGET", "TEST_KEYCLOAK_URL", "TEST_KEYCLOAK_ADMIN",
                    "TEST_KEYCLOAK_ADMIN_PASSWORD", "OWNER_UI_BROWSER_PG_ADMIN_URL")
        self.assertFalse([name for name in required if not os.environ.get(name)], "Missing actual browser fixture configuration")
        self.kubectl = ["kubectl", "--kubeconfig", os.environ["KUBECONFIG"]]
        self.browser = os.environ.get("OWNER_UI_BROWSER_CHROMIUM") or shutil.which("google-chrome") or shutil.which("chromium")
        self.assertTrue(self.browser and Path(self.browser).is_file(), "Actual Chromium binary is required")
        self.assertTrue(shutil.which("node"), "Node with native WebSocket support is required")
        self.temporary = tempfile.TemporaryDirectory(prefix="core-owner-browser-")
        self.addCleanup(self.temporary.cleanup)
        self.port, self.debug_port = free_port(), free_port()
        self.origin = f"http://127.0.0.1:{self.port}"
        self.suffix = uuid.uuid4().hex[:12]
        self.realm = "core-owner-browser-" + self.suffix
        self.namespace = "core-owner-browser-" + self.suffix
        self.database_name = "core_agent_ui_browser_" + self.suffix
        self.http = httpx.Client(timeout=20, trust_env=False)
        self.addCleanup(self.http.close)
        self.configure_database()
        self.configure_keycloak()
        self.configure_pod()

    def configure_database(self):
        admin = os.environ["OWNER_UI_BROWSER_PG_ADMIN_URL"]
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(self.database_name)))
        self.addCleanup(self.drop_database, admin)
        url = urlsplit(admin)
        dsn = urlunsplit(url._replace(path="/" + self.database_name))
        self.pod_dsn = urlunsplit(url._replace(netloc=f"{url.username}:{url.password}@127.0.0.1:49738", path="/" + self.database_name))
        self.command(["uv", "run", "core-agent-db", "migrate"], env={**os.environ, "DATABASE_MIGRATION_URL": dsn})

    def drop_database(self, admin):
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(self.database_name)))

    def configure_keycloak(self):
        keycloak = os.environ["TEST_KEYCLOAK_URL"].rstrip("/")
        self.assertEqual(urlsplit(keycloak).hostname, "127.0.0.1", "Fixture requires exact loopback issuer")
        self.assertEqual(urlsplit(keycloak).port, 49737, "Pod relay and browser must use the same issuer")
        self.admin_headers = self.keycloak_admin_headers()
        self.realm_admin = keycloak + "/admin/realms/" + self.realm
        self.secret, self.password = uuid.uuid4().hex, "Owner-browser-" + uuid.uuid4().hex
        mappers = [{"name": "realm-roles", "protocol": "openid-connect", "protocolMapper": "oidc-usermodel-realm-role-mapper",
                    "config": {"multivalued": "true", "claim.name": "realm_access.roles", "jsonType.label": "String",
                               "access.token.claim": "true", "introspection.token.claim": "true"}},
                   {"name": "agent-audience", "protocol": "openid-connect", "protocolMapper": "oidc-audience-mapper",
                    "config": {"included.custom.audience": "company-agent", "access.token.claim": "true",
                               "introspection.token.claim": "true"}}]
        created = self.http.post(keycloak + "/admin/realms", headers=self.admin_headers, json={
            "realm": self.realm, "enabled": True, "sslRequired": "none", "roles": {"realm": [{"name": "agent-owner"}, {"name": "agent-external"}]},
            "clients": [{"clientId": "introspection", "enabled": True, "publicClient": False, "secret": self.secret,
                         "protocol": "openid-connect"},
                        {"clientId": "browser", "enabled": True, "publicClient": True, "protocol": "openid-connect",
                         "standardFlowEnabled": True, "implicitFlowEnabled": False, "directAccessGrantsEnabled": False,
                         "redirectUris": [self.origin + "/ui/", self.origin + "/ui/?logged_out=1"], "webOrigins": [self.origin],
                         "attributes": {"pkce.code.challenge.method": "S256"}, "protocolMappers": mappers}],
            "users": [{"username": "browser-owner", "enabled": True, "emailVerified": True,
                       "firstName": "Browser", "lastName": "Owner", "email": "owner@example.test",
                       "realmRoles": ["agent-owner"], "clientRoles": {"realm-management": ["manage-clients", "manage-users", "view-clients", "view-users", "view-realm", "create-client"]}, "credentials": [{"type": "password", "value": self.password, "temporary": False}]}],
        })
        self.assertEqual(created.status_code, 201, "Actual Keycloak realm creation failed")
        self.addCleanup(self.remove_realm)
        self.issuer = keycloak + "/realms/" + self.realm

    def remove_realm(self):
        self.assertEqual(self.http.delete(self.realm_admin, headers=self.keycloak_admin_headers()).status_code, 204)

    def keycloak_admin_headers(self):
        login = self.http.post(os.environ["TEST_KEYCLOAK_URL"].rstrip("/") + "/realms/master/protocol/openid-connect/token", data={
            "client_id": "admin-cli", "grant_type": "password", "username": os.environ["TEST_KEYCLOAK_ADMIN"],
            "password": os.environ["TEST_KEYCLOAK_ADMIN_PASSWORD"],
        })
        self.assertEqual(login.status_code, 200, "Actual Keycloak admin login failed")
        return {"Authorization": "Bearer " + login.json()["access_token"]}

    def configure_pod(self):
        self.apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": self.namespace}})
        self.addCleanup(self.remove_pod)
        native = json.loads(self.kube("get", "configmap", "core-agent-sandbox-fixture", "-n", "default", "-o", "json"))
        self.apply({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "native", "namespace": self.namespace}, "data": native["data"]})
        self.apply({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "browser-fixture", "namespace": self.namespace},
                    "data": {"server.py": (self.root / "deploy/kubernetes/owner-browser-fixture.py").read_text()}})
        private = {"KEYCLOAK_ISSUER_URL": self.issuer, "KEYCLOAK_CLIENT_ID": "introspection", "KEYCLOAK_CLIENT_SECRET": self.secret,
                   "KEYCLOAK_UI_CLIENT_ID": "browser", "KEYCLOAK_AUDIENCE": "company-agent", "CORE_AGENT_TENANT_ID": self.namespace,
                   "SESSION_DATABASE_URL": self.pod_dsn, "DATABASE_URL": self.pod_dsn,
                   "PUSH_NOTIFICATION_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii")}
        self.apply({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "server-config", "namespace": self.namespace}, "stringData": private})
        pod = json.loads(self.kube("create", "--dry-run=client", "--validate=false", "-f", str(self.root / "deploy/kubernetes/sandbox-pod.yaml"), "-o", "json"))
        pod["metadata"] = {"name": "owner-browser", "namespace": self.namespace}
        spec = pod["spec"]
        spec["activeDeadlineSeconds"] = 420
        spec["terminationGracePeriodSeconds"] = 5
        backend = spec["containers"][0]
        backend["name"], backend["image"] = "backend", os.environ["OWNER_UI_BROWSER_IMAGE"]
        backend["command"] = ["uv", "run", "--no-sync", "python", "/browser-fixture/server.py", "backend"]
        backend["envFrom"] = [{"configMapRef": {"name": "native"}}, {"secretRef": {"name": "server-config"}}]
        settings = {"CORE_AGENT_ENVIRONMENT": "development", "CORE_AGENT_MEMORY": "disabled", "SESSION_STORAGE_TYPE": "postgres",
                    "TASK_STORAGE_TYPE": "postgres", "CORE_AGENT_ALLOWED_BUILTIN_TOOLS": "core_terminal_exec,core_response_files,core_cron_create,core_ask_owner,core_task_start,core_task_wait,core_python_exec", "CORE_AGENT_ALLOWED_SKILLS": "",
                    "CHAT_WORKSPACE_ROOT": "/data/chats", "LOCAL_WORKSPACE_ROOT": "/data/scratch", "DURABLE_STORAGE_ROOT": "/data/durable",
                    "UV_CACHE_DIR": "/tmp/uv-cache", "PYTHONDONTWRITEBYTECODE": "1", "OTEL_SDK_DISABLED": "true"}
        backend["env"] = [{"name": name, "value": value} for name, value in settings.items()]
        backend["readinessProbe"] = {"tcpSocket": {"port": 8000}, "initialDelaySeconds": 1, "periodSeconds": 1}
        backend["volumeMounts"] = [{"name": "fixture", "mountPath": "/browser-fixture", "readOnly": True},
                                   {"name": "data", "mountPath": "/data"}, {"name": "scratch", "mountPath": "/tmp"},
                                   {"name": "tun", "mountPath": "/dev/net/tun"}]
        spec["volumes"] = [{"name": "fixture", "configMap": {"name": "browser-fixture"}}, {"name": "data", "emptyDir": {"sizeLimit": "1Gi"}},
                           {"name": "scratch", "emptyDir": {"sizeLimit": "1Gi"}}, {"name": "tun", "hostPath": {"path": "/dev/net/tun", "type": "CharDevice"}}]
        spec["containers"].append({"name": "relay", "image": backend["image"], "imagePullPolicy": "Never",
                                   "command": ["python", "-I", "-S", "/browser-fixture/server.py", "relay"],
                                   "env": [{"name": name, "value": os.environ[name]} for name in ("BROWSER_KEYCLOAK_TARGET", "BROWSER_POSTGRES_TARGET")],
                                   "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                                   "volumeMounts": [{"name": "fixture", "mountPath": "/browser-fixture", "readOnly": True}]})
        self.apply(pod)
        self.kube("wait", "-n", self.namespace, "pod/owner-browser", "--for=condition=Ready", "--timeout=60s", timeout=65)
        active = json.loads(self.kube("get", "pod", "owner-browser", "-n", self.namespace, "-o", "json"))
        recorded = {"namespace": self.namespace, "node": active["spec"]["nodeName"],
                    "hostUsers": active["spec"]["hostUsers"], "securityContext": active["spec"]["securityContext"],
                    "backendSecurityContext": active["spec"]["containers"][0]["securityContext"],
                    "images": [{key: status.get(key) for key in ("name", "image", "imageID")}
                               for status in active["status"]["containerStatuses"]]}
        (self.evidence / "pod.json").write_text(json.dumps(recorded, indent=2) + "\n")
        forward = subprocess.Popen(self.kubectl + ["port-forward", "-n", self.namespace, "pod/owner-browser", f"{self.port}:8000"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(stop, forward)
        self.wait_url(self.origin + "/health/ready")

    def wait_url(self, url):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if self.http.get(url).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        self.fail("Real backend/browser endpoint did not become ready")

    def remove_pod(self):
        try:
            logs = self.kube("logs", "-n", self.namespace, "owner-browser", "-c", "backend")
            (self.evidence / "backend.log").write_text(logs)
        finally:
            self.kube("delete", "namespace", self.namespace, "--wait=true", "--timeout=50s", timeout=55)

    def test_owner_file_composer_login_retry_publication_reload_and_download(self):
        temporary = Path(self.temporary.name)
        files = []
        for index, content in enumerate((b"first report", b"second report", b"followup report")):
            folder = temporary / str(index)
            folder.mkdir()
            path = folder / "report.txt"
            path.write_bytes(content)
            files.append(str(path))
        process = subprocess.Popen([self.browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                                    f"--remote-debugging-port={self.debug_port}", f"--user-data-dir={temporary / 'profile'}", "about:blank"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(stop, process)
        self.wait_url(f"http://127.0.0.1:{self.debug_port}/json/version")
        downloads = temporary / "downloads"
        downloads.mkdir()
        config = {"origin": self.origin, "debugPort": self.debug_port, "username": "browser-owner", "password": self.password,
                  "files": files, "downloads": str(downloads), "evidence": str(self.evidence)}
        proof = self.command(["node", str(Path(__file__).with_name("owner_ui_files_browser.mjs"))], content=json.dumps(config), timeout=240)
        (self.evidence / "browser.log").write_text(proof)
        self.assertIn("PASS actual native file reads and persisted attachment history", proof)
        self.assertIn("PASS Markdown headings lists tables and fenced code render", proof)
        self.assertIn("PASS untrusted Markdown and Mermaid cannot execute or fetch external images", proof)
        self.assertIn("PASS history refresh preserves the rendered Mermaid image", proof)
        self.assertIn("PASS SSE outage with healthy canonical polling does not claim connection loss", proof)
        self.assertIn("PASS actual canonical read failure and recovery preserve pending chat work", proof)
        self.assertIn("PASS final answer stays reachable above composer after expanding execution", proof)
        self.assertIn("PASS Markdown file preview preserves safe rendering without external fetches", proof)
        self.assertIn("PASS Python file preview preserves exact indentation with syntax highlighting", proof)
        self.assertIn("PASS long file preview scrolls inside its bounds without covering properties", proof)
        self.assertIn("PASS actual confirmed chat deletion returns durable archive receipt", proof)
        self.assertIn("PASS archived chat retains authenticated canonical Task history and issued file access", proof)
        self.assertIn("PASS public reply is visible while the actual provider is blocked", proof)
        self.assertIn("PASS page reload uses canonical history without restoring the transient prefix", proof)
        self.assertIn("PASS canonical final reply replaces preview once with safe Markdown and Python", proof)
        self.assertIn("PASS actual owner policy persists independent access and material exemption for core_cron_create", proof)
        self.assertIn("PASS actual owner policy persists independent access and material exemption for core_terminal_exec", proof)
        self.assertIn("PASS owner UI runs same-chat cron despite model tool deny", proof)
        self.assertIn("PASS actual private peer reread exposes configured flag and disabled state only", proof)
        self.assertIn("PASS chat navigation shows meaningful titles without update dates or times", proof)
        self.assertIn("PASS invalid peer names send no mutation before a valid name is saved", proof)
        self.assertIn("PASS confirmed workspace cleanup deletes only the selected actual file", proof)
        self.assertIn("PASS browser reload preserves both completed roots and immutable file history after cleanup", proof)
        self.assertIn("PASS completed approval cards disappear while native decisions remain persisted", proof)
        self.assertIn("PASS Keycloak logout revokes the old owner token for a new HTTP request", proof)
        self.assertIn("PASS logged-out reload exposes no private UI and performs no automatic login or private requests", proof)
        self.assertIn("PASS explicit sign-in after logout opens real Keycloak login", proof)
        self.assertIn("PASS actual owner-session Keycloak issues a 30-day external token", proof)
        self.assertIn("PASS access listing contains no issued token", proof)
        self.assertIn("PASS issued credential authenticates external A2A entrance", proof)
        self.assertIn("PASS cancelled external account deletion sends no mutation", proof)
        self.assertIn("PASS confirmed external account deletion returns native receipt", proof)
        self.assertIn("PASS deleted native account token no longer authenticates external A2A", proof)
        self.assertIn("PASS late account listing cannot restore a deleted row or count", proof)
        self.assertIn("PASS unconfirmed deletion rereads canonical account list without replay", proof)


if __name__ == "__main__":
    unittest.main(verbosity=2)
