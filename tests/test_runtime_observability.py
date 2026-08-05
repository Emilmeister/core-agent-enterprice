import json
import os
import copy
import time
import unittest
import threading
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core_agent.audit import InMemoryAuditLog
from core_agent.artifacts import InMemoryArtifactStore
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
    ToolDefinition,
    ToolRegistry,
    ToolRuntime,
)
from core_agent.execution import ExecutionEnvironmentManager, ExecutionResult
from core_agent.memory import MemoryRegistry
from core_agent.memory_store import InMemoryMemoryStore


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
            "core_terminal_exec",
            "core_task_start",
            "core_task_wait",
            "core_delegate",
            "core_memory_search",
        },
        denied_builtin_tools=set(),
        allowed_mcp_servers={"docs"},
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
        a2a_interfaces=(("HTTP+JSON", "1.0"),),
        max_model_turns=10,
        max_tool_calls=10,
    )


def agent_config(memory="optional", *, max_turns=10, max_tools=10, mcp=True):
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
                "mcp": mcp,
                "skills": False,
                "human_input": True,
            },
            "tools": {
                "builtins": {
                    "default": "deny",
                    "allow": [
                        "core_terminal_exec",
                        "core_task_*",
                        "core_delegate",
                        "core_memory_*",
                    ],
                    "deny": [],
                },
                "mcp": {
                    "default": "deny",
                    "allow_servers": ["docs"],
                    "allow_tools": {
                        "docs": ["search", "read", "create", "update", "split"]
                    },
                },
            },
            "skills": {"default": "deny", "allow": []},
            "context": {
                "compact_at_working_ratio": 0.90,
                "compact_to_working_ratio": 0.15,
            },
            "execution": {"environment_profile": "local-pty-test"},
            "observability": {"otel_profile": "test"},
            "budgets": {"model_turns": max_turns, "tool_calls": max_tools},
        }
    )


DOCS_MCP = {
    "name": "docs",
    "required": False,
    "transport": {"type": "streamable_http", "url": "https://docs.test/mcp"},
}


def run_request(memory=True):
    # MCP is deployment configuration now; the flag only picks the agent fixture.
    return RunRequest.from_dict({"prompt": "Do it"})


def delegate_definition():
    return ToolDefinition(
        "core_delegate",
        "delegate",
        {
            "type": "object",
            "properties": {
                "instruction": {"type": "string", "minLength": 1},
                "tools": {"type": "array", "items": {"type": "string"}},
                "skills": {"type": "array", "items": {"type": "string"}},
                "budget": {
                    "type": "object",
                    "properties": {
                        "turns": {"type": "integer", "minimum": 1},
                        "tool_calls": {"type": "integer", "minimum": 1},
                    },
                    "required": ["turns", "tool_calls"],
                    "additionalProperties": False,
                },
                "background": {"type": "boolean"},
            },
            "required": ["instruction", "tools", "skills", "budget"],
            "additionalProperties": False,
        },
        mutating=False,
        risk_tags=frozenset(),
    )


def make_agent(
    model,
    *,
    memory="optional",
    mcp=True,
    platform_mcp=(DOCS_MCP,),
    connector=None,
    telemetry=None,
    max_turns=10,
    max_tools=10,
    task_scheduler=None,
    terminal_mutating=False,
    **agent_options,
):
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            "core_terminal_exec",
            "execute",
            {
                "type": "object",
                "properties": {"argv": {"type": "array", "items": {"type": "string"}}},
                "required": ["argv"],
                "additionalProperties": False,
            },
            mutating=terminal_mutating,
            risk_tags=frozenset(),
        )
    )
    tool_runtime = ToolRuntime(
        registry,
        ExecutionEnvironmentManager(IsolatedBackend()),
    )
    return CoreAgent(
        platform_config=platform(),
        agent_config=agent_config(
            memory, max_turns=max_turns, max_tools=max_tools, mcp=mcp
        ),
        model=model,
        tool_runtime=tool_runtime,
        mcp_connector=connector or InMemoryMcpConnector(),
        task_scheduler=task_scheduler or TaskScheduler(),
        event_store=InMemoryEventStore(),
        checkpoint_store=CheckpointStore(),
        audit_log=InMemoryAuditLog(),
        telemetry=telemetry or Telemetry(RecordingExporter()),
        platform_mcp=platform_mcp,
        **agent_options,
    )


