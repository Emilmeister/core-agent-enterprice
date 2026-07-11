from __future__ import annotations

import fnmatch
import uuid
from dataclasses import asdict, dataclass, is_dataclass
import json
import threading

from .config import AgentConfig, RunRequest, compile_effective_config
from .context import ContextItem, ContextState
from .errors import CoreError
from .skills import SkillResolver
from .tasks import DelegationContract
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
        depth=0,
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
        self.depth = depth
        self._current_run = threading.local()
        self.tool_runtime.handlers.update(
            {
                "core.task.start": self._task_start,
                "core.task.get": self._task_get,
                "core.task.list": self._task_list,
                "core.task.wait": self._task_wait,
                "core.task.cancel": self._task_cancel,
                "core.delegate": self._delegate,
            }
        )

    def _enabled_builtins(self):
        raw = self.agent_config.to_dict()["tools"]["builtins"]
        choices = set(self.tool_runtime.registry.names())
        choices &= set(self.platform_config.allowed_builtin_tools)
        if raw.get("default") != "allow":
            choices = {
                name
                for name in choices
                if any(
                    fnmatch.fnmatchcase(name, pattern)
                    for pattern in raw.get("allow", ())
                )
            }
        return {
            name
            for name in choices
            if name not in self.platform_config.denied_builtin_tools
            and not any(
                fnmatch.fnmatchcase(name, pattern) for pattern in raw.get("deny", ())
            )
        }

    @staticmethod
    def _value(value):
        if hasattr(value, "to_dict"):
            return value.to_dict()
        if is_dataclass(value):
            return asdict(value)
        return value

    def _task_snapshot(self, task):
        return {
            "task_id": task.id,
            "state": task.state,
            "result": self._value(task.result),
            "error": str(task.error) if task.error else None,
            "revision": task.revision,
        }

    def _task_start(self, arguments, run_id):
        target = arguments["tool"]
        if (
            target not in self._enabled_builtins()
            or target.startswith("core.task.")
            or target == "core.delegate"
        ):
            raise CoreError("CAPABILITY_DISABLED")
        task_run_id = f"{run_id}-background-{uuid.uuid4()}"

        def execute(cancel_event):
            if cancel_event.is_set():
                return None
            outcome = self.tool_runtime.execute(
                ToolCall(str(uuid.uuid4()), target, arguments.get("arguments", {})),
                run_id=task_run_id,
            )
            if isinstance(outcome, ApprovalRequest):
                raise CoreError("APPROVAL_REQUIRED")
            return self._value(outcome.output)

        task = self.task_scheduler.start(
            execute,
            owner_id=run_id,
            required=bool(arguments.get("required")),
            accepts_cancel_event=True,
        )
        return self._task_snapshot(task)

    def _task_get(self, arguments, run_id):
        return self._task_snapshot(
            self.task_scheduler.get(arguments["task_id"], owner_id=run_id)
        )

    def _task_list(self, arguments, run_id):
        return [
            self._task_snapshot(task)
            for task in self.task_scheduler.list(owner_id=run_id)
        ]

    def _task_wait(self, arguments, run_id):
        try:
            task = self.task_scheduler.wait(
                arguments["task_id"],
                arguments.get("timeout"),
                owner_id=run_id,
            )
        except TimeoutError:
            task = self.task_scheduler.get(arguments["task_id"], owner_id=run_id)
        return self._task_snapshot(task)

    def _task_cancel(self, arguments, run_id):
        return self._task_snapshot(
            self.task_scheduler.cancel(arguments["task_id"], owner_id=run_id)
        )

    def _delegate(self, arguments, run_id):
        contract = DelegationContract.from_dict(arguments)
        raw = self.agent_config.to_dict()
        request, effective = self._current_run.value
        parent_budget = {
            "turns": raw.get("budgets", {}).get("model_turns", 100),
            "tool_calls": raw.get("budgets", {}).get("tool_calls", 200),
            "depth": raw.get("budgets", {}).get("depth", 3),
        }
        if (
            self.depth >= parent_budget["depth"]
            or not set(contract.tools) <= self._enabled_builtins()
            or not set(contract.skills) <= set(effective.skills)
            or any(
                server not in effective.mcp_tools
                or not set(tools) <= set(effective.mcp_tools[server])
                for server, tools in contract.mcp.items()
            )
            or any(
                value > parent_budget.get(key, value)
                for key, value in contract.budget.items()
            )
        ):
            raise CoreError("CAPABILITY_DISABLED")

        child_raw = raw
        child_raw["tools"]["builtins"] = {
            "default": "deny",
            "allow": list(contract.tools),
            "deny": [],
        }
        child_raw["tools"]["mcp"] = {
            "default": "deny",
            "allow_servers": list(contract.mcp),
            "allow_tools": {key: list(value) for key, value in contract.mcp.items()},
        }
        child_raw["skills"] = {
            "default": "deny",
            "allow": list(contract.skills),
        }
        memory_enabled = "memory" in contract.mcp
        delegation_enabled = "core.delegate" in contract.tools
        child_raw["features"].update(
            {
                "memory": raw["features"].get("memory", "optional")
                if memory_enabled
                else "disabled",
                "mcp": bool(contract.mcp),
                "skills": bool(contract.skills),
                "terminal": "core.terminal.exec" in contract.tools,
                "background_tasks": delegation_enabled
                or any(name.startswith("core.task.") for name in contract.tools),
                "delegation": delegation_enabled,
            }
        )
        child_raw["budgets"]["model_turns"] = contract.budget.get(
            "turns", parent_budget["turns"]
        )
        child_raw["budgets"]["tool_calls"] = contract.budget.get(
            "tool_calls", parent_budget["tool_calls"]
        )
        child_registry = type(self.tool_runtime.registry)()
        for name in contract.tools:
            child_registry.register(self.tool_runtime.registry.get(name))
        child_runtime = type(self.tool_runtime)(
            child_registry,
            self.tool_runtime.policy,
            self.tool_runtime.approvals,
            self.tool_runtime.environment_manager,
            self.tool_runtime.event_sink,
        )
        child = CoreAgent(
            platform_config=self.platform_config,
            agent_config=AgentConfig.from_dict(child_raw),
            model=self.model,
            tool_runtime=child_runtime,
            mcp_connector=self.mcp_connector,
            task_scheduler=self.task_scheduler,
            event_store=self.event_store,
            checkpoint_store=self.checkpoint_store,
            audit_log=self.audit_log,
            telemetry=self.telemetry,
            compactor=self.compactor,
            token_counter=self.token_counter,
            depth=self.depth + 1,
        )
        task = self.task_scheduler.start(
            lambda: child.run(
                {
                    "prompt": contract.instruction,
                    "mcp": [
                        declaration
                        for declaration in request.mcp
                        if declaration["name"] in contract.mcp
                    ],
                    "skills": [
                        declaration
                        for declaration in request.skills
                        if declaration["name"] in contract.skills
                    ],
                }
            ),
            owner_id=run_id,
            required=True,
        )
        return self._task_snapshot(task)

    def run(self, request):
        if isinstance(request, dict):
            request = RunRequest.from_dict(request)
        if not isinstance(request, RunRequest):
            raise CoreError("INVALID_REQUEST")
        raw = self.agent_config.to_dict()
        memory_mode = raw["features"].get("memory", "disabled")
        discovered = {}
        if raw["features"].get("mcp"):
            for declaration in request.mcp:
                if declaration.get("role") == "memory" and memory_mode == "disabled":
                    continue
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
        self._current_run.value = (request, effective)
        skill_resolver = SkillResolver(
            [item for item in request.skills if item.get("name") in effective.skills]
        )
        active_skills = [
            skill_resolver.activate(skill.name)
            for skill in skill_resolver.discover()
            if skill.name.lower() in request.prompt.lower()
        ]
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
        for skill in active_skills:
            instructions += f"\nSKILL {skill.name}\n{skill.instructions}"
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
                        result_text = json.dumps(
                            self._value(outcome.output), sort_keys=True, default=str
                        )
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
                self.task_scheduler.assert_can_complete_parent(run_id)
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
