"""Native Docker lifetime proof; required in the sandbox CI job."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from core_agent.execution_identity import stopped_by_restart


@unittest.skipUnless(os.getenv("CORE_AGENT_RESTART_TEST_IMAGE"), "set CORE_AGENT_RESTART_TEST_IMAGE for native Docker restart")
class DockerRestartTests(unittest.TestCase):
    def docker(self, *arguments, input=None):
        result = subprocess.run(["docker", *arguments], check=True, capture_output=True, text=True, input=input, timeout=60)
        return result.stdout + (result.stderr if arguments[0] == "logs" else "")

    def identity(self, name, after=None):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            lines = self.docker("logs", name).splitlines()
            if lines:
                host = json.loads(lines[-1])
                if host is not None and host != after:
                    return host
            time.sleep(.2)
        self.fail("PID-1 identity was not captured after container start")

    def test_restart_confirms_same_container_but_not_live_peer(self):
        script = Path(__file__).resolve().parents[1] / "core_agent" / "execution_identity.py"
        code = ("import json,runpy,subprocess,time; "
                "m=runpy.run_path('/identity.py'); "
                "subprocess.Popen(['sleep','300']); "
                "print(json.dumps(m['execution_host']()),flush=True); time.sleep(300)")
        names = ["execution-restart-" + uuid.uuid4().hex for _ in range(2)]
        for name in names:
            self.addCleanup(subprocess.run, ["docker", "rm", "-f", name], capture_output=True, timeout=30)
            self.docker("run", "-d", "--name", name, "--network", "none", "--read-only", "--cap-drop", "ALL",
                        "--mount", f"type=bind,src={script},dst=/identity.py,readonly",
                        "--entrypoint", "/usr/local/bin/python3", os.environ["CORE_AGENT_RESTART_TEST_IMAGE"], "-c", code)
        old, peer = (self.identity(name) for name in names)
        self.assertFalse(stopped_by_restart(old, peer))
        self.docker("restart", "--time", "1", names[0])
        new = self.identity(names[0], after=old)
        self.assertTrue(stopped_by_restart(old, new))
        self.assertEqual(old["slot"], new["slot"])
        # Linux may reuse a namespace inode immediately after its destruction.
        self.assertGreater(new["init_start"], old["init_start"])
        self.assertEqual(peer, self.identity(names[1]))
        self.assertFalse(stopped_by_restart(peer, new))


    @unittest.skipUnless(os.getenv("TEST_DATABASE_URL") and os.getenv("TEST_POSTGRES_CONTAINER"),
                         "local PostgreSQL container is required for persisted replacement proof")
    def test_replacement_waits_for_exact_stop_confirmation_without_replay(self):
        """Common runtime + native Bubblewrap; not a Cloud Landlock/S3 gate."""
        import httpx
        import psycopg
        from psycopg.conninfo import conninfo_to_dict, make_conninfo
        from psycopg.sql import Identifier, SQL
        from core_agent.database import PostgresDatabase
        from core_agent.workflow import PostgresWorkflowStore

        repo = Path(__file__).resolve().parents[1]
        image = os.environ["CORE_AGENT_RESTART_TEST_IMAGE"]
        arch = self.docker("image", "inspect", image, "--format", "{{.Architecture}}").strip()
        daemon_arch = self.docker("info", "--format", "{{.Architecture}}").strip()
        self.assertEqual(arch, {"aarch64": "arm64", "x86_64": "amd64"}.get(daemon_arch, daemon_arch))
        options = conninfo_to_dict(os.environ["TEST_DATABASE_URL"])
        self.assertIn(options.get("host"), {"127.0.0.1", "localhost", "::1"})
        postgres = json.loads(self.docker("inspect", os.environ["TEST_POSTGRES_CONTAINER"]))[0]
        published = postgres["NetworkSettings"]["Ports"]["5432/tcp"]
        self.assertTrue(any(item["HostPort"] == options.get("port", "5432") for item in published))
        address = postgres["NetworkSettings"]["Networks"]["bridge"]["IPAddress"]
        schema = "restart_" + uuid.uuid4().hex
        tenant, context = uuid.uuid4().hex, uuid.uuid4().hex
        with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as connection:
            connection.execute(SQL("CREATE SCHEMA {}").format(Identifier(schema)))
        def drop_schema():
            with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as connection:
                connection.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(schema)))
        self.addCleanup(drop_schema)
        dsn = make_conninfo(os.environ["TEST_DATABASE_URL"], options="-c search_path=" + schema)
        database = PostgresDatabase(dsn, min_size=0, max_size=2)
        self.addCleanup(database.close)
        database.migrate()
        store = PostgresWorkflowStore(database)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        envfile = Path(temporary.name) / "worker.env"
        password = uuid.uuid4().hex
        environment = {
            "CORE_AGENT_ENVIRONMENT": "development", "CORE_AGENT_AUTH_MODE": "cloudru",
            "CORE_AGENT_TENANT_ID": tenant, "CORE_AGENT_OWNER_PASSWORD": password, "RESTART_NONCE": context,
            "AGENT_URL": "https://agent.example.test", "CORE_AGENT_MEMORY": "disabled",
            "CORE_AGENT_ALLOWED_BUILTIN_TOOLS": "core_terminal_exec", "CORE_AGENT_NEXT_STEPS": "false",
            "DATABASE_URL": make_conninfo(dsn, host=address, port=5432), "DATABASE_AUTO_MIGRATE": "false",
            "LOCAL_WORKSPACE_ROOT": "/tmp/restart/scratch", "CHAT_WORKSPACE_ROOT": "/tmp/restart/chats",
            "DURABLE_STORAGE_ROOT": "/tmp/restart/durable", "WORKSPACE_STORAGE_TYPE": "posix",
            "SANDBOX_BACKEND": "bubblewrap", "SANDBOX_DNS_SERVERS": "1.1.1.1",
            "SANDBOX_DENIED_CIDRS": "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
            "PYTHONDONTWRITEBYTECODE": "1", "UV_CACHE_DIR": "/tmp/uv-cache",
            "CORE_AGENT_STARTUP_DIAGNOSTICS": "false", "LOG_LEVEL": "ERROR",
        }
        envfile.write_text("".join(f"{key}={value}\n" for key, value in environment.items()))
        envfile.chmod(0o600)

        def wait_for(check, description):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                value = check()
                if value:
                    return value
                time.sleep(.1)
            self.fail(description)

        def start(role):
            name = "execution-replacement-" + uuid.uuid4().hex
            self.addCleanup(subprocess.run, ["docker", "rm", "-f", name], capture_output=True, timeout=30)
            self.docker("run", "-d", "--name", name, "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges=true", "--security-opt", "systempaths=unconfined",
                "--security-opt", f"seccomp={repo / 'deploy/security' / ('oci-' + arch + '.json')}",
                "--device", "/dev/net/tun:/dev/net/tun", "--pids-limit", "512", "--cpus", "2",
                "--memory", "1g", "--tmpfs", "/tmp:rw,exec,mode=1777,size=512m",
                "--mount", f"type=bind,src={repo / 'tests'},dst=/app/tests,readonly",
                "--publish", "127.0.0.1::8000", "--env-file", str(envfile), "--env", f"RESTART_ROLE={role}",
                "--entrypoint", "sh", image, "-ec",
                'exec "$(uv python find --offline --no-python-downloads /app/.venv/bin/python)" '
                '/app/tests/test_execution_restart_docker.py --replacement-worker')
            endpoint = "http://" + self.docker("port", name, "8000/tcp").strip()
            client = httpx.Client(base_url=endpoint, timeout=3, trust_env=False,
                                 headers={"Origin": environment["AGENT_URL"], "A2A-Version": "1.0"})
            self.addCleanup(client.close)
            def ready():
                try:
                    return client.get("/health/ready").status_code == 200
                except httpx.TransportError:
                    state = json.loads(self.docker("inspect", name))[0]["State"]
                    if state["Status"] in {"exited", "dead"}:
                        self.fail("native application exited:\n" + self.docker("logs", name)[-8000:])
                    return False
            try:
                wait_for(ready, "native application did not become ready")
            except self.failureException:
                self.fail("native application did not become ready:\n" + self.docker("logs", name)[-8000:])
            self.assertEqual(client.post("/api/auth/login", json={"password": password}).status_code, 200)
            # Local HTTP is the post-TLS-hop transport. Preserve the real signed
            # Secure cookie and configured HTTPS Origin; TLS has its own gate.
            self.assertTrue(client.cookies.jar)
            client.headers["Cookie"] = "; ".join(f"{cookie.name}={cookie.value}" for cookie in client.cookies.jar)
            return name, client

        # A real native command has started before any container interruption.
        old, first = start("original")
        sent = first.post("/a2a/owner/message:send", json={"message": {
            "messageId": uuid.uuid4().hex, "contextId": context, "role": "ROLE_USER",
            "parts": [{"text": "Run the controlled interrupted command"}]},
            "configuration": {"returnImmediately": True}})
        self.assertEqual(sent.status_code, 200)
        task_id = sent.json()["task"]["id"]
        marker = f"/api/chats/{context}/files/content?path=once.txt"
        wait_for(lambda: first.get(marker).content == b"once", "terminal command did not write its marker")
        original = store.lookup_task(task_id)
        self.assertEqual(original.tenant_id, tenant)
        self.assertEqual(original.state, "EXECUTING")
        owner = original.snapshot["execution_owner"]
        self.assertTrue(owner["local_execution_pending"])
        self.assertFalse(owner["cleanup_confirmed"])
        self.assertEqual(owner["execution_host"]["slot"], self.docker("inspect", old, "--format", "{{.Id}}").strip())
        command = original.snapshot["pending_call"]["arguments"]["argv"]
        def command_process():
            rows = [line.split(None, 2) for line in self.docker("top", old, "-eo", "pid,stat,args").splitlines()[1:]]
            matches = [row for row in rows if len(row) == 3 and row[2] == " ".join(command)]
            self.assertEqual(len(matches), 1, "the exact native command must still be present")
            self.assertNotIn(matches[0][1][0], {"Z", "X", "x"})
            return matches[0][0]
        process_id = command_process()
        self.assertEqual(self.docker("logs", old).count("RESTART_MODEL_CALL"), 1)
        self.docker("kill", "--signal", "STOP", old)  # Freeze PID 1, not its child command.
        wait_for(lambda: self.docker("exec", old, "cat", "/proc/1/stat").rsplit(")", 1)[1].split()[0] == "T",
                 "old application did not stop heartbeating")
        self.assertTrue(json.loads(self.docker("inspect", old))[0]["State"]["Running"])
        replacement, second = start("replacement")
        current_host = json.loads(self.docker("exec", replacement, "cat", "/tmp/execution-host.json"))
        self.assertFalse(stopped_by_restart(owner["execution_host"], current_host))
        wait_for(lambda: store.lookup_task(task_id).snapshot.get("execution_cleanup_pending"),
                 "replacement did not retain the uncertain execution")
        pending = store.lookup_task(task_id)
        self.assertEqual(pending.state, "EXECUTING")
        self.assertEqual(pending.snapshot["execution_owner"], owner)
        self.assertEqual(pending.snapshot["pending_call"], original.snapshot["pending_call"])
        self.assertEqual(command_process(), process_id)
        projected = second.get(f"/a2a/owner/tasks/{task_id}").json()
        self.assertEqual(projected["metadata"]["core_agent_execution"]["state"], "cleanup_pending")
        self.assertEqual(second.get(marker).status_code, 404)

        # Docker stopped-state is evidence for this exact owned container only.
        self.docker("kill", "--signal", "KILL", old)
        def stopped():
            state = json.loads(self.docker("inspect", old))[0]["State"]
            return not state["Running"] and state["Pid"] == 0
        wait_for(stopped, "Docker did not confirm the exact old container stopped")
        proof = dict(version=1, tenant_id=tenant, owner_id=original.owner_id, run_id=original.run_id,
                     **{key: owner[key] for key in ("instance_id", "worker_id", "generation")},
                     evidence="Docker confirmed stopped container " + owner["execution_host"]["slot"])
        def confirm(value):
            self.docker("exec", "-i", replacement, "sh", "-ec", "umask 077; cat > /tmp/stop.json", input=json.dumps(value))
            return self.docker("exec", replacement, "uv", "run", "--no-sync", "core-agent-db",
                               "confirm-execution-stop", "--file", "/tmp/stop.json")
        with self.assertRaises(subprocess.CalledProcessError):
            confirm({**proof, "generation": "wrong-generation"})
        self.assertFalse(store.lookup_task(task_id).snapshot["execution_owner"]["cleanup_confirmed"])
        confirm(proof)
        confirm(proof)
        wait_for(lambda: store.lookup_task(task_id).state == "ABORTED",
                 "confirmed interrupted command did not settle automatically")
        settled = store.lookup_task(task_id)
        self.assertEqual(settled.error_code, "SIDE_EFFECT_UNKNOWN")
        self.assertTrue(settled.snapshot["execution_owner"]["cleanup_confirmed"])
        self.assertEqual(settled.snapshot["execution_owner"]["cleanup_proof"]["kind"], "operator")
        projected = second.get(f"/a2a/owner/tasks/{task_id}").json()
        self.assertEqual(projected["status"]["state"], "TASK_STATE_FAILED")
        self.assertNotIn("core_agent_execution", projected.get("metadata", {}))
        self.assertEqual(second.get(marker).status_code, 404)
        with database.transaction() as connection:
            events = connection.execute("SELECT kind FROM core_events WHERE run_id=%s", (original.run_id,)).fetchall()
            audits = connection.execute("SELECT data FROM core_audit_records WHERE tenant_id=%s AND run_id=%s AND kind=%s",
                (tenant, original.run_id, "execution.stop_confirmed")).fetchall()
        self.assertEqual(sum(row["kind"] == "model.attempt.started" for row in events), 1)
        self.assertEqual(sum(row["kind"] == "tool.intent" for row in events), 1)
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["data"]["generation"], owner["generation"])
        self.assertNotIn("RESTART_MODEL_CALL", self.docker("logs", replacement))
        # The existing recovery loop continues scanning; terminal state stays stable.
        time.sleep(1.1)
        self.assertEqual(store.lookup_task(task_id).version, settled.version)


def _replacement_worker():
    """PID-1 application fixture, using packaged runtime and native launcher."""
    import uvicorn
    from core_agent import runtime
    from core_agent.app import create_app
    from core_agent.database import PostgresDatabase
    from core_agent.guardrails import GuardrailClassifier
    from core_agent.interactions import tool_origin
    from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
    from core_agent.sandbox import SandboxLauncher

    runtime.WORKFLOW_LEASE_TTL = 6
    runtime.WORKFLOW_LEASE_HEARTBEAT_INTERVAL = 1
    class ClearModel:
        def generate(self, **kwargs):
            return ModelResponse(message='{"verdict":"clear"}', finish_reason="stop")
        def count_tokens(self, text):
            return max(1, len(text.encode()) // 3)
    class CountingModel(ScriptedModel):
        def generate(self, **kwargs):
            print("RESTART_MODEL_CALL", flush=True)
            return super().generate(**kwargs)
    code = "from pathlib import Path; import time; Path('once.txt').open('a').write('once'); time.sleep(120) # " + os.environ["RESTART_NONCE"]
    responses = [ModelResponse(tool_requests=(ToolRequest("once", "core_terminal_exec", {
        "argv": ["/usr/local/bin/python3", "-c", code], "timeout": 120}),))] if os.environ["RESTART_ROLE"] == "original" else []
    model = CountingModel(responses)
    model.model = "native-replacement-fixture"
    app = create_app(model=model, database=PostgresDatabase(os.environ["DATABASE_URL"], min_size=0, max_size=4),
                     guardrail_classifier=GuardrailClassifier(ClearModel()))
    agent = app.state.core_agent
    manager = agent.tool_runtime.environment_manager
    assert isinstance(manager.backend.launcher, SandboxLauncher)
    assert os.getpid() == 1 and manager.execution_host is not None
    Path("/tmp/execution-host.json").write_text(json.dumps(manager.execution_host))
    name, tenant = "core_terminal_exec", os.environ["CORE_AGENT_TENANT_ID"]
    current = agent.interaction_store.get_policy(tenant, name, tool_origin(name))
    if current.mode != "allow":
        agent.interaction_store.update_policy(tenant, name, current.origin, mode="allow", guardrails_exempt=False,
                                             expected_revision=current.revision, actor_id="native-test")
    try:
        uvicorn.run(app, host="0.0.0.0", port=8000, log_level="error")
    finally:
        app.state.close()


if __name__ == "__main__" and sys.argv[1:] == ["--replacement-worker"]:
    _replacement_worker()