class RuntimeTests(unittest.TestCase):
    def _mark_executing_mutation(self, agent, task_id, *, parent_run_id=None):
        record, _raw, _discovered, _effective = agent._new_workflow(
            run_request(memory=False),
            task_id=task_id,
            identity="owner",
            session_id="session",
            tenant_id="default",
            parent_run_id=parent_run_id,
        )
        pending = {
            "id": "mutation-call",
            "name": "core_terminal_exec",
            "arguments": {"argv": ["mutate"]},
        }
        snapshot = copy.deepcopy(record.snapshot)
        snapshot["pending_response"] = CoreAgent._response_dict(
            ModelResponse(
                tool_requests=(
                    ToolRequest(
                        pending["id"], pending["name"], pending["arguments"]
                    ),
                )
            )
        )
        snapshot["tool_queue"] = [pending]
        snapshot["pending_call"] = pending
        snapshot["tool_calls"] = 1
        agent.workflow_store.consume_budget(record, tool_calls=1)
        return agent._record_transition(
            record,
            state="EXECUTING",
            snapshot=snapshot,
            event_kind="tool.intent",
            event_data={"tool_call_id": pending["id"], "mutating": True},
        )

    def _executing_subagent(self, agent, task_id):
        parent, _raw, _discovered, _effective = agent._new_workflow(
            run_request(memory=False),
            task_id=f"{task_id}-parent",
            identity="owner",
            session_id="session",
            tenant_id="default",
        )
        child_raw = agent.agent_config.to_dict()
        child = agent._child_agent(child_raw, ("core_terminal_exec",))
        scope = {
            "task_id": task_id,
            "identity": "owner",
            "session_id": "session",
            "tenant_id": "default",
            "parent_run_id": parent.run_id,
        }
        record = self._mark_executing_mutation(
            child, task_id, parent_run_id=parent.run_id
        )
        contract = {
            "request": run_request(memory=False).to_dict(),
            "agent_config": child_raw,
            "tools": ["core_terminal_exec"],
            "scope": scope,
        }
        return parent, record, contract

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
            RunRequest.from_dict({"prompt": "first correction"}),
            task_id="steering-task",
            message_id="message-1",
            identity="owner-1",
            session_id="context-1",
            tenant_id="tenant-1",
        )
        duplicate = agent.enqueue_message(
            RunRequest.from_dict({"prompt": "first correction"}),
            task_id="steering-task",
            message_id="message-1",
            identity="owner-1",
            session_id="context-1",
            tenant_id="tenant-1",
        )
        second = agent.enqueue_message(
            RunRequest.from_dict({"prompt": "second correction"}),
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
                RunRequest.from_dict({"prompt": "wrong context"}),
                task_id="steering-task",
                message_id="message-wrong-context",
                identity="owner-1",
                session_id="context-2",
                tenant_id="tenant-1",
            )
        self.assertEqual(wrong_context.exception.code, "INVALID_REQUEST")
        with self.assertRaises(CoreError) as wrong_owner:
            agent.enqueue_message(
                RunRequest.from_dict({"prompt": "wrong owner"}),
                task_id="steering-task",
                message_id="message-wrong-owner",
                identity="owner-2",
                session_id="context-1",
                tenant_id="tenant-1",
            )
        self.assertEqual(wrong_owner.exception.code, "TASK_NOT_FOUND")
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
                RunRequest.from_dict({"prompt": "too late"}),
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
            agent.run({"prompt": "x", "extra": True})
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
                            "call-1", "core_terminal_exec", {"argv": ["check"]}
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
        self.assertTrue(result.complete)
        self.assertEqual(result.completion_reason, "completed")
        self.assertEqual(result.usage.tool_calls, 1)
        self.assertEqual(len(model.calls), 2)
        second_context = model.calls[1].context
        self.assertIn("tool-ok", second_context)
        self.assertEqual(
            [message["role"] for message in model.calls[1].messages],
            ["user", "assistant", "tool"],
        )
        self.assertEqual(model.calls[1].messages[-1]["tool_call_id"], "call-1")
        self.assertNotIn("reasoning", result.to_dict())
        agent.close()

    def test_structured_logs_show_opt_in_provider_reasoning_without_secrets(self):
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_requests=(
                        ToolRequest(
                            "call-log", "core_terminal_exec", {"argv": ["check"]}
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
                ModelResponse(
                    message="finished",
                    reasoning="visible provider reasoning with sk-12345678901234567890",
                    prompt_tokens=20,
                    completion_tokens=5,
                    reasoning_tokens=3,
                    total_tokens=25,
                    finish_reason="stop",
                ),
            ]
        )
        agent = make_agent(model, memory="disabled", log_content=True)
        request = RunRequest.from_dict({"prompt": "run with sk-12345678901234567890"})
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
        self.assertIn("visible provider reasoning with [REDACTED]", encoded)
        self.assertIn("[REDACTED]", encoded)
        self.assertNotIn("sk-12345678901234567890", encoded)
        final_model_record = next(
            record
            for record in records
            if record["event"] == "model.response"
            and record["action"] == "final_answer"
        )
        self.assertTrue(final_model_record["reasoning_available"])
        self.assertEqual(final_model_record["reasoning_tokens"], 3)
        self.assertEqual(final_model_record["prompt_tokens"], 20)
        self.assertEqual(final_model_record["completion_tokens"], 5)
        self.assertEqual(final_model_record["total_tokens"], 25)

    def test_unreported_token_counter_stays_null_instead_of_redacted(self):
        model = ScriptedModel(
            [ModelResponse(message="done", prompt_tokens=7, total_tokens=7)]
        )
        agent = make_agent(model, memory="disabled")
        try:
            with self.assertLogs("core_agent.runtime", level="INFO") as captured:
                agent.run(RunRequest.from_dict({"prompt": "count"}))
        finally:
            agent.close()

        record = next(
            json.loads(item.getMessage())
            for item in captured.records
            if json.loads(item.getMessage())["event"] == "model.response"
        )
        self.assertIn("reasoning_tokens", record)
        self.assertIsNone(record["reasoning_tokens"])
        self.assertEqual(record["prompt_tokens"], 7)

    def test_structured_logs_hide_provider_reasoning_without_content_capture(self):
        model = ScriptedModel(
            [ModelResponse(message="done", reasoning="operator-only reasoning")]
        )
        agent = make_agent(model, memory="disabled", log_content=False)
        try:
            with self.assertLogs("core_agent.runtime", level="INFO") as captured:
                agent.run(run_request(memory=False))
        finally:
            agent.close()
        records = [json.loads(record.getMessage()) for record in captured.records]
        encoded = json.dumps(records)
        self.assertNotIn("operator-only reasoning", encoded)
        model_record = next(
            record for record in records if record["event"] == "model.response"
        )
        self.assertTrue(model_record["reasoning_available"])
        self.assertNotIn("reasoning", model_record)

    def test_terminal_start_failure_returns_to_model_and_task_completes(self):
        exporter = RecordingExporter()
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_requests=(
                        ToolRequest(
                            "call-1",
                            "core_terminal_exec",
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

    def test_mutating_tool_exception_aborts_with_unknown_side_effect(self):
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_requests=(
                        ToolRequest(
                            "mutation-call",
                            "core_terminal_exec",
                            {"argv": ["mutate"]},
                        ),
                    )
                )
            ]
        )
        agent = make_agent(model, memory="disabled", terminal_mutating=True)

        def lose_outcome(_arguments, _run_id):
            raise RuntimeError("connection lost after dispatch")

        agent.tool_runtime.handlers["core_terminal_exec"] = lose_outcome
        try:
            with self.assertRaises((CoreError, RuntimeError)):
                agent.run(
                    run_request(memory=False), task_id="mutating-tool-exception"
                )
            record = agent.workflow_store.lookup_task("mutating-tool-exception")
        finally:
            agent.close()
        self.assertEqual(record.state, "ABORTED")
        self.assertEqual(record.error_code, "SIDE_EFFECT_UNKNOWN")

    def test_resume_task_does_not_redispatch_executing_mutation(self):
        agent = make_agent(
            ScriptedModel([ModelResponse(message="must not resume")]),
            memory="disabled",
            terminal_mutating=True,
        )
        dispatches = []
        agent.tool_runtime.handlers["core_terminal_exec"] = (
            lambda _arguments, _run_id: dispatches.append("dispatched")
        )
        self._mark_executing_mutation(agent, "resume-executing-mutation")
        try:
            with self.assertRaises(CoreError) as caught:
                agent.resume_task("resume-executing-mutation")
            record = agent.workflow_store.lookup_task("resume-executing-mutation")
        finally:
            agent.close()
        self.assertEqual(caught.exception.code, "SIDE_EFFECT_UNKNOWN")
        self.assertEqual(dispatches, [])
        self.assertEqual(record.state, "ABORTED")
        self.assertEqual(record.error_code, "SIDE_EFFECT_UNKNOWN")

    def test_recover_subagent_does_not_redispatch_executing_mutation(self):
        agent = make_agent(
            ScriptedModel([ModelResponse(message="must not recover")]),
            memory="disabled",
            terminal_mutating=True,
        )
        dispatches = []
        agent.tool_runtime.environment_manager.execute_transient = (
            lambda _request, _run_id: dispatches.append("dispatched")
        )
        _parent, _record, contract = self._executing_subagent(
            agent, "recover-executing-mutation"
        )
        try:
            with self.assertRaises(CoreError) as caught:
                agent._recover_subagent(contract, threading.Event())
            record = agent.workflow_store.lookup_task("recover-executing-mutation")
        finally:
            agent.close()
        self.assertEqual(caught.exception.code, "SIDE_EFFECT_UNKNOWN")
        self.assertEqual(dispatches, [])
        self.assertEqual(record.state, "ABORTED")
        self.assertEqual(record.error_code, "SIDE_EFFECT_UNKNOWN")

    def test_cancel_task_does_not_mask_executing_mutation_as_canceled(self):
        agent = make_agent(
            ScriptedModel([ModelResponse(message="unused")]),
            memory="disabled",
            terminal_mutating=True,
        )
        self._mark_executing_mutation(agent, "cancel-executing-mutation")
        try:
            agent.cancel_task("cancel-executing-mutation")
            record = agent.workflow_store.lookup_task("cancel-executing-mutation")
        finally:
            agent.close()
        self.assertEqual(record.state, "ABORTED")
        self.assertEqual(record.error_code, "SIDE_EFFECT_UNKNOWN")

    def test_scheduler_cancel_preserves_unknown_subagent_outcome(self):
        agent = make_agent(
            ScriptedModel([ModelResponse(message="must not recover")]),
            memory="disabled",
            terminal_mutating=True,
        )
        parent, _record, contract = self._executing_subagent(
            agent, "canceled-recovery-mutation"
        )
        started = threading.Event()
        release = threading.Event()

        def recover(cancel_event):
            started.set()
            release.wait(1)
            return agent._recover_subagent(contract, cancel_event)

        task = agent.task_scheduler.start(
            recover,
            owner_id=parent.run_id,
            task_id="canceled-recovery-mutation",
            accepts_cancel_event=True,
            kind="subagent",
            contract=contract,
            recoverable=True,
            mutating=False,
        )
        self.assertTrue(started.wait(1))
        agent.task_scheduler.cancel(task.id, owner_id=parent.run_id)
        release.set()
        try:
            terminal = agent.task_scheduler.wait(task.id, timeout=1)
            record = agent.workflow_store.lookup_task(task.id)
        finally:
            agent.close()
        self.assertEqual(record.state, "ABORTED")
        self.assertEqual(record.error_code, "SIDE_EFFECT_UNKNOWN")
        self.assertEqual(terminal.state, "failed")
        self.assertIsInstance(terminal.error, CoreError)
        self.assertEqual(terminal.error.code, "SIDE_EFFECT_UNKNOWN")

    def test_provider_adapters_preserve_native_tool_call_and_result_messages(self):
        tools = {
            "core_terminal_exec": {
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
                            "name": "core_terminal_exec",
                            "arguments": {"argv": ["check"]},
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "name": "core_terminal_exec",
                "content": '{"status":"succeeded"}',
            },
        ]
        openai = CompatibleHttpModel(api_format="openai", model="test")
        body, _headers, reverse = openai._request(
            "unused", "system", tools, messages=messages
        )
        self.assertEqual(set(reverse), {"core_terminal_exec"})
        # Canonical names carry no dots, so the wire name is the canonical name
        # and the description needs no note explaining a rename.
        self.assertEqual(body["tools"][0]["function"]["name"], "core_terminal_exec")
        self.assertEqual(body["tools"][0]["function"]["description"], "execute")
        self.assertNotIn(
            "Canonical tool name", body["tools"][0]["function"]["description"]
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
        self.assertEqual(parsed.message, "Use core_terminal_exec.")

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

    def test_reasoning_effort_parsing_and_tool_replay_are_provider_native(self):
        tools = {
            "core_terminal_exec": {
                "description": "execute",
                "input_schema": {"type": "object"},
            }
        }
        openai = CompatibleHttpModel(
            api_format="openai",
            provider="minimax",
            model="MiniMax-M3",
            reasoning_effort="high",
            extra_body={"reasoning_effort": "low"},
        )
        body, _headers, reverse = openai._request("run", "system", tools)
        self.assertEqual(body["reasoning_effort"], "high")
        self.assertTrue(body["reasoning_split"])
        self.assertEqual(body["thinking"], {"type": "adaptive"})
        parsed = openai._parse_openai(
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "reasoning_content": "Inspect the request.",
                            "reasoning_details": [
                                {
                                    "type": "reasoning.text",
                                    "text": "Inspect the request.",
                                    "signature": "opaque-must-not-be-telemetry",
                                }
                            ],
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "function": {
                                        "name": next(iter(reverse)),
                                        "arguments": '{"argv":["check"]}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 8,
                    "total_tokens": 18,
                    "completion_tokens_details": {"reasoning_tokens": 6},
                },
            },
            reverse,
        )
        self.assertEqual(parsed.reasoning, "Inspect the request.")
        self.assertEqual(parsed.reasoning_tokens, 6)
        final = openai._parse_openai(
            {
                "choices": [
                    {
                        "message": {"content": "<think>Check once.</think>Answer."},
                        "finish_reason": "stop",
                    }
                ]
            },
            reverse,
        )
        self.assertEqual(final.reasoning, "Check once.")
        self.assertEqual(final.message, "Answer.")
        replay_messages = [
            {"role": "user", "content": "run"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {
                            "name": "core_terminal_exec",
                            "arguments": {"argv": ["check"]},
                        },
                    }
                ],
                "reasoning_replay": parsed.reasoning_replay,
            },
        ]
        replay_body, _headers, _reverse = openai._request(
            "unused", "system", tools, messages=replay_messages
        )
        assistant = replay_body["messages"][2]
        self.assertEqual(
            assistant["reasoning_details"],
            parsed.reasoning_replay["fields"]["reasoning_details"],
        )

        anthropic = CompatibleHttpModel(
            api_format="anthropic",
            provider="anthropic",
            model="claude-test",
            reasoning_effort="xhigh",
        )
        body, _headers, reverse = anthropic._request("run", "system", tools)
        self.assertEqual(body["output_config"], {"effort": "xhigh"})
        self.assertEqual(body["thinking"], {"type": "adaptive"})
        content = [
            {"type": "thinking", "thinking": "Use the tool.", "signature": "opaque"},
            {
                "type": "tool_use",
                "id": "call-2",
                "name": next(iter(reverse)),
                "input": {"argv": ["check"]},
            },
        ]
        parsed = anthropic._parse_anthropic(
            {
                "content": content,
                "usage": {"input_tokens": 7, "output_tokens": 5},
                "stop_reason": "tool_use",
            },
            reverse,
        )
        self.assertEqual(parsed.reasoning, "Use the tool.")
        replay_body, _headers, _reverse = anthropic._request(
            "unused",
            "system",
            tools,
            messages=[
                {"role": "user", "content": "run"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call-2",
                            "function": {
                                "name": "core_terminal_exec",
                                "arguments": {"argv": ["check"]},
                            },
                        }
                    ],
                    "reasoning_replay": parsed.reasoning_replay,
                },
            ],
        )
        self.assertEqual(replay_body["messages"][1]["content"], content)

        with self.assertRaises(CoreError) as caught:
            CompatibleHttpModel(
                api_format="openai",
                model="test",
                reasoning_effort="unbounded",
            )
        self.assertEqual(caught.exception.code, "CONFIG_INVALID")

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
        child = agent.workflow_store.lookup_task(result["task_id"])
        self.assertEqual(child.task_id, result["task_id"])
        self.assertEqual(child.parent_run_id, record.run_id)

    def test_completed_children_release_unused_finalization_reserves(self):
        model = ScriptedModel(
            [ModelResponse(message="child-one"), ModelResponse(message="child-two")]
        )
        agent = make_agent(model, memory="disabled", max_turns=4)
        parent, _raw, _discovered, _effective = agent._new_workflow(
            run_request(memory=False),
            task_id="reserve-release-parent",
            identity="owner",
            session_id="session",
            tenant_id="default",
        )
        contract = {
            "instruction": "Return one short result",
            "tools": [],
            "skills": [],
            "budget": {"turns": 2, "tool_calls": 1},
        }
        try:
            first = agent._delegate(contract, parent.run_id)
            second = agent._delegate(contract, parent.run_id)
        finally:
            agent.close()
        self.assertTrue(first["result"]["complete"])
        self.assertTrue(second["result"]["complete"])
        self.assertEqual(second["result"]["completion_reason"], "completed")

    def test_failed_child_start_returns_its_shared_budget_reservation(self):
        class FlakyScheduler(TaskScheduler):
            def __init__(self):
                super().__init__()
                self.start_attempts = 0

            def start(self, *args, **kwargs):
                self.start_attempts += 1
                if self.start_attempts == 1:
                    raise CoreError("TOOL_START_FAILED", "scheduler unavailable")
                return super().start(*args, **kwargs)

        scheduler = FlakyScheduler()
        model = ScriptedModel([ModelResponse(message="child-after-retry")])
        agent = make_agent(
            model,
            memory="disabled",
            max_turns=3,
            task_scheduler=scheduler,
        )
        parent, _raw, _discovered, _effective = agent._new_workflow(
            run_request(memory=False),
            task_id="failed-child-start-parent",
            identity="owner",
            session_id="session",
            tenant_id="default",
        )
        contract = {
            "instruction": "Return after scheduler recovery",
            "tools": [],
            "skills": [],
            "budget": {"turns": 2, "tool_calls": 1},
        }
        try:
            with self.assertRaises(CoreError) as caught:
                agent._delegate(contract, parent.run_id)
            self.assertEqual(caught.exception.code, "TOOL_START_FAILED")
            self.assertEqual(scheduler.list(owner_id=parent.run_id), ())
            self.assertEqual(tuple(agent.workflow_store._records), (parent.run_id,))
            recovered = agent._delegate(contract, parent.run_id)
        finally:
            agent.close()
        self.assertEqual(recovered["state"], "completed")
        self.assertTrue(recovered["result"]["complete"])

    def test_invalid_platform_turn_limit_leaves_no_workflow_or_budget_ledger(self):
        agent = make_agent(ScriptedModel([]), memory="disabled")
        agent.platform_config = PlatformConfig(
            **{**agent.platform_config.__dict__, "max_model_turns": 0}
        )
        try:
            with self.assertRaises(CoreError) as caught:
                agent.run(run_request(memory=False), task_id="invalid-zero-budget")
            self.assertEqual(caught.exception.code, "CONFIG_INVALID")
            self.assertEqual(agent.workflow_store._records, {})
            self.assertEqual(agent.workflow_store._budgets, {})
        finally:
            agent.close()

    def test_budget_exhaustion_uses_reserved_toolless_turn_and_completes_partial(self):
        model = ScriptedModel(
            [
                ModelResponse(continue_reasoning=True),
                ModelResponse(
                    message=(
                        "Verified: inspected the available input. "
                        "Unfinished: the remaining checks were not run because the "
                        "model-turn budget was exhausted."
                    )
                ),
            ]
        )
        agent = make_agent(model, memory="disabled", max_turns=2)
        try:
            result = agent.run(run_request(memory=False), task_id="partial-budget")
            record = agent.workflow_store.lookup_task("partial-budget")
        finally:
            agent.close()
        self.assertEqual(record.state, "COMPLETED")
        self.assertIsNone(record.error_code)
        self.assertEqual(result.terminal_state, "completed")
        self.assertFalse(result.complete)
        self.assertEqual(result.completion_reason, "budget_exhausted")
        self.assertEqual(result.exhausted_dimension, "model_turns")
        self.assertEqual(result.usage.model_turns, 2)
        self.assertEqual(len(model.calls), 2)
        self.assertNotEqual(model.calls[0].tools, frozenset())
        self.assertEqual(model.calls[1].tools, frozenset())
        self.assertIn("verified intermediate result", model.calls[1].instructions)
        self.assertIn("Unfinished", result.message)
        self.assertEqual(
            result.shared_budget,
            {
                "scope": "root",
                "used": {"model_turns": 2, "tool_calls": 0},
                "limits": {"model_turns": 2, "tool_calls": 10},
            },
        )

    def test_retry_attempts_are_charged_as_local_and_shared_model_usage(self):
        class RetryOnceModel:
            def __init__(self):
                self.calls = 0

            def generate(self, *, context, tools, instructions, messages=None):
                self.calls += 1
                if self.calls == 1:
                    raise CoreError(
                        "MODEL_UNAVAILABLE",
                        "retryable outage",
                        retryable=True,
                    )
                return ModelResponse(message="done after retry")

        model = RetryOnceModel()
        agent = make_agent(
            model,
            memory="disabled",
            max_turns=3,
            model_retries=1,
        )
        try:
            with patch("core_agent.runtime.time.sleep"):
                result = agent.run(
                    run_request(memory=False), task_id="charged-model-retry"
                )
        finally:
            agent.close()
        self.assertTrue(result.complete)
        self.assertEqual(model.calls, 2)
        self.assertEqual(result.usage.model_turns, 2)
        self.assertEqual(
            result.shared_budget["used"],
            {"model_turns": 2, "tool_calls": 0},
        )

    def test_crash_after_normal_attempt_checkpoint_preserves_charge_and_finalizes(self):
        from core_agent.workflow import InMemoryWorkflowStore

        class CrashAfterAttemptStore(InMemoryWorkflowStore):
            def __init__(self):
                super().__init__()
                self.crashed = False

            def transition(self, *args, **kwargs):
                record = super().transition(*args, **kwargs)
                if kwargs.get("event_kind") == "model.attempt.started" and not self.crashed:
                    self.crashed = True
                    raise KeyboardInterrupt("simulated process crash")
                return record

        store = CrashAfterAttemptStore()
        model = ScriptedModel(
            [ModelResponse(message="Verified state only. Unfinished work remains.")]
        )
        agent = make_agent(
            model,
            memory="disabled",
            max_turns=2,
            workflow_store=store,
        )
        try:
            with self.assertRaises(KeyboardInterrupt):
                agent.run(run_request(memory=False), task_id="crash-normal-attempt")
            persisted = store.lookup_task("crash-normal-attempt")
            self.assertEqual(persisted.snapshot["turns"], 1)
            self.assertEqual(
                store._budgets[persisted.snapshot["budget_root_id"]][2], 2
            )

            result = agent.resume_task("crash-normal-attempt")
        finally:
            agent.close()
        self.assertFalse(result.complete)
        self.assertEqual(result.usage.model_turns, 2)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(model.calls[0].tools, frozenset())

    def test_crash_after_finalizer_checkpoint_uses_fallback_without_redispatch(self):
        from core_agent.workflow import InMemoryWorkflowStore

        class CrashAfterFinalizerStore(InMemoryWorkflowStore):
            def __init__(self):
                super().__init__()
                self.crashed = False

            def transition(self, *args, **kwargs):
                record = super().transition(*args, **kwargs)
                if (
                    kwargs.get("event_kind") == "budget.finalization.started"
                    and not self.crashed
                ):
                    self.crashed = True
                    raise KeyboardInterrupt("simulated process crash")
                return record

        store = CrashAfterFinalizerStore()
        model = ScriptedModel([ModelResponse(message="must not be called")])
        agent = make_agent(
            model,
            memory="disabled",
            max_turns=1,
            workflow_store=store,
        )
        try:
            with self.assertRaises(KeyboardInterrupt):
                agent.run(run_request(memory=False), task_id="crash-finalizer-attempt")
            persisted = store.lookup_task("crash-finalizer-attempt")
            self.assertEqual(persisted.snapshot["turns"], 1)
            self.assertFalse(persisted.snapshot["finalization_turn_reserved"])

            result = agent.resume_task("crash-finalizer-attempt")
        finally:
            agent.close()
        self.assertFalse(result.complete)
        self.assertEqual(result.usage.model_turns, 1)
        self.assertEqual(len(model.calls), 0)
        self.assertIn("no verified final summary", result.message)

    def test_working_retry_cannot_consume_the_reserved_finalizer_turn(self):
        class RetryThenFinalizeModel:
            def __init__(self):
                self.calls = []

            def generate(self, *, context, tools, instructions, messages=None):
                self.calls.append(frozenset(tools))
                if len(self.calls) == 1:
                    raise CoreError(
                        "MODEL_UNAVAILABLE",
                        "retryable outage",
                        retryable=True,
                    )
                return ModelResponse(
                    message="Verified no work. Unfinished work remains."
                )

        model = RetryThenFinalizeModel()
        agent = make_agent(
            model,
            memory="disabled",
            max_turns=2,
            model_retries=1,
        )
        try:
            with patch("core_agent.runtime.time.sleep"):
                result = agent.run(
                    run_request(memory=False), task_id="retry-preserves-finalizer"
                )
        finally:
            agent.close()
        self.assertFalse(result.complete)
        self.assertEqual(result.completion_reason, "budget_exhausted")
        self.assertEqual(model.calls[1], frozenset())
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(result.usage.model_turns, 2)
        self.assertEqual(
            result.shared_budget["used"],
            {"model_turns": 2, "tool_calls": 0},
        )

    def test_finalizer_retry_is_charged_when_model_capacity_remains(self):
        class RetryFinalizerModel:
            def __init__(self):
                self.calls = []

            def generate(self, *, context, tools, instructions, messages=None):
                self.calls.append(frozenset(tools))
                if len(self.calls) == 1:
                    return ModelResponse(
                        tool_requests=(
                            ToolRequest(
                                "over-budget-tool",
                                "core_terminal_exec",
                                {"argv": ["unused"]},
                            ),
                        )
                    )
                if len(self.calls) == 2:
                    raise CoreError(
                        "MODEL_UNAVAILABLE",
                        "retryable finalizer outage",
                        retryable=True,
                    )
                return ModelResponse(
                    message="Verified no tool ran. Unfinished tool work remains."
                )

        model = RetryFinalizerModel()
        agent = make_agent(
            model,
            memory="disabled",
            max_turns=3,
            max_tools=0,
            model_retries=1,
        )
        try:
            with patch("core_agent.runtime.time.sleep"):
                result = agent.run(
                    run_request(memory=False), task_id="charged-finalizer-retry"
                )
        finally:
            agent.close()
        self.assertFalse(result.complete)
        self.assertEqual(result.exhausted_dimension, "tool_calls")
        self.assertEqual(model.calls[1:], [frozenset(), frozenset()])
        self.assertEqual(result.usage.model_turns, 3)
        self.assertEqual(
            result.shared_budget["used"],
            {"model_turns": 3, "tool_calls": 0},
        )

    def test_finalizer_retry_stops_at_the_reserved_hard_limit(self):
        class RetryableFinalizerOutage:
            def __init__(self):
                self.calls = 0

            def generate(self, *, context, tools, instructions, messages=None):
                self.calls += 1
                raise CoreError(
                    "MODEL_UNAVAILABLE",
                    "retryable finalizer outage",
                    retryable=True,
                )

        model = RetryableFinalizerOutage()
        agent = make_agent(
            model,
            memory="disabled",
            max_turns=1,
            model_retries=3,
        )
        try:
            with patch("core_agent.runtime.time.sleep"):
                result = agent.run(
                    run_request(memory=False), task_id="bounded-finalizer-retry"
                )
        finally:
            agent.close()
        self.assertFalse(result.complete)
        self.assertEqual(model.calls, 1)
        self.assertEqual(result.usage.model_turns, 1)
        self.assertEqual(
            result.shared_budget["used"],
            {"model_turns": 1, "tool_calls": 0},
        )
        self.assertIn("no verified final summary", result.message)

    def test_shared_budget_reports_child_usage_without_redefining_local_usage(self):
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_requests=(
                        ToolRequest(
                            "delegate-partial",
                            "core_delegate",
                            {
                                "instruction": "Inspect the child input",
                                "tools": [],
                                "skills": [],
                                "budget": {"turns": 2, "tool_calls": 1},
                            },
                        ),
                    )
                ),
                ModelResponse(continue_reasoning=True),
                ModelResponse(
                    message="Verified child input. Unfinished child checks remain."
                ),
                ModelResponse(
                    message="Verified child result. Unfinished parent checks remain."
                ),
            ]
        )
        agent = make_agent(model, memory="disabled", max_turns=4, max_tools=2)
        agent.tool_runtime.registry.register(delegate_definition())
        try:
            result = agent.run(
                run_request(memory=False), task_id="shared-budget-parent"
            )
            parent = agent.workflow_store.lookup_task("shared-budget-parent")
            child_task = agent.task_scheduler.list(owner_id=parent.run_id)[0]
            child = agent.workflow_store.lookup_task(child_task.id)
            resumed = agent.resume_task(child_task.id)
        finally:
            agent.close()
        shared = {
            "scope": "root",
            "used": {"model_turns": 4, "tool_calls": 1},
            "limits": {"model_turns": 4, "tool_calls": 2},
        }
        self.assertEqual(result.usage.model_turns, 2)
        self.assertEqual(result.shared_budget, shared)
        self.assertEqual(parent.result["shared_budget"], shared)
        self.assertEqual(child.result["shared_budget"], shared)
        self.assertEqual(child_task.result.shared_budget, shared)
        self.assertEqual(resumed.shared_budget, shared)

    def test_exhausted_tool_budget_returns_every_queued_result_without_dispatch(self):
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_requests=(
                        ToolRequest("call-1", "core_terminal_exec", {"argv": ["one"]}),
                        ToolRequest("call-2", "core_terminal_exec", {"argv": ["two"]}),
                        ToolRequest(
                            "call-3", "core_terminal_exec", {"argv": ["three"]}
                        ),
                    )
                ),
                ModelResponse(
                    message=(
                        "Verified: the first command completed. Unfinished: the second "
                        "and third commands were not run because the tool budget ended."
                    )
                ),
            ]
        )
        agent = make_agent(model, memory="disabled", max_turns=3, max_tools=1)
        try:
            result = agent.run(run_request(memory=False), task_id="tool-budget")
        finally:
            agent.close()
        self.assertEqual(agent.tool_runtime.execution_count, 1)
        self.assertFalse(result.complete)
        self.assertEqual(result.exhausted_dimension, "tool_calls")
        self.assertEqual(result.usage.tool_calls, 1)
        self.assertEqual(model.calls[1].tools, frozenset())
        tool_messages = [
            item for item in model.calls[1].messages if item.get("role") == "tool"
        ]
        self.assertEqual(
            [item["tool_call_id"] for item in tool_messages],
            ["call-1", "call-2", "call-3"],
        )
        for item in tool_messages[1:]:
            payload = json.loads(item["content"])
            self.assertEqual(payload["error_code"], "BUDGET_EXCEEDED")
            self.assertEqual(
                payload["output"]["error"]["details"]["dimension"],
                "tool_calls",
            )
            self.assertIn(
                "intermediate result",
                payload["output"]["error"]["details"]["instruction"],
            )

    def test_budget_finalizer_without_text_uses_non_fabricated_fallback(self):
        model = ScriptedModel(
            [
                ModelResponse(continue_reasoning=True),
                ModelResponse(continue_reasoning=True, reasoning="private plan"),
            ]
        )
        agent = make_agent(model, memory="disabled", max_turns=2)
        try:
            result = agent.run(run_request(memory=False))
        finally:
            agent.close()
        self.assertFalse(result.complete)
        self.assertIn("Budget exhausted", result.message)
        self.assertIn("no verified final summary", result.message)
        self.assertNotIn("private plan", result.message)

    def test_budget_finalizer_model_outage_uses_non_fabricated_fallback(self):
        class FinalizerOutageModel:
            def __init__(self):
                self.calls = 0

            def generate(self, *, context, tools, instructions, messages=None):
                self.calls += 1
                if self.calls == 1:
                    return ModelResponse(continue_reasoning=True)
                raise CoreError("MODEL_UNAVAILABLE", "provider is unavailable")

        model = FinalizerOutageModel()
        agent = make_agent(model, memory="disabled", max_turns=2)
        try:
            result = agent.run(
                run_request(memory=False), task_id="partial-finalizer-outage"
            )
            record = agent.workflow_store.lookup_task("partial-finalizer-outage")
        finally:
            agent.close()
        self.assertEqual(record.state, "COMPLETED")
        self.assertFalse(result.complete)
        self.assertEqual(result.completion_reason, "budget_exhausted")
        self.assertIn("no verified final summary", result.message)
        self.assertNotIn("provider is unavailable", result.message)

    def test_followup_during_budget_finalizer_does_not_add_a_model_turn(self):
        started = threading.Event()
        release = threading.Event()

        class BlockingFinalizerModel:
            def __init__(self):
                self.calls = 0

            def generate(self, *, context, tools, instructions, messages=None):
                self.calls += 1
                started.set()
                release.wait(2)
                return ModelResponse(message="stale finalizer text")

        model = BlockingFinalizerModel()
        agent = make_agent(model, memory="disabled", max_turns=1)
        results = []
        failures = []

        def run():
            try:
                results.append(
                    agent.run(
                        run_request(memory=False),
                        task_id="budget-finalizer-followup",
                        identity="owner",
                        session_id="session",
                        tenant_id="default",
                    )
                )
            except Exception as error:
                failures.append(error)

        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(started.wait(1))
        agent.enqueue_message(
            RunRequest.from_dict({"prompt": "late correction"}),
            task_id="budget-finalizer-followup",
            message_id="late-message",
            identity="owner",
            session_id="session",
            tenant_id="default",
        )
        release.set()
        worker.join(2)
        try:
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(model.calls, 1)
            self.assertEqual(results[0].usage.model_turns, 1)
            self.assertFalse(results[0].complete)
            self.assertIn("recorded but could not be processed", results[0].message)
            record = agent.workflow_store.lookup_task("budget-finalizer-followup")
            context = agent._context_from_dict(record.snapshot["context"])
            self.assertIn(
                "late correction", [item.content for item in context.transcript]
            )
        finally:
            agent.close()

    def test_joined_partial_child_is_consistent_in_workflow_scheduler_and_mailbox(self):
        model = ScriptedModel(
            [
                ModelResponse(continue_reasoning=True),
                ModelResponse(
                    message=(
                        "Verified: inspected the child input. Unfinished: further "
                        "checks were not run because the child budget ended."
                    )
                ),
            ]
        )
        agent = make_agent(model, memory="disabled")
        parent, _raw, _discovered, _effective = agent._new_workflow(
            run_request(memory=False),
            task_id="partial-child-parent",
            identity="owner",
            session_id="session",
            tenant_id="default",
        )
        try:
            joined = agent._delegate(
                {
                    "instruction": "Inspect what is available and report",
                    "tools": [],
                    "skills": [],
                    "budget": {"turns": 2, "tool_calls": 1},
                },
                parent.run_id,
            )
            child_task = agent.task_scheduler.get(joined["task_id"])
            child_record = agent.workflow_store.lookup_task(joined["task_id"])
            resumed = agent.resume_task(joined["task_id"])
            notifications = agent.task_scheduler.mailbox(parent.run_id).poll()
            lease = agent.workflow_store.acquire_lease(
                parent.run_id,
                tenant_id=parent.tenant_id,
                owner_id=parent.owner_id,
                worker_id=agent._worker_id,
                ttl=600,
            )
            try:
                _parent, consumed = agent._consume_task_notifications(
                    parent,
                    copy.deepcopy(parent.snapshot),
                    lease_token=lease,
                )
            finally:
                agent.workflow_store.release_lease(
                    parent.run_id,
                    tenant_id=parent.tenant_id,
                    worker_id=agent._worker_id,
                    token=lease,
                )
        finally:
            agent.close()
        expected = child_task.result.to_dict()
        self.assertEqual(joined["state"], "completed")
        self.assertEqual(joined["result"], expected)
        self.assertEqual(resumed.to_dict(), expected)
        self.assertEqual(notifications[-1].payload["result"].to_dict(), expected)
        self.assertEqual(child_record.state, "COMPLETED")
        self.assertEqual(child_record.result["message"], expected["message"])
        self.assertFalse(child_record.result["complete"])
        self.assertEqual(child_record.result["completion_reason"], "budget_exhausted")
        notification_item = next(
            item
            for item in agent._context_from_dict(consumed["context"]).active
            if item.kind == "task_notification"
        )
        notification_result = json.loads(notification_item.content)[
            "task_notification"
        ]["payload"]["result"]
        self.assertEqual(notification_result, expected)

    def test_budget_partial_cancels_an_active_background_child_before_completion(self):
        child_started = threading.Event()

        class BackgroundChildModel:
            def generate(self, *, context, tools, instructions, messages=None):
                if "Slow child" in context:
                    child_started.set()
                    time.sleep(0.08)
                    return ModelResponse(message="late child result")
                if tools:
                    return ModelResponse(
                        tool_requests=(
                            ToolRequest(
                                "background-child",
                                "core_delegate",
                                {
                                    "instruction": "Slow child",
                                    "tools": [],
                                    "skills": [],
                                    "budget": {"turns": 2, "tool_calls": 1},
                                    "background": True,
                                },
                            ),
                        )
                    )
                return ModelResponse(
                    message=(
                        "Verified: the child was started. Unfinished: its work was "
                        "canceled when the shared model budget ended."
                    )
                )

        model = BackgroundChildModel()
        agent = make_agent(model, memory="disabled", max_turns=4, max_tools=2)
        agent.tool_runtime.registry.register(delegate_definition())
        delegate = agent._delegate

        def start_after_child_reserved(arguments, run_id):
            result = delegate(arguments, run_id)
            self.assertTrue(child_started.wait(1))
            return result

        agent.tool_runtime.handlers["core_delegate"] = start_after_child_reserved
        try:
            result = agent.run(
                run_request(memory=False), task_id="partial-with-background-child"
            )
            parent = agent.workflow_store.lookup_task("partial-with-background-child")
            children = agent.task_scheduler.list(owner_id=parent.run_id)
            self.assertTrue(child_started.is_set())
            self.assertEqual(len(children), 1)
            child = agent.task_scheduler.wait(children[0].id, timeout=1)
            child_record = agent.workflow_store.lookup_task(child.id)
        finally:
            agent.close()
        self.assertFalse(result.complete)
        self.assertEqual(parent.state, "COMPLETED")
        self.assertEqual(child.state, "canceled")
        self.assertEqual(child_record.state, "CANCELLED")

    def test_budget_partial_does_not_wait_forever_for_noncooperative_task(self):
        gate = threading.Event()
        started = threading.Event()
        model = ScriptedModel(
            [ModelResponse(message="Verified input. Unfinished checks remain.")]
        )
        agent = make_agent(model, memory="disabled", max_turns=1)
        agent.budget_cancel_grace_seconds = 0.02
        parent, _raw, _discovered, _effective = agent._new_workflow(
            run_request(memory=False),
            task_id="noncooperative-budget-parent",
            identity="owner",
            session_id="session",
            tenant_id="default",
        )

        def ignore_cancel():
            started.set()
            gate.wait(2)
            return "late result"

        task = agent.task_scheduler.start(
            ignore_cancel,
            owner_id=parent.run_id,
            required=True,
            kind="test",
            contract={},
        )
        self.assertTrue(started.wait(1))
        results = []
        failures = []

        def finish_parent():
            try:
                results.append(agent._continue_workflow(parent))
            except Exception as error:
                failures.append(error)

        worker = threading.Thread(target=finish_parent)
        worker.start()
        worker.join(0.3)
        returned_with_task_active = not worker.is_alive()
        active_state = agent.task_scheduler.get(task.id).state
        try:
            self.assertTrue(returned_with_task_active)
            self.assertEqual(failures, [])
            self.assertEqual(active_state, "working")
            self.assertEqual(results[0].pending_tasks, (task.id,))
            self.assertIn("cancellation is not confirmed", results[0].message)
        finally:
            gate.set()
            worker.join(1)
            late = agent.task_scheduler.wait(task.id, timeout=1)
            notifications = agent.task_scheduler.mailbox(parent.run_id).poll()
            agent.close()
        self.assertEqual(late.state, "canceled")
        self.assertEqual(late.result, "late result")
        self.assertEqual(notifications[-1].payload["result"], "late result")

    def test_child_reservation_failure_is_a_tool_result_for_parent_finalization(self):
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_requests=(
                        ToolRequest(
                            "delegate-without-budget",
                            "core_delegate",
                            {
                                "instruction": "Do child work",
                                "tools": [],
                                "skills": [],
                                "budget": {"turns": 1, "tool_calls": 1},
                            },
                        ),
                    )
                ),
                ModelResponse(
                    message=(
                        "Verified: no child work started. Unfinished: the delegated "
                        "objective was not attempted because the shared model budget "
                        "had no child finalization reserve."
                    )
                ),
            ]
        )
        agent = make_agent(model, memory="disabled", max_turns=2)
        agent.tool_runtime.registry.register(delegate_definition())
        try:
            result = agent.run(run_request(memory=False), task_id="reserve-parent")
            parent = agent.workflow_store.lookup_task("reserve-parent")
            children = agent.task_scheduler.list(owner_id=parent.run_id)
        finally:
            agent.close()
        self.assertEqual(children, ())
        self.assertFalse(result.complete)
        self.assertEqual(result.exhausted_dimension, "model_turns")
        self.assertEqual(model.calls[1].tools, frozenset())
        tool_result = next(
            item
            for item in model.calls[1].messages
            if item.get("tool_call_id") == "delegate-without-budget"
        )
        payload = json.loads(tool_result["content"])
        self.assertEqual(payload["error_code"], "BUDGET_EXCEEDED")
        self.assertIn("intermediate result", json.dumps(payload))

    def test_lease_heartbeat_keeps_a_long_model_call_owned(self):
        from core_agent.workflow import InMemoryWorkflowStore

        class TrackingStore(InMemoryWorkflowStore):
            def __init__(self):
                super().__init__()
                self.renewals = 0

            def renew_lease(self, *args, **kwargs):
                self.renewals += 1
                return super().renew_lease(*args, **kwargs)

        class SlowModel:
            def generate(self, *, context, tools, instructions, messages=None):
                time.sleep(0.08)
                return ModelResponse(message="done")

        store = TrackingStore()
        with (
            patch("core_agent.runtime.WORKFLOW_LEASE_TTL", 0.04),
            patch("core_agent.runtime.WORKFLOW_LEASE_HEARTBEAT_INTERVAL", 0.01),
        ):
            agent = make_agent(SlowModel(), memory="disabled", workflow_store=store)
            try:
                result = agent.run(run_request(memory=False))
            finally:
                agent.close()
        self.assertEqual(result.message, "done")
        self.assertGreaterEqual(store.renewals, 2)

    def test_cancelled_child_does_not_dispatch_its_pending_tool_call(self):
        started = threading.Event()
        release = threading.Event()

        class BlockingChildModel:
            def generate(self, *, context, tools, instructions, messages=None):
                started.set()
                release.wait(2)
                return ModelResponse(
                    tool_requests=(
                        ToolRequest(
                            "must-not-run", "core_terminal_exec", {"argv": ["late"]}
                        ),
                    )
                )

        agent = make_agent(BlockingChildModel(), memory="disabled")
        record, _raw, _discovered, _effective = agent._new_workflow(
            run_request(memory=False),
            task_id="cancel-parent",
            identity="owner",
            session_id="session",
            tenant_id="default",
        )
        delegated = agent._delegate(
            {
                "instruction": "Wait, then run one command",
                "tools": ["core_terminal_exec"],
                "skills": [],
                "budget": {"turns": 3, "tool_calls": 1},
                "background": True,
            },
            record.run_id,
        )
        self.assertTrue(started.wait(1))
        agent.cancel_task("cancel-parent")
        release.set()
        child_task = agent.task_scheduler.wait(delegated["task_id"], timeout=1)
        child_record = agent.workflow_store.lookup_task(delegated["task_id"])
        try:
            self.assertEqual(child_task.state, "canceled")
            self.assertEqual(child_record.state, "CANCELLED")
            self.assertEqual(agent.tool_runtime.execution_count, 0)
        finally:
            agent.close()

    def test_canceling_child_by_its_id_converges_scheduler_and_workflow(self):
        started = threading.Event()
        release = threading.Event()

        class BlockingChildModel:
            def generate(self, *, context, tools, instructions, messages=None):
                started.set()
                release.wait(2)
                return ModelResponse(message="discarded after cancellation")

        agent = make_agent(BlockingChildModel(), memory="disabled")
        parent, _raw, _discovered, _effective = agent._new_workflow(
            run_request(memory=False),
            task_id="direct-child-cancel-parent",
            identity="owner",
            session_id="session",
            tenant_id="default",
        )
        delegated = agent._delegate(
            {
                "instruction": "Wait for cancellation",
                "tools": [],
                "skills": [],
                "budget": {"turns": 2, "tool_calls": 1},
                "background": True,
            },
            parent.run_id,
        )
        self.assertTrue(started.wait(1))
        agent.cancel_task(delegated["task_id"])
        ledger = agent.workflow_store._budgets[parent.snapshot["budget_root_id"]]
        self.assertEqual(ledger[2], 2)
        release.set()
        task = agent.task_scheduler.wait(delegated["task_id"], timeout=1)
        child = agent.workflow_store.lookup_task(delegated["task_id"])
        try:
            self.assertEqual(task.state, "canceled")
            self.assertEqual(child.state, "CANCELLED")
        finally:
            agent.close()

    def test_cancel_does_not_release_a_finalizer_that_already_started(self):
        started = threading.Event()
        release = threading.Event()

        class BlockingFinalizerModel:
            def generate(self, *, context, tools, instructions, messages=None):
                started.set()
                release.wait(2)
                return ModelResponse(message="discarded after cancellation")

        agent = make_agent(BlockingFinalizerModel(), memory="disabled")
        parent, _raw, _discovered, _effective = agent._new_workflow(
            run_request(memory=False),
            task_id="started-finalizer-parent",
            identity="owner",
            session_id="session",
            tenant_id="default",
        )
        delegated = agent._delegate(
            {
                "instruction": "Use only the finalization turn",
                "tools": [],
                "skills": [],
                "budget": {"turns": 1, "tool_calls": 1},
                "background": True,
            },
            parent.run_id,
        )
        self.assertTrue(started.wait(1))
        child = agent.workflow_store.lookup_task(delegated["task_id"])
        self.assertFalse(child.snapshot["finalization_turn_reserved"])
        agent.cancel_task(delegated["task_id"])
        ledger = agent.workflow_store._budgets[parent.snapshot["budget_root_id"]]
        self.assertEqual(ledger[2], 2)
        release.set()
        try:
            terminal = agent.task_scheduler.wait(delegated["task_id"], timeout=1)
            self.assertEqual(terminal.state, "canceled")
        finally:
            agent.close()

    def test_root_cancel_recursively_cancels_child_and_grandchild_workflows(self):
        grandchild_started = threading.Event()
        release = threading.Event()

        class NestedBlockingModel:
            def generate(self, *, context, tools, instructions, messages=None):
                messages = tuple(messages or ())
                if "core_delegate" in tools and not any(
                    item.get("role") == "tool" for item in messages
                ):
                    return ModelResponse(
                        tool_requests=(
                            ToolRequest(
                                "start-grandchild",
                                "core_delegate",
                                {
                                    "instruction": "Wait for release",
                                    "tools": [],
                                    "skills": [],
                                    "budget": {"turns": 2, "tool_calls": 1},
                                    "background": True,
                                },
                            ),
                        )
                    )
                grandchild_started.set()
                release.wait(2)
                return ModelResponse(message="must be discarded after cancel")

        agent = make_agent(NestedBlockingModel(), memory="disabled")
        agent.tool_runtime.registry.register(delegate_definition())
        parent, _raw, _discovered, _effective = agent._new_workflow(
            run_request(memory=False),
            task_id="recursive-cancel-parent",
            identity="owner",
            session_id="session",
            tenant_id="default",
        )
        child = agent._delegate(
            {
                "instruction": "Delegate once, then wait",
                "tools": ["core_delegate"],
                "skills": [],
                "budget": {"turns": 4, "tool_calls": 2},
                "background": True,
            },
            parent.run_id,
        )
        self.assertTrue(grandchild_started.wait(1))
        child_record = agent.workflow_store.lookup_task(child["task_id"])
        grandchildren = agent.task_scheduler.list(owner_id=child_record.run_id)
        self.assertEqual(len(grandchildren), 1)
        grandchild_id = grandchildren[0].id
        agent.cancel_task("recursive-cancel-parent")
        release.set()
        child_task = agent.task_scheduler.wait(child["task_id"], timeout=1)
        grandchild_task = agent.task_scheduler.wait(grandchild_id, timeout=1)
        try:
            self.assertEqual(child_task.state, "canceled")
            self.assertEqual(grandchild_task.state, "canceled")
            self.assertEqual(
                agent.workflow_store.lookup_task(child["task_id"]).state,
                "CANCELLED",
            )
            self.assertEqual(
                agent.workflow_store.lookup_task(grandchild_id).state,
                "CANCELLED",
            )
            self.assertEqual(agent.tool_runtime.execution_count, 0)
        finally:
            agent.close()

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

    def test_disabled_mcp_feature_never_connects_or_discovers_a_server(self):
        connector = InMemoryMcpConnector(
            catalogs={"docs": {"search": {"type": "object"}}},
            results={"docs.search": {"results": [{"text": "secret document"}]}},
        )
        model = ScriptedModel([ModelResponse(message="done")])
        agent = make_agent(model, mcp=False, connector=connector)
        result = agent.run(run_request())
        self.assertEqual(result.message, "done")
        self.assertEqual(connector.connections, ())
        self.assertNotIn("docs_search", model.calls[0].tools)
        agent.close()

    def test_content_logging_survives_a_tool_that_returns_a_bare_output(self):
        """An MCP tool returns its output, not a ToolResult; both must log."""
        connector = InMemoryMcpConnector(
            catalogs={"docs": {"search": {"type": "object"}}},
            results={"docs.search": {"results": [{"text": "leaderboard"}]}},
        )
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_requests=(
                        ToolRequest("call-1", "docs_search", {"query": "x"}),
                    ),
                    finish_reason="tool_calls",
                ),
                ModelResponse(message="done", finish_reason="stop"),
            ]
        )
        agent = make_agent(model, connector=connector, log_content=True)
        try:
            with self.assertLogs("core_agent.runtime", level="INFO") as captured:
                result = agent.run(run_request())
        finally:
            agent.close()
        self.assertEqual(result.message, "done")
        completed = [
            json.loads(record.getMessage())
            for record in captured.records
            if '"tool.completed"' in record.getMessage()
        ]
        self.assertEqual(completed[0]["output"], {"results": [{"text": "leaderboard"}]})

    def test_a_tool_reporting_its_own_failure_is_not_a_successful_result(self):
        """MCP answers a failed tool with 200 and isError, not with a transport error."""
        connector = InMemoryMcpConnector(
            catalogs={"docs": {"search": {"type": "object"}}},
            results={
                "docs.search": {
                    "isError": True,
                    "content": [{"type": "text", "text": "upstream quota exceeded"}],
                }
            },
        )
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_requests=(
                        ToolRequest("call-1", "docs_search", {"query": "x"}),
                    ),
                    finish_reason="tool_calls",
                ),
                ModelResponse(message="done", finish_reason="stop"),
            ]
        )
        agent = make_agent(model, connector=connector, log_content=True)
        try:
            with self.assertLogs("core_agent.runtime", level="INFO") as captured:
                result = agent.run(run_request())
        finally:
            agent.close()
        # The run continues: a failed tool is the model's problem to solve.
        self.assertEqual(result.message, "done")
        failed = [
            json.loads(record.getMessage())
            for record in captured.records
            if '"tool.failed"' in record.getMessage()
        ]
        self.assertEqual(failed[0]["error_code"], "TOOL_EXECUTION_FAILED")
        # The server's own text reaches the model instead of being dressed as data.
        self.assertIn("quota exceeded", json.dumps(failed[0]["output"]))

    def test_delegate_schema_names_and_describes_actual_capabilities(self):
        """A free string invites an identifier the parent cannot honour."""
        connector = InMemoryMcpConnector(
            catalogs={
                "docs": {"search": {"type": "object"}, "read": {"type": "object"}}
            }
        )
        model = ScriptedModel([ModelResponse(message="done")])
        agent = make_agent(model, connector=connector)
        agent.tool_runtime.registry.register(delegate_definition())
        registered = agent.tool_runtime.registry.get("core_delegate").input_schema
        try:
            effective = agent._resolve_capabilities(run_request())[2]
            catalog = agent._tool_catalog(
                effective, {"docs": connector.catalogs["docs"]}
            )
        finally:
            agent.close()
        schema = catalog["core_delegate"]["input_schema"]
        # One list, holding both kinds of tool under the names the catalogue
        # uses — there is nothing left for the model to sort by hand.
        self.assertEqual(
            schema["properties"]["tools"]["items"]["enum"],
            sorted(effective.model_tool_catalog),
        )
        self.assertIn(
            "core_terminal_exec", schema["properties"]["tools"]["items"]["enum"]
        )
        self.assertIn("docs_search", schema["properties"]["tools"]["items"]["enum"])
        self.assertEqual(schema["properties"]["skills"]["maxItems"], 0)
        self.assertNotIn("enum", schema["properties"]["skills"]["items"])
        self.assertNotIn("mcp", schema["properties"])
        for field in ("instruction", "tools", "skills", "budget", "background"):
            self.assertTrue(schema["properties"][field]["description"])
        for field in ("turns", "tool_calls"):
            self.assertTrue(
                schema["properties"]["budget"]["properties"][field]["description"]
            )
        self.assertEqual(
            schema["properties"]["budget"]["required"],
            ["turns", "tool_calls"],
        )
        # The registered schema is untouched: the enum belongs to this run.
        self.assertEqual(registered["properties"]["tools"]["items"], {"type": "string"})

    def test_a_dotted_mcp_name_resolves_to_its_own_server(self):
        """Splitting the canonical name on a dot would name the wrong server."""
        from core_agent.mcp import mcp_tool_index, mcp_tool_name

        self.assertEqual(mcp_tool_name("docs.eu", "search.v1"), "docs_eu_search_v1")
        index = mcp_tool_index({"docs.eu": frozenset({"search.v1"})})
        self.assertEqual(index["docs_eu_search_v1"], ("docs.eu", "search.v1"))
        # Two different pairs that would share a name are a collision, not a
        # silent overwrite that sends the call to the wrong server.
        with self.assertRaises(CoreError) as caught:
            mcp_tool_index({"a.b": frozenset({"c"}), "a": frozenset({"b_c"})})
        self.assertEqual(caught.exception.code, "TOOL_NAME_COLLISION")

    def test_mcp_server_is_filtered_to_the_tools_the_agent_allows(self):
        connector = InMemoryMcpConnector(
            catalogs={
                "docs": {
                    "search": {"type": "object"},
                    "read": {"type": "object"},
                    "delete": {"type": "object"},
                }
            }
        )
        model = ScriptedModel([ModelResponse(message="done")])
        agent = make_agent(model, connector=connector)
        agent.run(run_request())
        self.assertEqual(connector.connections, ("docs",))
        self.assertIn("docs_search", model.calls[0].tools)
        self.assertIn("docs_read", model.calls[0].tools)
        self.assertNotIn("docs_delete", model.calls[0].tools)
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
            catalogs={"docs": {"search": {"type": "object"}}},
            results={"docs.search": {"blob": "x" * 3_000}},
        )
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_requests=(ToolRequest(f"call-{index}", "docs_search", {}),)
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

    def test_large_tool_result_uses_artifact_excerpt_in_active_context(self):
        blob = "x" * 20_000
        connector = InMemoryMcpConnector(
            catalogs={"docs": {"search": {"type": "object"}}},
            results={"docs.search": {"blob": blob}},
        )
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_requests=(ToolRequest("large-result", "docs_search", {}),)
                ),
                ModelResponse(message="done"),
            ]
        )
        artifacts = InMemoryArtifactStore()
        agent = make_agent(
            model,
            connector=connector,
            context_window=10_000,
            output_reserve=500,
            artifact_store=artifacts,
        )
        try:
            result = agent.run(run_request(), task_id="large-tool-result")
            record = agent.workflow_store.lookup_task("large-tool-result")
        finally:
            agent.close()
        tool_message = next(
            item for item in model.calls[1].messages if item.get("role") == "tool"
        )
        active_payload = json.loads(tool_message["content"])
        reference = active_payload["output"]["artifact"]
        stored, content = artifacts.get("default", reference["id"])
        context = agent._context_from_dict(record.snapshot["context"])
        transcript_result = next(
            item for item in context.transcript if item.kind == "tool_result"
        )
        self.assertEqual(result.message, "done")
        self.assertTrue(active_payload["output"]["truncated"])
        self.assertLess(len(active_payload["output"]["excerpt"]), len(blob))
        self.assertEqual(reference["digest"], stored.digest)
        self.assertEqual(reference["size"], stored.size)
        self.assertEqual(content.decode(), transcript_result.content)
        self.assertIn(blob, transcript_result.content)

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
                            "call-1", "core_terminal_exec", {"argv": ["check"]}
                        ),
                    ),
                    prompt_tokens=40,
                    completion_tokens=6,
                    reasoning="Use the declared terminal tool.",
                    reasoning_replay={
                        "format": "openai",
                        "fields": {
                            "reasoning_details": [
                                {
                                    "text": "Use the declared terminal tool.",
                                    "signature": "opaque-replay-signature",
                                }
                            ]
                        },
                    },
                    reasoning_tokens=4,
                    total_tokens=46,
                    finish_reason="tool_calls",
                ),
                ModelResponse(message="done", finish_reason="stop"),
            ]
        )
        model.api_key = "provider-secret"
        agent = make_agent(model, memory="disabled", telemetry=telemetry)
        try:
            request = RunRequest.from_dict({"prompt": "Do it with provider-secret"})
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
        self.assertTrue(any("core_terminal_exec" in schema for schema in tool_schemas))
        self.assertEqual(
            first.attributes[
                "llm.output_messages.0.message.tool_calls.0.tool_call.function.name"
            ],
            "core_terminal_exec",
        )
        self.assertEqual(first.attributes["llm.token_count.total"], 46)
        reasoning_prefix = "llm.output_messages.0.message.contents.0.message_content"
        self.assertEqual(first.attributes[f"{reasoning_prefix}.type"], "reasoning")
        self.assertEqual(
            first.attributes[f"{reasoning_prefix}.text"],
            "Use the declared terminal tool.",
        )
        self.assertEqual(
            first.attributes["llm.token_count.completion_details.reasoning"], 4
        )
        self.assertEqual(first.status_code, "OK")
        second = llm_spans[1].attributes
        self.assertNotIn("opaque-replay-signature", str(first.attributes))
        self.assertNotIn("opaque-replay-signature", str(second))
        self.assertEqual(second["llm.input_messages.2.message.role"], "assistant")
        self.assertEqual(second["llm.input_messages.3.message.role"], "tool")
        self.assertEqual(
            model.calls[1].messages[1]["reasoning_replay"]["format"], "openai"
        )
        self.assertEqual(second["llm.input_messages.3.message.tool_call_id"], "call-1")

        tool_span = next(
            span for span in exporter.spans if span.name == "core_agent.tool.execute"
        )
        self.assertEqual(tool_span.attributes["openinference.span.kind"], "TOOL")
        self.assertEqual(tool_span.attributes["tool.name"], "core_terminal_exec")
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
        self.assertIsNone(invalid)
        self.assertIsNone(telemetry.extract({}))
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
            span for span in exporter.spans if span.name == "core_agent.task.execute"
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
                "llm.output_messages.0.message.contents.0.message_content.type": "reasoning",
                "llm.output_messages.0.message.contents.0.message_content.text": "secret reasoning",
                "llm.token_count.completion_details.reasoning": 7,
                "llm.tools.0.tool.json_schema": "secret schema",
                "core_agent.task.state": "working",
            },
        ) as span:
            span.set_attribute("output.value", "secret response")
        attrs = exporter.spans[-1].attributes
        self.assertNotIn("gen_ai.input.messages", attrs)
        self.assertNotIn("gen_ai.tool.call.arguments", attrs)
        self.assertNotIn("llm.input_messages.0.message.content", attrs)
        self.assertNotIn(
            "llm.output_messages.0.message.contents.0.message_content.text", attrs
        )
        self.assertNotIn("llm.tools.0.tool.json_schema", attrs)
        self.assertNotIn("output.value", attrs)
        self.assertEqual(attrs["llm.token_count.completion_details.reasoning"], 7)
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
        base = f"http://127.0.0.1:{server.server_port}"
        telemetry = Telemetry.otlp(
            trace_endpoint=f"{base}/v1/traces",
            metric_endpoint=f"{base}/v1/metrics",
            log_endpoint=f"{base}/v1/logs",
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
                "OTEL_ENDPOINT": "",
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

    def test_memory_subsystem_owns_in_process_spans_on_the_caller_trace(self):
        class FixedEmbeddings:
            version = "fixed-v1"

            def embed(self, text):
                return (1.0, float(len(text)))

        exporter = RecordingExporter()
        telemetry = Telemetry(exporter)
        registry = MemoryRegistry(
            InMemoryMemoryStore(),
            embedding_provider=FixedEmbeddings(),
            telemetry=telemetry,
        )
        service = registry.service("runtime-test", "user-42")
        with telemetry.span("core_agent.tool.execute") as caller_span:
            service.create(
                title="One", body="Alice knows Bob.", namespace="session/context-1"
            )
            service.search("Alice", namespace="session/context-1")
        names = [span.name for span in exporter.spans]
        self.assertIn("core_agent.memory.embed", names)
        self.assertIn("core_agent.memory.index_publish", names)
        self.assertIn("core_agent.memory.search", names)
        self.assertIn("core_agent.memory.search.bm25", names)
        self.assertIn("core_agent.memory.search.vector", names)
        self.assertIn("core_agent.memory.search.graph", names)
        self.assertIn("core_agent.memory.search.rerank", names)
        self.assertEqual(
            [name for name in names if name.startswith("memory_service.")], []
        )
        search_span = next(
            span for span in exporter.spans if span.name == "core_agent.memory.search"
        )
        self.assertEqual(search_span.context.trace_id, caller_span.context.trace_id)
        registry.close()


if __name__ == "__main__":
    unittest.main()
