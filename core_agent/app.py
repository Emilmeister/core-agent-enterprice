from __future__ import annotations

import atexit
import json
import os
from pathlib import Path

from starlette.responses import JSONResponse
from starlette.routing import Route

from .a2a import LOCAL_APPROVAL_STATUS_URI, AgentCard, Artifact
from .a2a_sdk import build_starlette_app
from .approvals import ApprovalManager, ApproveAllControlPlane
from .audit import InMemoryAuditLog
from .config import AgentConfig, PlatformConfig
from .durability import CheckpointStore, InMemoryEventStore
from .database import (
    PostgresAuditLog,
    PostgresCheckpointStore,
    PostgresDatabase,
    PostgresEventStore,
    PostgresTaskStore,
)
from .errors import CoreError
from .execution import LocalTerminalBackend, TerminalSessionManager
from .mcp import StreamableHttpMcpConnector
from .model import CompatibleHttpModel
from .observability import RecordingExporter, Telemetry
from .postgres_approvals import PostgresApprovalManager
from .runtime import ApprovalNeeded, CoreAgent
from .tasks import TaskScheduler
from .tools import (
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


def _boolean(name, default):
    value = os.getenv(name, default).lower()
    if value in {"1", "true", "yes"}:
        return True
    if value in {"0", "false", "no"}:
        return False
    raise CoreError("CONFIG_INVALID", f"{name} must be boolean")


def _state(database=None):
    environment = os.getenv("CORE_AGENT_ENVIRONMENT", "development")
    backend = os.getenv(
        "CORE_AGENT_STATE_BACKEND",
        "postgres" if environment == "production" or database else "test",
    )
    if environment == "production" and backend != "postgres":
        raise CoreError("PRODUCTION_DATABASE_REQUIRED")
    if backend == "test":
        return {
            "database": None,
            "approvals": None,
            "events": InMemoryEventStore(),
            "checkpoints": CheckpointStore(),
            "audit": InMemoryAuditLog(),
            "tasks": None,
        }
    if backend != "postgres":
        raise CoreError("CONFIG_INVALID", "unknown CORE_AGENT_STATE_BACKEND")
    database = database or PostgresDatabase.from_environment()
    try:
        auto_migrate = _boolean(
            "DATABASE_AUTO_MIGRATE", "false" if environment == "production" else "true"
        )
        if environment == "production" and auto_migrate:
            raise CoreError("PRODUCTION_AUTO_MIGRATE_FORBIDDEN")
        database.migrate() if auto_migrate else database.verify_schema()
    except Exception:
        database.close()
        raise
    return {
        "database": database,
        "approvals": PostgresApprovalManager(
            database,
            ttl_seconds=int(os.getenv("LOCAL_APPROVAL_DEFAULT_TTL_SECONDS", "7200")),
        ),
        "events": PostgresEventStore(database),
        "checkpoints": PostgresCheckpointStore(database),
        "audit": PostgresAuditLog(database),
        "tasks": PostgresTaskStore(database),
    }


def _agent(model, mcp_connector=None, *, state=None):
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
    local_approval_enabled = os.getenv("LOCAL_APPROVAL_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
    }
    state = state or _state()
    approvals = state["approvals"]
    if approvals is None:
        approval_path = os.getenv(
            "LOCAL_APPROVAL_DB_PATH", "/tmp/core-agent/state/approvals.sqlite3"
        )
        if approval_path != ":memory:":
            Path(approval_path).parent.mkdir(parents=True, exist_ok=True)
        approvals = ApprovalManager(
            approval_path,
            ttl_seconds=int(os.getenv("LOCAL_APPROVAL_DEFAULT_TTL_SECONDS", "7200")),
        )
    tools = ToolRuntime(
        registry,
        PolicyEngine(
            ApprovalMode(config.approval["mode"])
            if local_approval_enabled
            else ApprovalMode.NEVER
        ),
        approvals,
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
        event_store=state["events"],
        checkpoint_store=state["checkpoints"],
        audit_log=state["audit"],
        telemetry=telemetry,
    )
    return agent, telemetry


def create_app(
    *, model=None, mcp_connector=None, base_url=None, control_plane=None, database=None
):
    if os.getenv("CORE_AGENT_ENVIRONMENT", "development") == "production" and (
        control_plane is None or isinstance(control_plane, ApproveAllControlPlane)
    ):
        raise CoreError("LOCAL_OPERATOR_CONTROL_PLANE_REQUIRED")
    model = model or _model()
    state = _state(database)
    agent, telemetry = _agent(model, mcp_connector, state=state)
    control_plane = control_plane or ApproveAllControlPlane()

    def handle(request, context):
        user = context.call_context.user
        identity = user.user_name if user.is_authenticated else "anonymous"
        result = agent.run(
            request,
            task_id=context.task_id,
            identity=identity,
            session_id=context.context_id,
            tenant_id=context.tenant or "default",
        )
        if isinstance(result, ApprovalNeeded):
            return result
        return Artifact.text(result.message, {"run_id": result.run_id})

    def reserve_local(pending, context):
        return agent.reserve_local_approval(
            context.task_id, pending.request.id, control_plane
        )

    def dispatch_local(reserved, context):
        result = agent.dispatch_reserved_approval(
            context.task_id, reserved.approval_id, reserved.execution_id
        )
        if isinstance(result, ApprovalNeeded):
            return result
        return Artifact.text(result.message, {"run_id": result.run_id})

    def cancel(context):
        agent.cancel_local_approval(context.task_id)

    host = os.getenv("CORE_AGENT_HOST", "0.0.0.0")
    port = int(os.getenv("CORE_AGENT_PORT", "8000"))
    base_url = base_url or os.getenv("CORE_AGENT_BASE_URL", f"http://localhost:{port}")
    card = AgentCard(
        os.getenv("CORE_AGENT_NAME", "core-agent"),
        optional_extensions=(
            os.getenv("LOCAL_APPROVAL_EXTENSION_URI", LOCAL_APPROVAL_STATUS_URI),
        ),
    )
    app = build_starlette_app(
        agent_card=card,
        handler=handle,
        local_approval_reserve_handler=reserve_local,
        local_approval_dispatch_handler=dispatch_local,
        cancel_handler=cancel,
        is_waiting_local_approval=agent.is_waiting_local_approval,
        can_cancel=agent.can_cancel_local_approval,
        base_url=base_url,
        task_store=state["tasks"],
    )

    async def live(_request):
        return JSONResponse({"status": "ok"})

    async def ready(_request):
        try:
            if state["database"]:
                state["database"].check()
                state["database"].verify_schema()
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ready"})

    app.routes[0:0] = (Route("/health/live", live), Route("/health/ready", ready))
    app.state.core_agent = agent
    app.state.operator_control_plane = control_plane
    app.state.database = state["database"]

    def close():
        agent.close()
        telemetry.shutdown()
        if state["database"]:
            state["database"].close()

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
