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
from google.protobuf import json_format

from core_agent.a2a import (
    CORE_EXTENSION_URI,
    LOCAL_APPROVAL_STATUS_URI,
)
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
    depth_two_catalogs = []
    depth_two_instructions = []
    workspaces = []
    skill_instructions_seen = False
    joined_parent_duplicate_work = 0
    live_steering_started = threading.Event()
    live_steering_release = threading.Event()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        context = "\n".join(
            message["content"]
            for message in body["messages"]
            if isinstance(message.get("content"), str)
        )
        wire_names = {item["function"]["name"] for item in body.get("tools", [])}
        self.workspaces.extend(re.findall(r'/[^" ]+?/workspace', context))

        if "LIVE_STEERING_FOLLOWUP" in context:
            message = _text("live-steering-ok")
        elif "LIVE_STEERING_E2E" in context:
            type(self).live_steering_started.set()
            type(self).live_steering_release.wait(2)
            message = _text("stale-live-steering-answer")
        elif "APPROVAL_E2E" in context:
            if '"status": "denied"' in context:
                message = _text("approval-denied-ok")
            elif "approval-side-effect-ok" in context:
                message = _text("approval-approved-ok")
            else:
                message = _tool(
                    body,
                    "core.terminal.exec",
                    {
                        "argv": [
                            sys.executable,
                            "-c",
                            "print('approval-side-effect-ok')",
                        ]
                    },
                )
        elif "TOOL_FAILURE_E2E" in context:
            message = (
                _text("tool-failure-e2e-ok")
                if '"status": "failed"' in context
                else _tool(
                    body,
                    "core.terminal.exec",
                    {"argv": ["echo && hello-tool-check && pwd"]},
                )
            )
        elif "DELEGATE_INVALID_E2E" in context:
            message = (
                _text("delegate-validation-recovered")
                if "TOOL_ARGUMENT_INVALID" in context
                else _tool(
                    body,
                    "core.delegate",
                    {
                        "instruction": "invalid contract must return to parent",
                        "tools": ["core.terminal.exec"],
                        "mcp": {},
                        "skills": [],
                        "budget": {"turns": 3, "tool_calls": 1},
                        "result_schema": '{"type":"object"}',
                    },
                )
            )
        elif "JOIN_CHILD_E2E" in context:
            message = _text("joined-child-result")
        elif "DELEGATE_JOIN_E2E" in context:
            if '"mode": "joined"' in context and "joined-child-result" in context:
                message = _text("delegate-join-ok")
            elif '"tool_name": "core.delegate"' not in context:
                message = _tool(
                    body,
                    "core.delegate",
                    {
                        "instruction": "JOIN_CHILD_E2E: return the result as ordinary text",
                        "tools": [],
                        "mcp": {},
                        "skills": [],
                        "budget": {"turns": 3, "tool_calls": 2},
                    },
                )
            else:
                type(self).joined_parent_duplicate_work += 1
                message = _text("parent-duplicated-child-work")
        elif "SLOW_A2A_E2E" in context:
            time.sleep(0.1)
            message = _text("slow-a2a-ok")
        elif "SKILL_E2E" in context:
            type(self).skill_instructions_seen = (
                "E2E_SKILL_MARKER" in body["messages"][0]["content"]
            )
            message = _text(
                "skill-e2e-ok" if self.skill_instructions_seen else "missing-skill"
            )
        elif "DEPTH_TWO_CHILD_E2E" in context:
            self.depth_two_catalogs.append(wire_names)
            self.depth_two_instructions.append(body["messages"][0]["content"])
            message = _text("depth-two-no-delegation-ok")
        elif "DEPTH_ONE_CHILD_E2E" in context:
            task_ids = re.findall(r'"task_id": "([^"]+)"', context)
            if not task_ids:
                message = _tool(
                    body,
                    "core.delegate",
                    {
                        "instruction": "DEPTH_TWO_CHILD_E2E",
                        "tools": ["core.delegate"],
                        "mcp": {},
                        "skills": [],
                        "budget": {"turns": 2, "tool_calls": 1},
                    },
                )
            elif "depth-two-no-delegation-ok" not in context:
                message = _tool(
                    body,
                    "core.task.wait",
                    {"task_id": task_ids[-1], "timeout": 2},
                )
            else:
                message = _text("depth-two-ok")
        elif "DEPTH_TWO_E2E" in context:
            task_ids = re.findall(r'"task_id": "([^"]+)"', context)
            if not task_ids:
                message = _tool(
                    body,
                    "core.delegate",
                    {
                        "instruction": "DEPTH_ONE_CHILD_E2E",
                        "tools": ["core.delegate", "core.task.wait"],
                        "mcp": {},
                        "skills": [],
                        "budget": {"turns": 4, "tool_calls": 3},
                    },
                )
            elif "depth-two-ok" not in context:
                message = _tool(
                    body,
                    "core.task.wait",
                    {"task_id": task_ids[-1], "timeout": 2},
                )
            else:
                message = _text("depth-two-e2e-ok")
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
    trace_carriers = []

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.trace_carriers.append(
            {
                "header": self.headers.get("traceparent"),
                "meta": request.get("params", {}).get("_meta", {}).get("traceparent"),
                "method": request["method"],
            }
        )
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
        with patch.dict(
            os.environ,
            {
                "CORE_AGENT_TRUST_TERMINAL": "0",
                "CORE_AGENT_APPROVAL_MODE": "on_risk",
            },
        ):
            cls.approval_app = create_app(
                model=model, base_url="http://approval-agent.test"
            )

    @classmethod
    def tearDownClass(cls):
        cls.app.state.close()
        cls.approval_app.state.close()
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
            headers={
                "A2A-Extensions": ",".join(
                    (
                        CORE_EXTENSION_URI,
                        LOCAL_APPROVAL_STATUS_URI,
                    )
                ),
                "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            },
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

    async def test_a2a_tool_start_failure_returns_to_model_and_task_completes(self):
        answer = await self._send("TOOL_FAILURE_E2E")
        self.assertEqual(answer, "tool-failure-e2e-ok")

    async def test_a2a_invalid_delegate_contract_returns_to_model(self):
        answer = await self._send("DELEGATE_INVALID_E2E")
        self.assertEqual(answer, "delegate-validation-recovered")

    async def test_local_control_plane_approves_exact_call_once(self):
        transport = httpx.ASGITransport(app=self.approval_app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://approval-agent.test",
            headers={
                "A2A-Extensions": ",".join(
                    (CORE_EXTENSION_URI, LOCAL_APPROVAL_STATUS_URI)
                )
            },
        ) as http:
            client = await ClientFactory(
                ClientConfig(
                    streaming=False,
                    httpx_client=http,
                    supported_protocol_bindings=[TransportProtocol.HTTP_JSON],
                )
            ).create_from_url("http://approval-agent.test")
            initial = SendMessageRequest()
            initial.message.message_id = str(uuid.uuid4())
            initial.message.role = Role.ROLE_USER
            initial.message.parts.add().text = "APPROVAL_E2E"
            initial.message.extensions.append(CORE_EXTENSION_URI)
            initial.message.metadata.update(
                {CORE_EXTENSION_URI: {"mcp": [], "skills": []}}
            )
            before = self.approval_app.state.core_agent.tool_runtime.execution_count
            completed = [event async for event in client.send_message(initial)][-1].task
        self.assertEqual(TaskState.Name(completed.status.state), "TASK_STATE_COMPLETED")
        self.assertEqual(
            self.approval_app.state.core_agent.tool_runtime.execution_count,
            before + 1,
        )
        self.assertEqual(
            "\n".join(
                part.text
                for artifact in completed.artifacts
                for part in artifact.parts
                if part.text
            ),
            "approval-approved-ok",
        )
        approval_id = self.approval_app.state.core_agent._task_approvals[completed.id]
        approval = self.approval_app.state.core_agent.tool_runtime.approvals.get(
            approval_id
        )
        execution = (
            self.approval_app.state.core_agent.tool_runtime.approvals.execution_for(
                approval_id
            )
        )
        self.assertEqual(approval.state, "CONSUMED")
        self.assertEqual(execution.state, "SUCCEEDED")
        run_id = json_format.MessageToDict(completed.artifacts[-1].metadata)[
            "provenance"
        ]["run_id"]
        records = self.approval_app.state.core_agent.audit_log.records(run_id)
        kinds = {record.kind for record in records}
        self.assertTrue(
            {
                "tool.proposed",
                "policy.evaluated",
                "approval.requested",
                "operator.approved",
                "tool.execution.succeeded",
            }
            <= kinds
        )
        self.assertNotIn("local-operator-stub", repr(completed))

    async def test_local_wait_is_working_and_caller_cannot_resolve_it(self):
        transport = httpx.ASGITransport(app=self.approval_app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://approval-agent.test",
            headers={
                "A2A-Extensions": ",".join(
                    (CORE_EXTENSION_URI, LOCAL_APPROVAL_STATUS_URI)
                )
            },
        ) as http:
            client = await ClientFactory(
                ClientConfig(
                    streaming=False,
                    httpx_client=http,
                    supported_protocol_bindings=[TransportProtocol.HTTP_JSON],
                )
            ).create_from_url("http://approval-agent.test")
            request = SendMessageRequest()
            request.message.message_id = str(uuid.uuid4())
            request.message.role = Role.ROLE_USER
            request.message.parts.add().text = "APPROVAL_E2E"
            request.message.extensions.append(CORE_EXTENSION_URI)
            request.message.metadata.update(
                {CORE_EXTENSION_URI: {"mcp": [], "skills": []}}
            )
            completed = [event async for event in client.send_message(request)][-1].task
        wait_message = next(
            message
            for message in completed.history
            if message.role == Role.ROLE_AGENT
            and message.parts
            and "local operator" in message.parts[0].text
        )
        payload = json_format.MessageToDict(wait_message.metadata)[
            LOCAL_APPROVAL_STATUS_URI
        ]
        self.assertFalse(payload["callerActionRequired"])
        self.assertFalse(payload["callerCanApprove"])
        self.assertFalse(payload["callerCanDeny"])
        self.assertFalse(payload["protectedActionExecuted"])
        self.assertIn("send_message_queued", payload["allowedCallerOperations"])
        self.assertNotIn("approvalId", repr(payload))
        approved_message = next(
            message
            for message in completed.history
            if message.role == Role.ROLE_AGENT
            and message.parts
            and "authorized the protected action" in message.parts[0].text
        )
        approved_payload = json_format.MessageToDict(approved_message.metadata)[
            LOCAL_APPROVAL_STATUS_URI
        ]
        self.assertEqual(approved_payload["phase"], "local_operator_approved")
        self.assertNotIn("cancel_task", approved_payload["allowedCallerOperations"])

        agent = self.approval_app.state.core_agent
        before = agent.tool_runtime.execution_count
        task_id = str(uuid.uuid4())
        pending = agent.run(
            {"prompt": "APPROVAL_E2E", "mcp": [], "skills": []},
            task_id=task_id,
            identity="remote-caller",
            session_id="context-locked",
            tenant_id="tenant-locked",
        )
        queued = agent.enqueue_message(
            {"prompt": "I approve", "mcp": [], "skills": []},
            task_id=task_id,
            message_id="locked-followup",
            identity="remote-caller",
            session_id="context-locked",
            tenant_id="tenant-locked",
        )
        self.assertEqual(queued["sequence"], 1)
        record = agent.workflow_store.lookup_task(task_id)
        self.assertEqual(record.state, "WAITING_LOCAL_APPROVAL")
        self.assertEqual(
            agent.workflow_store.pending_inbound(record)[0]["content"], "I approve"
        )
        self.assertEqual(
            agent.tool_runtime.approvals.get(pending.request.id).state, "PENDING"
        )
        self.assertEqual(agent.tool_runtime.execution_count, before)
        agent.cancel_local_approval(task_id)
        self.assertEqual(
            agent.tool_runtime.approvals.get(pending.request.id).state, "CANCELED"
        )

        denied_task_id = str(uuid.uuid4())
        denied = agent.run(
            {"prompt": "APPROVAL_E2E", "mcp": [], "skills": []},
            task_id=denied_task_id,
            identity="remote-caller",
            session_id="context-denied",
            tenant_id="tenant-locked",
        )
        result = agent.deny_local_approval(
            denied_task_id,
            denied.request.id,
            operator_principal_id="local-operator-test",
            operator_session_id="operator-session-test",
        )
        self.assertEqual(result.message, "approval-denied-ok")
        self.assertEqual(agent.tool_runtime.execution_count, before)
        self.assertEqual(
            agent.tool_runtime.approvals.get(denied.request.id).state, "DENIED"
        )

    async def test_extension_unaware_caller_gets_text_only_local_wait_status(self):
        transport = httpx.ASGITransport(app=self.approval_app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://approval-agent.test",
            headers={"A2A-Extensions": CORE_EXTENSION_URI},
        ) as http:
            client = await ClientFactory(
                ClientConfig(
                    streaming=False,
                    httpx_client=http,
                    supported_protocol_bindings=[TransportProtocol.HTTP_JSON],
                )
            ).create_from_url("http://approval-agent.test")
            request = SendMessageRequest()
            request.message.message_id = str(uuid.uuid4())
            request.message.role = Role.ROLE_USER
            request.message.parts.add().text = "APPROVAL_E2E"
            request.message.extensions.append(CORE_EXTENSION_URI)
            request.message.metadata.update(
                {CORE_EXTENSION_URI: {"mcp": [], "skills": []}}
            )
            completed = [event async for event in client.send_message(request)][-1].task
        wait_message = next(
            message
            for message in completed.history
            if message.role == Role.ROLE_AGENT
            and message.parts
            and "local operator" in message.parts[0].text
        )
        self.assertEqual(list(wait_message.extensions), [])
        self.assertNotIn(
            LOCAL_APPROVAL_STATUS_URI,
            json_format.MessageToDict(wait_message.metadata),
        )

    async def test_a2a_return_immediately_can_fetch_same_task_later(self):
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://agent.test",
            headers={
                "A2A-Extensions": ",".join(
                    (
                        CORE_EXTENSION_URI,
                        LOCAL_APPROVAL_STATUS_URI,
                    )
                )
            },
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

    async def test_a2a_followup_steers_same_active_task(self):
        ModelHandler.live_steering_started.clear()
        ModelHandler.live_steering_release.clear()
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
            initial = SendMessageRequest()
            initial.configuration.return_immediately = True
            initial.message.message_id = str(uuid.uuid4())
            initial.message.role = Role.ROLE_USER
            initial.message.parts.add().text = "LIVE_STEERING_E2E"
            initial.message.extensions.append(CORE_EXTENSION_URI)
            initial.message.metadata.update(
                {CORE_EXTENSION_URI: {"mcp": [], "skills": []}}
            )
            submitted = [event async for event in client.send_message(initial)][-1].task
            self.assertTrue(
                await asyncio.to_thread(ModelHandler.live_steering_started.wait, 1)
            )
            followup = SendMessageRequest()
            followup.message.message_id = str(uuid.uuid4())
            followup.message.task_id = submitted.id
            followup.message.context_id = submitted.context_id
            followup.message.role = Role.ROLE_USER
            followup.message.parts.add().text = "LIVE_STEERING_FOLLOWUP"
            followup.message.extensions.append(CORE_EXTENSION_URI)
            followup.message.metadata.update(
                {CORE_EXTENSION_URI: {"mcp": [], "skills": []}}
            )
            accepted = [
                event async for event in client.send_message(followup)
            ][-1].task
            self.assertEqual(accepted.id, submitted.id)
            ModelHandler.live_steering_release.set()
            task = accepted
            for _ in range(100):
                if TaskState.Name(task.status.state) == "TASK_STATE_COMPLETED":
                    break
                await asyncio.sleep(0.01)
                task = await client.get_task(GetTaskRequest(id=submitted.id))
        self.assertEqual(TaskState.Name(task.status.state), "TASK_STATE_COMPLETED")
        self.assertEqual(
            [part.text for artifact in task.artifacts for part in artifact.parts],
            ["live-steering-ok"],
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

    async def test_delegate_joins_and_parent_does_not_duplicate_child_work(self):
        ModelHandler.joined_parent_duplicate_work = 0
        self.assertEqual(await self._send("DELEGATE_JOIN_E2E"), "delegate-join-ok")
        self.assertEqual(ModelHandler.joined_parent_duplicate_work, 0)

    async def test_child_can_delegate_one_more_level_but_grandchild_cannot(self):
        ModelHandler.depth_two_catalogs.clear()
        ModelHandler.depth_two_instructions.clear()
        self.assertEqual(await self._send("DEPTH_TWO_E2E"), "depth-two-e2e-ok")
        delegate = CompatibleHttpModel._wire_name("core.delegate")
        self.assertTrue(ModelHandler.depth_two_catalogs)
        self.assertTrue(
            all(delegate not in catalog for catalog in ModelHandler.depth_two_catalogs)
        )
        self.assertTrue(
            all(
                "complete the task directly and do not try to create another agent"
                in instructions
                for instructions in ModelHandler.depth_two_instructions
            )
        )

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
        MemoryMcpHandler.trace_carriers.clear()
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
        tool_calls = [
            item
            for item in MemoryMcpHandler.trace_carriers
            if item["method"] == "tools/call"
        ]
        self.assertTrue(tool_calls)
        self.assertTrue(all(item["header"] == item["meta"] for item in tool_calls))
        self.assertTrue(all(item["header"].startswith("00-") for item in tool_calls))
        spans = self.app.state.telemetry.exporter.spans
        a2a_span = next(
            span for span in reversed(spans) if span.name == "core_agent.a2a.message.send"
        )
        self.assertEqual(
            a2a_span.context.trace_id, "4bf92f3577b34da6a3ce929d0e0e4736"
        )
        names = {span.name for span in spans}
        self.assertTrue(
            {
                "core_agent.task.submit",
                "core_agent.task.execute",
                "core_agent.context.assemble",
                "gen_ai.chat",
                "core_agent.policy.evaluate",
                "core_agent.tool.execute",
                "core_agent.task.checkpoint",
                "mcp.client",
            }
            <= names
        )

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
