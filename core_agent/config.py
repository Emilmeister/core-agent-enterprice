from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
from dataclasses import dataclass

from .errors import CoreError
from .mcp import mcp_tool_index
from .skills import SKILL_TOOLS
from .tools import RESPONSE_BEGIN_TOOL

# Advertised (binding, version) pairs; see core_agent/a2a.py for why they pair up.
A2A_INTERFACES = (("HTTP+JSON", "1.0"), ("JSONRPC", "1.0"))


MAX_SUBAGENT_DEPTH = 2
RUNTIME_MODES = frozenset({"with_terminal", "without_terminal"})
TERMINAL_MODE_TOOLS = frozenset({"core_terminal_exec", "core_task_start"})
RETIRED_ARTIFACT_TOOLS = frozenset({"core_artifact_save", "core_artifact_load", "core_artifact_list"})


@dataclass(frozen=True)
class RunRequest:
    """The only user input. MCP servers and skills come from configuration."""

    prompt: str
    # Incoming binary Parts. Transport-only: never serialized back into a Message,
    # so a follow-up cannot replay someone else's upload.
    attachments: tuple = ()

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {"prompt"}:
            raise CoreError("INVALID_REQUEST")
        if not isinstance(value["prompt"], str) or not value["prompt"].strip():
            raise CoreError("INVALID_REQUEST")
        return cls(value["prompt"])

    def to_dict(self):
        return {"prompt": self.prompt}


class AgentConfig:
    _fields = {
        "schema_version",
        "agent",
        "model",
        "features",
        "tools",
        "skills",
        "context",
        "execution",
        "observability",
        "budgets",
        "delegation",
    }

    def __init__(self, raw):
        self._raw = copy.deepcopy(raw)

    @classmethod
    def from_dict(cls, raw):
        if not isinstance(raw, dict) or set(raw) - cls._fields:
            raise CoreError("CONFIG_INVALID")
        required = {
            "schema_version",
            "agent",
            "model",
            "features",
            "tools",
            "skills",
            "context",
                "execution",
            "observability",
        }
        if not required <= set(raw):
            raise CoreError("CONFIG_INVALID")
        features = raw.get("features", {})
        if features.get("delegation") and not features.get("background_tasks"):
            raise CoreError("CONFIG_CONFLICT")
        runtime_mode = raw.get("execution", {}).get(
            "runtime_mode", "with_terminal"
        )
        if runtime_mode not in RUNTIME_MODES:
            raise CoreError("CONFIG_INVALID")
        budgets = raw.get("budgets", {})
        if not isinstance(budgets, dict):
            raise CoreError("CONFIG_INVALID")
        model_turns = budgets.get("model_turns")
        if model_turns is not None and (
            not isinstance(model_turns, int)
            or isinstance(model_turns, bool)
            or model_turns < 1
        ):
            raise CoreError("CONFIG_INVALID")
        depth = budgets.get("depth", MAX_SUBAGENT_DEPTH)
        if (
            not isinstance(depth, int)
            or isinstance(depth, bool)
            or not 0 <= depth <= MAX_SUBAGENT_DEPTH
        ):
            raise CoreError("CONFIG_INVALID")
        return cls(raw)

    def to_dict(self):
        return copy.deepcopy(self._raw)

    def __getattr__(self, name):
        try:
            return self._raw[name]
        except KeyError:
            raise AttributeError(name) from None


@dataclass(frozen=True)
class PlatformConfig:
    allowed_builtin_tools: set[str]
    denied_builtin_tools: set[str]
    allowed_mcp_servers: set[str]
    denied_mcp_tools: dict[str, set[str]]
    allowed_skills: set[str]
    supported_features: set[str]
    a2a_interfaces: tuple[tuple[str, str], ...] = A2A_INTERFACES
    max_model_turns: int = 100
    max_tool_calls: int = 200


@dataclass(frozen=True)
class Warning:
    code: str
    capability: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class EffectiveConfig:
    builtin_tools: frozenset[str]
    mcp_tools: dict[str, frozenset[str]]
    skills: frozenset[str]
    enabled_capability_policies: frozenset[str]
    warnings: tuple[Warning, ...]
    model_tool_catalog: frozenset[str]
    digest: str
    audit_snapshot: str
    a2a_interfaces: tuple[tuple[str, str], ...]
    agent_name: str
    kernel_version: str = "kernel-v1"

    def require_tool(self, name):
        if name not in self.model_tool_catalog:
            raise CoreError("CAPABILITY_DISABLED")
        return name


def _expand(patterns, choices):
    return {
        choice
        for choice in choices
        if any(fnmatch.fnmatchcase(choice, pattern) for pattern in patterns)
    }


