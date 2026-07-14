import json
import os
import copy
import tempfile
import time
import unittest
import threading
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core_agent.audit import InMemoryAuditLog
from core_agent.config import AgentConfig, PlatformConfig, RunRequest
from core_agent.durability import (
    CheckpointStore,
    InMemoryEventStore,
    LeaseManager,
    RecoveryManager,
)
from core_agent.errors import CoreError
from core_agent.mcp import InMemoryMcpConnector
from core_agent.kernel import KernelCompiler
from core_agent.model import (
    CompatibleHttpModel,
    ModelResponse,
    ScriptedModel,
    ToolRequest,
)
from core_agent.observability import FailingExporter, RecordingExporter, Telemetry
from core_agent.runtime import CoreAgent
from core_agent.tasks import TaskScheduler
from core_agent.tools import (
    ApprovalManager,
    ApprovalMode,
    PolicyEngine,
    ToolDefinition,
    ToolRegistry,
    ToolRuntime,
)
from core_agent.execution import ExecutionEnvironmentManager, ExecutionResult
from memory_service.service import MemoryService


class IsolatedBackend:
    local = True
    capabilities = {
        "pty",
        "process_groups",
        "workspace_separation",
        "resource_limits",
        "process_tree_teardown",
    }

    def create(self, spec):
        return type(
            "Environment",
            (),
            {
                "id": f"env-{spec.run_id}",
                "spec": spec,
                "execute": lambda inner, request: ExecutionResult(
                    0, "tool-ok", "", (), ()
                ),
                "destroy": lambda inner: None,
            },
        )()


def platform():
    return PlatformConfig(
        allowed_builtin_tools={
            "core.terminal.exec",
            "core.task.start",
            "core.task.wait",
            "core.delegate",
        },
        denied_builtin_tools=set(),
        allowed_mcp_servers={"memory"},
        denied_mcp_tools={},
        allowed_skills=set(),
        supported_features={
            "memory",
            "background_tasks",
            "delegation",
            "terminal",
            "mcp",
            "human_input",
        },
        a2a_protocol_versions=("1.0",),
        a2a_bindings=("HTTP+JSON",),
        max_model_turns=10,
        max_tool_calls=10,
    )


def agent_config(memory="optional", *, max_turns=10):
    return AgentConfig.from_dict(
        {
            "schema_version": "v1alpha1",
            "agent": {"name": "runtime-test", "profile_prompt": "Act as a test agent."},
            "model": {"route": "scripted"},
            "features": {
                "memory": memory,
                "background_tasks": True,
                "delegation": True,
                "terminal": True,
                "filesystem_mutation": False,
                "mcp": True,
                "skills": False,
                "human_input": True,
            },
            "tools": {
                "builtins": {
                    "default": "deny",
                    "allow": ["core.terminal.exec", "core.task.*", "core.delegate"],
                    "deny": [],
                },
                "mcp": {
                    "default": "deny",
                    "allow_servers": ["memory"],
                    "allow_tools": {
                        "memory": ["search", "read", "create", "update", "split"]
                    },
                },
            },
            "skills": {"default": "deny", "allow": []},
            "context": {
                "compact_at_working_ratio": 0.90,
                "compact_to_working_ratio": 0.15,
            },
            "approval": {"mode": "on_risk"},
            "execution": {"environment_profile": "local-pty-test"},
            "observability": {"otel_profile": "test"},
            "budgets": {"model_turns": max_turns, "tool_calls": 10},
        }
    )


def run_request(memory=True):
    mcp = []
    if memory:
        mcp.append(
            {
                "name": "memory",
                "role": "memory",
                "required": False,
                "transport": {
                    "type": "streamable_http",
                    "url": "https://memory.test/mcp",
                },
            }
        )
    return RunRequest.from_dict({"prompt": "Do it", "mcp": mcp, "skills": []})


def make_agent(
    model,
    *,
    memory="optional",
    connector=None,
    telemetry=None,
    max_turns=10,
    **agent_options,
):
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            "core.terminal.exec",
            "execute",
            {
                "type": "object",
                "properties": {"argv": {"type": "array", "items": {"type": "string"}}},
                "required": ["argv"],
                "additionalProperties": False,
            },
            mutating=False,
            risk_tags=frozenset(),
        )
    )
    tool_runtime = ToolRuntime(
        registry,
        PolicyEngine(ApprovalMode.ON_RISK),
        ApprovalManager(),
        ExecutionEnvironmentManager(IsolatedBackend()),
    )
    return CoreAgent(
        platform_config=platform(),
        agent_config=agent_config(memory, max_turns=max_turns),
        model=model,
        tool_runtime=tool_runtime,
        mcp_connector=connector or InMemoryMcpConnector(),
        task_scheduler=TaskScheduler(),
        event_store=InMemoryEventStore(),
        checkpoint_store=CheckpointStore(),
        audit_log=InMemoryAuditLog(),
        telemetry=telemetry or Telemetry(RecordingExporter()),
        **agent_options,
    )


