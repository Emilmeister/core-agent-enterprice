from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from enum import Enum

from .approvals import ApprovalManager as ApprovalManager
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
    error_code: str | None = None


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
    if "enum" in schema and value not in schema["enum"]:
        return False
    if schema.get("type") == "object":
        if not isinstance(value, dict):
            return False
        if any(key not in value for key in schema.get("required", [])):
            return False
        if len(value) < schema.get("minProperties", 0):
            return False
        if schema.get("additionalProperties") is False and set(value) - set(
            schema.get("properties", {})
        ):
            return False
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", {})
        return all(
            _validate(properties[key], item)
            if key in properties
            else not isinstance(additional, dict) or _validate(additional, item)
            for key, item in value.items()
        )
    if schema.get("type") == "array":
        return isinstance(value, list) and all(
            _validate(schema.get("items", {}), item) for item in value
        )
    if schema.get("type") == "string":
        return (
            isinstance(value, str)
            and len(value) >= schema.get("minLength", 0)
            and len(value) <= schema.get("maxLength", len(value))
            and (
                "pattern" not in schema
                or re.search(schema["pattern"], value) is not None
            )
        )
    if schema.get("type") == "integer":
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value >= schema.get("minimum", value)
            and value <= schema.get("maximum", value)
        )
    if schema.get("type") == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value >= schema.get("minimum", value)
            and value <= schema.get("maximum", value)
        )
    if schema.get("type") == "boolean":
        return isinstance(value, bool)
    return True


def validate_json_schema(schema, value):
    return isinstance(schema, dict) and _validate(schema, value)


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

    @staticmethod
    def validate(call, definition):
        if _contains_private_reasoning(call.arguments) or not _validate(
            definition.input_schema, call.arguments
        ):
            raise CoreError("TOOL_ARGUMENT_INVALID")

    def execute(
        self,
        call,
        *,
        run_id,
        identity=None,
        session_id=None,
        task_id=None,
        tenant_id=None,
        environment="local-container",
        policy_version="core-policy-v1",
    ):
        self._emit("tool.requested", call_id=call.id)
        definition = self.registry.get(call.name)
        self.validate(call, definition)
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
                task_id=task_id or run_id,
                context_id=session_id or run_id,
                tenant_id=tenant_id or "default",
                caller_principal_id=identity or "anonymous",
                environment=environment,
                policy_version=policy_version,
            )
        self._emit("policy.allowed", call_id=call.id)
        return self._execute(call, run_id)

    def _execute(self, call, run_id):
        self._emit("tool.started", call_id=call.id)
        self.execution_count += 1
        if call.name in self.handlers:
            result = self.handlers[call.name](call.arguments, run_id)
        else:
            request = (
                call.arguments
                if call.name == "core.terminal.exec"
                else {"tool": call.name, "arguments": call.arguments}
            )
            result = self.environment_manager.execute_transient(request, run_id)
        status = getattr(result, "status", "succeeded")
        if status not in {"failed", "timed_out"}:
            status = "succeeded"
        error_code = (
            "TOOL_TIMED_OUT"
            if status == "timed_out"
            else ("TOOL_RETURNED_FAILED" if status == "failed" else None)
        )
        self._emit(
            "tool.completed" if status == "succeeded" else "tool.failed",
            call_id=call.id,
            status=status,
        )
        return ToolResult(call.id, status, result, error_code)

    def resume_approved(self, call, approval_id, *, run_id):
        definition = self.registry.get(call.name)
        self.validate(call, definition)
        if self.policy.evaluate(definition) == "deny":
            raise CoreError("POLICY_DENIED")
        execution = self.approvals.authorize_dispatch(approval_id, call)
        try:
            result = self._execute(call, run_id)
        except Exception as error:
            self.approvals.finish_execution(
                execution.id,
                "FAILED",
                error_code=getattr(error, "code", type(error).__name__),
            )
            raise
        state = "SUCCEEDED" if result.status == "succeeded" else "FAILED"
        self.approvals.finish_execution(
            execution.id,
            state,
            outcome=result,
            error_code=result.error_code,
        )
        return result

    def request_input(self, prompt, schema, *, run_id):
        return InformationRequest(
            str(uuid.uuid4()), "information_required", prompt, schema
        )