def compile_effective_config(
    platform, agent, declared_mcp, discovered, *, legacy_ungated_skills=False
):
    raw = agent.to_dict()
    features = raw["features"]
    requested_servers = {item["name"]: item for item in declared_mcp}
    warnings = []

    for feature, value in features.items():
        enabled = value not in (False, "disabled")
        if enabled and feature not in platform.supported_features:
            if value == "required":
                raise CoreError("CAPABILITY_DISABLED")
            warnings.append(Warning("CAPABILITY_FILTERED", feature))

    # Feature values are a mix of booleans and mode strings, and "disabled" is a
    # non-empty string: a truthiness test on the raw value would let a disabled
    # capability through every gate below.
    gates = {name: value not in (False, "disabled") for name, value in features.items()}
    memory_mode = features.get("memory", "disabled")

    builtins = raw["tools"]["builtins"]
    choices = set(platform.allowed_builtin_tools)
    allowed = (
        choices
        if builtins.get("default") == "allow"
        else _expand(builtins.get("allow", []), choices)
    )
    denied = _expand(builtins.get("deny", []), choices) | set(
        platform.denied_builtin_tools
    )
    allowed -= denied | RETIRED_ARTIFACT_TOOLS
    runtime_mode = raw["execution"].get("runtime_mode", "with_terminal")
    if runtime_mode == "without_terminal":
        allowed -= TERMINAL_MODE_TOOLS

    capability_for_prefix = {
        "core_terminal_": "terminal",
        "core_python_": "python",
        "core_fs_": "filesystem_mutation",
        "core_task_": "background_tasks",
        "core_delegate": "delegation",
        "core_ask_owner": "human_input",
        "core_agent_": "remote_agents",
        "core_memory_": "memory",
    }
    allowed = {
        tool
        for tool in allowed
        if gates.get(
            next(
                (
                    feature
                    for prefix, feature in capability_for_prefix.items()
                    if tool.startswith(prefix)
                ),
                "",
            ),
            True,
        )
    }
    if memory_mode == "required" and not any(
        tool.startswith("core_memory_") for tool in allowed
    ):
        raise CoreError("REQUIRED_CAPABILITY_MISSING")

    mcp_tools = {}
    mcp_policy = raw["tools"]["mcp"]
    for server, declaration in requested_servers.items():
        if server not in platform.allowed_mcp_servers:
            if declaration.get("required"):
                raise CoreError("CAPABILITY_DISABLED")
            warnings.append(Warning("CAPABILITY_FILTERED", server))
            continue
        if not gates.get("mcp", True) or "mcp" not in platform.supported_features:
            continue
        catalog = set(discovered.get(server, {}))
        explicit = set(mcp_policy.get("allow_tools", {}).get(server, []))
        if declaration.get("owner_configured") and server in mcp_policy.get("owner_servers", ()):
            explicit = catalog
        server_allowed = catalog & explicit
        server_allowed -= set(platform.denied_mcp_tools.get(server, set()))
        if server in set(mcp_policy.get("allow_servers", [])):
            mcp_tools[server] = frozenset(server_allowed)

    skills = frozenset()
    if legacy_ungated_skills or (
        gates.get("skills", True) and "skills" in platform.supported_features
    ):
        skills = frozenset(
            set(raw["skills"].get("allow", [])) & set(platform.allowed_skills)
        )
    policies = {
        feature
        for feature, value in features.items()
        if value not in (False, "disabled") and feature in platform.supported_features
    }
    if not any(tool.startswith("core_memory_") for tool in allowed):
        policies.discard("memory")
    if "core_python_exec" not in allowed:
        policies.discard("python")
    policies.discard("artifacts")
    if "core_response_files" in allowed:
        policies.add("response_files")
    if "core_agent_send_message" not in allowed:
        policies.discard("remote_agents")

    model_catalog = set(allowed)
    model_catalog.update(
        mcp_tool_index(mcp_tools, reserved_names=model_catalog | SKILL_TOOLS | {RESPONSE_BEGIN_TOOL.name})
    )
    snapshot_value = {
        "builtin_tools": sorted(allowed),
        "mcp_tools": {key: sorted(value) for key, value in sorted(mcp_tools.items())},
        "skills": sorted(skills),
        "policies": sorted(policies),
        "agent_profile_digest": hashlib.sha256(
            raw["agent"].get("profile_prompt", "").encode()
        ).hexdigest(),
        "model_route": raw["model"].get("route"),
        "budgets": raw.get("budgets", {}),
        "context": raw["context"],
        "execution_profile": raw["execution"].get("environment_profile"),
        "runtime_mode": runtime_mode,
        "otel_profile": raw["observability"].get("otel_profile"),
        "kernel_version": "kernel-v1",
    }
    audit_snapshot = json.dumps(snapshot_value, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(audit_snapshot.encode()).hexdigest()
    return EffectiveConfig(
        frozenset(allowed),
        mcp_tools,
        skills,
        frozenset(policies),
        tuple(warnings),
        frozenset(model_catalog),
        digest,
        audit_snapshot,
        platform.a2a_interfaces,
        raw["agent"]["name"],
    )