class RuntimeTests(unittest.TestCase):
    def test_live_steering_delivers_ordered_idempotent_messages_before_completion(self):
        started = threading.Event()
        release = threading.Event()

        class SteeringModel:
            def __init__(self):
                self.calls = []

            def generate(self, *, context, tools, instructions, messages=None):
                self.calls.append(tuple(messages or ()))
                if len(self.calls) == 1:
                    started.set()
                    self.assert_released = release.wait(2)
                    return ModelResponse(message="stale answer")
                return ModelResponse(message="steered answer")

        model = SteeringModel()
        agent = make_agent(model, memory="disabled")
        result = []
        failure = []

        def run():
            try:
                result.append(
                    agent.run(
                        run_request(memory=False),
                        task_id="steering-task",
                        identity="owner-1",
                        session_id="context-1",
                        tenant_id="tenant-1",
                    )
                )
            except Exception as error:
                failure.append(error)

        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(started.wait(1))
        first = agent.enqueue_message(
            RunRequest.from_dict(
                {"prompt": "first correction", "mcp": [], "skills": []}
            ),
            task_id="steering-task",
            message_id="message-1",
            identity="owner-1",
            session_id="context-1",
            tenant_id="tenant-1",
        )
        duplicate = agent.enqueue_message(
            RunRequest.from_dict(
                {"prompt": "first correction", "mcp": [], "skills": []}
            ),
            task_id="steering-task",
            message_id="message-1",
            identity="owner-1",
            session_id="context-1",
            tenant_id="tenant-1",
        )
        second = agent.enqueue_message(
            RunRequest.from_dict(
                {"prompt": "second correction", "mcp": [], "skills": []}
            ),
            task_id="steering-task",
            message_id="message-2",
            identity="owner-1",
            session_id="context-1",
            tenant_id="tenant-1",
        )
        self.assertEqual(first["sequence"], duplicate["sequence"])
        self.assertEqual((first["sequence"], second["sequence"]), (1, 2))
        with self.assertRaises(CoreError) as wrong_context:
            agent.enqueue_message(
                RunRequest.from_dict(
                    {"prompt": "wrong context", "mcp": [], "skills": []}
                ),
                task_id="steering-task",
                message_id="message-wrong-context",
                identity="owner-1",
                session_id="context-2",
                tenant_id="tenant-1",
            )
        self.assertEqual(wrong_context.exception.code, "INVALID_REQUEST")
        with self.assertRaises(CoreError) as wrong_owner:
            agent.enqueue_message(
                RunRequest.from_dict(
                    {"prompt": "wrong owner", "mcp": [], "skills": []}
                ),
                task_id="steering-task",
                message_id="message-wrong-owner",
                identity="owner-2",
                session_id="context-1",
                tenant_id="tenant-1",
            )
        self.assertEqual(wrong_owner.exception.code, "TASK_NOT_FOUND")
        with self.assertRaises(CoreError) as changed_capabilities:
            agent.enqueue_message(
                RunRequest.from_dict(
                    {
                        "prompt": "change capabilities",
                        "mcp": [],
                        "skills": [{"name": "new-skill"}],
                    }
                ),
                task_id="steering-task",
                message_id="message-new-capability",
                identity="owner-1",
                session_id="context-1",
                tenant_id="tenant-1",
            )
        self.assertEqual(changed_capabilities.exception.code, "INVALID_REQUEST")
        release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failure, [])
        self.assertEqual(result[0].message, "steered answer")
        self.assertTrue(model.assert_released)
        self.assertEqual(
            [
                message["content"]
                for message in model.calls[1]
                if message.get("role") == "user"
            ],
            ["Do it", "first correction", "second correction"],
        )
        with self.assertRaises(CoreError) as terminal:
            agent.enqueue_message(
                RunRequest.from_dict(
                    {"prompt": "too late", "mcp": [], "skills": []}
                ),
                task_id="steering-task",
                message_id="message-3",
                identity="owner-1",
                session_id="context-1",
                tenant_id="tenant-1",
            )
        self.assertEqual(terminal.exception.code, "TASK_TERMINAL")
        agent.close()

    def test_invalid_request_fails_before_model_or_tools(self):
        model = ScriptedModel([ModelResponse(message="should not run")])
        agent = make_agent(model)
        with self.assertRaises(CoreError) as caught:
            agent.run({"prompt": "x", "mcp": [], "skills": [], "extra": True})
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")
        self.assertEqual(model.calls, ())
        self.assertEqual(agent.tool_runtime.execution_count, 0)
        agent.close()

    def test_agent_loop_executes_valid_tool_then_returns_final_message(self):
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_requests=(
                        ToolRequest(
                            "call-1", "core.terminal.exec", {"argv": ["check"]}
                        ),
                    )
                ),
                ModelResponse(message="done"),
            ]
        )
        agent = make_agent(model, memory="disabled")
        result = agent.run(run_request(memory=False))
        self.assertEqual(result.message, "done")
        self.assertEqual(result.terminal_state, "completed")
        self.assertEqual(result.usage.tool_calls, 1)
        self.assertEqual(len(model.calls), 2)
        second_context = model.calls[1].context
        self.assertIn("tool-ok", second_context)
        self.assertEqual(
            [message["role"] for message in model.calls[1].messages],
            ["user", "assistant", "tool"],
        )
        self.assertEqual(
            model.calls[1].messages[-1]["tool_call_id"], "call-1"
        )
        self.assertNotIn("reasoning", result.to_dict())
        agent.close()

    def test_structured_logs_show_flow_without_private_reasoning_or_secrets(self):
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_requests=(
                        ToolRequest(
                            "call-log", "core.terminal.exec", {"argv": ["check"]}
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
                ModelResponse(
                    message="finished",
                    reasoning="private chain must not be logged",
                    prompt_tokens=20,
                    completion_tokens=5,
                    total_tokens=25,
                    finish_reason="stop",
                ),
            ]
        )
        agent = make_agent(model, memory="disabled", log_content=True)
        request = RunRequest.from_dict(
            {
                "prompt": "run with sk-12345678901234567890",
                "mcp": [],
                "skills": [],
            }
        )
        try:
            with self.assertLogs("core_agent.runtime", level="INFO") as captured:
                result = agent.run(request)
        finally:
            agent.close()

        records = [json.loads(record.getMessage()) for record in captured.records]
        events = {record["event"] for record in records}
        self.assertEqual(result.message, "finished")
        self.assertTrue(
            {
                "task.started",
                "model.requested",
                "model.response",
                "tool.requested",
                "tool.completed",
                "workflow.transition",
            }
            <= events
        )
        encoded = json.dumps(records)
        self.assertIn('"argv": ["check"]', encoded)
        self.assertIn("finished", encoded)
        self.assertIn("[REDACTED]", encoded)
        self.assertNotIn("private chain must not be logged", encoded)
        final_model_record = next(
            record
            for record in records
            if record["event"] == "model.response"
            and record["action"] == "final_answer"
        )
        self.assertTrue(final_model_record["reasoning_private"])
        self.assertEqual(final_model_record["prompt_tokens"], 20)
        self.assertEqual(final_model_record["completion_tokens"], 5)
        self.assertEqual(final_model_record["total_tokens"], 25)

    def test_terminal_start_failure_returns_to_model_and_task_completes(self):
        exporter = RecordingExporter()
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_requests=(
                        ToolRequest(
                            "call-1",
                            "core.terminal.exec",
                            {"argv": ["echo && hello-tool-check && pwd"]},
                        ),
                    )
                ),
                ModelResponse(message="The command was malformed."),
            ]
        )
        agent = make_agent(
            model,
            memory="disabled",
            telemetry=Telemetry(exporter, content_enabled=True),
        )

        def fail_before_start(_request, _run_id):
            raise CoreError(
                "TOOL_START_FAILED",
                "No such file or directory: 'echo && hello-tool-check && pwd'",
            )

        agent.tool_runtime.environment_manager.execute_transient = fail_before_start
        try:
            result = agent.run(run_request(memory=False))
        finally:
            agent.close()

        self.assertEqual(result.terminal_state, "completed")
        self.assertEqual(result.message, "The command was malformed.")
        self.assertEqual(len(model.calls), 2)
        self.assertIn('"status": "failed"', model.calls[1].context)
        self.assertIn('"error_code": "TOOL_START_FAILED"', model.calls[1].context)
        tool_span = next(
            span for span in exporter.spans if span.name == "core_agent.tool.execute"
        )
        self.assertEqual(tool_span.status_code, "ERROR")
        self.assertEqual(tool_span.attributes["core_agent.tool.outcome"], "failed")
        self.assertEqual(
            tool_span.attributes["core_agent.error.code"], "TOOL_START_FAILED"
        )

    def test_provider_adapters_preserve_native_tool_call_and_result_messages(self):
        tools = {
            "core.terminal.exec": {
                "description": "execute",
                "input_schema": {"type": "object"},
            }
        }
        messages = [
            {"role": "user", "content": "run it"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {
                            "name": "core.terminal.exec",
                            "arguments": {"argv": ["check"]},
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "name": "core.terminal.exec",
                "content": '{"status":"succeeded"}',
            },
        ]
        openai = CompatibleHttpModel(api_format="openai", model="test")
        body, _headers, reverse = openai._request(
            "unused", "system", tools, messages=messages
        )
        self.assertEqual(set(reverse), {"core_terminal_exec"})
        self.assertIn(
            "Canonical tool name: core.terminal.exec",
            body["tools"][0]["function"]["description"],
        )
        self.assertEqual(
            [message["role"] for message in body["messages"]],
            ["system", "user", "assistant", "tool"],
        )
        self.assertEqual(body["messages"][2]["tool_calls"][0]["id"], "call-1")
        self.assertEqual(body["messages"][3]["tool_call_id"], "call-1")
        parsed = openai._parse_openai(
            {
                "choices": [
                    {
                        "message": {"content": "Use core_terminal_exec."},
                        "finish_reason": "stop",
                    }
                ]
            },
            reverse,
        )
        self.assertEqual(parsed.message, "Use core.terminal.exec.")

        anthropic = CompatibleHttpModel(api_format="anthropic", model="test")
        body, _headers, _reverse = anthropic._request(
            "unused", "system", tools, messages=messages
        )
        self.assertEqual(
            [message["role"] for message in body["messages"]],
            ["user", "assistant", "user"],
        )
        self.assertEqual(body["messages"][1]["content"][0]["type"], "tool_use")
        self.assertEqual(body["messages"][2]["content"][0]["type"], "tool_result")

    def test_provider_wire_alias_adds_hash_only_for_a_real_collision(self):
        model = CompatibleHttpModel(api_format="openai", model="test")
        schemas, reverse = model._tools({"example.tool": {}, "example_tool": {}})
        aliases = {item["function"]["name"] for item in schemas}
        self.assertEqual(set(reverse.values()), {"example.tool", "example_tool"})
        self.assertEqual(len(aliases), 2)
        self.assertIn("example_tool", aliases)
        self.assertTrue(
            any(name.startswith("example_tool_") for name in aliases - {"example_tool"})
        )

    def test_stale_delegate_call_at_maximum_depth_creates_no_task(self):
        model = ScriptedModel([ModelResponse(message="must not run")])
        agent = make_agent(model, memory="disabled", depth=2)
        request = run_request(memory=False)
        _raw, _discovered, effective = agent._resolve_capabilities(request)
        run_id = "stale-depth-two"
        agent._run_contexts[run_id] = (request, effective)
        with self.assertRaises(CoreError) as caught:
            agent._delegate(
                {
                    "instruction": "must not start",
                    "tools": [],
                    "mcp": {},
                    "skills": [],
                    "budget": {"turns": 1, "tool_calls": 1},
                },
                run_id,
            )
        self.assertEqual(caught.exception.code, "BUDGET_EXCEEDED")
        self.assertEqual(agent.task_scheduler.list(owner_id=run_id), ())
        self.assertEqual(model.calls, ())
        agent.close()

    def test_delegate_joins_by_default_and_returns_child_result(self):
        model = ScriptedModel([ModelResponse(message="child-result")])
        agent = make_agent(model, memory="disabled")
        request = run_request(memory=False)
        record, _raw, _discovered, _effective = agent._new_workflow(
            request,
            task_id="joined-parent-task",
            identity="owner",
            session_id="session",
            tenant_id="default",
        )
        try:
            result = agent._delegate(
                {
                    "instruction": "Return child result",
                    "tools": [],
                    "mcp": {},
                    "skills": [],
                    "budget": {"turns": 2, "tool_calls": 1},
                },
                record.run_id,
            )
        finally:
            agent.close()
        self.assertEqual(result["mode"], "joined")
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["result"]["message"], "child-result")

    def test_completed_background_notification_enters_parent_context_once(self):
        model = ScriptedModel([ModelResponse(message="unused")])
        agent = make_agent(model, memory="disabled")
        record, _raw, _discovered, _effective = agent._new_workflow(
            run_request(memory=False),
            task_id="notification-parent",
            identity="owner",
            session_id="session",
            tenant_id="default",
        )
        task = agent.task_scheduler.start(
            lambda: {"message": "child-notification"},
            owner_id=record.run_id,
            required=True,
            kind="test",
            contract={},
        )
        agent.task_scheduler.wait(task.id, owner_id=record.run_id)
        lease = agent.workflow_store.acquire_lease(
            record.run_id,
            tenant_id=record.tenant_id,
            owner_id=record.owner_id,
            worker_id=agent._worker_id,
            ttl=600,
        )
        try:
            record, snapshot = agent._consume_task_notifications(
                record, copy.deepcopy(record.snapshot), lease_token=lease
            )
            _record, second = agent._consume_task_notifications(
                record, copy.deepcopy(snapshot), lease_token=lease
            )
        finally:
            agent.workflow_store.release_lease(
                record.run_id,
                tenant_id=record.tenant_id,
                worker_id=agent._worker_id,
                token=lease,
            )
            agent.close()
        notification_items = [
            item
            for item in agent._context_from_dict(second["context"]).active
            if item.kind == "task_notification"
        ]
        self.assertEqual(len(notification_items), 1)
        self.assertIn("child-notification", notification_items[0].content)

    def test_memory_disabled_never_connects_or_discovers_memory(self):
        connector = InMemoryMcpConnector(
            catalogs={"memory": {"search": {"type": "object"}}},
            results={"memory.search": {"results": [{"text": "secret memory"}]}},
        )
        model = ScriptedModel([ModelResponse(message="done")])
        agent = make_agent(model, memory="disabled", connector=connector)
        result = agent.run(run_request(memory=True))
        self.assertEqual(result.message, "done")
        self.assertEqual(connector.connections, ())
        self.assertNotIn("memory.search", model.calls[0].tools)
        self.assertNotIn("MEMORY", model.calls[0].instructions)
        agent.close()

    def test_memory_mcp_is_filtered_and_visible_only_when_enabled(self):
        connector = InMemoryMcpConnector(
            catalogs={
                "memory": {
                    "search": {"type": "object"},
                    "read": {"type": "object"},
                    "delete": {"type": "object"},
                }
            }
        )
        model = ScriptedModel([ModelResponse(message="done")])
        agent = make_agent(model, connector=connector)
        agent.run(run_request(memory=True))
        self.assertEqual(connector.connections, ("memory",))
        self.assertIn("memory.search", model.calls[0].tools)
        self.assertIn("memory.read", model.calls[0].tools)
        self.assertNotIn("memory.delete", model.calls[0].tools)
        agent.close()

    def test_protected_kernel_is_compiled_persisted_and_cannot_be_replaced_by_profile(
        self,
    ):
        model = ScriptedModel([ModelResponse(message="done")])
        compiler = KernelCompiler(
            "SAFETY IMMUTABLE",
            "HOST EFFECTIVE CONFIG ONLY",
            "KERNEL DURABLE RULES",
            {"memory": "MEMORY AUTHORING CONTRACT"},
        )
        agent = make_agent(model, kernel_compiler=compiler)
        result = agent.run(run_request(memory=True), task_id="kernel-task")
        instructions = model.calls[0].instructions
        self.assertLess(
            instructions.index("KERNEL DURABLE RULES"),
            instructions.index("Act as a test agent."),
        )
        self.assertIn("MEMORY AUTHORING CONTRACT", instructions)
        record = agent.workflow_store.lookup_task("kernel-task")
        self.assertEqual(record.snapshot["compiled_instructions"], instructions)
        self.assertTrue(record.snapshot["protected_kernel_digest"])
        self.assertIn("Do it", model.calls[0].context)
        self.assertNotIn("Do it", instructions)
        self.assertEqual(result.message, "done")
        agent.close()

    def test_runtime_compacts_twice_and_keeps_prompt_and_full_transcript(self):
        connector = InMemoryMcpConnector(
            catalogs={"memory": {"search": {"type": "object"}}},
            results={"memory.search": {"blob": "x" * 3_000}},
        )
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_requests=(ToolRequest(f"call-{index}", "memory.search", {}),)
                )
                for index in range(2)
            ]
            + [ModelResponse(message="done")]
        )
        agent = make_agent(
            model,
            connector=connector,
            context_window=1_000,
            output_reserve=100,
        )
        result = agent.run(run_request(memory=True), task_id="compact-task")
        record = agent.workflow_store.lookup_task("compact-task")
        context = agent._context_from_dict(record.snapshot["context"])
        self.assertEqual(context.active[0].content, "Do it")
        self.assertEqual(context.transcript[0].content, "Do it")
        self.assertGreater(len(context.transcript), len(context.active))
        self.assertEqual(
            agent.event_store.count(result.run_id, kind="context.compacted"), 2
        )
        agent.close()

    def test_budget_stops_loop_without_automatic_growth(self):
        model = ScriptedModel([ModelResponse(continue_reasoning=True)] * 5)
        agent = make_agent(model, memory="disabled", max_turns=2)
        with self.assertRaises(CoreError) as caught:
            agent.run(run_request(memory=False))
        self.assertEqual(caught.exception.code, "BUDGET_EXCEEDED")
        self.assertEqual(len(model.calls), 2)
        agent.close()

    def test_audit_is_append_only_and_records_before_publication(self):
        model = ScriptedModel([ModelResponse(message="done")])
        agent = make_agent(model, memory="disabled")
        result = agent.run(run_request(memory=False))
        audit = agent.audit_log.records(result.run_id)
        events = agent.event_store.events(result.run_id)
        self.assertEqual(
            [record.sequence for record in audit],
            sorted(record.sequence for record in audit),
        )
        self.assertEqual(audit[-1].kind, "task.completed")
        self.assertLessEqual(audit[-1].written_at, events[-1].published_at)
        with self.assertRaises(CoreError):
            agent.audit_log.replace(result.run_id, 1, {"changed": True})
        agent.close()


