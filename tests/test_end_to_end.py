import asyncio
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
from a2a.server.context import ServerCallContext
from a2a.types import (
    CancelTaskRequest,
    GetTaskRequest,
    Role,
    SendMessageRequest,
    SubscribeToTaskRequest,
    Task,
    TaskState,
    TaskStatus,
)
from a2a.utils.constants import TransportProtocol

from tests.app_support import create_app
from core_agent.a2a import AgentCard, Artifact
from core_agent.a2a_sdk import build_starlette_app
from core_agent.errors import CoreError
from core_agent.mcp import InMemoryMcpConnector
from core_agent.model import CompatibleHttpModel
from core_agent.model import ModelResponse, ScriptedModel


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
        elif "BUDGET_PARTIAL_E2E" in context:
            message = _text(
                "Verified: no tool work ran. Unfinished: the requested work remains."
            )
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
                    "core_terminal_exec",
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
                    "core_terminal_exec",
                    {"argv": ["echo && hello-tool-check && pwd"]},
                )
            )
        elif "DELEGATE_INVALID_E2E" in context:
            message = (
                _text("delegate-validation-recovered")
                if "TOOL_ARGUMENT_INVALID" in context
                else _tool(
                    body,
                    "core_delegate",
                    {
                        "instruction": "invalid contract must return to parent",
                        "tools": ["core_terminal_exec"],
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
            elif '"tool_name": "core_delegate"' not in context:
                message = _tool(
                    body,
                    "core_delegate",
                    {
                        "instruction": "JOIN_CHILD_E2E: return the result as ordinary text",
                        "tools": [],
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
            if "E2E_SKILL_MARKER" in body["messages"][0]["content"]:
                type(self).skill_instructions_seen = True
                type(self).skill_instruction_payload = body["messages"][0]["content"]
                message = _text("skill-e2e-ok")
            else:
                message = _tool(
                    body,
                    "core_skill_activate",
                    {"names": ["e2e-skill"]},
                )
        elif "DELEGATE_PACKAGE_E2E" in context:
            if "child-package-ok" in context:
                message = _text("delegated-package-ok")
            else:
                message = _tool(
                    body,
                    "core_delegate",
                    {
                        "instruction": (
                            "CHILD_PACKAGE_E2E apply the specialized response procedure"
                        ),
                        "tools": [],
                        "skills": ["e2e-skill"],
                        "budget": {"turns": 3, "tool_calls": 1},
                    },
                )
        elif "CHILD_PACKAGE_E2E" in context:
            if "E2E_SKILL_MARKER" in body["messages"][0]["content"]:
                message = _text("child-package-ok")
            else:
                message = _tool(
                    body,
                    "core_skill_activate",
                    {"names": ["e2e-skill"]},
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
                    "core_delegate",
                    {
                        "instruction": "DEPTH_TWO_CHILD_E2E",
                        "tools": ["core_delegate"],
                        "skills": [],
                        "budget": {"turns": 2, "tool_calls": 1},
                    },
                )
            elif "depth-two-no-delegation-ok" not in context:
                message = _tool(
                    body,
                    "core_task_wait",
                    {"task_id": task_ids[-1], "timeout": 2},
                )
            else:
                message = _text("depth-two-ok")
        elif "DEPTH_TWO_E2E" in context:
            task_ids = re.findall(r'"task_id": "([^"]+)"', context)
            if not task_ids:
                message = _tool(
                    body,
                    "core_delegate",
                    {
                        "instruction": "DEPTH_ONE_CHILD_E2E",
                        "tools": ["core_delegate", "core_task_wait"],
                        "skills": [],
                        "budget": {"turns": 4, "tool_calls": 3},
                    },
                )
            elif "depth-two-ok" not in context:
                message = _tool(
                    body,
                    "core_task_wait",
                    {"task_id": task_ids[-1], "timeout": 2},
                )
            else:
                message = _text("depth-two-e2e-ok")
        elif "CHILD_MEMORY_E2E" in context:
            self.child_memory_catalogs.append(wire_names)
            message = (
                _tool(
                    body,
                    "core_memory_search",
                    {"query": "Alice Acme", "limit": 5},
                )
                if '"index_revision"' not in context
                else _text("child-memory-ok")
            )
        elif "DELEGATE_MEMORY_E2E" in context:
            task_ids = re.findall(r'"task_id": "([^"]+)"', context)
            if not task_ids:
                message = _tool(
                    body,
                    "core_delegate",
                    {
                        "instruction": "CHILD_MEMORY_E2E: search shared memory",
                        "tools": ["core_memory_search"],
                        "skills": [],
                        "budget": {"turns": 4, "tool_calls": 2},
                    },
                )
            elif "child-memory-ok" not in context:
                message = _tool(
                    body,
                    "core_task_wait",
                    {"task_id": task_ids[-1], "timeout": 2},
                )
            else:
                message = _text("delegate-memory-ok")
        elif "CHILD_E2E" in context:
            self.child_catalogs.append(wire_names)
            message = (
                _tool(body, "core_terminal_exec", {"argv": ["pwd"]})
                if "terminal_session_id" not in context
                else _text(f"child-ok {self.workspaces[-1]}")
            )
        elif "DELEGATE_E2E" in context:
            task_ids = re.findall(r'"task_id": "([^"]+)"', context)
            if "terminal_session_id" not in context:
                message = _tool(body, "core_terminal_exec", {"argv": ["pwd"]})
            elif not task_ids:
                message = _tool(
                    body,
                    "core_delegate",
                    {
                        "instruction": "CHILD_E2E: run pwd and report the workspace",
                        "tools": ["core_terminal_exec"],
                        "skills": [],
                        "budget": {"turns": 4, "tool_calls": 2},
                    },
                )
            elif "child-ok" not in context:
                message = _tool(
                    body,
                    "core_task_wait",
                    {"task_id": task_ids[-1], "timeout": 2},
                )
            else:
                message = _text("delegate-ok")
        elif "BACKGROUND_E2E" in context:
            task_ids = re.findall(r'"task_id": "([^"]+)"', context)
            if not task_ids:
                message = _tool(
                    body,
                    "core_task_start",
                    {
                        "tool": "core_terminal_exec",
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
                message = _tool(body, "core_task_list", {})
            elif len(task_ids) == 2:
                message = _tool(body, "core_task_get", {"task_id": task_ids[-1]})
            elif len(task_ids) == 3:
                message = _tool(
                    body,
                    "core_task_wait",
                    {"task_id": task_ids[-1], "timeout": 2},
                )
            else:
                message = _text("background-e2e-ok")
        elif "MEMORY_E2E" in context:
            memory_ids = re.findall(r'"memory_id": "([^"]+)"', context)
            if not memory_ids:
                message = _tool(
                    body,
                    "core_memory_create",
                    {
                        "title": "Bob and Beta",
                        "body": "Bob founded Beta.",
                        "kind": "fact",
                    },
                )
            elif '"index_revision"' not in context:
                message = _tool(
                    body,
                    "core_memory_search",
                    {"query": "Bob Beta", "limit": 5},
                )
            elif memory_ids[1:] == memory_ids[:1]:
                # The search returned exactly the note that create just wrote.
                message = _text("<think>private memory reasoning</think>memory-e2e-ok")
            else:
                message = _text("memory-search-missed")
        else:
            message = (
                _tool(
                    body,
                    "core_terminal_exec",
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


class CoreAgentEndToEndTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        cls.skill = root / "e2e-skill"
        cls.skill.mkdir()
        (cls.skill / "SKILL.md").write_text(
            "---\n"
            "name: e2e-skill\n"
            "description: Applies the specialized response procedure.\n"
            "---\n"
            "E2E_SKILL_MARKER\n  Verify all expected outputs.\nSecond instruction remains exact.\n",
            encoding="utf-8",
        )
        cls.model_server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
        threading.Thread(target=cls.model_server.serve_forever, daemon=True).start()
        cls.environment = patch.dict(
            os.environ,
            {
                "CORE_AGENT_ENVIRONMENT": "development",
                "LOCAL_WORKSPACE_ROOT": str(root / "workspaces"),
                "RUNTIME_MAX_LLM_CALLS": "10",
                "CORE_AGENT_MAX_TOOL_CALLS": "10",
                "CORE_AGENT_ALLOWED_SKILLS": "e2e-skill",
                "SKILLS_ROOT": str(root),
                # Pins the memory scope so the test can address the same corpus.
                "USER_ID": "e2e-user",
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
        cls.model_server.server_close()
        cls.environment.stop()
        cls.temp.cleanup()

    async def _send_task(self, prompt, app=None):
        transport = httpx.ASGITransport(app=app or self.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://agent.test",
            headers={
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
            events = [event async for event in client.send_message(request)]
            task = events[-1].task
            deadline = asyncio.get_running_loop().time() + 5
            while task.status.state in {
                TaskState.TASK_STATE_SUBMITTED, TaskState.TASK_STATE_WORKING,
            } and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.025)
                task = await client.get_task(GetTaskRequest(id=task.id))
        self.assertEqual(TaskState.Name(task.status.state), "TASK_STATE_COMPLETED")
        return task

    async def _send(self, prompt):
        task = await self._send_task(prompt)
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

    async def test_live_a2a_result_exposes_local_and_shared_budget_metadata(self):
        task = await self._send_task("TERMINAL_METADATA_E2E")
        provenance = task.artifacts[0].metadata["provenance"]
        self.assertTrue(provenance["complete"])
        self.assertEqual(provenance["completion_reason"], "completed")
        self.assertEqual(provenance["usage"], {"model_turns": 2.0, "tool_calls": 1.0})
        self.assertEqual(provenance["shared_budget"]["scope"], "root")
        self.assertEqual(
            provenance["shared_budget"]["used"],
            {"model_turns": 2.0, "tool_calls": 1.0},
        )

    async def test_live_a2a_budget_partial_matches_recovery_provenance_contract(self):
        model = CompatibleHttpModel(
            api_format="openai",
            model="budget-e2e-model",
            base_url=f"http://127.0.0.1:{self.model_server.server_port}/v1",
        )
        with patch.dict(os.environ, {"RUNTIME_MAX_LLM_CALLS": "1"}):
            app = create_app(model=model, base_url="http://agent.test")
        try:
            task = await self._send_task("BUDGET_PARTIAL_E2E", app=app)
            provenance = task.artifacts[0].metadata["provenance"]
        finally:
            app.state.close()
        self.assertFalse(provenance["complete"])
        self.assertEqual(provenance["completion_reason"], "budget_exhausted")
        self.assertEqual(provenance["exhausted_dimension"], "model_turns")
        self.assertEqual(provenance["usage"], {"model_turns": 1.0, "tool_calls": 0.0})
        self.assertEqual(
            provenance["shared_budget"]["used"],
            {"model_turns": 1.0, "tool_calls": 0.0},
        )

    async def test_a2a_tool_start_failure_returns_to_model_and_task_completes(self):
        answer = await self._send("TOOL_FAILURE_E2E")
        self.assertEqual(answer, "tool-failure-e2e-ok")

    async def test_a2a_invalid_delegate_contract_returns_to_model(self):
        answer = await self._send("DELEGATE_INVALID_E2E")
        self.assertEqual(answer, "delegate-validation-recovered")

    async def test_a2a_followup_steers_same_active_task(self):
        ModelHandler.live_steering_started.clear()
        ModelHandler.live_steering_release.clear()
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://agent.test",
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
            accepted = [event async for event in client.send_message(followup)][-1].task
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

    async def test_a2a_followup_and_cancel_work_during_mcp_cold_start(self):
        started = threading.Event()

        class StartingConnector(InMemoryMcpConnector):
            cold_start_timeout = 30.0

            def connect(self, declaration, *, cancel_event=None, deadline=None):
                started.set()
                cancel_event.wait(2)
                raise CoreError("TASK_CANCELLED")

        model = ScriptedModel([ModelResponse(message="must not run")])
        model.model = "cold-start-e2e-model"
        with patch.dict(
            os.environ,
            {"MCP_URL": "http://127.0.0.1:1/mcp", "CORE_AGENT_MEMORY": "disabled"},
        ):
            app = create_app(
                model=model,
                mcp_connector=StartingConnector(),
                base_url="http://cold-start-agent.test",
            )
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://cold-start-agent.test"
            ) as http:
                client = await ClientFactory(
                    ClientConfig(
                        streaming=False,
                        httpx_client=http,
                        supported_protocol_bindings=[TransportProtocol.HTTP_JSON],
                    )
                ).create_from_url("http://cold-start-agent.test")
                initial = SendMessageRequest()
                initial.configuration.return_immediately = True
                initial.message.message_id = str(uuid.uuid4())
                initial.message.role = Role.ROLE_USER
                initial.message.parts.add().text = "COLD_START_E2E"
                submitted = [event async for event in client.send_message(initial)][
                    -1
                ].task
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                working = await client.get_task(GetTaskRequest(id=submitted.id))
                self.assertEqual(
                    TaskState.Name(working.status.state), "TASK_STATE_WORKING"
                )

                followup = SendMessageRequest()
                followup.message.message_id = str(uuid.uuid4())
                followup.message.task_id = submitted.id
                followup.message.context_id = submitted.context_id
                followup.message.role = Role.ROLE_USER
                followup.message.parts.add().text = "COLD_START_FOLLOWUP"
                accepted = [event async for event in client.send_message(followup)][
                    -1
                ].task
                self.assertEqual(accepted.id, submitted.id)

                cancelled = await client.cancel_task(CancelTaskRequest(id=submitted.id))
                self.assertEqual(cancelled.id, submitted.id)
                self.assertEqual(
                    TaskState.Name(cancelled.status.state), "TASK_STATE_CANCELED"
                )
        finally:
            app.state.close()
        self.assertEqual(model.calls, ())

    async def test_shutdown_during_admitted_mcp_discovery_stays_recoverable(self):
        started = threading.Event()

        class StartingConnector(InMemoryMcpConnector):
            cold_start_timeout = 30.0

            def connect(self, declaration, *, cancel_event=None, deadline=None):
                started.set()
                cancel_event.wait(2)
                raise CoreError(cancel_event.error_code)

        model = ScriptedModel([ModelResponse(message="must not run")])
        model.model = "shutdown-discovery-model"
        with patch.dict(
            os.environ,
            {"MCP_URL": "http://127.0.0.1:1/mcp", "CORE_AGENT_MEMORY": "disabled"},
        ):
            app = create_app(
                model=model,
                mcp_connector=StartingConnector(),
                base_url="http://shutdown-discovery.test",
            )
        handler = app.state.a2a_request_handler
        context = ServerCallContext()
        request = SendMessageRequest()
        request.configuration.return_immediately = True
        request.message.message_id = str(uuid.uuid4())
        request.message.role = Role.ROLE_USER
        request.message.parts.add().text = "shutdown during MCP discovery"
        submitted = await handler.on_message_send(request, context)
        self.assertTrue(await asyncio.to_thread(started.wait, 1))

        await asyncio.to_thread(app.state.close)
        deadline = time.monotonic() + 1
        while True:
            persisted = await handler.task_store.get(submitted.id, context)
            if (
                persisted.status.state != TaskState.TASK_STATE_WORKING
                or time.monotonic() >= deadline
            ):
                break
            await asyncio.sleep(0.01)

        self.assertEqual(persisted.status.state, TaskState.TASK_STATE_WORKING)
        workflow = app.state.core_agent.workflow_store.lookup_task(submitted.id)
        self.assertEqual(workflow.state, "RUNNING")
        self.assertEqual(model.calls, ())

    async def test_a2a_immediate_cancel_waits_for_durable_admission(self):
        entered_handler = threading.Event()
        release_handler = threading.Event()
        model = ScriptedModel([ModelResponse(message="must not run")])
        model.model = "immediate-cancel-model"
        with patch.dict(
            os.environ,
            {"MCP_URL": "http://127.0.0.1:1/mcp", "CORE_AGENT_MEMORY": "disabled"},
        ):
            app = create_app(
                model=model,
                mcp_connector=InMemoryMcpConnector(catalogs={"mcp": {}}),
                base_url="http://immediate-cancel.test",
            )
        original_run = app.state.core_agent.run

        def delayed_run(*args, **kwargs):
            entered_handler.set()
            release_handler.wait(5)
            return original_run(*args, **kwargs)

        app.state.core_agent.run = delayed_run
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://immediate-cancel.test"
            ) as http:
                client = await ClientFactory(
                    ClientConfig(
                        streaming=False,
                        httpx_client=http,
                        supported_protocol_bindings=[TransportProtocol.HTTP_JSON],
                    )
                ).create_from_url("http://immediate-cancel.test")
                initial = SendMessageRequest()
                initial.configuration.return_immediately = True
                initial.message.message_id = str(uuid.uuid4())
                initial.message.role = Role.ROLE_USER
                initial.message.parts.add().text = "IMMEDIATE_CANCEL_E2E"
                submitted = [event async for event in client.send_message(initial)][
                    -1
                ].task
                self.assertTrue(await asyncio.to_thread(entered_handler.wait, 1))

                cancellation = asyncio.create_task(
                    client.cancel_task(CancelTaskRequest(id=submitted.id))
                )
                await asyncio.sleep(2.1)
                self.assertFalse(cancellation.done())
                release_handler.set()
                cancelled = await cancellation

                self.assertEqual(cancelled.id, submitted.id)
                self.assertEqual(
                    TaskState.Name(cancelled.status.state), "TASK_STATE_CANCELED"
                )
        finally:
            release_handler.set()
            app.state.close()
        self.assertEqual(model.calls, ())

    async def test_fresh_subscription_is_passive_and_observes_durable_updates(self):
        resume_calls = []

        def resume(context, _publisher):
            resume_calls.append(context.task_id)
            return Artifact.text("unexpected resume")

        app = build_starlette_app(
            agent_card=AgentCard.minimal("subscription-test"),
            handler=lambda *_args: Artifact.text("unused"),
            cancel_handler=lambda _context: None,
            base_url="http://subscription.test",
            resume_handler=resume,
            followup_handler=lambda *_args: None,
        )
        handler = app.state.a2a_request_handler
        context = ServerCallContext()
        waiting = Task(
            id="passive-subscription",
            context_id="passive-context",
            status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED),
        )
        await handler.task_store.save(waiting, context)

        stream = handler.on_subscribe_to_task(
            SubscribeToTaskRequest(id=waiting.id), context
        )
        first = await anext(stream)
        self.assertEqual(first.status.state, TaskState.TASK_STATE_INPUT_REQUIRED)
        await stream.aclose()
        await asyncio.sleep(0.05)
        self.assertEqual(resume_calls, [])

        working = Task(
            id="recovered-subscription",
            context_id="recovered-context",
            status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
        )
        await handler.task_store.save(working, context)
        stream = handler.on_subscribe_to_task(
            SubscribeToTaskRequest(id=working.id), context
        )
        first = await anext(stream)
        self.assertEqual(first.status.state, TaskState.TASK_STATE_WORKING)
        completed = Task()
        completed.CopyFrom(working)
        completed.status.state = TaskState.TASK_STATE_COMPLETED
        await handler.task_store.save(completed, context)
        terminal = await asyncio.wait_for(anext(stream), timeout=1)
        await stream.aclose()

        self.assertEqual(terminal.status.state, TaskState.TASK_STATE_COMPLETED)
        self.assertEqual(resume_calls, [])

    async def test_active_subscription_starts_with_persisted_task_and_stops_at_terminal(
        self,
    ):
        app = build_starlette_app(
            agent_card=AgentCard.minimal("active-subscription-test"),
            handler=lambda *_args: Artifact.text("unused"),
            cancel_handler=lambda _context: None,
            base_url="http://active-subscription.test",
            resume_handler=lambda *_args: Artifact.text("unused"),
            followup_handler=lambda *_args: None,
        )
        handler = app.state.a2a_request_handler
        context = ServerCallContext()
        working = Task(
            id="active-subscription",
            context_id="active-context",
            status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
        )
        completed = Task()
        completed.CopyFrom(working)
        completed.status.state = TaskState.TASK_STATE_COMPLETED
        trailing = Task()
        trailing.CopyFrom(working)
        include_initial = []

        class Active:
            async def subscribe(self, *, include_initial_task):
                include_initial.append(include_initial_task)
                yield completed
                yield trailing

        await handler.task_store.save(working, context)
        handler._active_task_registry._active_tasks[working.id] = Active()
        stream = handler.on_subscribe_to_task(
            SubscribeToTaskRequest(id=working.id), context
        )

        first = await anext(stream)
        terminal = await anext(stream)
        with self.assertRaises(StopAsyncIteration):
            await anext(stream)

        self.assertEqual(first.status.state, TaskState.TASK_STATE_WORKING)
        self.assertEqual(terminal.status.state, TaskState.TASK_STATE_COMPLETED)
        self.assertEqual(include_initial, [False])

    async def test_active_subscription_recovers_terminal_completed_before_attach(self):
        entered = threading.Event()
        release = threading.Event()

        def complete(*_args):
            entered.set()
            release.wait(1)
            return Artifact.text("completed before attach")

        app = build_starlette_app(
            agent_card=AgentCard.minimal("active-subscription-race-test"),
            handler=complete,
            cancel_handler=lambda _context: None,
            base_url="http://active-subscription-race.test",
            resume_handler=complete,
            followup_handler=lambda *_args: None,
        )
        handler = app.state.a2a_request_handler
        context = ServerCallContext()
        request = SendMessageRequest()
        request.configuration.return_immediately = True
        request.message.message_id = str(uuid.uuid4())
        request.message.role = Role.ROLE_USER
        request.message.parts.add().text = "complete before subscribe attaches"
        submitted = await handler.on_message_send(request, context)
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        active = await handler._active_task_registry.get(submitted.id)

        stream = handler.on_subscribe_to_task(
            SubscribeToTaskRequest(id=submitted.id), context
        )
        initial = await anext(stream)
        release.set()
        await asyncio.wait_for(active._is_finished.wait(), 1)
        events = []
        while True:
            event = await asyncio.wait_for(anext(stream), 1)
            events.append(event)
            if (
                isinstance(event, Task)
                and event.status.state
                in {
                    TaskState.TASK_STATE_COMPLETED,
                    TaskState.TASK_STATE_FAILED,
                    TaskState.TASK_STATE_CANCELED,
                    TaskState.TASK_STATE_REJECTED,
                }
            ) or (
                hasattr(event, "status")
                and event.status.state
                in {
                    TaskState.TASK_STATE_COMPLETED,
                    TaskState.TASK_STATE_FAILED,
                    TaskState.TASK_STATE_CANCELED,
                    TaskState.TASK_STATE_REJECTED,
                }
            ):
                break
        with self.assertRaises(StopAsyncIteration):
            await anext(stream)

        self.assertEqual(initial.status.state, TaskState.TASK_STATE_WORKING)
        self.assertEqual(events[-1].status.state, TaskState.TASK_STATE_COMPLETED)
        persisted = await handler.task_store.get(submitted.id, context)
        self.assertEqual(
            persisted.artifacts[0].parts[0].text, "completed before attach"
        )

    async def test_active_subscription_observes_terminal_from_recovery_worker(self):
        def hand_off(*_args):
            raise CoreError("LEASE_LOST")

        app = build_starlette_app(
            agent_card=AgentCard.minimal("active-subscription-handoff-test"),
            handler=hand_off,
            cancel_handler=lambda _context: None,
            base_url="http://active-subscription-handoff.test",
            resume_handler=hand_off,
            followup_handler=lambda *_args: None,
        )
        handler = app.state.a2a_request_handler
        context = ServerCallContext()
        request = SendMessageRequest()
        request.configuration.return_immediately = True
        request.message.message_id = str(uuid.uuid4())
        request.message.role = Role.ROLE_USER
        request.message.parts.add().text = "handoff to another worker"
        submitted = await handler.on_message_send(request, context)
        active = await handler._active_task_registry.get(submitted.id)
        deadline = time.monotonic() + 1
        while True:
            persisted = await handler.task_store.get(submitted.id, context)
            if (
                persisted.status.state == TaskState.TASK_STATE_WORKING
                or time.monotonic() >= deadline
            ):
                break
            await asyncio.sleep(0.01)

        stream = handler.on_subscribe_to_task(
            SubscribeToTaskRequest(id=submitted.id), context
        )
        initial = await anext(stream)
        completed = Task()
        completed.CopyFrom(initial)
        completed.status.state = TaskState.TASK_STATE_COMPLETED
        await handler.task_store.save(completed, context)
        terminal = await asyncio.wait_for(anext(stream), 1)
        await stream.aclose()
        active._producer_task.cancel()
        await asyncio.wait_for(active._is_finished.wait(), 1)

        self.assertEqual(initial.status.state, TaskState.TASK_STATE_WORKING)
        self.assertEqual(terminal.status.state, TaskState.TASK_STATE_COMPLETED)

    async def test_a2a_handoff_errors_only_stay_working_after_durable_admission(self):
        cases = (
            ("LEASE_LOST", {}, TaskState.TASK_STATE_WORKING),
            (
                "WORKER_STOPPED",
                {"workflow_admitted": True},
                TaskState.TASK_STATE_WORKING,
            ),
            ("WORKER_STOPPED", {}, TaskState.TASK_STATE_FAILED),
        )
        for index, (code, data, expected) in enumerate(cases):
            with self.subTest(code=code, data=data):

                def fail(*_args, code=code, data=data):
                    raise CoreError(code, data=data)

                app = build_starlette_app(
                    agent_card=AgentCard.minimal(f"handoff-{index}"),
                    handler=fail,
                    cancel_handler=lambda _context: None,
                    base_url=f"http://handoff-{index}.test",
                    resume_handler=fail,
                    followup_handler=lambda *_args: None,
                )
                handler = app.state.a2a_request_handler
                context = ServerCallContext()
                request = SendMessageRequest()
                request.configuration.return_immediately = True
                request.message.message_id = str(uuid.uuid4())
                request.message.role = Role.ROLE_USER
                request.message.parts.add().text = code
                submitted = await handler.on_message_send(request, context)

                deadline = time.monotonic() + 1
                while True:
                    persisted = await handler.task_store.get(submitted.id, context)
                    if persisted.status.state == expected:
                        break
                    if time.monotonic() >= deadline:
                        break
                    await asyncio.sleep(0.01)

                self.assertEqual(persisted.status.state, expected)
                if expected == TaskState.TASK_STATE_WORKING:
                    await handler.on_cancel_task(
                        CancelTaskRequest(id=submitted.id), context
                    )

    async def test_late_cancel_preserves_the_completed_worker_result(self):
        committed = threading.Event()
        release = threading.Event()

        def complete(*_args):
            committed.set()
            release.wait(1)
            return Artifact.text("already completed")

        def reject_late_cancel(_context):
            release.set()
            raise CoreError("TASK_NOT_CANCELABLE")

        app = build_starlette_app(
            agent_card=AgentCard.minimal("late-cancel-test"),
            handler=complete,
            cancel_handler=reject_late_cancel,
            base_url="http://late-cancel.test",
            resume_handler=complete,
            followup_handler=lambda *_args: None,
        )
        handler = app.state.a2a_request_handler
        context = ServerCallContext()
        request = SendMessageRequest()
        request.configuration.return_immediately = True
        request.message.message_id = str(uuid.uuid4())
        request.message.role = Role.ROLE_USER
        request.message.parts.add().text = "finish while cancel arrives"
        submitted = await handler.on_message_send(request, context)
        self.assertTrue(await asyncio.to_thread(committed.wait, 1))

        try:
            terminal = await handler.on_cancel_task(
                CancelTaskRequest(id=submitted.id), context
            )
        finally:
            release.set()

        self.assertEqual(terminal.status.state, TaskState.TASK_STATE_COMPLETED)
        self.assertEqual(terminal.artifacts[0].parts[0].text, "already completed")

    async def test_late_cancel_during_publication_preserves_completed_result(self):
        def complete(*_args):
            return Artifact.text("durable result")

        def reject_late_cancel(_context):
            raise CoreError("TASK_NOT_CANCELABLE")

        app = build_starlette_app(
            agent_card=AgentCard.minimal("publication-cancel-test"),
            handler=complete,
            cancel_handler=reject_late_cancel,
            base_url="http://publication-cancel.test",
            resume_handler=complete,
            followup_handler=lambda *_args: None,
        )
        handler = app.state.a2a_request_handler
        executor = handler._active_task_registry._agent_executor
        original_publish = executor._publish_artifact
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed_publish(updater, artifact):
            entered.set()
            await release.wait()
            await original_publish(updater, artifact)

        executor._publish_artifact = delayed_publish
        context = ServerCallContext()
        request = SendMessageRequest()
        request.configuration.return_immediately = True
        request.message.message_id = str(uuid.uuid4())
        request.message.role = Role.ROLE_USER
        request.message.parts.add().text = "cancel during result publication"
        submitted = await handler.on_message_send(request, context)
        await asyncio.wait_for(entered.wait(), 1)

        cancellation = asyncio.create_task(
            handler.on_cancel_task(CancelTaskRequest(id=submitted.id), context)
        )
        await asyncio.sleep(0)
        release.set()
        terminal = await asyncio.wait_for(cancellation, 2)
        persisted = await handler.task_store.get(submitted.id, context)

        self.assertEqual(terminal.status.state, TaskState.TASK_STATE_COMPLETED)
        self.assertEqual(persisted.status.state, TaskState.TASK_STATE_COMPLETED)
        self.assertEqual(terminal.artifacts[0].parts[0].text, "durable result")

    async def test_cancel_of_idle_task_does_not_leak_publication_state(self):
        app = build_starlette_app(
            agent_card=AgentCard.minimal("idle-cancel-test"),
            handler=lambda *_args: Artifact.text("unused"),
            cancel_handler=lambda _context: None,
            base_url="http://idle-cancel.test",
            resume_handler=lambda *_args: Artifact.text("unused"),
            followup_handler=lambda *_args: None,
        )
        handler = app.state.a2a_request_handler
        context = ServerCallContext()
        task = Task(
            id="idle-cancel",
            context_id="idle-cancel-context",
            status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
        )
        await handler.task_store.save(task, context)

        cancelled = await handler.on_cancel_task(CancelTaskRequest(id=task.id), context)
        executor = handler._active_task_registry._agent_executor

        self.assertEqual(cancelled.status.state, TaskState.TASK_STATE_CANCELED)
        self.assertNotIn(task.id, executor._cancel_publications)

    async def test_late_cancel_without_local_worker_restores_durable_result(self):
        def reject_late_cancel(_context):
            raise CoreError("TASK_NOT_CANCELABLE")

        app = build_starlette_app(
            agent_card=AgentCard.minimal("remote-late-cancel-test"),
            handler=lambda *_args: Artifact.text("unused"),
            cancel_handler=reject_late_cancel,
            base_url="http://remote-late-cancel.test",
            resume_handler=lambda *_args: Artifact.text("durable result"),
            followup_handler=lambda *_args: None,
        )
        handler = app.state.a2a_request_handler
        context = ServerCallContext()
        task = Task(
            id="remote-late-cancel",
            context_id="remote-late-cancel-context",
            status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
        )
        await handler.task_store.save(task, context)

        terminal = await handler.on_cancel_task(CancelTaskRequest(id=task.id), context)

        self.assertEqual(terminal.status.state, TaskState.TASK_STATE_COMPLETED)
        self.assertEqual(terminal.artifacts[0].parts[0].text, "durable result")

    async def test_background_task_starts_and_passively_waits_for_terminal_result(self):
        self.assertEqual(await self._send("BACKGROUND_E2E"), "background-e2e-ok")

    async def test_child_agent_has_focused_catalog_and_separate_workspace(self):
        ModelHandler.child_catalogs.clear()
        ModelHandler.workspaces.clear()
        self.assertEqual(await self._send("DELEGATE_E2E"), "delegate-ok")
        self.assertTrue(ModelHandler.child_catalogs)
        terminal_name = CompatibleHttpModel._wire_name("core_terminal_exec")
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
        delegate = CompatibleHttpModel._wire_name("core_delegate")
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
        self.assertEqual(
            await self._send("DELEGATE_MEMORY_E2E"),
            "delegate-memory-ok",
        )
        memory_search = CompatibleHttpModel._wire_name("core_memory_search")
        memory_create = CompatibleHttpModel._wire_name("core_memory_create")
        self.assertTrue(ModelHandler.child_memory_catalogs)
        self.assertTrue(
            all(
                catalog == {memory_search}
                for catalog in ModelHandler.child_memory_catalogs
            )
        )
        # The memory tool the parent kept for itself never reaches the child.
        self.assertTrue(
            all(
                memory_create not in catalog
                for catalog in ModelHandler.child_memory_catalogs
            )
        )

    async def test_memory_search_reaches_markdown_indexes_and_graph(self):
        span_offset = len(self.app.state.telemetry.exporter.spans)
        self.assertEqual(await self._send("MEMORY_E2E"), "memory-e2e-ok")
        agent = self.app.state.core_agent
        memory = agent.memory_registry.service(
            agent.agent_config.agent["name"], "e2e-user"
        )
        written = next(
            memory_id
            for memory_id, document in memory.list_documents().items()
            if document.title == "Bob and Beta"
        )
        self.assertEqual(memory.graph_mentions("Bob"), (written,))
        self.assertTrue(
            all(value == "ready" for value in memory.index_status().values())
        )
        spans = self.app.state.telemetry.exporter.spans[span_offset:]
        execution_span = next(
            span for span in spans if span.name == "core_agent.task.execute"
        )
        self.assertEqual(
            execution_span.context.trace_id, "4bf92f3577b34da6a3ce929d0e0e4736"
        )
        names = {span.name for span in spans}
        self.assertFalse(any(name.startswith("core_agent.a2a.") for name in names))
        self.assertNotIn("core_agent.task.submit", names)
        self.assertEqual(
            {span.context.trace_id for span in spans},
            {"4bf92f3577b34da6a3ce929d0e0e4736"},
        )
        self.assertTrue(
            {
                "core_agent.task.execute",
                "core_agent.context.assemble",
                "gen_ai.chat",
                "core_agent.tool.execute",
                "core_agent.task.checkpoint",
                "core_agent.memory.index_publish",
                "core_agent.memory.search",
            }
            <= names
        )

    async def test_skill_is_selected_by_meaning_and_activated_during_the_loop(self):
        ModelHandler.skill_instructions_seen = False
        # The skill is declared by SKILLS_ROOT + CORE_AGENT_ALLOWED_SKILLS.
        self.assertEqual(
            await self._send("SKILL_E2E apply the specialized response procedure"),
            "skill-e2e-ok",
        )
        self.assertTrue(ModelHandler.skill_instructions_seen)

    async def test_activated_skill_body_reaches_provider_on_new_root_in_same_chat(self):
        first = await self._send_task("SKILL_E2E apply the specialized response procedure")
        ModelHandler.skill_instructions_seen = False
        agent = self.app.state.core_agent
        original = agent.workflow_store.lookup_task(first.id)
        following = agent._new_workflow({"prompt": "SKILL_E2E continue the procedure"},
            task_id=str(uuid.uuid4()), identity=original.owner_id, tenant_id=original.tenant_id,
            session_id=original.context_id, previous_root_run_id=original.run_id,
            defer_initialization=True)[0]
        result = await asyncio.to_thread(agent.resume_task, following.task_id)
        self.assertTrue(ModelHandler.skill_instructions_seen)
        self.assertIn("E2E_SKILL_MARKER\n  Verify all expected outputs.\nSecond instruction remains exact.\n",
            ModelHandler.skill_instruction_payload)
        self.assertEqual((result.usage.model_turns, result.usage.tool_calls), (1, 0))
        self.assertEqual(result.message, "skill-e2e-ok")

    async def test_delegated_skill_implies_child_activation_tool(self):
        self.assertEqual(
            await self._send("DELEGATE_PACKAGE_E2E"),
            "delegated-package-ok",
        )


if __name__ == "__main__":
    unittest.main()
