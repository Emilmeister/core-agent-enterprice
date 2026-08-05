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
from a2a.types import GetTaskRequest, Role, SendMessageRequest, TaskState
from a2a.utils.constants import TransportProtocol

from core_agent.app import create_app
from core_agent.model import CompatibleHttpModel


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
            "---\nname: e2e-skill\ndescription: E2E skill\n---\nE2E_SKILL_MARKER\n",
            encoding="utf-8",
        )
        cls.model_server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
        threading.Thread(target=cls.model_server.serve_forever, daemon=True).start()
        cls.environment = patch.dict(
            os.environ,
            {
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
                "CORE_AGENT_TRUST_TERMINAL": "0",
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
        self.assertEqual(
            provenance["usage"], {"model_turns": 2.0, "tool_calls": 1.0}
        )
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
        self.assertEqual(
            provenance["usage"], {"model_turns": 1.0, "tool_calls": 0.0}
        )
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
        self.assertEqual(
            await self._send("MEMORY_E2E"), "memory-e2e-ok"
        )
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

    async def test_explicit_skill_is_validated_and_activated_before_model_call(self):
        ModelHandler.skill_instructions_seen = False
        # The skill is declared by SKILLS_ROOT + CORE_AGENT_ALLOWED_SKILLS.
        self.assertEqual(
            await self._send("SKILL_E2E use e2e-skill"), "skill-e2e-ok"
        )
        self.assertTrue(ModelHandler.skill_instructions_seen)


if __name__ == "__main__":
    unittest.main()
