from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from .errors import CoreError


class ApprovalMode(str, Enum):
    ON_RISK = "on_risk"
    ALWAYS = "always"
    NEVER = "never"


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict
    mutating: bool
    risk_tags: frozenset[str]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ToolResult:
    tool_call_id: str
    status: str
    output: object = None


@dataclass(frozen=True)
class RuntimeEvent:
    kind: str
    data: dict = field(default_factory=dict)


class ToolRegistry:
    def __init__(self):
        self._tools = {}

    def register(self, definition):
        if definition.name in self._tools:
            raise CoreError("TOOL_NAME_COLLISION")
        self._tools[definition.name] = definition

    def get(self, name):
        try:
            return self._tools[name]
        except KeyError:
            raise CoreError("TOOL_NOT_FOUND") from None

    def names(self):
        return frozenset(self._tools)


def _validate(schema, value):
    if schema.get("type") == "object":
        if not isinstance(value, dict):
            return False
        if any(key not in value for key in schema.get("required", [])):
            return False
        if schema.get("additionalProperties") is False and set(value) - set(
            schema.get("properties", {})
        ):
            return False
        return all(
            _validate(schema["properties"][key], item)
            for key, item in value.items()
            if key in schema.get("properties", {})
        )
    if schema.get("type") == "array":
        return isinstance(value, list) and all(
            _validate(schema.get("items", {}), item) for item in value
        )
    if schema.get("type") == "string":
        return isinstance(value, str)
    return True


def _contains_private_reasoning(value):
    if isinstance(value, dict):
        return any(
            key.lower() in {"reasoning", "chain_of_thought", "scratchpad"}
            or _contains_private_reasoning(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_private_reasoning(item) for item in value)
    return False


class PolicyEngine:
    def __init__(self, approval_mode=ApprovalMode.ON_RISK):
        self.approval_mode = ApprovalMode(approval_mode)

    def evaluate(self, definition):
        risky = bool(definition.risk_tags) or definition.mutating
        if not risky and self.approval_mode != ApprovalMode.ALWAYS:
            return "allow"
        if self.approval_mode == ApprovalMode.NEVER:
            return "deny"
        return "require_approval"


def _digest(arguments):
    return hashlib.sha256(
        json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass
class ApprovalRequest:
    id: str
    tool_call_id: str
    tool_name: str
    argument_digest: str
    risks: tuple[str, ...]
    arguments: dict
    scope_options: tuple[str, ...] = ("single_call",)
    identity: str | None = None
    session_id: str | None = None
    status: str = "pending"
    decision: str | None = None


@dataclass
class ApprovalGrant:
    id: str
    scope: str
    identity: str | None
    session_id: str | None
    expires_at: float
    max_uses: int
    uses: int = 0
    revoked: bool = False


@dataclass(frozen=True)
class GrantAuthorization:
    grant_id: str


class ApprovalManager:
    def __init__(self):
        self._requests = {}
        self._grants = {}

    def request(
        self,
        call,
        *,
        risks,
        scope_options=("single_call",),
        identity=None,
        session_id=None,
    ):
        request = ApprovalRequest(
            str(uuid.uuid4()),
            call.id,
            call.name,
            _digest(call.arguments),
            tuple(sorted(risks)),
            dict(call.arguments),
            tuple(scope_options),
            identity,
            session_id,
        )
        self._requests[request.id] = request
        return request

    def resolve(
        self, request_id, decision, *, scope="single_call", expires_in=0, max_uses=1
    ):
        try:
            request = self._requests[request_id]
        except KeyError:
            raise CoreError("APPROVAL_NOT_FOUND") from None
        if request.status != "pending":
            raise CoreError("APPROVAL_ALREADY_RESOLVED")
        request.status = "resolved"
        request.decision = decision
        if decision == "approve" and scope != "single_call":
            grant = ApprovalGrant(
                str(uuid.uuid4()),
                scope,
                request.identity,
                request.session_id,
                time.monotonic() + expires_in,
                max_uses,
            )
            self._grants[grant.id] = grant
            return grant
        return request

    def get(self, request_id):
        return self._requests[request_id]

    def authorize_with_grants(self, call, *, identity, session_id, policy_version):
        repo = call.arguments.get("repo")
        expected = f"session:{call.name}:{repo}"
        now = time.monotonic()
        for grant in self._grants.values():
            if (
                not grant.revoked
                and grant.scope == expected
                and grant.identity == identity
                and grant.session_id == session_id
                and grant.expires_at >= now
                and grant.uses < grant.max_uses
            ):
                grant.uses += 1
                return GrantAuthorization(grant.id)
        return None

    def revoke(self, grant_id):
        self._grants[grant_id].revoked = True


@dataclass(frozen=True)
class InformationRequest:
    id: str
    kind: str
    prompt: str
    schema: dict


class ToolRuntime:
    def __init__(
        self,
        registry,
        policy,
        approvals,
        environment_manager,
        event_sink=None,
        handlers=None,
    ):
        self.registry = registry
        self.policy = policy
        self.approvals = approvals
        self.environment_manager = environment_manager
        self.event_sink = event_sink or (lambda event: None)
        self.handlers = dict(handlers or {})
        self.execution_count = 0

    def _emit(self, kind, **data):
        self.event_sink(RuntimeEvent(kind, data))

    def execute(self, call, *, run_id, identity=None, session_id=None):
        self._emit("tool.requested", call_id=call.id)
        definition = self.registry.get(call.name)
        if _contains_private_reasoning(call.arguments) or not _validate(
            definition.input_schema, call.arguments
        ):
            raise CoreError("TOOL_ARGUMENT_INVALID")
        self._emit("tool.validated", call_id=call.id)
        decision = self.policy.evaluate(definition)
        if decision == "deny":
            self._emit("policy.denied", call_id=call.id)
            return ToolResult(call.id, "denied")
        if decision == "require_approval":
            self._emit("approval.requested", call_id=call.id)
            return self.approvals.request(
                call,
                risks=definition.risk_tags,
                identity=identity,
                session_id=session_id,
            )
        self._emit("policy.allowed", call_id=call.id)
        return self._execute(call, run_id)

    def _execute(self, call, run_id):
        self._emit("tool.started", call_id=call.id)
        self.execution_count += 1
        if call.name in self.handlers:
            result = self.handlers[call.name](call.arguments, run_id)
            self._emit("tool.completed", call_id=call.id)
            return ToolResult(call.id, "succeeded", result)
        request = (
            call.arguments
            if call.name == "core.terminal.exec"
            else {"tool": call.name, "arguments": call.arguments}
        )
        result = self.environment_manager.execute_transient(request, run_id)
        self._emit("tool.completed", call_id=call.id)
        return ToolResult(call.id, "succeeded", result)

    def resume_approved(self, call, approval_id, *, run_id):
        request = self.approvals.get(approval_id)
        if request.decision != "approve":
            raise CoreError("APPROVAL_DENIED")
        definition = self.registry.get(call.name)
        if _contains_private_reasoning(call.arguments) or not _validate(
            definition.input_schema, call.arguments
        ):
            raise CoreError("TOOL_ARGUMENT_INVALID")
        if self.policy.evaluate(definition) == "deny":
            raise CoreError("POLICY_DENIED")
        if (
            request.tool_call_id != call.id
            or request.tool_name != call.name
            or request.argument_digest != _digest(call.arguments)
        ):
            raise CoreError("APPROVAL_ARGUMENTS_CHANGED")
        return self._execute(call, run_id)

    def request_input(self, prompt, schema, *, run_id):
        return InformationRequest(
            str(uuid.uuid4()), "information_required", prompt, schema
        )
