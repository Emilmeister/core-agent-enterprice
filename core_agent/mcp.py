from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import ipaddress
import json
import math
import re
import ssl
import threading
import time
import uuid
from urllib.parse import urlparse

import httpx

from .errors import CoreError
from .security import redact


# Published MCP revisions this client interoperates with, newest first: the first
# entry is what we propose, the rest are what we still accept from a server.
MCP_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
RETRYABLE_HTTP_STATUSES = frozenset({408, 429, *range(500, 600)})
RESERVED_MCP_HEADERS = frozenset(
    {
        "accept",
        "content-type",
        "mcp-method",
        "mcp-name",
        "mcp-protocol-version",
        "mcp-session-id",
    }
)


def _cancel_error(cancel_event):
    return getattr(cancel_event, "error_code", "TASK_CANCELLED")


def _credential_safe(value, headers):
    secrets = tuple({secret for item in headers.values() if isinstance(item, str)
                     for secret in (item, item.removeprefix("Bearer ")) if secret})
    def clean(item):
        if isinstance(item, dict):
            return {redact(key, secrets): clean(content) for key, content in item.items()}
        if isinstance(item, list):
            return [clean(content) for content in item]
        return redact(item, secrets) if isinstance(item, str) else item
    return clean(value)


def mcp_tool_name(server, tool):
    """The canonical name of one MCP tool, ready for any model API.

    A dot cannot separate server from tool here: a server is free to publish a
    tool whose own name contains one, and the split would then name the wrong
    server. The mapping back to `(server, tool)` is kept as an index, so the
    name itself carries no structure that has to be parsed.
    """
    return re.sub(r"[^A-Za-z0-9_-]", "_", f"{server}_{tool}")


def mcp_tool_index(mcp_tools, *, reserved_names=()):
    """Canonical name -> (server, tool) for every allowed MCP tool."""
    index = {}
    for server, tools in mcp_tools.items():
        for tool in tools:
            name = mcp_tool_name(server, tool)
            if name in reserved_names:
                raise CoreError(
                    "TOOL_NAME_COLLISION",
                    f"{name} collides with a runtime tool",
                )
            if index.setdefault(name, (server, tool)) != (server, tool):
                raise CoreError(
                    "TOOL_NAME_COLLISION",
                    f"{name} names more than one MCP tool",
                )
    return index


@dataclass(frozen=True)
class McpWarning:
    code: str
    message: str = ""


@dataclass(frozen=True)
class McpSnapshot:
    name: str
    protocol_state: str
    catalog_revision: int
    tools: frozenset[str]
    warning: McpWarning | None = None


@dataclass(frozen=True)
class McpResult:
    status: str
    value: object = None
    trusted_instructions: bool = False
    route: str | None = None
    kind: str | None = None


class InMemoryMcpConnector:
    def __init__(
        self,
        *,
        catalogs=None,
        capabilities=None,
        fail_connections=None,
        resources=None,
        prompts=None,
        results=None,
    ):
        self.catalogs = catalogs or {}
        self.capabilities = capabilities or {}
        self.fail_connections = set(fail_connections or ())
        self.resources = resources or {}
        self.prompts = prompts or {}
        self.results = results or {}
        self._connections = []

    cold_start_timeout = 0.0

    def for_run(self):
        return self

    @property
    def connections(self):
        return tuple(self._connections)

    def connect(self, declaration, *, cancel_event=None, deadline=None):
        if cancel_event is not None and cancel_event.is_set():
            raise CoreError(_cancel_error(cancel_event))
        name = declaration["name"]
        if name in self.fail_connections:
            raise CoreError("MCP_CONNECTION_FAILED")
        self._connections.append(name)
        return dict(self.catalogs.get(name, {}))

    def update_catalog(self, name, catalog):
        self.catalogs[name] = dict(catalog)

    def call(self, server, tool, arguments):
        return self.results.get(f"{server}.{tool}", {})


def _failure_reason(error):
    """One short, redacted line naming what actually failed."""
    if isinstance(error, httpx.HTTPStatusError):
        return f"http status {error.response.status_code}"
    if _caused_by(error, ssl.SSLError):
        return "TLS error"
    known = (
        (ConnectionRefusedError, "Connection refused"),
        (ConnectionResetError, "Connection reset"),
        (ConnectionAbortedError, "Connection aborted"),
        (BrokenPipeError, "Broken pipe"),
        (TimeoutError, "Connection timed out"),
    )
    for kind, message in known:
        if _caused_by(error, kind):
            return message
    return redact(type(error).__name__)[:200]


