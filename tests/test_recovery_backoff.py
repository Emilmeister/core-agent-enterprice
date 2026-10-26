import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core_agent.errors import CoreError
from core_agent.model import ModelResponse, ScriptedModel
from tests.app_support import create_app


class RecoveryBackoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        environment = patch.dict(os.environ, {
            "CORE_AGENT_ENVIRONMENT": "test", "CORE_AGENT_MEMORY": "disabled",
            "LOCAL_WORKSPACE_ROOT": self.temp.name + "/scratch",
            "CHAT_WORKSPACE_ROOT": self.temp.name + "/chats",
            "CORE_AGENT_ALLOWED_BUILTIN_TOOLS": "core_task_list", "LOG_LEVEL": "ERROR",
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        model = ScriptedModel([ModelResponse(message="unused")])
        model.model = "backoff-test"
        with patch("core_agent.runtime.CoreAgent.recover_workflows", return_value=()):
            app = create_app(model=model)
        self.addCleanup(app.state.close)
        self.agent = app.state.core_agent
        self.candidate = SimpleNamespace(task_id="task", run_id="run", tenant_id=None)
        self.now = [1000.0]
        clock = patch("core_agent.runtime.time.monotonic", side_effect=lambda: self.now[0])
        clock.start()
        self.addCleanup(clock.stop)

    def fail_once(self):
        with patch.object(self.agent, "resume_task", side_effect=CoreError("MODEL_UNAVAILABLE", "model HTTP 400: bad")):
            self.agent._resume_recovered_workflow(self.candidate, None)

    def test_provider_failures_pause_recovery_with_growing_delay(self):
        self.fail_once()
        self.assertEqual(self.agent._recovery_backoff["task"], (1, 1030.0))
        with patch("threading.Thread.start"):
            self.assertFalse(self.agent._launch_recovery(self.candidate))
            self.now[0] = 1031.0
            self.assertTrue(self.agent._launch_recovery(self.candidate))
        self.agent._recovery_workers.clear()
        self.fail_once()
        self.assertEqual(self.agent._recovery_backoff["task"], (2, 1091.0))

    def test_success_clears_pause_and_long_run_resets_count(self):
        self.fail_once()
        self.now[0] = 1031.0 + 601
        self.fail_once()
        self.assertEqual(self.agent._recovery_backoff["task"][0], 1)
        with patch.object(self.agent, "resume_task", return_value=None):
            self.agent._resume_recovered_workflow(self.candidate, None)
        self.assertNotIn("task", self.agent._recovery_backoff)

    def test_other_errors_do_not_pause_recovery(self):
        with patch.object(self.agent, "resume_task", side_effect=CoreError("TOOL_UNAVAILABLE")):
            self.agent._resume_recovered_workflow(self.candidate, None)
        self.assertNotIn("task", self.agent._recovery_backoff)


if __name__ == "__main__":
    unittest.main()
