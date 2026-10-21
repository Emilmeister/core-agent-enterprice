"""Cloud profile contract; native enforcement is a separate mandatory gate."""
import os
from pathlib import Path
import platform
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from core_agent.errors import CoreError, ExecutionNotStarted
from core_agent.sandbox import SandboxPolicy
from core_agent.sandbox_landlock import LandlockLauncher, LandlockProcess, validate_backend, _validate_workspace


class LandlockConfigurationTests(unittest.TestCase):
    def test_explicit_cloud_development_only(self):
        self.assertEqual(validate_backend("", cloud=False, environment="production"), "bubblewrap")
        self.assertEqual(validate_backend("landlock", cloud=True, environment="development"), "landlock")
        for backend, cloud, environment in [
            ("auto", True, "development"), ("landlock", False, "development"),
            ("landlock", True, "production"), ("landlock", True, "test"),
        ]:
            with self.subTest(backend=backend, cloud=cloud, environment=environment):
                with self.assertRaises(CoreError) as raised:
                    validate_backend(backend, cloud=cloud, environment=environment)
                self.assertEqual(raised.exception.code, "CONFIG_INVALID")

    def test_readonly_input_layout_fails_before_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            inputs = root / "attachments"
            inputs.mkdir()
            _validate_workspace(root, inputs)
            (inputs / "original.txt").write_text("unchanged")
            with self.assertRaises(CoreError):
                _validate_workspace(root, inputs)
            self.assertEqual((inputs / "original.txt").read_text(), "unchanged")

    def test_missing_security_is_never_plain_subprocess_fallback(self):
        policy = SandboxPolicy(("1.1.1.1",), ("10.0.0.0/8",))
        launcher = LandlockLauncher(policy)
        with patch.object(launcher, "_native_check", side_effect=CoreError("EXECUTION_ENVIRONMENT_UNAVAILABLE")):
            with patch("core_agent.sandbox.subprocess.Popen") as process:
                with self.assertRaises(ExecutionNotStarted):
                    launcher.start(["true"], workspace_path="/workspace", readonly_input_path="/workspace/attachments")
                process.assert_not_called()
        launcher.close()

    def test_scratch_is_preserved_when_process_death_is_unconfirmed(self):
        import shutil
        launcher = LandlockLauncher(SandboxPolicy(("1.1.1.1",), ("10.0.0.0/8",)))
        process = LandlockProcess(launcher, Mock(pid=1234), Mock())
        scratch = Path(process._launch_resources()["scratch"])
        self.addCleanup(shutil.rmtree, scratch)
        (scratch / "canary").write_text("preserve")
        with patch("core_agent.sandbox_landlock._stop_children", side_effect=OSError("unconfirmed")):
            with patch.object(process, "_close"):
                with self.assertRaises(CoreError):
                    process._emergency_cleanup()
        self.assertEqual((scratch / "canary").read_text(), "preserve")
        process._process.wait.assert_not_called()
        self.assertTrue(launcher._unhealthy)

    def test_failed_scratch_removal_does_not_signal_a_reaped_group(self):
        import shutil
        launcher = LandlockLauncher(SandboxPolicy(("1.1.1.1",), ("10.0.0.0/8",)))
        process = LandlockProcess(launcher, Mock(pid=1234), Mock())
        scratch = Path(process._launch_resources()["scratch"])
        self.addCleanup(shutil.rmtree, scratch)
        with patch("core_agent.sandbox_landlock.shutil.rmtree", side_effect=PermissionError("blocked")):
            with self.assertRaises(CoreError):
                process._cleanup_resources()
            with patch("core_agent.sandbox_landlock._stop_children") as stop:
                with patch.object(process, "_close"):
                    with self.assertRaises(CoreError):
                        process._emergency_cleanup()
                stop.assert_not_called()
        process._process.wait.assert_not_called()
        self.assertTrue(scratch.is_dir())
        self.assertTrue(launcher._unhealthy)


class LandlockNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("CORE_AGENT_REQUIRE_LANDLOCK_TESTS") != "1":
            raise unittest.SkipTest("Requires dedicated native Linux Landlock container; no enforcement claimed")
        if platform.system() != "Linux" or os.getpid() != 1:
            raise AssertionError("Run the mandatory Landlock gate as container PID 1")
        cls.policy = SandboxPolicy(("1.1.1.1",), ("10.0.0.0/8",))
        launcher = LandlockLauncher(cls.policy)
        try:
            launcher.preflight()
        finally:
            launcher.close()

    def test_persistent_workspace_and_process_tree_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            inputs = workspace / "attachments"
            inputs.mkdir()
            launcher = LandlockLauncher(self.policy)
            self.addCleanup(launcher.close)
            process = launcher.start(["sh", "-c", "printf saved > result; sleep 60 &"],
                workspace_path=workspace, readonly_input_path=inputs,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            scratch = process._scratch
            self.assertEqual(process.wait(10), 0)
            self.assertFalse(scratch.exists())
            self.assertEqual((workspace / "result").read_text(), "saved")
            self.assertFalse(launcher._unhealthy)

    def test_python_broker_uses_only_its_inherited_connection(self):
        from core_agent.execution import LocalTerminalBackend, TerminalSessionManager
        from core_agent.python_exec import execute_python
        from core_agent.workspace import WorkspaceBinding
        with tempfile.TemporaryDirectory() as temporary:
            manager = TerminalSessionManager(LocalTerminalBackend(temporary, launcher=LandlockLauncher(self.policy)))
            self.addCleanup(manager.close)
            manager.bind_run("landlock-python", WorkspaceBinding("tenant", "owner", "chat"))
            calls = []
            def dispatch(name, arguments, request_id):
                self.assertTrue(request_id)
                calls.append((name, arguments))
                return {"value": 57500}
            result = execute_python(manager, run_id="landlock-python", code='print(tools.call("demo", {"a": 1})["value"])',
                tool_names=("demo",), dispatch=dispatch)
            self.assertEqual(result.exit_code, 0, result.stdout)
            self.assertIn("57500", result.stdout)
            self.assertEqual(calls, [("demo", {"a": 1})])

    def test_cancel_and_supervisor_death_confirm_process_tree_stop(self):
        from core_agent.sandbox_landlock import _group_members
        import time
        for failure in ("cancel", "supervisor_death"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary)
                inputs = workspace / "attachments"
                inputs.mkdir()
                launcher = LandlockLauncher(self.policy)
                process = launcher.start(["sh", "-c", "sleep 60 & sleep 60"],
                    workspace_path=workspace, readonly_input_path=inputs,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                scratch = process._scratch
                (scratch / "canary").write_text("temporary")
                if failure == "cancel":
                    process.stop()
                else:
                    os.kill(process.pid, signal.SIGKILL)
                    with self.assertRaises(CoreError) as raised:
                        process.wait(5)
                    self.assertEqual(raised.exception.code, "SIDE_EFFECT_UNKNOWN")
                self.assertFalse(any(not dead for _, dead in _group_members(process.pid, time.monotonic() + 1)))
                self.assertFalse(scratch.exists())
                launcher.close()

    def test_cleanup_stops_worker_after_its_main_thread_exits(self):
        import select
        import time

        code = """
import ctypes, os, pathlib, platform, sys, threading, time
if sys.argv[1] == 'normal' and os.fork():
    while not pathlib.Path('release-parent').exists():
        time.sleep(0.01)
    os._exit(0)
descriptor = os.open('heartbeat', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
def write_forever():
    while True:
        os.write(descriptor, b'x')
        time.sleep(0.01)
threading.Thread(target=write_forever, daemon=True).start()
pathlib.Path('leader.pid').write_text(str(os.getpid()))
ctypes.CDLL(None).syscall(93 if platform.machine() == 'aarch64' else 60, 0)
"""
        for completion in ("normal", "cancel"):
            with self.subTest(completion=completion), tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary)
                inputs = workspace / "attachments"
                inputs.mkdir()
                launcher = LandlockLauncher(self.policy)
                process = launcher.start(["/usr/local/bin/python3", "-I", "-S", "-c", code, completion],
                    workspace_path=workspace, readonly_input_path=inputs,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                descriptor = None
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        pid_path = workspace / "leader.pid"
                        if pid_path.exists() and pid_path.read_text():
                            pid = int(pid_path.read_text())
                            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
                            if state == "Z":
                                descriptor = os.pidfd_open(pid)
                                break
                        time.sleep(0.01)
                    self.assertIsNotNone(descriptor, "The worker's main thread did not exit")
                    self.assertFalse(select.select([descriptor], [], [], 0)[0],
                        "The worker must still be alive when its main thread is a zombie")
                    if completion == "normal":
                        (workspace / "release-parent").touch()
                        self.assertEqual(process.wait(10), 0)
                    else:
                        process.stop()
                    self.assertTrue(select.select([descriptor], [], [], 0)[0],
                        "Cleanup returned while a worker thread could still write")
                    size = (workspace / "heartbeat").stat().st_size
                    time.sleep(0.05)
                    self.assertEqual((workspace / "heartbeat").stat().st_size, size)
                    self.assertFalse(launcher._unhealthy)
                finally:
                    if descriptor is not None:
                        try:
                            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        finally:
                            os.close(descriptor)
                    try:
                        process.stop()
                    finally:
                        launcher.close()
