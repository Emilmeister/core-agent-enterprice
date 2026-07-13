from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
from dataclasses import dataclass

from .errors import CoreError


MAX_SUBAGENT_DEPTH = 2


@dataclass(frozen=True)
class RunRequest:
    prompt: str
    mcp: tuple[dict, ...]
    skills: tuple[dict, ...]

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {"prompt", "mcp", "skills"}:
            raise CoreError("INVALID_REQUEST")
        if not isinstance(value["prompt"], str) or not value["prompt"].strip():
            raise CoreError("INVALID_REQUEST")
        if not isinstance(value["mcp"], list) or not isinstance(value["skills"], list):
            raise CoreError("INVALID_REQUEST")
        names = [item.get("name") for item in value["mcp"] if isinstance(item, dict)]
        if (
            len(names) != len(value["mcp"])
            or None in names
            or len(names) != len(set(names))
        ):
            raise CoreError("INVALID_REQUEST")
        return cls(
            value["prompt"],
            tuple(copy.deepcopy(value["mcp"])),
            tuple(copy.deepcopy(value["skills"])),
        )

    def to_dict(self):
        return {
            "prompt": self.prompt,
            "mcp": copy.deepcopy(list(self.mcp)),
            "skills": copy.deepcopy(list(self.skills)),
        }


class AgentConfig:
    _fields = {
        "schema_version",
        "agent",
        "model",
        "features",
        "tools",
        "skills",
        "context",
        "approval",
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
            "approval",
            "execution",
            "observability",
        }
        if not required <= set(raw):
            raise CoreError("CONFIG_INVALID")
        features = raw.get("features", {})
        if features.get("delegation") and not features.get("background_tasks"):
            raise CoreError("CONFIG_CONFLICT")
        budgets = raw.get("budgets", {})
        if not isinstance(budgets, dict):
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
    a2a_protocol_versions: tuple[str, ...] = ("1.0",)
    a2a_bindings: tuple[str, ...] = ("HTTP+JSON",)
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
    a2a_protocol_versions: tuple[str, ...]
    a2a_bindings: tuple[str, ...]
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


def compile_effective_config(platform, agent, request, discovered):
    raw = agent.to_dict()
    features = raw["features"]
    requested_servers = {item["name"]: item for item in request.mcp}
    warnings = []

    for feature, value in features.items():
        enabled = value not in (False, "disabled")
        if enabled and feature not in platform.supported_features:
            if value == "required":
                raise CoreError("CAPABILITY_DISABLED")
            warnings.append(Warning("CAPABILITY_FILTERED", feature))

    memory_mode = features.get("memory", "disabled")
    memory_servers = {
        name
        for name, declaration in requested_servers.items()
        if declaration.get("role") == "memory"
    }
    memory = next((requested_servers[name] for name in memory_servers), None)
    if memory_mode == "disabled" and memory and memory.get("required"):
        raise CoreError("CAPABILITY_DISABLED")
    if memory_mode == "required" and not memory:
        raise CoreError("REQUIRED_CAPABILITY_MISSING")

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
    allowed -= denied

    capability_for_prefix = {
        "core.terminal.": "terminal",
        "core.fs.": "filesystem_mutation",
        "core.task.": "background_tasks",
        "core.delegate": "delegation",
    }
    allowed = {
        tool
        for tool in allowed
        if features.get(
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

    mcp_tools = {}
    mcp_policy = raw["tools"]["mcp"]
    for server, declaration in requested_servers.items():
        if server not in platform.allowed_mcp_servers:
            if declaration.get("required"):
                raise CoreError("CAPABILITY_DISABLED")
            warnings.append(Warning("CAPABILITY_FILTERED", server))
            continue
        if server in memory_servers and memory_mode == "disabled":
            warnings.append(Warning("CAPABILITY_FILTERED", "memory"))
            continue
        if not features.get("mcp", True):
            continue
        catalog = set(discovered.get(server, {}))
        explicit = set(mcp_policy.get("allow_tools", {}).get(server, []))
        server_allowed = catalog & explicit
        server_allowed -= set(platform.denied_mcp_tools.get(server, set()))
        if server in set(mcp_policy.get("allow_servers", [])):
            mcp_tools[server] = frozenset(server_allowed)

    requested_skills = {item.get("name") for item in request.skills}
    skills = frozenset(
        requested_skills
        & set(raw["skills"].get("allow", []))
        & set(platform.allowed_skills)
    )
    policies = {
        feature
        for feature, value in features.items()
        if value not in (False, "disabled") and feature in platform.supported_features
    }
    if memory_mode == "disabled" or not (memory_servers & set(mcp_tools)):
        policies.discard("memory")

    model_catalog = set(allowed)
    for server, tools in mcp_tools.items():
        model_catalog.update(f"{server}.{tool}" for tool in tools)
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
        "approval_mode": raw["approval"].get("mode"),
        "execution_profile": raw["execution"].get("environment_profile"),
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
        platform.a2a_protocol_versions,
        platform.a2a_bindings,
        raw["agent"]["name"],
    )
