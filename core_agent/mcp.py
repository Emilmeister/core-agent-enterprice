from __future__ import annotations

import contextlib
from dataclasses import dataclass
import ipaddress
import json
import re
import time
import uuid
from urllib.parse import urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .errors import CoreError
from .security import redact


# Published MCP revisions this client interoperates with, newest first: the first
# entry is what we propose, the rest are what we still accept from a server.
MCP_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


def mcp_tool_name(server, tool):
    """The canonical name of one MCP tool, ready for any model API.

    A dot cannot separate server from tool here: a server is free to publish a
    tool whose own name contains one, and the split would then name the wrong
    server. The mapping back to `(server, tool)` is kept as an index, so the
    name itself carries no structure that has to be parsed.
    """
    return re.sub(r"[^A-Za-z0-9_-]", "_", f"{server}_{tool}")


def mcp_tool_index(mcp_tools):
    """Canonical name -> (server, tool) for every allowed MCP tool."""
    index = {}
    for server, tools in mcp_tools.items():
        for tool in tools:
            name = mcp_tool_name(server, tool)
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

    @property
    def connections(self):
        return tuple(self._connections)

    def connect(self, declaration):
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
    if isinstance(error, HTTPError):
        return f"http status {error.code}"
    reason = getattr(error, "reason", None) or error
    return redact(f"{type(error).__name__}: {reason}")[:200]


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
            name, "initialized", 1, frozenset(mcp_tool_name(name, tool) for tool in catalog)
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
        headers=None,
        protocol_versions=MCP_PROTOCOL_VERSIONS,
        telemetry=None,
    ):
        self.timeout = timeout
        # A Streamable HTTP server may answer with an event stream and hold the
        # connection open far longer than a single request timeout allows.
        self.sse_read_timeout = sse_read_timeout
        self.headers = dict(headers or {})
        self._servers = {}
        self._connections = []
        self.protocol_versions = tuple(protocol_versions)
        self._negotiated_versions = {}
        # Streamable HTTP servers hand out a session on initialize and
        # reject later requests that do not carry it back.
        self._sessions = {}
        self.telemetry = telemetry

    @property
    def connections(self):
        return tuple(self._connections)

    def _read_event_stream(self, response, request_id):
        """Take the JSON-RPC payload answering `request_id` from an SSE response.

        A Streamable HTTP server may answer either with plain JSON or with an
        event stream; both carry the same envelope. The stream is not only the
        answer: the server may put progress notifications, logs and its own
        requests in front of it, and a slow tool nearly always does. Those carry
        no `id`, so taking the first frame returns a result-less envelope — an
        empty output reported to the model as success, which reads exactly like
        "the server found nothing".
        """
        deadline = time.monotonic() + self.sse_read_timeout
        data = []

        def answering(payload):
            text = payload.strip()
            if not text or text == "[DONE]":
                return None
            frame = json.loads(text)
            return frame if frame.get("id") == request_id else None

        while time.monotonic() < deadline:
            raw = response.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
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
        raise CoreError("MCP_PROTOCOL_ERROR", "event stream carried no result")

    def _rpc(self, server, method, params=None, *, notification=False):
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
                **self.headers,
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
            request = Request(
                endpoint,
                data=json.dumps(payload).encode(),
                headers=headers,
                method="POST",
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    session = response.headers.get("Mcp-Session-Id")
                    if session:
                        self._sessions[server] = session
                    if notification:
                        return None
                    if "text/event-stream" in (
                        response.headers.get("Content-Type") or ""
                    ):
                        value = self._read_event_stream(response, payload.get("id"))
                    else:
                        value = json.load(response)
            except Exception as error:
                # The generic code alone cannot separate a wrong URL from a TLS
                # refusal, an unreachable host or a rejected protocol.
                raise CoreError(
                    "MCP_CONNECTION_FAILED",
                    f"{method}: {_failure_reason(error)}",
                    retryable=True,
                ) from error
        if "error" in value:
            raise CoreError("MCP_PROTOCOL_ERROR", data=value["error"])
        return value.get("result", {})

    def connect(self, declaration):
        name = declaration["name"]
        self._servers[name] = declaration
        initialized = self._rpc(
            name,
            "initialize",
            {
                "protocolVersion": self.protocol_versions[0],
                "capabilities": {},
                "clientInfo": {"name": "core-agent", "version": "1.0"},
            },
        )
        negotiated = initialized.get("protocolVersion", self.protocol_versions[0])
        if negotiated not in self.protocol_versions:
            raise CoreError(
                "MCP_PROTOCOL_ERROR",
                f"server negotiated protocol {negotiated!r}; "
                f"this client accepts {', '.join(self.protocol_versions)}",
            )
        self._negotiated_versions[name] = negotiated
        self._rpc(name, "notifications/initialized", notification=True)
        result = self._rpc(name, "tools/list")
        self._connections.append(name)
        return {
            tool["name"]: tool.get("inputSchema", {})
            for tool in result.get("tools", [])
        }

    def call(self, server, tool, arguments):
        return self._rpc(server, "tools/call", {"name": tool, "arguments": arguments})
