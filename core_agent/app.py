from __future__ import annotations

import atexit
import json
import os

from .a2a import AgentCard, Artifact
from .a2a_sdk import build_starlette_app
from .audit import InMemoryAuditLog
from .config import AgentConfig, PlatformConfig
from .durability import CheckpointStore, InMemoryEventStore
from .errors import CoreError
from .execution import LocalTerminalBackend, TerminalSessionManager
from .mcp import StreamableHttpMcpConnector
from .model import CompatibleHttpModel
from .observability import RecordingExporter, Telemetry
from .runtime import CoreAgent
from .tasks import TaskScheduler
from .tools import (
    ApprovalManager,
    ApprovalMode,
    PolicyEngine,
    ToolDefinition,
    ToolRegistry,
    ToolRuntime,
)


def _csv(name, default=""):
    return tuple(
        value.strip() for value in os.getenv(name, default).split(",") if value.strip()
    )


def _json(name):
    value = os.getenv(name)
    try:
        result = json.loads(value) if value else {}
    except json.JSONDecodeError as error:
        raise CoreError("CONFIG_INVALID", f"{name} must contain JSON") from error
    if not isinstance(result, dict):
        raise CoreError("CONFIG_INVALID", f"{name} must contain a JSON object")
    return result


def _allowed_mcp_tools(servers):
    grouped = {server: [] for server in servers}
    for value in _csv(
        "CORE_AGENT_ALLOWED_MCP_TOOLS",
        "memory.memory.search,memory.memory.read,memory.memory.create,"
        "memory.memory.update,memory.memory.split,memory.memory.index_status",
    ):
        server, separator, tool = value.partition(".")
        if separator and server in grouped:
            grouped[server].append(tool)
    return grouped


def _model():
    model_name = os.getenv("MODEL_NAME")
    if not model_name:
        raise CoreError("CONFIG_INVALID", "MODEL_NAME is required")
    return CompatibleHttpModel(
        api_format=os.getenv("MODEL_API_FORMAT", "openai").lower(),
        model=model_name,
        base_url=os.getenv("MODEL_BASE_URL"),
        endpoint=os.getenv("MODEL_ENDPOINT"),
        api_key=os.getenv("MODEL_API_KEY"),
        timeout=float(os.getenv("MODEL_TIMEOUT", "120")),
        max_tokens=int(os.getenv("MODEL_MAX_TOKENS", "4096")),
        headers=_json("MODEL_HEADERS_JSON"),
        extra_body=_json("MODEL_EXTRA_BODY_JSON"),
        anthropic_version=os.getenv("ANTHROPIC_VERSION", "2023-06-01"),
    )


