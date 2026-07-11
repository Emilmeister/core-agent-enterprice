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


def _agent(model):
    servers = set(_csv("CORE_AGENT_ALLOWED_MCP_SERVERS", "memory"))
    mcp_tools = _allowed_mcp_tools(servers)
    platform = PlatformConfig(
        allowed_builtin_tools={"core.terminal.exec"},
        denied_builtin_tools=set(),
        allowed_mcp_servers=servers,
        denied_mcp_tools={},
        allowed_skills=set(),
        supported_features={"memory", "mcp", "terminal", "filesystem_mutation"},
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
                "background_tasks": False,
                "delegation": False,
                "terminal": True,
                "filesystem_mutation": True,
                "mcp": True,
                "skills": False,
                "human_input": False,
            },
            "tools": {
                "builtins": {
                    "default": "deny",
                    "allow": ["core.terminal.exec"],
                    "deny": [],
                },
                "mcp": {
                    "default": "deny",
                    "allow_servers": sorted(servers),
                    "allow_tools": mcp_tools,
                },
            },
            "skills": {"default": "deny", "allow": []},
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
        mcp_connector=StreamableHttpMcpConnector(
            headers=_json("CORE_AGENT_MCP_HEADERS_JSON")
        ),
        task_scheduler=TaskScheduler(telemetry),
        event_store=InMemoryEventStore(),
        checkpoint_store=CheckpointStore(),
        audit_log=InMemoryAuditLog(),
        telemetry=telemetry,
    )
    return agent, telemetry


def create_app():
    model = _model()
    agent, telemetry = _agent(model)

    def handle(request, _context):
        result = agent.run(request)
        return Artifact.text(result.message, {"run_id": result.run_id})

    host = os.getenv("CORE_AGENT_HOST", "0.0.0.0")
    port = int(os.getenv("CORE_AGENT_PORT", "8000"))
    base_url = os.getenv("CORE_AGENT_BASE_URL", f"http://localhost:{port}")
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
    app.state.bind = (host, port)
    return app


def main():
    import uvicorn

    app = create_app()
    host, port = app.state.bind
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
