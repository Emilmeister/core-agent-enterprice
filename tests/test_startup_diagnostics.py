"""Operator startup diagnostics must explain the failing stage without secrets."""
import json
import os
import platform
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from core_agent import app as application
from core_agent.errors import CoreError, SandboxDiagnostics
from core_agent.model import ScriptedModel
from core_agent.startup_diagnostics import StartupDiagnostics
from tests.app_support import UnsandboxedTestLauncher


class StartupDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = patch.dict(os.environ, {
            "CORE_AGENT_ENVIRONMENT": "test",
            "CORE_AGENT_STARTUP_DIAGNOSTICS": "true",
            "CORE_AGENT_MEMORY": "disabled",
            "LOCAL_WORKSPACE_ROOT": self.directory.name + "/scratch",
            "LLM_API_KEY": "private-key-never-print",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.model = ScriptedModel([])
        self.model.model = "diagnostic-test"

    def records(self, capture):
        rows = []
        for record in capture.records:
            try:
                row = json.loads(record.getMessage())
            except ValueError:
                continue
            if row.get("event", "").startswith("startup."):
                rows.append(row)
        return rows

    def test_configuration_failure_identifies_stage_without_environment_values(self):
        os.environ["CORE_AGENT_ENVIRONMENT"] = "private-invalid-value"
        with self.assertLogs("core_agent.runtime", level="INFO") as logs:
            with self.assertRaises(CoreError):
                application.create_app(model=self.model)
        failed = [r for r in self.records(logs) if r.get("status") == "failed"]
        self.assertTrue(any(r.get("stage") == "configuration" for r in failed))
        self.assertIn("CONFIG_INVALID", str(failed))
        self.assertNotIn("private-invalid-value", str(logs.output))
        self.assertNotIn("private-key-never-print", str(logs.output))

    def test_cloud_trial_ignores_platform_storage_settings_without_using_credentials(self):
        cloud = {
            "CORE_AGENT_AUTH_MODE": "cloudru", "CORE_AGENT_TENANT_ID": "trial",
            "CORE_AGENT_OWNER_PASSWORD": "private-owner-password",
            "AGENT_URL": "https://agent.example.test",
            "CHAT_WORKSPACE_ROOT": self.directory.name + "/chats",
            "ARTIFACT_STORAGE_TYPE": "s3",
            "ARTIFACT_S3_SECRET_ACCESS_KEY": "private-platform-s3-key",
        }
        with patch.dict(os.environ, cloud), self.assertLogs("core_agent.runtime", level="INFO") as logs:
            instance = application.create_app(model=self.model, sandbox_launcher=UnsandboxedTestLauncher())
            instance.state.close()
        self.assertIn({"event": "startup.storage", "backend": "temporary-posix", "durable": False},
                      self.records(logs))
        self.assertNotIn("private-platform-s3-key", str(logs.output))
        with patch.dict(os.environ, {"ARTIFACT_STORAGE_TYPE": "s3"}), self.assertRaises(CoreError):
            application.create_app(model=self.model, sandbox_launcher=UnsandboxedTestLauncher())

    def test_preflight_failure_preserves_finite_sandbox_details(self):
        launcher = UnsandboxedTestLauncher()
        diagnostic = SandboxDiagnostics(stage="namespace", reason="os_error", elapsed_ms=5,
                                        cleanup="confirmed", errno=1)
        launcher.preflight = lambda: (_ for _ in ()).throw(CoreError(
            "EXECUTION_ENVIRONMENT_UNAVAILABLE", "private-error-detail", diagnostics=diagnostic))
        with self.assertLogs("core_agent.runtime", level="INFO") as logs:
            with self.assertRaises(CoreError):
                application.create_app(model=self.model, sandbox_launcher=launcher)
        failures = [r for r in self.records(logs) if r.get("stage") == "sandbox" and r.get("status") == "failed"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["sandbox"], diagnostic.as_dict())
        self.assertNotIn("private-error-detail", str(logs.output))
        self.assertTrue(launcher._closed)

    def test_s3_diagnostics_show_effective_settings_without_credentials(self):
        from urllib.parse import quote

        secret = "private-s3/secret+" * 50
        cloud = {
            "CORE_AGENT_AUTH_MODE": "cloudru", "CORE_AGENT_ENVIRONMENT": "development",
            "CORE_AGENT_TENANT_ID": "trial", "CORE_AGENT_OWNER_PASSWORD": "private-owner-password",
            "AGENT_URL": "https://agent.example.test", "CHAT_WORKSPACE_ROOT": self.directory.name + "/chats",
            "WORKSPACE_STORAGE_TYPE": "s3", "ARTIFACT_S3_BUCKET": "platform_bucket",
            "ARTIFACT_S3_REGION": "platform-region", "ARTIFACT_S3_ACCESS_KEY_ID": "private-access-key",
            "ARTIFACT_S3_TENANT_ID": "storage-tenant",
            "ARTIFACT_S3_SECRET_ACCESS_KEY": secret, "WORKSPACE_S3_SESSION_TOKEN": "private-session-token",
            "ARTIFACT_S3_ENDPOINT_URL": "https://private-user:private-password@s3.example.test/?token=private-query#private-fragment",
        }
        for enabled, endpoint, expected in (
            ("true", cloud["ARTIFACT_S3_ENDPOINT_URL"], "https://s3.example.test/"),
            ("false", cloud["ARTIFACT_S3_ENDPOINT_URL"], None),
            ("true", "https://s3.example.test/" + quote(secret, safe=""), "[REDACTED]"),
            ("true", "https://s3.example.test/private-access-key", "[REDACTED]"),
        ):
            with self.subTest(enabled=enabled, expected=expected), patch.dict(os.environ, {
                    **cloud, "CORE_AGENT_STARTUP_DIAGNOSTICS": enabled,
                    "ARTIFACT_S3_ENDPOINT_URL": endpoint}), patch.object(
                    application, "_state", side_effect=CoreError("CONFIG_INVALID", "stop before database")):
                capture = self.assertLogs if enabled == "true" else self.assertNoLogs
                with capture("core_agent.runtime", level="INFO") as logs:
                    with self.assertRaises(CoreError):
                        application.create_app(model=self.model, sandbox_launcher=UnsandboxedTestLauncher())
                rows = [row for row in self.records(logs) if row["event"] == "startup.s3_configuration"] if logs else []
                self.assertEqual(rows, [{"event": "startup.s3_configuration",
                    "endpoint_url": expected,
                    "bucket": "platform_bucket", "region": "platform-region"}] if enabled == "true" else [])
                for value in ("private-user", "private-password", "private-query", "private-fragment",
                              "private-access-key", "private-s3", "private-session-token", "private-owner-password"):
                    self.assertNotIn(value, str(logs.output) if logs else "")

    def test_success_logs_stages_and_disabled_switch_suppresses_diagnostics(self):
        for enabled in ("true", "false"):
            with self.subTest(enabled=enabled), patch.dict(os.environ, {"CORE_AGENT_STARTUP_DIAGNOSTICS": enabled}):
                with self.assertLogs("core_agent.runtime", level="INFO") as logs:
                    instance = application.create_app(model=self.model, sandbox_launcher=UnsandboxedTestLauncher())
                    instance.state.close()
                stages = [r for r in self.records(logs) if r.get("event") == "startup.stage"]
                if enabled == "true":
                    self.assertTrue(any(r.get("stage") == "application_ready" and r.get("status") == "succeeded" for r in stages))
                    self.assertTrue(any(r.get("stage") == "database" and r.get("status") == "succeeded" for r in stages))
                    facts = [r for r in self.records(logs) if r.get("event") == "startup.platform"]
                    self.assertEqual(len(facts), 1)
                    self.assertEqual(set(facts[0]["helpers"]), {"bwrap", "slirp4netns", "nft", "nsenter", "ip"})
                else:
                    self.assertEqual(stages, [])

    def test_main_sanitizes_unexpected_database_failure(self):
        with patch.object(application, "_model", return_value=self.model), patch.object(
                application, "_state", side_effect=RuntimeError("private-password-in-exception")):
            with self.assertLogs("core_agent.runtime", level="INFO") as logs:
                with self.assertRaises(SystemExit) as error:
                    application.main()
        self.assertEqual(error.exception.code, 1)
        self.assertNotIn("private-password-in-exception", str(logs.output))
        failures = [r for r in self.records(logs) if r.get("status") == "failed"]
        self.assertTrue(any(r.get("stage") == "database" for r in failures))

    def test_failed_stage_remains_visible_at_critical_log_level(self):
        with patch.dict(os.environ, {"LOG_LEVEL": "CRITICAL"}), patch.object(
                application, "_model", return_value=self.model), patch.object(
                application, "_state", side_effect=RuntimeError("private-database-detail")):
            with self.assertLogs("core_agent.runtime", level="CRITICAL") as logs:
                with self.assertRaises(SystemExit):
                    application.main()
        failures = [r for r in self.records(logs) if r.get("status") == "failed"]
        self.assertTrue(any(r.get("stage") == "database" for r in failures))
        self.assertNotIn("private-database-detail", str(logs.output))

    @unittest.skipUnless(platform.system() == "Linux", "real Linux process cleanup")
    def test_sandbox_probe_reports_denied_operation_without_stderr_or_environment(self):
        from core_agent.sandbox_diagnostics import _run_probe
        result = _run_probe([sys.executable, "-I", "-S", "-c",
            "import os,sys; assert 'LLM_API_KEY' not in os.environ; "
            "sys.stderr.write('bwrap: Creating new namespace failed: Operation not permitted\\n'"
            " + 'private-key-never-print /private/workspace'); sys.exit(1)"], timeout=2)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["operation"], "namespace_create")
        self.assertEqual(result["errno"], 1)
        self.assertEqual(result["returncode"], 1)
        self.assertEqual(result["cleanup"], "confirmed")
        self.assertNotIn("private", str(result))

    @unittest.skipUnless(platform.system() == "Linux", "real Linux process cleanup")
    def test_sandbox_probe_bounds_output_and_timeout(self):
        from core_agent.sandbox_diagnostics import _run_probe
        for code, timeout, reason in (
            ("import sys; sys.stderr.write('secret' * 10000)", 2, "output_limit"),
            ("import time; time.sleep(60)", 0.1, "timeout"),
            ("import sys; sys.stderr.write('unknown private detail'); sys.exit(7)", 2, "unclassified"),
        ):
            with self.subTest(reason=reason):
                started = time.monotonic()
                result = _run_probe([sys.executable, "-I", "-S", "-c", code], timeout=timeout)
                self.assertEqual(result["reason"], reason)
                self.assertEqual(result["cleanup"], "confirmed")
                self.assertNotIn("secret", str(result))
                self.assertNotIn("private", str(result))
                self.assertLess(time.monotonic() - started, 4)

    @unittest.skipUnless(platform.system() == "Linux", "real Linux process cleanup")
    def test_sandbox_probe_stops_children_after_parent_exit_and_timeout(self):
        from pathlib import Path
        from core_agent.sandbox_diagnostics import _run_probe
        for parent_delay in (0, 60):
            with self.subTest(parent_delay=parent_delay):
                pid_file = Path(self.directory.name) / "child-pid"
                code = ("import os,time; from pathlib import Path; child=os.fork(); "
                        "child == 0 and time.sleep(60); "
                        f"Path({str(pid_file)!r}).write_text(str(child)); time.sleep({parent_delay})")
                result = _run_probe([sys.executable, "-I", "-S", "-c", code], timeout=0.5)
                self.assertEqual(result["cleanup"], "confirmed")
                self.assertEqual(result["reason"], "completed" if parent_delay == 0 else "timeout")
                child = int(pid_file.read_text())
                try:
                    fields = Path(f"/proc/{child}/stat").read_bytes().rsplit(b")", 1)[1].split()
                except FileNotFoundError:
                    continue
                self.assertIn(fields[0], (b"Z", b"X"))

    def test_sandbox_probe_budget_and_permission_message_are_not_success(self):
        from core_agent.sandbox_diagnostics import _failure, run_probes
        for message in (b"bwrap: No permissions to create new namespace, private details",
                        b"bwrap: No permissions to create a new namespace, private details"):
            projected = _failure(message)
            self.assertEqual(projected, {"reason": "permission_denied", "operation": "namespace_create"})
        with patch("core_agent.sandbox_diagnostics.time.monotonic", side_effect=[0] + [11] * 13), \
                patch("core_agent.sandbox_diagnostics._run_probe") as launch, \
                self.assertLogs("core_agent.runtime", level="CRITICAL") as logs:
            run_probes(StartupDiagnostics(True))
        launch.assert_not_called()
        self.assertTrue(all(r["status"] == "skipped" and r["reason"] == "budget_exhausted"
                            for r in self.records(logs)))

    def test_sandbox_probe_disabled_or_non_linux_never_spawns(self):
        with patch("subprocess.Popen") as launch:
            StartupDiagnostics(False).sandbox()
            with patch("platform.system", return_value="Darwin"):
                StartupDiagnostics(True).sandbox()
        launch.assert_not_called()

    def test_sandbox_probe_failure_logs_at_critical_without_replacing_preflight(self):
        from core_agent.sandbox_diagnostics import run_probes
        denied = {"status": "failed", "reason": "permission_denied", "errno": 1,
                  "operation": "namespace_create", "returncode": 1, "cleanup": "confirmed"}
        with patch("core_agent.sandbox_diagnostics._run_probe", return_value=denied), \
                self.assertLogs("core_agent.runtime", level="CRITICAL") as logs:
            run_probes(StartupDiagnostics(True))
        rows = self.records(logs)
        self.assertEqual({r["probe"] for r in rows}, {
            "user_namespace", "user_root_mapping", "mount_namespace", "pid_namespace",
            "ipc_namespace", "network_namespace", "uts_namespace", "bwrap_minimal",
            "bwrap_proc", "bwrap_full",
            "landlock_abi", "landlock_filesystem", "seccomp_filter",
        })
        launcher = UnsandboxedTestLauncher()
        launcher.preflight = lambda: (_ for _ in ()).throw(CoreError(
            "EXECUTION_ENVIRONMENT_UNAVAILABLE", "Sandbox did not start"))
        with patch.object(application, "SandboxLauncher", return_value=launcher), \
                patch.object(application, "SandboxPolicy"), \
                patch.object(StartupDiagnostics, "sandbox") as probe, \
                self.assertRaises(CoreError) as error:
            application.create_app(model=self.model)
        probe.assert_called_once()
        self.assertEqual(error.exception.code, "EXECUTION_ENVIRONMENT_UNAVAILABLE")
        self.assertTrue(launcher._closed)

    def test_sandbox_probe_cleanup_failure_stops_probes(self):
        from core_agent.sandbox_diagnostics import run_probes
        with patch("core_agent.sandbox_diagnostics._run_probe", return_value={
                "status": "failed", "reason": "timeout", "cleanup": "unconfirmed"}) as launch, \
                self.assertLogs("core_agent.runtime", level="CRITICAL"), \
                self.assertRaises(CoreError) as error:
            run_probes(StartupDiagnostics(True))
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(error.exception.code, "EXECUTION_ENVIRONMENT_UNAVAILABLE")

    def test_capability_output_rejects_unknown_or_inconsistent_fields(self):
        from core_agent.sandbox_capability_probe import parse_result
        good = {"status": "succeeded", "reason": "completed", "operation": "landlock_abi", "abi": 6}
        self.assertEqual(parse_result(json.dumps(good).encode(), 0), good)
        for row, code in ((dict(good, secret="private-key-never-print"), 0),
                          (dict(good, abi=True), 0), (dict(good, operation="private-path"), 0),
                          (good, 1), (dict(good, reason="permission_denied"), 0)):
            with self.subTest(row=row, code=code):
                result = parse_result(json.dumps(row).encode(), code)
                self.assertEqual(result, {"status": "failed", "reason": "invalid_probe_result"})
        self.assertEqual(parse_result(b"private traceback", 1)["reason"], "invalid_probe_result")

    @unittest.skipUnless(platform.system() == "Linux", "real Linux capability probes")
    def test_capability_probes_are_isolated_and_preserve_parent_access(self):
        from pathlib import Path
        from core_agent.sandbox_diagnostics import _run_probe
        from core_agent import sandbox_capability_probe
        helper = str(Path(sandbox_capability_probe.__file__).resolve())
        for name in ("landlock_abi", "landlock_filesystem", "seccomp_filter"):
            with self.subTest(probe=name):
                result = _run_probe([sys.executable, "-I", "-S", helper, name], timeout=2, structured=True)
                self.assertEqual(result["cleanup"], "confirmed")
                self.assertIn(result["status"], {"succeeded", "failed"})
                if result["reason"] == "os_error":
                    # libseccomp may report ECANCELED when its load syscall is
                    # unavailable under CPU emulation. This must remain a failure.
                    self.assertEqual((result["status"], result["operation"], result["errno"]),
                                     ("failed", "seccomp_load", 125))
                else:
                    self.assertIn(result["reason"], {"completed", "permission_denied", "unsupported", "disabled"})
                self.assertNotIn("private", str(result))
                self.assertGreaterEqual(os.getppid(), 0)
                with open("/dev/zero", "rb") as source:
                    self.assertEqual(source.read(1), b"\0")
