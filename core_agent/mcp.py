from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import uuid
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .errors import CoreError


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

    def call(self, name, arguments):
        return self.results.get(name, {})


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
            name, "initialized", 1, frozenset(f"{name}.{tool}" for tool in catalog)
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
                frozenset(f"{name}.{tool}" for tool in latest),
            )
        return self._snapshots[name]

    def call_tool(self, target, arguments):
        decision = self.policy("tool", target, arguments)
        if decision == "deny":
            raise CoreError("POLICY_DENIED")
        if decision == "require_approval":
            return McpResult("approval_required")
        return McpResult("succeeded", self.connector.call(target, arguments))

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

    def __init__(self, *, timeout=30, headers=None, protocol_versions=("2025-11-25",)):
        self.timeout = timeout
        self.headers = dict(headers or {})
        self._servers = {}
        self._connections = []
        self.protocol_versions = tuple(protocol_versions)
        self._negotiated_versions = {}

    @property
    def connections(self):
        return tuple(self._connections)

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
            raise CoreError("MCP_CONNECTION_FAILED")
        payload = {"jsonrpc": "2.0", "method": method}
        if not notification:
            payload["id"] = str(uuid.uuid4())
        if params is not None:
            payload["params"] = params
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Mcp-Method": method,
            **self.headers,
        }
        target_name = (params or {}).get("name") or (params or {}).get("uri")
        if target_name:
            headers["Mcp-Name"] = target_name
        if server in self._negotiated_versions:
            headers["MCP-Protocol-Version"] = self._negotiated_versions[server]
        request = Request(
            endpoint, data=json.dumps(payload).encode(), headers=headers, method="POST"
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                if notification:
                    return None
                value = json.load(response)
        except Exception as error:
            raise CoreError(
                "MCP_CONNECTION_FAILED", str(error), retryable=True
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
                "MCP_PROTOCOL_ERROR", "unsupported negotiated protocol version"
            )
        self._negotiated_versions[name] = negotiated
        self._rpc(name, "notifications/initialized", notification=True)
        result = self._rpc(name, "tools/list")
        self._connections.append(name)
        return {
            tool["name"]: tool.get("inputSchema", {})
            for tool in result.get("tools", [])
        }

    def call(self, target, arguments):
        server, tool = target.split(".", 1)
        return self._rpc(server, "tools/call", {"name": tool, "arguments": arguments})