def _caused_by(error, classes):
    seen = set()
    while error is not None and id(error) not in seen:
        if isinstance(error, classes):
            return True
        seen.add(id(error))
        error = error.__cause__ or error.__context__
    return False


def _retryable_transport_error(error):
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in RETRYABLE_HTTP_STATUSES
    if _caused_by(error, ssl.SSLError):
        return False
    return isinstance(
        error,
        (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError),
    )


def _validated_timeout(value, name, *, allow_zero=False):
    if isinstance(value, bool):
        raise CoreError("CONFIG_INVALID", f"{name} must be numeric")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise CoreError("CONFIG_INVALID", f"{name} must be numeric") from None
    if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        sign = "non-negative" if allow_zero else "positive"
        raise CoreError("CONFIG_INVALID", f"{name} must be finite and {sign}")
    return value


def _run_async(factory):
    """Run one coroutine behind the connector's synchronous public API."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())

    outcome = []

    def run():
        try:
            outcome.append((True, asyncio.run(factory())))
        except BaseException as error:
            outcome.append((False, error))

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    succeeded, value = outcome[0]
    if not succeeded:
        raise value
    return value


class McpManager:
    def __init__(self, connector, *, allowed_servers, policy_evaluator=None):
        self.connector = connector
        self.allowed_servers = set(allowed_servers)
        self.policy = policy_evaluator or (lambda kind, target, payload: "allow")
        self._snapshots = {}
        self._catalog_values = {}

    def connect(self, declaration):
        name = declaration["name"]
        if name not in self.allowed_servers:
            raise CoreError("CAPABILITY_DISABLED")
        try:
            catalog = self.connector.connect(declaration)
        except CoreError as error:
            if declaration.get("required"):
                raise
            snapshot = McpSnapshot(
                name, "disabled", 0, frozenset(), McpWarning(error.code)
            )
            self._snapshots[name] = snapshot
            return snapshot
        self._catalog_values[name] = catalog
        snapshot = McpSnapshot(
            name,
            "initialized",
            1,
            frozenset(mcp_tool_name(name, tool) for tool in catalog),
        )
        self._snapshots[name] = snapshot
        return snapshot

    def snapshot(self, name):
        return self._snapshots[name]

    def accept_notifications_at_safe_point(self, name):
        latest = dict(self.connector.catalogs.get(name, {}))
        if latest != self._catalog_values.get(name):
            self._catalog_values[name] = latest
            old = self._snapshots[name]
            self._snapshots[name] = McpSnapshot(
                name,
                "initialized",
                old.catalog_revision + 1,
                frozenset(mcp_tool_name(name, tool) for tool in latest),
            )
        return self._snapshots[name]

    def call_tool(self, target, arguments):
        decision = self.policy("tool", target, arguments)
        if decision == "deny":
            raise CoreError("POLICY_DENIED")
        if decision == "require_approval":
            return McpResult("approval_required")
        server, _, tool = target.partition(".")
        return McpResult("succeeded", self.connector.call(server, tool, arguments))

    def read_resource(self, server, uri):
        if self.policy("resource", uri, {}) == "deny":
            raise CoreError("POLICY_DENIED")
        return McpResult(
            "succeeded", self.connector.resources.get(uri), trusted_instructions=False
        )

    def get_prompt(self, server, name, arguments):
        if self.policy("prompt", name, arguments) == "deny":
            raise CoreError("POLICY_DENIED")
        return McpResult(
            "succeeded", self.connector.prompts.get(name), trusted_instructions=False
        )

    def sample(self, server, payload, *, route):
        if self.policy("sampling", server, payload) == "deny":
            raise CoreError("POLICY_DENIED")
        return McpResult("succeeded", route=route)

    def elicit(self, server, schema):
        if self.policy("elicitation", server, schema) == "deny":
            raise CoreError("POLICY_DENIED")
        return McpResult("input_required", kind="information_required")


class StreamableHttpMcpConnector:
    """Minimal MCP Streamable HTTP client with initialize/discovery and JSON-RPC calls."""

    def __init__(
        self,
        *,
        timeout=30,
        sse_read_timeout=300,
        cold_start_timeout=300,
        headers=None,
        protocol_versions=MCP_PROTOCOL_VERSIONS,
        telemetry=None,
    ):
        self.timeout = _validated_timeout(timeout, "MCP timeout")
        # A Streamable HTTP server may answer with an event stream and hold the
        # connection open far longer than a single request timeout allows.
        self.sse_read_timeout = _validated_timeout(
            sse_read_timeout, "MCP SSE read timeout"
        )
        self.cold_start_timeout = _validated_timeout(
            cold_start_timeout, "MCP cold-start timeout", allow_zero=True
        )
        self.headers = dict(headers or {})
        if any(
            not isinstance(name, str) or name.lower() in RESERVED_MCP_HEADERS
            for name in self.headers
        ):
            raise CoreError(
                "CONFIG_INVALID",
                "MCP custom headers cannot override transport-managed headers",
            )
        self._servers = {}
        self._connections = []
        self.protocol_versions = tuple(protocol_versions)
        self._negotiated_versions = {}
        # Streamable HTTP servers hand out a session on initialize and
        # reject later requests that do not carry it back.
        self._sessions = {}
        self.telemetry = telemetry

    def for_run(self):
        """Return transport state owned by one workflow run."""
        return type(self)(
            timeout=self.timeout,
            sse_read_timeout=self.sse_read_timeout,
            cold_start_timeout=self.cold_start_timeout,
            headers=self.headers,
            protocol_versions=self.protocol_versions,
            telemetry=self.telemetry,
        )

    @property
    def connections(self):
        return tuple(self._connections)

    async def _read_event_stream(
        self, response, request_id, *, deadline=None, cancel_event=None
    ):
        """Take the JSON-RPC payload answering `request_id` from an SSE response.

        A Streamable HTTP server may answer either with plain JSON or with an
        event stream; both carry the same envelope. The stream is not only the
        answer: the server may put progress notifications, logs and its own
        requests in front of it, and a slow tool nearly always does. Those carry
        no `id`, so taking the first frame returns a result-less envelope — an
        empty output reported to the model as success, which reads exactly like
        "the server found nothing".
        """
        read_deadline = time.monotonic() + self.sse_read_timeout
        if deadline is not None:
            read_deadline = min(read_deadline, deadline)
        data = []

        def answering(payload):
            text = payload.strip()
            if not text or text == "[DONE]":
                return None
            frame = json.loads(text)
            if not isinstance(frame, dict):
                raise CoreError(
                    "MCP_PROTOCOL_ERROR", "event stream carried an invalid frame"
                )
            return frame if frame.get("id") == request_id else None

        async for raw in response.aiter_lines():
            self._raise_if_cancelled(cancel_event)
            if time.monotonic() >= read_deadline:
                raise CoreError(
                    "MCP_CONNECTION_FAILED",
                    "event stream read deadline expired",
                    retryable=True,
                )
            line = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
            if line.startswith("data:"):
                data.append(line[len("data:") :].removeprefix(" "))
                continue
            if line:
                continue
            frame = answering("\n".join(data))
            data = []
            if frame is not None:
                return frame
        frame = answering("\n".join(data))
        if frame is not None:
            return frame
        if deadline is not None and time.monotonic() >= deadline:
            raise CoreError(
                "MCP_CONNECTION_FAILED",
                "event stream cold-start deadline expired",
                retryable=True,
            )
        raise CoreError("MCP_PROTOCOL_ERROR", "event stream carried no result")

    @staticmethod
    def _validate_envelope(value, request_id, method):
        if (
            not isinstance(value, dict)
            or value.get("jsonrpc") != "2.0"
            or value.get("id") != request_id
            or (("result" in value) == ("error" in value))
        ):
            raise CoreError(
                "MCP_PROTOCOL_ERROR", f"{method}: invalid JSON-RPC response"
            )
        if "error" in value and not isinstance(value["error"], dict):
            raise CoreError("MCP_PROTOCOL_ERROR", f"{method}: invalid JSON-RPC error")
        if "error" in value and (
            not isinstance(value["error"].get("code"), int)
            or isinstance(value["error"].get("code"), bool)
            or not isinstance(value["error"].get("message"), str)
        ):
            raise CoreError("MCP_PROTOCOL_ERROR", f"{method}: invalid JSON-RPC error")

    def _rpc(
        self,
        server,
        method,
        params=None,
        *,
        notification=False,
        deadline=None,
        cancel_event=None,
    ):
        self._raise_if_cancelled(cancel_event)
        declaration = self._servers[server]
        endpoint = declaration["transport"]["url"]
        parsed = urlparse(endpoint)
        loopback = parsed.hostname == "localhost"
        try:
            loopback = (
                loopback or ipaddress.ip_address(parsed.hostname or "").is_loopback
            )
        except ValueError:
            pass
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise CoreError(
                "MCP_CONNECTION_FAILED",
                f"{parsed.scheme or 'missing'} scheme is not allowed; use https",
            )
        span = (
            self.telemetry.span(
                "mcp.client",
                attributes={
                    "rpc.system": "jsonrpc",
                    "rpc.method": method,
                    "server.address": parsed.hostname or "unknown",
                },
            )
            if self.telemetry
            else contextlib.nullcontext()
        )
        with span as active_span:
            call_params = dict(params or {})
            payload = {"jsonrpc": "2.0", "method": method}
            if not notification:
                payload["id"] = str(uuid.uuid4())
            if params is not None:
                payload["params"] = call_params
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Mcp-Method": method,
                **declaration.get("headers", self.headers),
            }
            if self.telemetry:
                carrier = {}
                self.telemetry.inject(active_span.context, carrier)
                headers.update(carrier)
                call_params["_meta"] = carrier
            target_name = call_params.get("name") or call_params.get("uri")
            if target_name:
                headers["Mcp-Name"] = target_name
            if server in self._negotiated_versions:
                headers["MCP-Protocol-Version"] = self._negotiated_versions[server]
            if server in self._sessions:
                headers["Mcp-Session-Id"] = self._sessions[server]
            request_deadline = time.monotonic() + self.timeout
            if deadline is not None:
                request_deadline = min(request_deadline, deadline)
            active_deadline = [request_deadline]

            async def exchange():
                transport_timeout = httpx.Timeout(
                    connect=request_timeout,
                    read=None,
                    write=request_timeout,
                    pool=request_timeout,
                )
                async with httpx.AsyncClient(timeout=transport_timeout) as client:
                    async with client.stream(
                        "POST",
                        endpoint,
                        content=json.dumps(payload).encode(),
                        headers=headers,
                    ) as response:
                        response.raise_for_status()
                        session = response.headers.get("Mcp-Session-Id")
                        if session:
                            self._sessions[server] = session
                        if notification:
                            return None
                        if "text/event-stream" in (
                            response.headers.get("Content-Type") or ""
                        ):
                            active_deadline[0] = min(
                                deadline if deadline is not None else math.inf,
                                time.monotonic() + self.sse_read_timeout,
                            )
                            return await self._read_event_stream(
                                response,
                                payload.get("id"),
                                deadline=active_deadline[0],
                                cancel_event=cancel_event,
                            )
                        return json.loads(
                            b"".join([chunk async for chunk in response.aiter_bytes()])
                        )

            async def request():
                task = asyncio.create_task(exchange())
                try:
                    while True:
                        self._raise_if_cancelled(cancel_event)
                        remaining = active_deadline[0] - time.monotonic()
                        if remaining <= 0:
                            raise CoreError(
                                "MCP_CONNECTION_FAILED",
                                f"{method}: request deadline expired",
                                retryable=True,
                            )
                        done, _pending = await asyncio.wait(
                            (task,), timeout=min(0.05, remaining)
                        )
                        if done:
                            return await task
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

            try:
                self._raise_if_cancelled(cancel_event)
                request_timeout = self.timeout
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CoreError(
                            "MCP_CONNECTION_FAILED",
                            f"{method}: cold-start deadline expired",
                            retryable=True,
                        )
                    request_timeout = min(request_timeout, remaining)
                value = _run_async(request)
            except CoreError:
                raise
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise CoreError(
                    "MCP_PROTOCOL_ERROR", f"{method}: invalid JSON response"
                ) from error
            except Exception as error:
                raise CoreError(
                    "MCP_CONNECTION_FAILED",
                    f"{method}: {_failure_reason(error)}",
                    retryable=_retryable_transport_error(error),
                ) from error
            self._raise_if_cancelled(cancel_event)
            if deadline is not None and time.monotonic() >= deadline:
                raise CoreError(
                    "MCP_CONNECTION_FAILED",
                    f"{method}: cold-start deadline expired",
                    retryable=True,
                )
        if notification:
            return None
        self._validate_envelope(value, payload["id"], method)
        value = _credential_safe(value, declaration.get("headers", self.headers))
        if "error" in value:
            raise CoreError("MCP_PROTOCOL_ERROR", data=value["error"])
        return value["result"]

    def _connect_once(self, name, *, deadline=None, cancel_event=None):
        initialized = self._rpc(
            name,
            "initialize",
            {
                "protocolVersion": self.protocol_versions[0],
                "capabilities": {},
                "clientInfo": {"name": "core-agent", "version": "1.0"},
            },
            deadline=deadline,
            cancel_event=cancel_event,
        )
        if not isinstance(initialized, dict):
            raise CoreError(
                "MCP_PROTOCOL_ERROR", "initialize: result must be an object"
            )
        negotiated = initialized.get("protocolVersion", self.protocol_versions[0])
        if negotiated not in self.protocol_versions:
            raise CoreError(
                "MCP_PROTOCOL_ERROR",
                f"server negotiated protocol {negotiated!r}; "
                f"this client accepts {', '.join(self.protocol_versions)}",
            )
        self._negotiated_versions[name] = negotiated
        self._rpc(
            name,
            "notifications/initialized",
            notification=True,
            deadline=deadline,
            cancel_event=cancel_event,
        )
        result = self._rpc(
            name, "tools/list", deadline=deadline, cancel_event=cancel_event
        )
        if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
            raise CoreError("MCP_PROTOCOL_ERROR", "tools/list: tools must be an array")
        catalog = {}
        for tool in result["tools"]:
            if (
                not isinstance(tool, dict)
                or not isinstance(tool.get("name"), str)
                or not tool["name"]
                or not isinstance(tool.get("inputSchema", {}), dict)
            ):
                raise CoreError(
                    "MCP_PROTOCOL_ERROR", "tools/list: invalid tool descriptor"
                )
            catalog[tool["name"]] = tool.get("inputSchema", {})
        return catalog

    def _cold_start_failure(self, name, error, *, timeout_seconds=None):
        timeout_seconds = (
            self.cold_start_timeout if timeout_seconds is None else timeout_seconds
        )
        return CoreError(
            "MCP_CONNECTION_FAILED",
            f"{name} did not become ready within {timeout_seconds:g}s; "
            f"last failure: {error}",
            retryable=True,
            data={
                "reason": "cold_start_timeout",
                "timeout_seconds": timeout_seconds,
                "last_error_code": error.code,
            },
        )

    @staticmethod
    def _raise_if_cancelled(cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            raise CoreError(_cancel_error(cancel_event))

    def connect(self, declaration, *, cancel_event=None, deadline=None):
        name = declaration["name"]
        self._servers[name] = declaration
        one_shot = deadline == 0.0
        if deadline is None and self.cold_start_timeout > 0:
            deadline = time.monotonic() + self.cold_start_timeout
        delay = 1.0
        while True:
            self._raise_if_cancelled(cancel_event)
            self._sessions.pop(name, None)
            self._negotiated_versions.pop(name, None)
            try:
                catalog = self._connect_once(
                    name,
                    deadline=None if one_shot else deadline,
                    cancel_event=cancel_event,
                )
            except CoreError as error:
                if error.code != "MCP_CONNECTION_FAILED" or not error.retryable:
                    self._sessions.pop(name, None)
                    self._negotiated_versions.pop(name, None)
                    raise
                if one_shot:
                    self._sessions.pop(name, None)
                    self._negotiated_versions.pop(name, None)
                    raise self._cold_start_failure(
                        name, error, timeout_seconds=0.0
                    ) from error
                if deadline is None:
                    self._sessions.pop(name, None)
                    self._negotiated_versions.pop(name, None)
                    raise self._cold_start_failure(name, error) from error
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._sessions.pop(name, None)
                    self._negotiated_versions.pop(name, None)
                    raise self._cold_start_failure(name, error) from error
                wait = min(delay, remaining)
                if cancel_event is not None:
                    if cancel_event.wait(wait):
                        raise CoreError(_cancel_error(cancel_event)) from error
                else:
                    time.sleep(wait)
                delay = min(delay * 2, 8.0)
                continue
            self._raise_if_cancelled(cancel_event)
            self._connections.append(name)
            return catalog

    def call(self, server, tool, arguments):
        return self._rpc(server, "tools/call", {"name": tool, "arguments": arguments})

    def close(self):
        # Every RPC owns and closes its HTTP client; there is no live transport here.
        pass
