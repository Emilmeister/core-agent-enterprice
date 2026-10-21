"""Operator startup diagnostics must explain the failing stage without secrets."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from core_agent import app as application
from core_agent.errors import CoreError, SandboxDiagnostics
from core_agent.model import ScriptedModel
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
