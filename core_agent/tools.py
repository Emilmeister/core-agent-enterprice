from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from .errors import CoreError


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict
    mutating: bool
    risk_tags: frozenset[str]


CRON_CREATE_TOOL = ToolDefinition(
    "core_cron_create",
    "Create a recurring task in this chat. Use a five-field cron expression and an IANA timezone "
    "(default Europe/Moscow). Owner approval and current tool policy apply.",
    {"type": "object", "properties": {
        "prompt": {"type": "string", "minLength": 1},
        "expression": {"type": "string", "minLength": 1, "maxLength": 256},
        "timezone": {"type": "string", "minLength": 1, "maxLength": 256},
    }, "required": ["prompt", "expression"], "additionalProperties": False},
    mutating=True, risk_tags=frozenset({"external_write"}),
)


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


_JSON_TYPES = {
    dict: "object",
    list: "array",
    str: "string",
    bool: "boolean",
    int: "integer",
    float: "number",
    type(None): "null",
}


def _kind(value):
    return _JSON_TYPES.get(type(value), type(value).__name__)


def _reason(schema, value, path="arguments"):
    """Why `value` fails `schema`, or None when it does not.

    One function rather than a validator beside an explainer: the model is asked
    to correct its arguments, and a bare code tells it nothing to correct, so the
    reason has to come from the same walk that rejects. The path and the types
    are named; the value never is, because the value is request content.
    """
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} must be one of {sorted(map(str, schema['enum']))}"
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            return f"{path} must be an object, got {_kind(value)}"
        for key in schema.get("required", []):
            if key not in value:
                return f"{path}.{key} is required"
        if len(value) < schema.get("minProperties", 0):
            return f"{path} needs at least {schema['minProperties']} propert(y|ies)"
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", {})
        if additional is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                return f"{path} does not accept {', '.join(unknown)}"
        for key, item in value.items():
            if key in properties:
                reason = _reason(properties[key], item, f"{path}.{key}")
            elif isinstance(additional, dict):
                reason = _reason(additional, item, f"{path}.{key}")
            else:
                reason = None
            if reason:
                return reason
        return None
    if expected == "array":
        if not isinstance(value, list):
            return f"{path} must be an array, got {_kind(value)}"
        if len(value) < schema.get("minItems", 0):
            return f"{path} needs at least {schema['minItems']} item(s)"
        if len(value) > schema.get("maxItems", len(value)):
            return f"{path} accepts at most {schema['maxItems']} item(s)"
        for index, item in enumerate(value):
            reason = _reason(schema.get("items", {}), item, f"{path}[{index}]")
            if reason:
                return reason
        return None
    if expected == "string":
        if not isinstance(value, str):
            return f"{path} must be a string, got {_kind(value)}"
        if len(value) < schema.get("minLength", 0):
            return f"{path} is shorter than {schema['minLength']} characters"
        if len(value) > schema.get("maxLength", len(value)):
            return f"{path} is longer than {schema['maxLength']} characters"
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            return f"{path} does not match {schema['pattern']}"
        return None
    if expected in ("integer", "number"):
        types = (int,) if expected == "integer" else (int, float)
        if not isinstance(value, types) or isinstance(value, bool):
            return f"{path} must be {'an' if expected == 'integer' else 'a'} {expected}, got {_kind(value)}"
        if value < schema.get("minimum", value):
            return f"{path} must be at least {schema['minimum']}"
        if value > schema.get("maximum", value):
            return f"{path} must be at most {schema['maximum']}"
        return None
    if expected == "boolean" and not isinstance(value, bool):
        return f"{path} must be a boolean, got {_kind(value)}"
    return None


def _validate(schema, value):
    return _reason(schema, value) is None


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
        environment_manager,
        event_sink=None,
        handlers=None,
    ):
        self.registry = registry
        self.environment_manager = environment_manager
        self.event_sink = event_sink or (lambda event: None)
        self.handlers = dict(handlers or {})
        self.execution_count = 0

    def _emit(self, kind, **data):
        self.event_sink(RuntimeEvent(kind, data))

    @staticmethod
    def validate(call, definition):
        if _contains_private_reasoning(call.arguments):
            raise CoreError("TOOL_ARGUMENT_INVALID", "arguments carry private reasoning")
        reason = _reason(definition.input_schema, call.arguments)
        if reason:
            raise CoreError("TOOL_ARGUMENT_INVALID", reason)

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
        return self._execute(call, run_id)

    def _execute(self, call, run_id):
        self._emit("tool.started", call_id=call.id)
        self.execution_count += 1
        if call.name in self.handlers:
            result = self.handlers[call.name](call.arguments, run_id)
        else:
            request = (
                call.arguments
                if call.name == "core_terminal_exec"
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

    def request_input(self, prompt, schema, *, run_id):
        return InformationRequest(
            str(uuid.uuid4()), "information_required", prompt, schema
        )