class DurabilityTests(unittest.TestCase):
    def test_lease_allows_only_one_owner_and_can_be_recovered(self):
        leases = LeaseManager()
        first = leases.acquire("run-1", "worker-a", ttl=0.02)
        with self.assertRaises(CoreError) as caught:
            leases.acquire("run-1", "worker-b", ttl=1)
        self.assertEqual(caught.exception.code, "LEASE_LOST")
        time.sleep(0.03)
        second = leases.acquire("run-1", "worker-b", ttl=1)
        self.assertNotEqual(first.token, second.token)
        with self.assertRaises(CoreError):
            leases.renew("run-1", "worker-a", first.token, ttl=1)

    def test_recovery_restores_pending_approval_input_and_notifications(self):
        events = InMemoryEventStore()
        checkpoints = CheckpointStore()
        events.append("run-1", "task.started", {})
        events.append("run-1", "approval.required", {"approval_id": "apr-1"})
        events.append("run-1", "input.required", {"input_id": "input-1"})
        events.append("run-1", "task.notification", {"notification_id": "notice-1"})
        checkpoints.save("run-1", events.revision("run-1"), {"state": "waiting"})
        restored = RecoveryManager(events, checkpoints).recover("run-1")
        self.assertEqual(restored.pending_approvals, ("apr-1",))
        self.assertEqual(restored.pending_inputs, ("input-1",))
        self.assertEqual(restored.pending_notifications, ("notice-1",))
        self.assertEqual(restored.revision, events.revision("run-1"))

    def test_unknown_mutating_side_effect_is_not_replayed(self):
        events = InMemoryEventStore()
        checkpoints = CheckpointStore()
        events.append(
            "run-1", "tool.intent", {"tool_call_id": "call-1", "mutating": True}
        )
        checkpoints.save("run-1", 1, {"state": "tool_running"})
        with self.assertRaises(CoreError) as caught:
            RecoveryManager(events, checkpoints).recover("run-1")
        self.assertEqual(caught.exception.code, "SIDE_EFFECT_UNKNOWN")
        self.assertEqual(events.count("run-1", kind="tool.started"), 0)


