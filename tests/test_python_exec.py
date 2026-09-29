import os
import tempfile
import unittest
from unittest.mock import patch

from core_agent.app import create_app
from core_agent.errors import CoreError
from core_agent.mcp import InMemoryMcpConnector
from core_agent.model import ModelResponse, ScriptedModel, ToolRequest


class InterpreterChoiceTests(unittest.TestCase):
    """The tool must run the interpreter a terminal `pip install` writes to."""

    def test_the_agents_own_virtualenv_is_not_the_interpreter(self):
        import sys
        from pathlib import Path

        from core_agent.python_exec import _interpreter

        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / "python3"
            outside.write_text("#!/bin/sh\n")
            outside.chmod(0o755)
            own = Path(sys.executable).parent
            # The image puts the virtualenv bin first, so answering `which` is
            # answering with the interpreter that has no pip.
            path = os.pathsep.join([str(own), str(directory)])
            with patch.dict(os.environ, {"PATH": path}):
                self.assertEqual(_interpreter(), str(outside))
            with patch.dict(os.environ, {"PATH": str(own)}):
                # Nothing else exists: the virtualenv is the deployment's python.
                self.assertEqual(_interpreter(), sys.executable)

    def test_the_interpreter_keeps_installed_packages_and_drops_the_workspace(self):
        from core_agent.python_exec import RUNNER, execute_python

        captured = {}

        class Recorder:
            def execute_transient(self, request, run_id):
                captured.update(request)
                raise SystemExit

        with self.assertRaises(SystemExit):
            execute_python(
                Recorder(), run_id="run-1", code="pass", tool_names=(), dispatch=None
            )
        argv = captured["argv"]
        self.assertEqual(argv[3:5], ["-c", RUNNER])
        # `-I` would also drop user site-packages, which is exactly where a
        # terminal `pip install` puts the package.
        self.assertNotIn("-I", argv)
        self.assertIn("-P", argv)


