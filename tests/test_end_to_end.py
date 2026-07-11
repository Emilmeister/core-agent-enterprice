import asyncio
import dataclasses
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.types import GetTaskRequest, Role, SendMessageRequest, TaskState
from a2a.utils.constants import TransportProtocol

from core_agent.a2a import CORE_EXTENSION_URI
from core_agent.app import create_app
from core_agent.model import CompatibleHttpModel
from memory_service.service import MemoryService


def _tool(body, internal_name, arguments):
    wire_name = CompatibleHttpModel._wire_name(internal_name)
    function = next(
        item["function"]
        for item in body["tools"]
        if item["function"]["name"] == wire_name
    )
    return {
        "content": None,
        "tool_calls": [
            {
                "id": str(uuid.uuid4()),
                "type": "function",
                "function": {
                    "name": function["name"],
                    "arguments": json.dumps(arguments),
                },
            }
        ],
    }


def _text(value):
    return {"content": value}


class ModelHandler(BaseHTTPRequestHandler):
    child_catalogs = []
    child_memory_catalogs = []
    workspaces = []
    skill_instructions_seen = False

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        context = body["messages"][-1]["content"]
        wire_names = {item["function"]["name"] for item in body.get("tools", [])}
        self.workspaces.extend(re.findall(r'/[^" ]+?/workspace', context))

        if "SLOW_A2A_E2E" in context:
            time.sleep(0.1)
            message = _text("slow-a2a-ok")
        elif "SKILL_E2E" in context:
            type(self).skill_instructions_seen = (
                "E2E_SKILL_MARKER" in body["messages"][0]["content"]
            )
            message = _text(
                "skill-e2e-ok" if self.skill_instructions_seen else "missing-skill"
            )
        elif "CHILD_MEMORY_E2E" in context:
            self.child_memory_catalogs.append(wire_names)
            message = (
                _tool(
                    body,
                    "memory.memory.search",
                    {"query": "Alice Acme", "namespace": "session/e2e", "limit": 5},
                )
                if '"memory_id": "mem-e2e"' not in context
                else _text("child-memory-ok")
            )
        elif "DELEGATE_MEMORY_E2E" in context:
            task_ids = re.findall(r'"task_id": "([^"]+)"', context)
            if not task_ids:
                message = _tool(
                    body,
                    "core.delegate",
                    {
                        "instruction": "CHILD_MEMORY_E2E: search shared memory",
                        "tools": [],
                        "mcp": {"memory": ["memory.search"]},
                        "skills": [],
                        "budget": {"turns": 4, "tool_calls": 2},
                    },
                )
            elif "child-memory-ok" not in context:
                message = _tool(
                    body,
                    "core.task.wait",
                    {"task_id": task_ids[-1], "timeout": 2},
                )
            else:
                message = _text("delegate-memory-ok")
        elif "CHILD_E2E" in context:
            self.child_catalogs.append(wire_names)
            message = (
                _tool(body, "core.terminal.exec", {"argv": ["pwd"]})
                if "terminal_session_id" not in context
                else _text(f"child-ok {self.workspaces[-1]}")
            )
        elif "DELEGATE_E2E" in context:
            task_ids = re.findall(r'"task_id": "([^"]+)"', context)
            if "terminal_session_id" not in context:
                message = _tool(body, "core.terminal.exec", {"argv": ["pwd"]})
            elif not task_ids:
                message = _tool(
                    body,
                    "core.delegate",
                    {
                        "instruction": "CHILD_E2E: run pwd and report the workspace",
                        "tools": ["core.terminal.exec"],
                        "mcp": {},
                        "skills": [],
                        "budget": {"turns": 4, "tool_calls": 2},
                    },
                )
            elif "child-ok" not in context:
                message = _tool(
                    body,
                    "core.task.wait",
                    {"task_id": task_ids[-1], "timeout": 2},
                )
            else:
                message = _text("delegate-ok")
        elif "BACKGROUND_E2E" in context:
            task_ids = re.findall(r'"task_id": "([^"]+)"', context)
            if not task_ids:
                message = _tool(
                    body,
                    "core.task.start",
                    {
                        "tool": "core.terminal.exec",
                        "arguments": {
                            "argv": [
                                sys.executable,
                                "-c",
                                "import time; time.sleep(.05); print('background-ok')",
                            ]
                        },
                        "required": False,
                    },
                )
            elif len(task_ids) == 1:
                message = _tool(body, "core.task.list", {})
            elif len(task_ids) == 2:
                message = _tool(body, "core.task.get", {"task_id": task_ids[-1]})
            elif len(task_ids) == 3:
                message = _tool(
                    body,
                    "core.task.wait",
                    {"task_id": task_ids[-1], "timeout": 2},
                )
            else:
                message = _text("background-e2e-ok")
        elif "MEMORY_E2E" in context:
            if '"repository_revision": 2' not in context:
                message = _tool(
                    body,
                    "memory.memory.create",
                    {
                        "path": "created.md",
                        "content": _created_markdown(),
                        "expected_repository_revision": 1,
                    },
                )
            elif '"memory_id": "mem-created"' not in context:
                message = _tool(
                    body,
                    "memory.memory.search",
                    {"query": "Bob Beta", "namespace": "session/e2e", "limit": 5},
                )
            else:
                message = _text("<think>private memory reasoning</think>memory-e2e-ok")
        else:
            message = (
                _tool(
                    body,
                    "core.terminal.exec",
                    {"argv": [sys.executable, "-c", "print('terminal-e2e-ok')"]},
                )
                if "terminal-e2e-ok" not in context
                else _text("<think>private terminal reasoning</think>terminal-e2e-ok")
            )

        data = json.dumps({"choices": [{"message": message}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        pass


class MemoryMcpHandler(BaseHTTPRequestHandler):
    service = None

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        method = request["method"]
        if method == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return
        if method == "initialize":
            result = {
                "protocolVersion": request["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "memory-e2e", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "memory.search",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                                "namespace": {"type": "string"},
                                "limit": {"type": "integer"},
                            },
                            "required": ["query", "namespace"],
                        },
                    },
                    {
                        "name": "memory.create",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "content": {"type": "string"},
                                "expected_repository_revision": {"type": "integer"},
                            },
                            "required": [
                                "path",
                                "content",
                                "expected_repository_revision",
                            ],
                        },
                    },
                ]
            }
        elif method == "tools/call":
            arguments = request["params"]["arguments"]
            if request["params"]["name"] == "memory.create":
                result = dataclasses.asdict(
                    self.service.create(
                        arguments["path"],
                        arguments["content"],
                        arguments["expected_repository_revision"],
                    )
                )
            else:
                result = self.service.search(
                    arguments["query"],
                    namespace=arguments["namespace"],
                    limit=arguments.get("limit", 10),
                ).to_dict()
        else:
            self.send_error(400)
            return
        data = json.dumps(
            {"jsonrpc": "2.0", "id": request["id"], "result": result}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        pass


def _markdown():
    return """---
id: mem-e2e
title: Alice and Acme
namespace: session/e2e
kind: fact
status: active
created_at: 2026-07-11T10:00:00Z
updated_at: 2026-07-11T10:00:00Z
sources:
  - task_id: e2e
    event_revision: 1
---
Alice founded Acme.
"""


def _created_markdown():
    return (
        _markdown()
        .replace("mem-e2e", "mem-created")
        .replace("Alice and Acme", "Bob and Beta")
        .replace("Alice founded Acme.", "Bob founded Beta.")
    )


class CoreAgentEndToEndTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        cls.memory = MemoryService(root / "memory")
        cls.memory.create("facts.md", _markdown(), expected_repository_revision=0)
        cls.skill = root / "e2e-skill"
        cls.skill.mkdir()
        (cls.skill / "SKILL.md").write_text(
            "---\nname: e2e-skill\ndescription: E2E skill\n---\nE2E_SKILL_MARKER\n",
            encoding="utf-8",
        )
        MemoryMcpHandler.service = cls.memory
        cls.model_server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
        cls.mcp_server = ThreadingHTTPServer(("127.0.0.1", 0), MemoryMcpHandler)
        for server in (cls.model_server, cls.mcp_server):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        cls.environment = patch.dict(
            os.environ,
            {
                "LOCAL_WORKSPACE_ROOT": str(root / "workspaces"),
                "CORE_AGENT_MAX_MODEL_TURNS": "10",
                "CORE_AGENT_MAX_TOOL_CALLS": "10",
                "CORE_AGENT_ALLOWED_SKILLS": "e2e-skill",
            },
        )
        cls.environment.start()
        model = CompatibleHttpModel(
            api_format="openai",
            model="e2e-model",
            base_url=f"http://127.0.0.1:{cls.model_server.server_port}/v1",
        )
        cls.app = create_app(model=model, base_url="http://agent.test")

    @classmethod
    def tearDownClass(cls):
        cls.app.state.close()
        cls.model_server.shutdown()
        cls.mcp_server.shutdown()
        cls.model_server.server_close()
        cls.mcp_server.server_close()
        cls.memory.close()
        cls.environment.stop()
        cls.temp.cleanup()

    async def _send(self, prompt, mcp=(), skills=()):
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://agent.test",
            headers={"A2A-Extensions": CORE_EXTENSION_URI},
        ) as http:
            config = ClientConfig(
                streaming=False,
                httpx_client=http,
                supported_protocol_bindings=[TransportProtocol.HTTP_JSON],
            )
            client = await ClientFactory(config).create_from_url("http://agent.test")
            request = SendMessageRequest()
            request.message.message_id = str(uuid.uuid4())
            request.message.role = Role.ROLE_USER
            request.message.parts.add().text = prompt
            request.message.extensions.append(CORE_EXTENSION_URI)
            request.message.metadata.update(
                {CORE_EXTENSION_URI: {"mcp": list(mcp), "skills": list(skills)}}
            )
            events = [event async for event in client.send_message(request)]
        task = events[-1].task
        self.assertEqual(TaskState.Name(task.status.state), "TASK_STATE_COMPLETED")
        return "\n".join(
            part.text
            for artifact in task.artifacts
            for part in artifact.parts
            if part.text
        )

    async def test_a2a_model_terminal_and_private_reasoning_boundary(self):
        answer = await self._send("TERMINAL_E2E")
        self.assertEqual(answer, "terminal-e2e-ok")
        self.assertNotIn("private terminal reasoning", answer)

    async def test_a2a_return_immediately_can_fetch_same_task_later(self):
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://agent.test",
            headers={"A2A-Extensions": CORE_EXTENSION_URI},
        ) as http:
            client = await ClientFactory(
                ClientConfig(
                    streaming=False,
                    httpx_client=http,
                    supported_protocol_bindings=[TransportProtocol.HTTP_JSON],
                )
            ).create_from_url("http://agent.test")
            request = SendMessageRequest()
            request.configuration.return_immediately = True
            request.message.message_id = str(uuid.uuid4())
            request.message.role = Role.ROLE_USER
            request.message.parts.add().text = "SLOW_A2A_E2E"
            request.message.extensions.append(CORE_EXTENSION_URI)
            request.message.metadata.update(
                {CORE_EXTENSION_URI: {"mcp": [], "skills": []}}
            )
            events = [event async for event in client.send_message(request)]
            task_id = events[-1].task.id
            task = events[-1].task
            for _ in range(50):
                if TaskState.Name(task.status.state) == "TASK_STATE_COMPLETED":
                    break
                await asyncio.sleep(0.01)
                task = await client.get_task(GetTaskRequest(id=task_id))
        self.assertEqual(TaskState.Name(task.status.state), "TASK_STATE_COMPLETED")
        self.assertEqual(
            [part.text for artifact in task.artifacts for part in artifact.parts],
            ["slow-a2a-ok"],
        )

    async def test_background_task_starts_and_passively_waits_for_terminal_result(self):
        self.assertEqual(await self._send("BACKGROUND_E2E"), "background-e2e-ok")

    async def test_child_agent_has_focused_catalog_and_separate_workspace(self):
        ModelHandler.child_catalogs.clear()
        ModelHandler.workspaces.clear()
        self.assertEqual(await self._send("DELEGATE_E2E"), "delegate-ok")
        self.assertTrue(ModelHandler.child_catalogs)
        terminal_name = CompatibleHttpModel._wire_name("core.terminal.exec")
        self.assertTrue(
            all(catalog == {terminal_name} for catalog in ModelHandler.child_catalogs)
        )
        self.assertGreaterEqual(len(set(ModelHandler.workspaces)), 2)

    async def test_child_receives_only_explicit_shared_memory_tool(self):
        ModelHandler.child_memory_catalogs.clear()
        declaration = {
            "name": "memory",
            "role": "memory",
            "required": True,
            "transport": {
                "type": "streamable_http",
                "url": f"http://127.0.0.1:{self.mcp_server.server_port}/mcp",
            },
        }
        self.assertEqual(
            await self._send("DELEGATE_MEMORY_E2E", (declaration,)),
            "delegate-memory-ok",
        )
        memory_search = CompatibleHttpModel._wire_name("memory.memory.search")
        self.assertTrue(ModelHandler.child_memory_catalogs)
        self.assertTrue(
            all(
                catalog == {memory_search}
                for catalog in ModelHandler.child_memory_catalogs
            )
        )

    async def test_memory_mcp_search_reaches_markdown_indexes_and_graph(self):
        declaration = {
            "name": "memory",
            "role": "memory",
            "required": True,
            "transport": {
                "type": "streamable_http",
                "url": f"http://127.0.0.1:{self.mcp_server.server_port}/mcp",
            },
        }
        self.assertEqual(
            await self._send("MEMORY_E2E", (declaration,)), "memory-e2e-ok"
        )
        self.assertEqual(self.memory.graph_mentions("Bob"), ("mem-created",))
        status = self.memory.index_status(self.memory.repository_revision)
        self.assertTrue(all(value == "ready" for value in status.components.values()))

    async def test_explicit_skill_is_validated_and_activated_before_model_call(self):
        ModelHandler.skill_instructions_seen = False
        declaration = {"name": "e2e-skill", "source": self.skill.as_uri()}
        self.assertEqual(
            await self._send("SKILL_E2E use e2e-skill", skills=(declaration,)),
            "skill-e2e-ok",
        )
        self.assertTrue(ModelHandler.skill_instructions_seen)


if __name__ == "__main__":
    unittest.main()
