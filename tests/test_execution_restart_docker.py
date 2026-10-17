"""Native Docker lifetime proof; required in the sandbox CI job."""
import json
import os
import subprocess
import time
import unittest
import uuid
from pathlib import Path

from core_agent.execution_identity import stopped_by_restart


@unittest.skipUnless(os.getenv("CORE_AGENT_RESTART_TEST_IMAGE"), "set CORE_AGENT_RESTART_TEST_IMAGE for native Docker restart")
class DockerRestartTests(unittest.TestCase):
    def docker(self, *arguments):
        return subprocess.run(["docker", *arguments], check=True, capture_output=True, text=True, timeout=60).stdout

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