class PythonExecTests(unittest.TestCase):
    def _environment(self, workspace, **extra):
        return {
            "CORE_AGENT_ENVIRONMENT": "development",
            "SESSION_STORAGE_TYPE": "in-memory",
            "CORE_AGENT_RUNTIME_MODE": "without_terminal",
            "CORE_AGENT_ALLOWED_BUILTIN_TOOLS": ("core_python_exec,core_task_list"),
            "MCP_ALLOWED_SERVERS": "docs",
            # MCP servers are deployment configuration now.
            "MCP_URL": "https://docs.test/docs",
            "MCP_COLD_START_TIMEOUT_SECONDS": "0",
            "MCP_ALLOWED_TOOLS": "docs.search",
            "LOCAL_WORKSPACE_ROOT": workspace,
            **extra,
        }

    def test_a_workspace_file_is_saved_by_path_without_becoming_a_string(self):
        """Binary a run produced has no reason to travel as base64 to be kept."""
        code = """
payload = bytes(range(256)) * 8
open("deck.pptx", "wb").write(payload)
saved = tools.call("core_artifact_save", {"filename": "deck.pptx", "path": "deck.pptx"})
print(saved["size"], saved["media_type"])
"""
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_requests=(
                        ToolRequest("python-1", "core_python_exec", {"code": code}),
                    )
                ),
                ModelResponse(message="saved"),
            ]
        )
        model.model = "python-test"
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        environment = self._environment(
            workspace.name,
            CORE_AGENT_ALLOWED_BUILTIN_TOOLS="core_python_exec,core_artifact_save",
        )
        with patch.dict(os.environ, environment, clear=True):
            app = create_app(model=model)
        try:
            result = app.state.core_agent.run({"prompt": "keep the deck"})
            self.assertEqual(result.message, "saved")
            self.assertIn("2048", model.calls[1].context)
            # The image has no /etc/mime.types, so this must not depend on one.
            self.assertIn("presentationml", model.calls[1].context)

            stored, content = app.state.core_agent.artifact_service.load(
                app_name=app.state.core_agent.agent_config.agent["name"],
                user_id="anonymous",
                session_id=result.run_id,
                filename="deck.pptx",
            )
            self.assertEqual(content, bytes(range(256)) * 8)
            self.assertEqual(stored.size, 2048)
            # The image ships no /etc/mime.types: this must not depend on one.
            self.assertEqual(
                stored.media_type,
                "application/vnd.openxmlformats-officedocument"
                ".presentationml.presentation",
            )
        finally:
            app.state.close()

    def test_a_path_outside_the_workspace_or_both_inputs_are_refused(self):
        code = """
for arguments in (
    {"filename": "a", "path": "../../etc/passwd"},
    {"filename": "a", "path": "deck.pptx", "content": "text"},
    {"filename": "a"},
):
    try:
        tools.call("core_artifact_save", arguments)
        print("accepted", arguments)
    except ToolCallError as error:
        print(error.code)
"""
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_requests=(
                        ToolRequest("python-1", "core_python_exec", {"code": code}),
                    )
                ),
                ModelResponse(message="refused"),
            ]
        )
        model.model = "python-test"
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        environment = self._environment(
            workspace.name,
            CORE_AGENT_ALLOWED_BUILTIN_TOOLS="core_python_exec,core_artifact_save",
        )
        with patch.dict(os.environ, environment, clear=True):
            app = create_app(model=model)
        try:
            run_id = app.state.core_agent.run({"prompt": "try to escape"}).run_id
            printed = model.calls[1].context
            service = app.state.core_agent.artifact_service
            name = app.state.core_agent.agent_config.agent["name"]
        finally:
            app.state.close()
        # Each of the three refusals printed its code, and nothing was stored.
        self.assertEqual(printed.count("TOOL_ARGUMENT_INVALID"), 3)
        with self.assertRaises(CoreError) as caught:
            service.load(
                app_name=name, user_id="anonymous", session_id=run_id, filename="a"
            )
        self.assertEqual(caught.exception.code, "NOT_FOUND")

    def test_python_calls_builtin_and_mcp_through_the_parent_broker(self):
        code = """
docs = tools.call("docs_search", {"query": "Alice"})
tasks = tools.call("core_task_list", {})
print(docs["answer"], len(tasks), sorted(tools.names))
"""
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_requests=(
                        ToolRequest("python-1", "core_python_exec", {"code": code}),
                    )
                ),
                ModelResponse(message="python-broker-ok"),
            ]
        )
        model.model = "python-test"
        connector = InMemoryMcpConnector(
            catalogs={
                "docs": {
                    "search": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                        "additionalProperties": False,
                    }
                }
            },
            results={"docs.search": {"answer": "Alice"}},
        )
        request = {
            "prompt": "use python",
        }
        workspace = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, self._environment(workspace.name), clear=True):
            app = create_app(model=model, mcp_connector=connector)
        try:
            result = app.state.core_agent.run(request)
            self.assertEqual(result.message, "python-broker-ok")
            self.assertIn("core_python_exec", model.calls[0].tools)
            self.assertIn("PYTHON:", model.calls[0].instructions)
            self.assertIn("datetime.now().astimezone()", model.calls[0].instructions)
            self.assertNotIn("core_terminal_exec", model.calls[0].tools)
            self.assertNotIn("core_task_start", model.calls[0].tools)
            self.assertIn("Alice 0", model.calls[1].context)
            self.assertIn("core_task_list", model.calls[1].context)
            audit = app.state.core_agent.audit_log.records(result.run_id)
            nested = [
                item for item in audit if item.data.get("source") == "core_python_exec"
            ]
            self.assertEqual(
                [(item.kind, item.data["tool_name"]) for item in nested],
                [
                    ("tool.execution.started", "docs_search"),
                    ("tool.execution.succeeded", "docs_search"),
                    ("tool.execution.started", "core_task_list"),
                    ("tool.execution.succeeded", "core_task_list"),
                ],
            )
            spans = app.state.core_agent.telemetry.exporter.spans
            tool_spans = [
                span for span in spans if span.name == "core_agent.tool.execute"
            ]
            names = {span.attributes.get("tool.name") for span in tool_spans}
            self.assertTrue(
                {"core_python_exec", "docs_search", "core_task_list"} <= names
            )
            traces = {
                span.context.trace_id
                for span in tool_spans
                if span.attributes.get("tool.name") in names
            }
            self.assertEqual(len(traces), 1)
        finally:
            app.state.close()
            workspace.cleanup()

    def test_python_exception_and_timeout_return_to_the_model(self):
        for call_id, arguments, expected in (
            ("python-error", {"code": "raise ValueError('boom')"}, "ValueError"),
            (
                "python-timeout",
                {"code": "import time; time.sleep(5)", "timeout": 0.03},
                "timed_out",
            ),
        ):
            with self.subTest(call_id=call_id), tempfile.TemporaryDirectory() as root:
                model = ScriptedModel(
                    [
                        ModelResponse(
                            tool_requests=(
                                ToolRequest(call_id, "core_python_exec", arguments),
                            )
                        ),
                        ModelResponse(message="recovered"),
                    ]
                )
                model.model = "python-failure-test"
                with patch.dict(os.environ, self._environment(root), clear=True):
                    app = create_app(model=model)
                try:
                    result = app.state.core_agent.run({"prompt": "run failing python"})
                    self.assertEqual(result.message, "recovered")
                    self.assertIn('"status": "', model.calls[1].context)
                    self.assertIn(expected, model.calls[1].context)
                finally:
                    app.state.close()

    def test_python_cannot_be_started_as_background_tool(self):
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_requests=(
                        ToolRequest(
                            "background-python",
                            "core_task_start",
                            {
                                "tool": "core_python_exec",
                                "arguments": {"code": "print('no')"},
                                "required": False,
                            },
                        ),
                    )
                ),
                ModelResponse(message="background-denied-safely"),
            ]
        )
        model.model = "python-background-test"
        with (
            tempfile.TemporaryDirectory() as workspace,
            patch.dict(
                os.environ,
                self._environment(
                    workspace,
                    CORE_AGENT_RUNTIME_MODE="with_terminal",
                    CORE_AGENT_ALLOWED_BUILTIN_TOOLS=(
                        "core_python_exec,core_task_start,core_task_wait"
                    ),
                ),
                clear=True,
            ),
        ):
            app = create_app(model=model)
            try:
                result = app.state.core_agent.run(
                    {"prompt": "start python in background"}
                )
                self.assertEqual(result.message, "background-denied-safely")
                self.assertIn("CAPABILITY_DISABLED", model.calls[1].context)
            finally:
                app.state.close()
