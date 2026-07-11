from __future__ import annotations

import uuid
from dataclasses import dataclass
import json

from .config import RunRequest, compile_effective_config
from .context import ContextItem, ContextState
from .errors import CoreError
from .tools import ApprovalRequest, ToolCall, ToolResult


@dataclass(frozen=True)
class Usage:
    model_turns: int
    tool_calls: int


@dataclass(frozen=True)
class RunResult:
    run_id: str
    message: str
    terminal_state: str
    usage: Usage

    def to_dict(self):
        return {
            "run_id": self.run_id,
            "message": self.message,
            "terminal_state": self.terminal_state,
            "usage": {
                "model_turns": self.usage.model_turns,
                "tool_calls": self.usage.tool_calls,
            },
        }


class CoreAgent:
    def __init__(
        self,
        *,
        platform_config,
        agent_config,
        model,
        tool_runtime,
        mcp_connector,
        task_scheduler,
        event_store,
        checkpoint_store,
        audit_log,
        telemetry,
        compactor=None,
        token_counter=None,
    ):
        self.platform_config = platform_config
        self.agent_config = agent_config
        self.model = model
        self.tool_runtime = tool_runtime
        self.mcp_connector = mcp_connector
        self.task_scheduler = task_scheduler
        self.event_store = event_store
        self.checkpoint_store = checkpoint_store
        self.audit_log = audit_log
        self.telemetry = telemetry
        self.compactor = compactor
        self.token_counter = token_counter or (lambda text: max(1, len(text) // 4))

    def run(self, request):
        if isinstance(request, dict):
            request = RunRequest.from_dict(request)
        if not isinstance(request, RunRequest):
            raise CoreError("INVALID_REQUEST")
        raw = self.agent_config.to_dict()
        memory_mode = raw["features"].get("memory", "disabled")
        discovered = {}
        if raw["features"].get("mcp") and memory_mode != "disabled":
            for declaration in request.mcp:
                if declaration["name"] in self.platform_config.allowed_mcp_servers:
                    try:
                        discovered[declaration["name"]] = self.mcp_connector.connect(
                            declaration
                        )
                    except CoreError:
                        if declaration.get("required"):
                            raise
        effective = compile_effective_config(
            self.platform_config, self.agent_config, request, discovered
        )
        run_id = str(uuid.uuid4())
        self.audit_log.append(
            run_id,
            "config.snapshot",
            {"digest": effective.digest, "snapshot": effective.audit_snapshot},
        )
        self.audit_log.append(run_id, "task.started", {})
        self.event_store.append(run_id, "task.started", {})
        self.checkpoint_store.save(
            run_id,
            self.event_store.revision(run_id),
            {"state": "working", "effective_config_digest": effective.digest},
        )
        prompt_item = ContextItem(
            "prompt", request.prompt, self.token_counter(request.prompt), pinned=True
        )
        context_state = ContextState((prompt_item,), (prompt_item,), (1, 1))
        instructions = "CORE KERNEL\n" + raw["agent"].get("profile_prompt", "")
        if "memory" in effective.enabled_capability_policies:
            instructions += "\nMEMORY POLICY"
        budgets = raw.get("budgets", {})
        max_turns = min(
            budgets.get("model_turns", self.platform_config.max_model_turns),
            self.platform_config.max_model_turns,
        )
        max_tools = min(
            budgets.get("tool_calls", self.platform_config.max_tool_calls),
            self.platform_config.max_tool_calls,
        )
        turns = 0
        tool_calls = 0
        while turns < max_turns:
            if self.compactor:
                context_state = self.compactor.maybe_compact(context_state)
            context = "\n".join(item.content for item in context_state.active)
            turns += 1
            tool_catalog = {}
            for name in effective.model_tool_catalog:
                server, separator, remote_tool = name.partition(".")
                if separator and server in effective.mcp_tools:
                    tool_catalog[name] = {
                        "description": name,
                        "input_schema": discovered.get(server, {}).get(remote_tool, {}),
                    }
                else:
                    try:
                        definition = self.tool_runtime.registry.get(name)
                    except CoreError as error:
                        if error.code != "TOOL_NOT_FOUND":
                            raise
                        tool_catalog[name] = {}
                    else:
                        tool_catalog[name] = {
                            "description": definition.description,
                            "input_schema": definition.input_schema,
                        }
            response = self.model.generate(
                context=context,
                tools=tool_catalog,
                instructions=instructions,
            )
            for tool_request in response.tool_requests:
                if tool_calls >= max_tools:
                    raise CoreError("BUDGET_EXCEEDED")
                tool_calls += 1
                effective.require_tool(tool_request.name)
                server, separator, remote_tool = tool_request.name.partition(".")
                if separator and server in effective.mcp_tools:
                    outcome = self.mcp_connector.call(
                        tool_request.name, tool_request.arguments
                    )
                    result_text = json.dumps(outcome, sort_keys=True, default=str)
                else:
                    outcome = self.tool_runtime.execute(
                        ToolCall(
                            tool_request.id, tool_request.name, tool_request.arguments
                        ),
                        run_id=run_id,
                    )
                    if isinstance(outcome, ApprovalRequest):
                        raise CoreError(
                            "APPROVAL_REQUIRED", data={"approval_id": outcome.id}
                        )
                    if isinstance(outcome, ToolResult):
                        result_text = str(outcome.output)
                result_item = ContextItem(
                    "tool_result", result_text, self.token_counter(result_text)
                )
                context_state = ContextState(
                    context_state.active + (result_item,),
                    context_state.transcript + (result_item,),
                    (
                        context_state.sequence_range[0],
                        context_state.sequence_range[1] + 1,
                    ),
                )
            if response.message is not None:
                self.audit_log.append(
                    run_id, "task.completed", {"message": response.message}
                )
                self.event_store.append(
                    run_id, "task.completed", {"message": response.message}
                )
                self.checkpoint_store.save(
                    run_id,
                    self.event_store.revision(run_id),
                    {"state": "completed", "tool_calls": tool_calls},
                )
                return RunResult(
                    run_id, response.message, "completed", Usage(turns, tool_calls)
                )
        raise CoreError("BUDGET_EXCEEDED")

    def close(self):
        self.task_scheduler.close()
