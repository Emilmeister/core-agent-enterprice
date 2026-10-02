"""Explicit local execution adapter for portable unit tests, never isolation proof.

Native Linux tests import the real composition root and do not use this module.
"""
import os
import signal
import subprocess
import sys
from pathlib import Path

from core_agent.app import create_app as production_create_app
from core_agent.guardrails import GuardrailClassifier
from core_agent.model import ModelResponse


class ClearGuardrailModel:
    """Deterministic fixture; runtime guardrail behavior has its own tests."""
    def generate(self, **kwargs):
        return ModelResponse(message='{"verdict":"clear"}', finish_reason="stop")

    def count_tokens(self, text):
        return max(1, (len(text.encode("utf-8")) + 2) // 3)


class UnsandboxedTestProcess:
    def __init__(self, process):
        self.process = process
        self.pid = process.pid

    @property
    def returncode(self):
        return self.process.returncode

    def wait(self, timeout=None):
        return self.process.wait(timeout)

    def stop(self):
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return self.process.wait(timeout=3)


class UnsandboxedTestLauncher:
    """No namespaces or network policy: only exercise application plumbing."""
    def __init__(self):
        self.processes = []
        self._unhealthy = False
        self._closed = False

    def preflight(self):
        pass

    def start(self, argv, *, workspace_path, readonly_input_path, cwd=".",
              environment=None, stdin=None, stdout=None, stderr=None,
              broker_socket=None, timeout=None):
        argv = list(argv)
        if broker_socket is not None:
            assert argv[0] == "/usr/local/bin/python3"
            assert argv[-2] == "/run/core-agent/broker.sock"
            argv[0], argv[-2] = sys.executable, str(broker_socket)
        environment = dict(environment or {})
        environment["HOME"] = str(workspace_path)
        environment["TMPDIR"] = str(Path(workspace_path) / ".tmp")
        Path(environment["TMPDIR"]).mkdir(exist_ok=True)
        handle = UnsandboxedTestProcess(subprocess.Popen(
            argv, cwd=Path(workspace_path) / cwd, env=environment,
            stdin=stdin, stdout=stdout, stderr=stderr,
            start_new_session=True, close_fds=True,
        ))
        self.processes.append(handle)
        return handle

    def close(self):
        self._closed = True
        for process in self.processes:
            if process.returncode is None:
                process.stop()


def create_app(**kwargs):
    kwargs.setdefault("sandbox_launcher", UnsandboxedTestLauncher())
    injected = "guardrail_classifier" not in kwargs
    if injected:
        kwargs["guardrail_classifier"] = GuardrailClassifier(ClearGuardrailModel())
    app = production_create_app(**kwargs)
    if injected:
        kwargs["guardrail_classifier"].clock = lambda: app.state.core_agent.workflow_store.current_time()
    return app
