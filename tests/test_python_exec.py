import os
import tempfile
import unittest
from unittest.mock import patch

from core_agent.app import create_app
from core_agent.mcp import InMemoryMcpConnector
from core_agent.model import ModelResponse, ScriptedModel, ToolRequest


class PythonExecTests(unittest.TestCase):
    def _environment(self, workspace, **extra):
        return {
            "CORE_AGENT_STATE_BACKEND": "test",
            "LOCAL_APPROVAL_DB_PATH": ":memory:",
            "LOCAL_APPROVAL_ENABLED": "false",
            "CORE_AGENT_RUNTIME_MODE": "with_terminal",
            "CORE_AGENT_ALLOWED_BUILTIN_TOOLS": (
                "core.python.exec,core.artifact.put"
            ),
            "CORE_AGENT_ALLOWED_MCP_SERVERS": "memory",
            "CORE_AGENT_ALLOWED_MCP_TOOLS": "memory.search",
            "LOCAL_WORKSPACE_ROOT": workspace,
            **extra,
        }

    def test_python_calls_builtin_and_mcp_through_the_parent_broker(self):
        code = """
memory = tools.call("memory.search", {"query": "Alice"})
artifact = tools.call(
    "core.artifact.put",
    {"content": memory["answer"], "media_type": "text/plain"},
)
print(memory["answer"], artifact["id"], sorted(tools.names))
"""
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_requests=(
                        ToolRequest("python-1", "core.python.exec", {"code": code}),
                    )
                ),
                ModelResponse(message="python-broker-ok"),
            ]
        )
        model.model = "python-test"
        connector = InMemoryMcpConnector(
            catalogs={
                "memory": {
                    "search": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                        "additionalProperties": False,
                    }
                }
            },
            results={"memory.search": {"answer": "Alice"}},
        )
        request = {
            "prompt": "use python",
            "mcp": [
                {
                    "name": "memory",
                    "role": "memory",
                    "required": True,
                    "transport": {"type": "test"},
                }
            ],
            "skills": [],
        }
        workspace = tempfile.TemporaryDirectory()
        with patch.dict(
            os.environ, self._environment(workspace.name), clear=True
        ):
            app = create_app(model=model, mcp_connector=connector)
        try:
            result = app.state.core_agent.run(request)
            self.assertEqual(result.message, "python-broker-ok")
            self.assertIn("Alice sha256:", model.calls[1].context)
            self.assertIn("core.artifact.put", model.calls[1].context)
            audit = app.state.core_agent.audit_log.records(result.run_id)
            nested = [
                item
                for item in audit
                if item.data.get("source") == "core.python.exec"
            ]
            self.assertEqual(
                [(item.kind, item.data["tool_name"]) for item in nested],
                [
                    ("tool.execution.started", "memory.search"),
                    ("tool.execution.succeeded", "memory.search"),
                    ("tool.execution.started", "core.artifact.put"),
                    ("tool.execution.succeeded", "core.artifact.put"),
                ],
            )
            spans = app.state.core_agent.telemetry.exporter.spans
            tool_spans = [span for span in spans if span.name == "core_agent.tool.execute"]
            names = {span.attributes.get("tool.name") for span in tool_spans}
            self.assertTrue(
                {"core.python.exec", "memory.search", "core.artifact.put"} <= names
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
                                ToolRequest(
                                    call_id, "core.python.exec", arguments
                                ),
                            )
                        ),
                        ModelResponse(message="recovered"),
                    ]
                )
                model.model = "python-failure-test"
                with patch.dict(
                    os.environ, self._environment(root), clear=True
                ):
                    app = create_app(model=model)
                try:
                    result = app.state.core_agent.run(
                        {"prompt": "run failing python", "mcp": [], "skills": []}
                    )
                    self.assertEqual(result.message, "recovered")
                    self.assertIn('"status": "', model.calls[1].context)
                    self.assertIn(expected, model.calls[1].context)
                finally:
                    app.state.close()

    def test_python_nested_risky_tool_is_denied_without_hitl(self):
        code = """
try:
    tools.call("memory.create", {"content": "must not run"})
except ToolCallError as error:
    print(error.code)
"""
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_requests=(
                        ToolRequest("python-deny", "core.python.exec", {"code": code}),
                    )
                ),
                ModelResponse(message="denied-safely"),
            ]
        )
        model.model = "python-deny-test"
        connector = InMemoryMcpConnector(
            catalogs={
                "memory": {
                    "create": {
                        "type": "object",
                        "properties": {"content": {"type": "string"}},
                        "required": ["content"],
                        "additionalProperties": False,
                    }
                }
            },
            results={"memory.create": {"unexpected": True}},
        )
        request = {
            "prompt": "verify policy",
            "mcp": [
                {
                    "name": "memory",
                    "role": "memory",
                    "required": True,
                    "transport": {"type": "test"},
                }
            ],
            "skills": [],
        }
        with tempfile.TemporaryDirectory() as workspace, patch.dict(
            os.environ,
            self._environment(
                workspace,
                CORE_AGENT_ALLOWED_BUILTIN_TOOLS="core.python.exec",
                CORE_AGENT_ALLOWED_MCP_TOOLS="memory.create",
            ),
            clear=True,
        ):
            app = create_app(model=model, mcp_connector=connector)
            try:
                result = app.state.core_agent.run(request)
                self.assertEqual(result.message, "denied-safely")
                self.assertIn("POLICY_DENIED", model.calls[1].context)
                audit = app.state.core_agent.audit_log.records(result.run_id)
                denied = [item for item in audit if item.kind == "tool.denied"]
                self.assertEqual(denied[0].data["tool_name"], "memory.create")
            finally:
                app.state.close()

    def test_python_cannot_be_started_as_background_tool(self):
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_requests=(
                        ToolRequest(
                            "background-python",
                            "core.task.start",
                            {
                                "tool": "core.python.exec",
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
        with tempfile.TemporaryDirectory() as workspace, patch.dict(
            os.environ,
            self._environment(
                workspace,
                CORE_AGENT_ALLOWED_BUILTIN_TOOLS=(
                    "core.python.exec,core.task.start,core.task.wait"
                ),
            ),
            clear=True,
        ):
            app = create_app(model=model)
            try:
                result = app.state.core_agent.run(
                    {"prompt": "start python in background", "mcp": [], "skills": []}
                )
                self.assertEqual(result.message, "background-denied-safely")
                self.assertIn("CAPABILITY_DISABLED", model.calls[1].context)
            finally:
                app.state.close()
