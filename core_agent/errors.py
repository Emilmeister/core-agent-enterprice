from dataclasses import dataclass, fields
import json
import logging
import math
from pathlib import Path
import re
from urllib.error import HTTPError, URLError

from httpx import HTTPStatusError
from requests.exceptions import HTTPError as RequestsHTTPError


_MODEL_FAILURE_VALUES = {
    "kind": frozenset({"http", "timeout", "transport", "response_too_large", "invalid_json",
                       "invalid_response", "no_text", "no_public_text", "unknown"}),
    "exception_type": frozenset({"HTTPError", "TimeoutError", "URLError", "HTTPException", "OSError",
                                 "JSONDecodeError", "UnicodeDecodeError", "KeyError", "IndexError", "TypeError", "CoreError"}),
    "response_format": frozenset({"none", "json_schema", "json_object"}),
    "finish_reason": frozenset({"stop", "length", "tool_calls", "function_call", "content_filter",
                                "end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn", "refusal"}),
    "request_phase": frozenset({"request", "response", "parse"}),
}


def model_failure_metadata(error):
    """Revalidate private adapter metadata at every operator logging boundary."""
    value = getattr(error, "model_failure", None)
    if type(value) is not dict:
        return {}
    result = {"kind": "unknown"}
    for key, allowed in _MODEL_FAILURE_VALUES.items():
        item = value.get(key)
        if type(item) is str and item in allowed:
            result[key] = item
    for key, lower, upper in (
        ("http_status", 100, 599), ("elapsed_ms", 0, 2**31 - 1),
        ("max_tokens", 1, 2**63 - 1),
        *((key, 0, 2**63 - 1) for key in (
            "prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens")),
    ):
        item = value.get(key)
        if type(item) is int and lower <= item <= upper:
            result[key] = item
    timeout = value.get("timeout_seconds")
    if type(timeout) in {int, float} and 0 < timeout <= 2**31 - 1 and math.isfinite(timeout):
        result["timeout_seconds"] = timeout
    reasoning = value.get("reasoning_available")
    if type(reasoning) is bool:
        result["reasoning_available"] = reasoning
    return result


@dataclass(frozen=True)
class SandboxDiagnostics:
    """Finite private operator metadata; never part of public error data."""
    stage: str
    reason: str
    elapsed_ms: int
    cleanup: str
    errno: int | None = None
    returncode: int | None = None
    slots_active: int | None = None
    slots_limit: int | None = None
    memory_current_bytes: int | None = None
    memory_limit_bytes: int | None = None
    pids_current: int | None = None
    pids_limit: int | None = None
    oom_kill_count: int | None = None

    def __post_init__(self):
        if (any(type(value) is not str for value in (self.stage, self.reason, self.cleanup))
                or self.stage not in {'native_check', 'queue', 'supervisor', 'namespace', 'network', 'target_exec', 'cleanup'}
                or self.reason not in {'timeout', 'child_exit', 'protocol_invalid', 'os_error', 'unavailable',
                    'unsupported', 'dependency_missing', 'policy_invalid', 'validation_failed', 'cancelled'}
                or self.cleanup not in {'not_started', 'confirmed', 'unconfirmed'}):
            raise ValueError('Invalid sandbox diagnostics')
        for field in fields(self):
            if field.name in {'stage', 'reason', 'cleanup'}:
                continue
            value = getattr(self, field.name)
            if value is None and field.name != 'elapsed_ms':
                continue
            lower, upper = (0, 2**63 - 1)
            if field.name == 'elapsed_ms':
                upper = 2**31 - 1
            elif field.name == 'errno':
                lower, upper = 1, 4095
            elif field.name == 'returncode':
                lower, upper = -255, 255
            elif field.name.endswith('_limit') or field.name == 'memory_limit_bytes':
                lower = 1
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError('Invalid sandbox diagnostics')

    def as_dict(self):
        return {field.name: getattr(self, field.name) for field in fields(self)
                if getattr(self, field.name) is not None}