def _agent(model, mcp_connector=None):
    servers = set(_csv("CORE_AGENT_ALLOWED_MCP_SERVERS", "memory"))
    allowed_skills = set(_csv("CORE_AGENT_ALLOWED_SKILLS"))
    mcp_tools = _allowed_mcp_tools(servers)
    builtin_tools = {
        "core.terminal.exec",
        "core.task.start",
        "core.task.get",
        "core.task.list",
        "core.task.wait",
        "core.task.cancel",
        "core.delegate",
    }
    platform = PlatformConfig(
        allowed_builtin_tools=builtin_tools,
        denied_builtin_tools=set(),
        allowed_mcp_servers=servers,
        denied_mcp_tools={},
        allowed_skills=allowed_skills,
        supported_features={
            "memory",
            "mcp",
            "terminal",
            "filesystem_mutation",
            "background_tasks",
            "delegation",
        },
        max_model_turns=int(os.getenv("CORE_AGENT_MAX_MODEL_TURNS", "100")),
        max_tool_calls=int(os.getenv("CORE_AGENT_MAX_TOOL_CALLS", "200")),
    )
    config = AgentConfig.from_dict(
        {
            "schema_version": "v1alpha1",
            "agent": {
                "name": os.getenv("CORE_AGENT_NAME", "core-agent"),
                "profile_prompt": os.getenv(
                    "CORE_AGENT_PROFILE",
                    "Complete the user's task using available tools.",
                ),
            },
            "model": {"route": model.model},
            "features": {
                "memory": os.getenv("CORE_AGENT_MEMORY", "optional"),
                "background_tasks": True,
                "delegation": True,
                "terminal": True,
                "filesystem_mutation": True,
                "mcp": True,
                "skills": bool(allowed_skills),
                "human_input": False,
            },
            "tools": {
                "builtins": {
                    "default": "deny",
                    "allow": sorted(builtin_tools),
                    "deny": [],
                },
                "mcp": {
                    "default": "deny",
                    "allow_servers": sorted(servers),
                    "allow_tools": mcp_tools,
                },
            },
            "skills": {"default": "deny", "allow": sorted(allowed_skills)},
            "context": {
                "compact_at_working_ratio": 0.90,
                "compact_to_working_ratio": 0.15,
            },
            "approval": {"mode": os.getenv("CORE_AGENT_APPROVAL_MODE", "on_risk")},
            "execution": {"environment_profile": "local-pty"},
            "observability": {"otel_profile": "otlp"},
            "budgets": {
                "model_turns": platform.max_model_turns,
                "tool_calls": platform.max_tool_calls,
                "depth": int(os.getenv("CORE_AGENT_MAX_DEPTH", "3")),
            },
        }
    )
    registry = ToolRegistry()
    trusted = os.getenv("CORE_AGENT_TRUST_TERMINAL", "1").lower() in {
        "1",
        "true",
        "yes",
    }
    registry.register(
        ToolDefinition(
            "core.terminal.exec",
            "Execute argv in the agent's local terminal workspace.",
            {
                "type": "object",
                "properties": {
                    "argv": {"type": "array", "items": {"type": "string"}},
                    "cwd": {"type": "string"},
                    "timeout": {"type": "number"},
                    "max_output_bytes": {"type": "integer"},
                },
                "required": ["argv"],
                "additionalProperties": False,
            },
            mutating=not trusted,
            risk_tags=frozenset() if trusted else frozenset({"local_execution"}),
        )
    )
    task_definitions = {
        "core.task.start": (
            "Start allowed tool work in the background and return its task handle.",
            {
                "tool": {"type": "string"},
                "arguments": {"type": "object"},
                "required": {"type": "boolean"},
            },
            ["tool", "arguments"],
        ),
        "core.task.get": (
            "Get an owned background task snapshot.",
            {"task_id": {"type": "string"}},
            ["task_id"],
        ),
        "core.task.list": ("List background tasks owned by this agent run.", {}, []),
        "core.task.wait": (
            "Passively wait for an owned task or timeout.",
            {"task_id": {"type": "string"}, "timeout": {"type": "number"}},
            ["task_id"],
        ),
        "core.task.cancel": (
            "Request cancellation of an owned background task.",
            {"task_id": {"type": "string"}},
            ["task_id"],
        ),
        "core.delegate": (
            "Start a focused child Core Agent with an exact capability contract.",
            {
                "instruction": {"type": "string"},
                "tools": {"type": "array", "items": {"type": "string"}},
                "mcp": {"type": "object"},
                "skills": {"type": "array", "items": {"type": "string"}},
                "budget": {"type": "object"},
                "result_schema": {"type": "string"},
            },
            ["instruction", "tools", "mcp", "skills", "budget"],
        ),
    }
    for name, (description, properties, required) in task_definitions.items():
        registry.register(
            ToolDefinition(
                name,
                description,
                {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
                mutating=False,
                risk_tags=frozenset(),
            )
        )
    sessions = TerminalSessionManager(
        LocalTerminalBackend(os.getenv("LOCAL_WORKSPACE_ROOT", "/tmp/core-agent/runs"))
    )
    tools = ToolRuntime(
        registry,
        PolicyEngine(ApprovalMode(config.approval["mode"])),
        ApprovalManager(),
        sessions,
    )
    telemetry = (
        Telemetry.otlp(endpoint=os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"])
        if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        else Telemetry(RecordingExporter())
    )
    agent = CoreAgent(
        platform_config=platform,
        agent_config=config,
        model=model,
        tool_runtime=tools,
        mcp_connector=mcp_connector
        or StreamableHttpMcpConnector(headers=_json("CORE_AGENT_MCP_HEADERS_JSON")),
        task_scheduler=TaskScheduler(telemetry),
        event_store=InMemoryEventStore(),
        checkpoint_store=CheckpointStore(),
        audit_log=InMemoryAuditLog(),
        telemetry=telemetry,
    )
    return agent, telemetry


def create_app(*, model=None, mcp_connector=None, base_url=None):
    model = model or _model()
    agent, telemetry = _agent(model, mcp_connector)

    def handle(request, _context):
        result = agent.run(request)
        return Artifact.text(result.message, {"run_id": result.run_id})

    host = os.getenv("CORE_AGENT_HOST", "0.0.0.0")
    port = int(os.getenv("CORE_AGENT_PORT", "8000"))
    base_url = base_url or os.getenv("CORE_AGENT_BASE_URL", f"http://localhost:{port}")
    app = build_starlette_app(
        agent_card=AgentCard.minimal(os.getenv("CORE_AGENT_NAME", "core-agent")),
        handler=handle,
        base_url=base_url,
    )
    app.state.core_agent = agent

    def close():
        agent.close()
        telemetry.shutdown()

    atexit.register(close)
    app.state.close = close
    app.state.bind = (host, port)
    return app


def main():
    import uvicorn

    app = create_app()
    host, port = app.state.bind
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
