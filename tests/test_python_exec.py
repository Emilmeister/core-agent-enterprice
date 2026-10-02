import os
import asyncio
import hashlib
import json
import tempfile
import unittest
import uuid
from unittest.mock import patch

from tests.app_support import create_app
from core_agent.mcp import InMemoryMcpConnector
from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
from tests.test_auth import AuthAppTestCase, TEST_DATABASE_URL


class PythonResponseFileTests(AuthAppTestCase):
    durable_blobs = True

    async def execute(self, code):
        self.model._responses = [ModelResponse(tool_requests=(ToolRequest("python-files", "core_python_exec", {"code": code}),)),
                                 ModelResponse(message="Files ready")]
        task = await self.submit("owner-a", uuid.uuid4().hex, "python-files-chat")
        agent = self.app.state.core_agent
        async with asyncio.timeout(5):
            while True:
                record = agent.workflow_store.lookup_task(task["id"])
                if record.state in {"COMPLETED", "FAILED", "CANCELLED", "ABORTED"}:
                    break
                await asyncio.sleep(0.01)
        self.assertEqual(record.state, "COMPLETED", record.error_code)
        return agent._terminal_result(record), record

    async def test_binary_workspace_selection_through_python_broker_is_frozen_without_base64(self):
        code = """
payload = bytes(range(256)) * 8
open("deck.pptx", "wb").write(payload)
selected = tools.call("core_response_files", {"paths": ["deck.pptx"]})
print(selected["files"][0]["size_bytes"], selected["files"][0]["media_type"])
open("deck.pptx", "wb").write(b"changed after selection")
"""
        result, record = await self.execute(code)
        ref, = result.outgoing_files
        expected = bytes(range(256)) * 8
        self.assertEqual(ref["size_bytes"], 2048)
        self.assertEqual(ref["media_type"], "application/vnd.openxmlformats-officedocument.presentationml.presentation")
        self.assertEqual(ref["sha256"], hashlib.sha256(expected).hexdigest())
        context = self.model.calls[-1].context
        self.assertIn("2048", context)
        self.assertIn("presentationml", context)
        self.assertNotIn('"raw"', context)
        self.assertNotIn('"base64"', context)
        downloaded = await self.http.get(f"/api/chats/python-files-chat/tasks/{record.task_id}/files/{ref['file_id']}",
                                         headers=self.headers("owner-a"))
        self.assertEqual(downloaded.status_code, 200, downloaded.text)
        self.assertEqual(downloaded.content, expected)
        self.assertFalse(hasattr(self.app.state.core_agent, "artifact_service"))

    async def test_invalid_last_path_is_atomic_and_retired_python_tool_cannot_dispatch(self):
        code = """
open("empty.bin", "wb").write(b"")
tools.call("core_response_files", {"paths": ["empty.bin"]})
for name, arguments in (
    ("core_response_files", {"paths": ["empty.bin", "../../etc/passwd"]}),
    ("core_response_files", {"paths": ["empty.bin"], "content": "forbidden"}),
    ("core_artifact_save", {"filename": "old.bin", "content": "forbidden"}),
):
    try:
        tools.call(name, arguments)
        print("unexpected acceptance")
    except ToolCallError as error:
        print(error.code)
"""
        result, record = await self.execute(code)
        ref, = result.outgoing_files
        self.assertEqual((ref["name"], ref["size_bytes"], ref["sha256"]),
                         ("empty.bin", 0, hashlib.sha256(b"").hexdigest()))
        context = self.model.calls[-1].context
        self.assertIn("INVALID_FILE_PATH", context)
        self.assertIn("TOOL_ARGUMENT_INVALID", context)
        outer = self.app.state.core_agent._context_from_dict(record.snapshot["context"]).transcript
        output = next(json.loads(item.content)["output"] for item in outer if item.kind == "tool_result")
        self.assertEqual(output["stdout"].splitlines(),
                         ["INVALID_FILE_PATH", "TOOL_ARGUMENT_INVALID", "CAPABILITY_DISABLED"])
        self.assertNotIn('"artifact_name"', json.dumps(output))
        downloaded = await self.http.get(f"/api/chats/python-files-chat/tasks/{record.task_id}/files/{ref['file_id']}",
                                         headers=self.headers("owner-b"))
        self.assertEqual(downloaded.status_code, 200, downloaded.text)
        self.assertEqual(downloaded.content, b"")


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL is required")
class PostgresPythonResponseFileTests(PythonResponseFileTests):
    use_postgres = True


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