class CoreError(Exception):
    def __init__(self, code, message=None, *, retryable=False, data=None, diagnostics=None):
        self.code = code
        self.message = message or code
        self.retryable = retryable
        self.data = data or {}
        self.diagnostics = diagnostics if type(diagnostics) is SandboxDiagnostics else None
        super().__init__(self.message)


class ExecutionNotStarted(CoreError):
    """Trusted local evidence that the requested command was never released."""


def error_log_fields(error):
    """Bounded private diagnostics, including suppressed causes, without content."""
    pending, seen, records = [error], set(), []
    while pending and len(records) < 6:
        current = pending.pop(0)
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        record = {"error_type": "Exception"}
        records.append(record)
        try:
            name = type(current).__name__
            if type(name) is str and re.fullmatch(r"[A-Za-z_]\w{0,63}", name, re.ASCII):
                record["error_type"] = name
            code = getattr(current, "code", None)
            if type(code) is str and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code):
                record["error_code"] = code
            retryable = getattr(current, "retryable", None)
            if type(retryable) is bool:
                record["retryable"] = retryable
            status = current.code if isinstance(current, HTTPError) else None
            if isinstance(current, (HTTPStatusError, RequestsHTTPError)):
                status = getattr(current.response, "status_code", None)
            if type(status) is int and 100 <= status <= 599:
                record["http_status"] = status
            number = getattr(current, "errno", None)
            if type(number) is int and 1 <= number <= 4095:
                record["errno"] = number
            sqlstate = getattr(current, "sqlstate", None)
            if type(sqlstate) is str and re.fullmatch(r"[0-9A-Z]{5}", sqlstate):
                record["sqlstate"] = sqlstate
            provider = model_failure_metadata(current)
            if provider:
                record["provider_failure"] = provider
            diagnostic = getattr(current, "diagnostics", None)
            if type(diagnostic) is SandboxDiagnostics:
                try:
                    record["sandbox_diagnostics"] = SandboxDiagnostics(
                        **SandboxDiagnostics.as_dict(diagnostic)).as_dict()
                except (TypeError, ValueError):
                    pass
            traceback, frames = current.__traceback__, []
            root = Path(__file__).parent
            for _ in range(64):
                if traceback is None:
                    break
                code = traceback.tb_frame.f_code
                path = Path(code.co_filename)
                if path.is_relative_to(root) and re.fullmatch(r"[A-Za-z_]\w{0,63}", code.co_name, re.ASCII):
                    frames.append({"filename": path.name, "function": code.co_name, "line": traceback.tb_lineno})
                traceback = traceback.tb_next
            if frames:
                record["traceback"] = frames[-8:]
            pending.extend((current.__cause__, current.__context__))
            if isinstance(current, URLError):
                pending.append(current.reason)
            if isinstance(current, BaseExceptionGroup):
                pending.extend(current.exceptions[:6])
        except Exception:
            # Diagnostics must not replace the original failure, including when
            # a foreign exception has a broken attribute/property implementation.
            pass
    if not records:
        return {}
    result = records[0]
    if len(records) > 1:
        result["causes"] = records[1:]
        for record in records[1:]:
            for name in ("provider_failure", "sandbox_diagnostics"):
                if name in record:
                    result.setdefault(name, record[name])
    return result


def log_error(event, error, *, logger=None, level=logging.WARNING, **fields):
    """Best-effort private error log; call-site fields must contain safe metadata."""
    try:
        fields = {key: value for key, value in fields.items()
                  if not key.endswith("_id") or
                  (type(value) is str and re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", value))}
        payload = {"event": event, **error_log_fields(error), **fields}
        target = logger if logger is not None else logging.getLogger("core_agent.runtime")
        target.log(level, json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False))
    except Exception:
        # A broken handler or diagnostic must not change the original outcome.
        pass