class ObservabilityTests(unittest.TestCase):
    def test_openinference_llm_and_tool_spans_show_agent_inputs_and_outputs(self):
        exporter = RecordingExporter()
        telemetry = Telemetry(exporter, content_enabled=True)
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_requests=(
                        ToolRequest(
                            "call-1", "core.terminal.exec", {"argv": ["check"]}
                        ),
                    ),
                    prompt_tokens=40,
                    completion_tokens=6,
                    total_tokens=46,
                    finish_reason="tool_calls",
                ),
                ModelResponse(message="done", finish_reason="stop"),
            ]
        )
        model.api_key = "provider-secret"
        agent = make_agent(model, memory="disabled", telemetry=telemetry)
        try:
            request = RunRequest.from_dict(
                {"prompt": "Do it with provider-secret", "mcp": [], "skills": []}
            )
            agent.run(request, session_id="context-1")
        finally:
            agent.close()

        llm_spans = [span for span in exporter.spans if span.name == "gen_ai.chat"]
        self.assertEqual(len(llm_spans), 2)
        first = llm_spans[0]
        self.assertEqual(first.attributes["openinference.span.kind"], "LLM")
        self.assertEqual(first.attributes["session.id"], "context-1")
        self.assertEqual(
            first.attributes["llm.input_messages.0.message.content"],
            model.calls[0].instructions,
        )
        self.assertEqual(
            first.attributes["llm.input_messages.1.message.content"],
            model.calls[0].context.replace("provider-secret", "[REDACTED]"),
        )
        self.assertNotIn("provider-secret", str(first.attributes))
        tool_schemas = [
            value
            for key, value in first.attributes.items()
            if key.startswith("llm.tools.")
        ]
        self.assertTrue(any("core.terminal.exec" in schema for schema in tool_schemas))
        self.assertEqual(
            first.attributes[
                "llm.output_messages.0.message.tool_calls.0.tool_call.function.name"
            ],
            "core.terminal.exec",
        )
        self.assertEqual(first.attributes["llm.token_count.total"], 46)
        self.assertEqual(first.status_code, "OK")
        second = llm_spans[1].attributes
        self.assertEqual(second["llm.input_messages.2.message.role"], "assistant")
        self.assertEqual(second["llm.input_messages.3.message.role"], "tool")
        self.assertEqual(
            second["llm.input_messages.3.message.tool_call_id"], "call-1"
        )

        tool_span = next(
            span for span in exporter.spans if span.name == "core_agent.tool.execute"
        )
        self.assertEqual(tool_span.attributes["openinference.span.kind"], "TOOL")
        self.assertEqual(tool_span.attributes["tool.name"], "core.terminal.exec")
        self.assertIn('"check"', tool_span.attributes["input.value"])
        self.assertIn("tool-ok", tool_span.attributes["output.value"])
        self.assertEqual(tool_span.status_code, "OK")

    def test_w3c_context_propagates_without_becoming_authorization(self):
        exporter = RecordingExporter()
        telemetry = Telemetry(exporter)
        incoming = {
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        }
        context = telemetry.extract(incoming)
        outgoing = {}
        telemetry.inject(context, outgoing)
        self.assertEqual(outgoing["traceparent"], incoming["traceparent"])
        self.assertIsNone(context.tenant_id)
        self.assertIsNone(context.authorization)
        invalid = telemetry.extract(
            {"traceparent": "00-not-a-trace-id-not-a-span-id-01"}
        )
        self.assertNotEqual(invalid.trace_id, "not-a-trace-id")
        self.assertEqual(telemetry.dropped_records, 1)

    def test_background_task_uses_new_trace_with_link_not_long_request_span(self):
        exporter = RecordingExporter()
        telemetry = Telemetry(exporter)
        with telemetry.span("core_agent.task.submit") as submission:
            linked = telemetry.start_background_span(
                "core_agent.task.execute", submission.context
            )
        self.assertTrue(submission.ended)
        self.assertNotEqual(linked.context.trace_id, submission.context.trace_id)
        self.assertEqual(linked.links[0].trace_id, submission.context.trace_id)
        linked.end()

    def test_subagent_task_continues_parent_trace(self):
        exporter = RecordingExporter()
        telemetry = Telemetry(exporter)
        scheduler = TaskScheduler(telemetry=telemetry)
        with telemetry.span("core_agent.parent") as parent:
            task = scheduler.start(
                lambda: "child-ok",
                owner_id="parent-run",
                kind="subagent",
                continue_trace=True,
            )
        scheduler.wait(task.id, timeout=1, owner_id="parent-run")
        child = next(
            span
            for span in exporter.spans
            if span.name == "core_agent.task.execute"
        )
        self.assertEqual(child.context.trace_id, parent.context.trace_id)
        self.assertEqual(child.links, ())
        scheduler.close()

    def test_background_span_without_submission_context_is_valid(self):
        exporter = RecordingExporter()
        telemetry = Telemetry(exporter)
        with telemetry.start_background_span("core_agent.task.execute", None) as span:
            self.assertEqual(span.links, ())
        self.assertTrue(span.ended)

    def test_content_is_off_and_metric_labels_are_bounded(self):
        exporter = RecordingExporter()
        telemetry = Telemetry(exporter, content_enabled=False)
        with telemetry.span(
            "gen_ai.chat",
            attributes={
                "gen_ai.input.messages": "secret prompt",
                "gen_ai.tool.call.arguments": {"token": "secret"},
                "llm.input_messages.0.message.content": "secret system prompt",
                "llm.tools.0.tool.json_schema": "secret schema",
                "core_agent.task.state": "working",
            },
        ) as span:
            span.set_attribute("output.value", "secret response")
        attrs = exporter.spans[-1].attributes
        self.assertNotIn("gen_ai.input.messages", attrs)
        self.assertNotIn("gen_ai.tool.call.arguments", attrs)
        self.assertNotIn("llm.input_messages.0.message.content", attrs)
        self.assertNotIn("llm.tools.0.tool.json_schema", attrs)
        self.assertNotIn("output.value", attrs)
        self.assertEqual(attrs["openinference.span.kind"], "LLM")
        self.assertEqual(attrs["core_agent.task.state"], "working")
        with self.assertRaises(CoreError) as caught:
            telemetry.metric("core_agent.tasks", 1, labels={"task_id": "task-1"})
        self.assertEqual(caught.exception.code, "TELEMETRY_CARDINALITY_VIOLATION")

    def test_exporter_failure_does_not_damage_task_or_audit(self):
        audit = InMemoryAuditLog()
        telemetry = Telemetry(FailingExporter())
        audit.append("run-1", "task.completed", {"status": "ok"})
        with telemetry.span("core_agent.task.execute"):
            pass
        self.assertEqual(audit.records("run-1")[0].kind, "task.completed")
        self.assertEqual(telemetry.dropped_records, 1)

    def test_span_records_error_status_without_error_message_content(self):
        exporter = RecordingExporter()
        telemetry = Telemetry(exporter)
        with self.assertRaisesRegex(ValueError, "secret detail"):
            with telemetry.span("core_agent.test"):
                raise ValueError("secret detail")
        span = exporter.spans[-1]
        self.assertEqual(span.status_code, "ERROR")
        self.assertEqual(span.status_message, "ValueError")
        self.assertEqual(span.attributes["error.type"], "ValueError")
        self.assertNotIn("secret detail", str(span.attributes))

    def test_otlp_http_exports_traces_metrics_and_logs_without_content(self):
        received = []

        class Collector(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                received.append((self.path, body))
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Collector)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        telemetry = Telemetry.otlp(
            endpoint=f"http://127.0.0.1:{server.server_port}",
            service_name="collector-integration-test",
        )
        try:
            with telemetry.span(
                "core_agent.test",
                attributes={"gen_ai.input.messages": "secret prompt"},
            ):
                pass
            telemetry.metric("core_agent.test.count", 1, labels={"outcome": "ok"})
            telemetry.log(
                "safe-event",
                attributes={"gen_ai.output.messages": "secret response"},
            )
            telemetry.shutdown()
        finally:
            server.shutdown()
            server.server_close()
        paths = {path for path, _body in received}
        self.assertEqual(paths, {"/v1/traces", "/v1/metrics", "/v1/logs"})
        payload = b"".join(body for _path, body in received)
        self.assertNotIn(b"secret prompt", payload)
        self.assertNotIn(b"secret response", payload)

    def test_otlp_env_can_export_only_traces_to_a_trace_backend(self):
        received = []

        class TraceBackend(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                received.append((self.path, body))
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), TraceBackend)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        endpoint = f"http://127.0.0.1:{server.server_port}/v1/traces"
        with patch.dict(
            os.environ,
            {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "",
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": endpoint,
                "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "",
                "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "",
                "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "true",
            },
        ):
            telemetry = Telemetry.otlp_from_env(service_name="trace-only-test")
        self.assertTrue(telemetry.content_enabled)
        try:
            with telemetry.span("core_agent.trace_only"):
                pass
            telemetry.metric("core_agent.trace_only.count", 1)
            telemetry.log("trace-only-safe-log")
            telemetry.shutdown()
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual([path for path, _body in received], ["/v1/traces"])

    def test_otlp_env_is_optional(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(Telemetry.otlp_from_env())

    def test_memory_service_continues_core_mcp_trace_and_owns_internal_spans(self):
        exporter = RecordingExporter()
        telemetry = Telemetry(exporter)
        with tempfile.TemporaryDirectory() as temp:
            service = MemoryService(Path(temp), telemetry=telemetry)
            service.search("bootstrap", namespace="session/context-1")
            content = (
                "---\nid: mem-1\ntitle: One\nnamespace: session/context-1\nkind: fact\n"
                "status: active\ncreated_at: now\nupdated_at: now\nsources: []\n---\nAlice knows Bob.\n"
            )
            with telemetry.span(
                "mcp.client", attributes={"mcp.server": "memory"}
            ) as client_span:
                carrier = {}
                telemetry.inject(client_span.context, carrier)
                service.create("session/one.md", content, 0, trace_carrier=carrier)
            names = [span.name for span in exporter.spans]
            self.assertIn("mcp.client", names)
            self.assertIn("memory_service.mcp.request", names)
            self.assertIn("memory_service.ner", names)
            self.assertIn("memory_service.index_publish", names)
            self.assertIn("memory_service.search.bm25", names)
            self.assertIn("memory_service.search.vector", names)
            self.assertIn("memory_service.search.graph", names)
            self.assertIn("memory_service.search.rerank", names)
            core_internal = [
                name for name in names if name.startswith("core_agent.memory.")
            ]
            self.assertEqual(core_internal, [])
            memory_span = next(
                span
                for span in exporter.spans
                if span.name == "memory_service.mcp.request"
            )
            self.assertEqual(memory_span.context.trace_id, client_span.context.trace_id)
            service.close()


if __name__ == "__main__":
    unittest.main()
