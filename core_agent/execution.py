from __future__ import annotations

import ipaddress
import json
import uuid
from dataclasses import dataclass, replace
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .errors import CoreError
from .security import redact


@dataclass(frozen=True)
class ExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    artifacts: tuple
    side_effects: tuple


@dataclass(frozen=True)
class EnvironmentSpec:
    tenant_id: str
    run_id: str
    workspace_snapshot: str
    writable_paths: tuple[str, ...]
    network_allowlist: tuple[str, ...]
    secrets: dict
    parent_run_id: str | None = None
    overlay_id: str | None = None
    host_root_mounted: bool = False
    runtime_socket_mounted: bool = False
    ssh_agent_forwarded: bool = False
    cloud_metadata_access: bool = False
    network_mode: str = "deny"
    privileged: bool = False

    def hardened(self):
        return replace(
            self,
            host_root_mounted=False,
            runtime_socket_mounted=False,
            ssh_agent_forwarded=False,
            cloud_metadata_access=False,
            network_mode="allowlist" if self.network_allowlist else "deny",
            privileged=False,
        )

    def checkpoint_dict(self):
        return {
            "tenant_id": self.tenant_id,
            "run_id": self.run_id,
            "parent_run_id": self.parent_run_id,
            "workspace_snapshot": self.workspace_snapshot,
            "writable_paths": self.writable_paths,
            "network_allowlist": self.network_allowlist,
            "overlay_id": self.overlay_id,
        }


class ExecutionEnvironmentManager:
    REQUIRED = {
        "mount_namespace",
        "process_namespace",
        "user_namespace",
        "network_namespace",
        "immutable_image",
        "resource_limits",
        "process_tree_teardown",
    }

    def __init__(self, backend):
        if not getattr(backend, "isolated", False) or not self.REQUIRED <= set(
            getattr(backend, "capabilities", set())
        ):
            raise CoreError("EXECUTION_ENVIRONMENT_UNAVAILABLE")
        self.backend = backend
        self._environments = {}
        self.telemetry_records = []

    def create(self, spec):
        hardened = replace(
            spec.hardened(), overlay_id=spec.overlay_id or str(uuid.uuid4())
        )
        environment = self.backend.create(hardened)
        self._environments[environment.id] = environment
        self.telemetry_records.append(
            {
                "event": "environment.created",
                "id": environment.id,
                "run_id": spec.run_id,
            }
        )
        return environment

    def execute(self, environment_id, request):
        try:
            environment = self._environments[environment_id]
        except KeyError:
            raise CoreError("EXECUTION_ENVIRONMENT_UNAVAILABLE") from None
        materialized = dict(request)
        refs = materialized.pop("secret_refs", [])
        if refs:
            materialized["secrets"] = {
                name: environment.spec.secrets[name]
                for name in refs
                if name in environment.spec.secrets
            }
        result = environment.execute(materialized)
        self.telemetry_records.append(
            {"event": "tool.executed", "environment_id": environment_id}
        )
        return ExecutionResult(
            result.exit_code,
            redact(result.stdout, set(environment.spec.secrets.values())),
            redact(result.stderr, set(environment.spec.secrets.values())),
            result.artifacts,
            result.side_effects,
        )

    def execute_transient(self, request, run_id):
        environment = self.create(
            EnvironmentSpec("default", run_id, "runtime", ("/workspace",), (), {})
        )
        try:
            return self.execute(environment.id, request)
        finally:
            self.destroy(environment.id)

    def destroy(self, environment_id):
        environment = self._environments.pop(environment_id)
        environment.destroy()


class EgressPolicy:
    def __init__(self, allowed_hosts):
        self.allowed_hosts = set(allowed_hosts)

    def allow(self, url, *, resolved_ip):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in self.allowed_hosts:
            raise CoreError("POLICY_DENIED")
        address = ipaddress.ip_address(resolved_ip)
        blocked = [
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
            ipaddress.ip_network("127.0.0.0/8"),
            ipaddress.ip_network("169.254.0.0/16"),
            ipaddress.ip_network("::1/128"),
            ipaddress.ip_network("fc00::/7"),
            ipaddress.ip_network("fe80::/10"),
        ]
        if any(
            address in network
            for network in blocked
            if address.version == network.version
        ):
            raise CoreError("POLICY_DENIED")
        return True

    def follow_redirect(self, source, target, *, resolved_ip):
        return self.allow(target, resolved_ip=resolved_ip)


class RemoteExecutionBackend:
    """Execution-plane adapter; user commands run behind a remote sandbox RPC boundary."""

    isolated = True
    capabilities = ExecutionEnvironmentManager.REQUIRED

    def __init__(self, endpoint, *, authorization=None, timeout=30):
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.hostname:
            raise CoreError("EXECUTION_ENVIRONMENT_UNAVAILABLE")
        self.endpoint = endpoint.rstrip("/")
        self.authorization = authorization
        self.timeout = timeout

    def _call(self, path, payload):
        headers = {"Content-Type": "application/json"}
        if self.authorization:
            headers["Authorization"] = self.authorization
        request = Request(
            self.endpoint + path,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except Exception as error:
            raise CoreError(
                "EXECUTION_ENVIRONMENT_UNAVAILABLE", str(error), retryable=True
            ) from error

    def create(self, spec):
        payload = spec.checkpoint_dict()
        payload["secret_refs"] = sorted(spec.secrets)
        response = self._call("/v1/environments", payload)
        if not response.get("environment_id"):
            raise CoreError("EXECUTION_ENVIRONMENT_UNAVAILABLE")
        return _RemoteEnvironment(self, response["environment_id"], spec)


class _RemoteEnvironment:
    def __init__(self, backend, environment_id, spec):
        self.backend = backend
        self.id = environment_id
        self.spec = spec

    def execute(self, request):
        response = self.backend._call(f"/v1/environments/{self.id}/execute", request)
        return ExecutionResult(
            int(response.get("exit_code", 1)),
            str(response.get("stdout", "")),
            str(response.get("stderr", "")),
            tuple(response.get("artifacts", ())),
            tuple(response.get("side_effects", ())),
        )

    def destroy(self):
        self.backend._call(f"/v1/environments/{self.id}/destroy", {})
