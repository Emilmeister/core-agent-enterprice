from __future__ import annotations

import errno
import hashlib
import ipaddress
import json
import os
import pty
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlparse

from .errors import CoreError
from .security import redact


@dataclass(frozen=True)
class ExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    artifacts: tuple
    side_effects: tuple
    status: str = "succeeded"
    terminal_session_id: str | None = None
    process_group_id: int | None = None
    used_pty: bool = False
    timed_out: bool = False
    truncated: bool = False
    cleanup: str = "process_exited"
    duration: float = 0.0


@dataclass(frozen=True)
class EnvironmentSpec:
    tenant_id: str
    run_id: str
    workspace_snapshot: str
    writable_paths: tuple[str, ...]
    network_allowlist: tuple[str, ...]
    secrets: dict
    owner_id: str | None = None
    parent_run_id: str | None = None
    durable_root: str | None = None
    environment_allowlist: tuple[str, ...] = ()
    max_output_bytes: int = 1_000_000
    session_id: str | None = None
    workspace_id: str | None = None
    isolation_level: str = "process"
    os_security_boundary: bool = False
    runtime_socket_mounted: bool = False
    service_account_token_mounted: bool = False

    def hardened(self):
        return replace(
            self,
            owner_id=self.owner_id or self.run_id,
            isolation_level="process",
            os_security_boundary=False,
            runtime_socket_mounted=False,
            service_account_token_mounted=False,
        )

    def checkpoint_dict(self):
        return {
            "tenant_id": self.tenant_id,
            "run_id": self.run_id,
            "owner_id": self.owner_id,
            "parent_run_id": self.parent_run_id,
            "workspace_snapshot": self.workspace_snapshot,
            "writable_paths": self.writable_paths,
            "network_allowlist": self.network_allowlist,
            "session_id": self.session_id,
            "workspace_id": self.workspace_id,
            "isolation_level": self.isolation_level,
            "os_security_boundary": self.os_security_boundary,
        }


@dataclass
class ProcessHandle:
    id: str
    terminal_session_id: str
    process_group_id: int
    state: str = "running"
    _process: subprocess.Popen = field(repr=False, default=None)
    _master_fd: int = field(repr=False, default=-1)
    _max_output_bytes: int = field(repr=False, default=1_000_000)
    _started_at: float = field(repr=False, default_factory=time.monotonic)
    _timeout: float | None = field(repr=False, default=None)
    _output: bytearray = field(repr=False, default_factory=bytearray)
    _done: threading.Event = field(repr=False, default_factory=threading.Event)
    _lock: threading.Lock = field(repr=False, default_factory=threading.Lock)
    _truncated: bool = field(repr=False, default=False)
    _timed_out: bool = field(repr=False, default=False)
    _cleanup: str = field(repr=False, default="process_exited")
    _result: ExecutionResult | None = field(repr=False, default=None)


@dataclass(frozen=True)
class WorkspaceSnapshot:
    id: str
    parent_id: str | None
    files: dict


class WorkspaceSnapshotStore:
    """Immutable content-addressed snapshots for an S3-backed mounted path."""

    def __init__(self, root):
        self.root = Path(root).absolute()
        self.blobs = self.root / "blobs" / "sha256"
        self.snapshots = self.root / "snapshots"
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.snapshots.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _digest(content):
        return hashlib.sha256(content).hexdigest()

    @staticmethod
    def _relative(path):
        value = path.as_posix()
        if value.startswith("/") or ".." in path.parts or value in {"", "."}:
            raise CoreError("WORKSPACE_SNAPSHOT_INVALID")
        return value

    def _put_blob(self, content):
        digest = self._digest(content)
        target = self.blobs / digest
        try:
            with target.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            if self._digest(target.read_bytes()) != digest:
                raise CoreError("ARTIFACT_INTEGRITY_FAILED") from None
        return digest

    def publish(self, workspace, *, parent_id=None):
        workspace = Path(workspace).resolve()
        files = {}
        for path in sorted(workspace.rglob("*")):
            relative = path.relative_to(workspace)
            if relative.parts and relative.parts[0] == ".tmp":
                continue
            if path.is_symlink():
                raise CoreError("WORKSPACE_SNAPSHOT_INVALID", "symlinks are forbidden")
            if not path.is_file():
                continue
            content = path.read_bytes()
            files[self._relative(relative)] = {
                "sha256": self._put_blob(content),
                "size": len(content),
                "mode": path.stat().st_mode & 0o777,
            }
        manifest = {"schema_version": 1, "parent_id": parent_id, "files": files}
        encoded = json.dumps(
            manifest, sort_keys=True, separators=(",", ":")
        ).encode()
        snapshot_id = self._digest(encoded)
        prefix = self.snapshots / snapshot_id
        prefix.mkdir(exist_ok=True)
        committed = prefix / "manifest.json"
        try:
            with committed.open("xb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            if committed.read_bytes() != encoded:
                raise CoreError("ARTIFACT_INTEGRITY_FAILED") from None
        return WorkspaceSnapshot(snapshot_id, parent_id, files)

    def get(self, snapshot_id):
        if not isinstance(snapshot_id, str) or len(snapshot_id) != 64:
            raise CoreError("WORKSPACE_SNAPSHOT_NOT_FOUND")
        manifest_path = self.snapshots / snapshot_id / "manifest.json"
        try:
            encoded = manifest_path.read_bytes()
            manifest = json.loads(encoded)
        except (OSError, json.JSONDecodeError):
            raise CoreError("WORKSPACE_SNAPSHOT_NOT_FOUND") from None
        if self._digest(encoded) != snapshot_id or manifest.get("schema_version") != 1:
            raise CoreError("ARTIFACT_INTEGRITY_FAILED")
        files = manifest.get("files")
        if not isinstance(files, dict):
            raise CoreError("ARTIFACT_INTEGRITY_FAILED")
        return WorkspaceSnapshot(snapshot_id, manifest.get("parent_id"), files)

    def materialize(self, snapshot_id, destination):
        snapshot = self.get(snapshot_id)
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        for relative, metadata in snapshot.files.items():
            path = Path(relative)
            self._relative(path)
            blob = self.blobs / metadata["sha256"]
            try:
                content = blob.read_bytes()
            except OSError:
                raise CoreError("ARTIFACT_INTEGRITY_FAILED") from None
            if (
                self._digest(content) != metadata["sha256"]
                or len(content) != metadata["size"]
            ):
                raise CoreError("ARTIFACT_INTEGRITY_FAILED")
            target = destination / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(metadata["mode"])
        return snapshot

    @staticmethod
    def _workspace_files(workspace):
        result = {}
        workspace = Path(workspace)
        for path in sorted(workspace.rglob("*")):
            if path.is_file() and not path.is_symlink() and ".tmp" not in path.parts:
                result[path.relative_to(workspace).as_posix()] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
        return result

    def merge(self, *, base_snapshot_id, child_snapshot_id, target_workspace):
        base = self.get(base_snapshot_id)
        child = self.get(child_snapshot_id)
        if child.parent_id != base.id:
            raise CoreError("WORKSPACE_BASE_MISMATCH")
        target_workspace = Path(target_workspace)
        current = self._workspace_files(target_workspace)
        base_hashes = {path: value["sha256"] for path, value in base.files.items()}
        child_hashes = {path: value["sha256"] for path, value in child.files.items()}
        changed = {
            path
            for path in set(base_hashes) | set(child_hashes)
            if base_hashes.get(path) != child_hashes.get(path)
        }
        conflicts = sorted(
            path for path in changed if current.get(path) != base_hashes.get(path)
        )
        if conflicts:
            raise CoreError("WORKSPACE_CONFLICT", data={"paths": conflicts})
        for relative in changed:
            target = target_workspace / relative
            metadata = child.files.get(relative)
            if metadata is None:
                target.unlink(missing_ok=True)
                continue
            content = (self.blobs / metadata["sha256"]).read_bytes()
            if self._digest(content) != metadata["sha256"]:
                raise CoreError("ARTIFACT_INTEGRITY_FAILED")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(metadata["mode"])
        return self.publish(target_workspace, parent_id=base.id)


class LocalTerminalBackend:
    local = True
    capabilities = {
        "pty",
        "process_groups",
        "workspace_separation",
        "resource_limits",
        "process_tree_teardown",
    }

    def __init__(self, root, *, snapshot_store=None, base_snapshot=None):
        # Keep the caller-visible path spelling (notably /var vs /private/var on macOS).
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        self.snapshot_store = snapshot_store
        self.base_snapshot = base_snapshot

    @staticmethod
    def _path_component(value):
        if (
            not isinstance(value, str)
            or not value
            or value in {".", ".."}
            or Path(value).name != value
        ):
            raise CoreError("TOOL_ARGUMENT_INVALID")
        return value

    def create(self, spec):
        workspace = (
            self.root
            / self._path_component(spec.tenant_id)
            / self._path_component(spec.run_id)
            / self._path_component(spec.workspace_id)
            / "workspace"
        )
        workspace.mkdir(parents=True, exist_ok=False)
        snapshot_id = spec.workspace_snapshot or self.base_snapshot
        snapshot = Path(snapshot_id) if snapshot_id else None
        if snapshot and snapshot.is_dir():
            shutil.copytree(snapshot, workspace, dirs_exist_ok=True, symlinks=True)
        elif snapshot_id and self.snapshot_store:
            self.snapshot_store.materialize(snapshot_id, workspace)
        return _LocalTerminalSession(spec, workspace, self.snapshot_store)


class _LocalTerminalSession:
    def __init__(self, spec, workspace, snapshot_store=None):
        self.id = spec.session_id
        self.spec = spec
        self.workspace = workspace
        self._processes = {}
        self._lock = threading.Lock()
        self._closed = False
        self.snapshot_store = snapshot_store
        self.final_snapshot = None

    def _cwd(self, requested):
        candidate = Path(requested or ".")
        candidate = candidate if candidate.is_absolute() else self.workspace / candidate
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self.workspace.resolve())
        except ValueError:
            raise CoreError("TOOL_ARGUMENT_INVALID", "cwd escapes workspace") from None
        if not resolved.is_dir():
            raise CoreError("TOOL_ARGUMENT_INVALID", "cwd is not a directory")
        return resolved

    def _environment(self, request):
        requested = request.get("env", {})
        if not isinstance(requested, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in requested.items()
        ):
            raise CoreError("TOOL_ARGUMENT_INVALID")
        if set(requested) - set(self.spec.environment_allowlist):
            raise CoreError("POLICY_DENIED")
        temporary = self.workspace / ".tmp"
        temporary.mkdir(exist_ok=True)
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.workspace),
            "TMPDIR": str(temporary),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }
        environment.update(requested)
        environment.update(request.get("_secret_env", {}))
        return environment

    def start(self, request):
        if self._closed:
            raise CoreError("TASK_TERMINAL")
        argv = request.get("argv")
        if (
            not isinstance(argv, (list, tuple))
            or not argv
            or not all(
                isinstance(value, str) and value and "\0" not in value for value in argv
            )
        ):
            raise CoreError("TOOL_ARGUMENT_INVALID")
        max_output = request.get("max_output_bytes", self.spec.max_output_bytes)
        timeout = request.get("timeout")
        if not isinstance(max_output, int) or max_output <= 0:
            raise CoreError("TOOL_ARGUMENT_INVALID")
        if timeout is not None and (
            not isinstance(timeout, (int, float)) or timeout <= 0
        ):
            raise CoreError("TOOL_ARGUMENT_INVALID")

        cwd = self._cwd(request.get("cwd"))
        environment = self._environment(request)
        master_fd, slave_fd = pty.openpty()
        try:
            process = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=environment,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                close_fds=True,
            )
        except (OSError, ValueError) as error:
            os.close(master_fd)
            raise CoreError("TOOL_EXECUTION_FAILED", str(error)) from error
        finally:
            os.close(slave_fd)

        handle = ProcessHandle(
            id=str(uuid.uuid4()),
            terminal_session_id=self.id,
            process_group_id=process.pid,
            _process=process,
            _master_fd=master_fd,
            _max_output_bytes=max_output,
            _timeout=float(timeout) if timeout is not None else None,
        )
        with self._lock:
            self._processes[handle.id] = handle
        threading.Thread(target=self._drain, args=(handle,), daemon=True).start()
        return handle

    def _drain(self, handle):
        try:
            while True:
                try:
                    chunk = os.read(handle._master_fd, 4096)
                except OSError as error:
                    if error.errno in {errno.EIO, errno.EBADF}:
                        break
                    raise
                if not chunk:
                    break
                with handle._lock:
                    remaining = handle._max_output_bytes - len(handle._output)
                    if remaining > 0:
                        handle._output.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        handle._truncated = True
        finally:
            handle._process.wait()
            if handle.state == "running":
                handle.state = "exited" if handle._process.returncode == 0 else "failed"
            handle._done.set()

    def _get(self, process_id):
        try:
            return self._processes[process_id]
        except KeyError:
            raise CoreError("TASK_NOT_FOUND") from None

    def write(self, process_id, data):
        handle = self._get(process_id)
        if handle._done.is_set():
            raise CoreError("TASK_TERMINAL")
        encoded = data.encode() if isinstance(data, str) else data
        if not isinstance(encoded, bytes):
            raise CoreError("TOOL_ARGUMENT_INVALID")
        os.write(handle._master_fd, encoded)

    def read(self, process_id):
        handle = self._get(process_id)
        with handle._lock:
            return bytes(handle._output).decode(errors="replace")

    def _terminate(self, handle, *, timed_out=False):
        if handle._done.is_set():
            return
        handle._timed_out = timed_out
        handle.state = "timed_out" if timed_out else "canceled"
        handle._cleanup = "process_group_terminated"
        try:
            os.killpg(handle.process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            return
        if not handle._done.wait(0.1):
            try:
                os.killpg(handle.process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def wait(self, process_id, timeout=None):
        handle = self._get(process_id)
        effective_timeout = timeout if timeout is not None else handle._timeout
        if not handle._done.wait(effective_timeout):
            self._terminate(handle, timed_out=True)
            handle._done.wait(1)
        if handle._result:
            return handle._result
        output = self.read(process_id)
        handle._result = ExecutionResult(
            exit_code=handle._process.returncode
            if handle._process.returncode is not None
            else -1,
            stdout=output,
            stderr="",
            artifacts=(),
            side_effects=(),
            status="timed_out"
            if handle._timed_out
            else ("succeeded" if handle._process.returncode == 0 else "failed"),
            terminal_session_id=self.id,
            process_group_id=handle.process_group_id,
            used_pty=True,
            timed_out=handle._timed_out,
            truncated=handle._truncated,
            cleanup=handle._cleanup,
            duration=time.monotonic() - handle._started_at,
        )
        try:
            os.close(handle._master_fd)
        except OSError:
            pass
        return handle._result

    def execute(self, request):
        handle = self.start(request)
        return self.wait(handle.id)

    def cancel(self, process_id):
        self._terminate(self._get(process_id))

    def destroy(self):
        self._closed = True
        for handle in tuple(self._processes.values()):
            self._terminate(handle)
            handle._done.wait(1)
            try:
                os.close(handle._master_fd)
            except OSError:
                pass
        if self.snapshot_store:
            parent_id = self.spec.workspace_snapshot or None
            self.final_snapshot = self.snapshot_store.publish(
                self.workspace, parent_id=parent_id
            )
        shutil.rmtree(self.workspace.parent, ignore_errors=True)
        return self.final_snapshot


class TerminalSessionManager:
    REQUIRED = {
        "pty",
        "process_groups",
        "workspace_separation",
        "resource_limits",
        "process_tree_teardown",
    }

    def __init__(self, backend):
        if not getattr(backend, "local", False) or not self.REQUIRED <= set(
            getattr(backend, "capabilities", set())
        ):
            raise CoreError("EXECUTION_ENVIRONMENT_UNAVAILABLE")
        self.backend = backend
        self._environments = {}
        self.telemetry_records = []
        self._run_environments = {}
        self._lock = threading.Lock()

    def create(self, spec):
        prepared = replace(
            spec.hardened(),
            session_id=spec.session_id or str(uuid.uuid4()),
            workspace_id=spec.workspace_id or str(uuid.uuid4()),
        )
        environment = self.backend.create(prepared)
        self._environments[environment.id] = environment
        self.telemetry_records.append(
            {
                "event": "terminal.session.created",
                "session_id": environment.id,
                "run_id": spec.run_id,
            }
        )
        return environment

    def _owned(self, environment_id, owner_id):
        try:
            environment = self._environments[environment_id]
        except KeyError:
            raise CoreError("TASK_NOT_FOUND") from None
        if owner_id is not None and environment.spec.owner_id != owner_id:
            raise CoreError("POLICY_DENIED")
        return environment

    @staticmethod
    def _materialize(environment, request):
        materialized = dict(request)
        refs = materialized.pop("secret_refs", [])
        materialized["_secret_env"] = {
            name: environment.spec.secrets[name]
            for name in refs
            if name in environment.spec.secrets
        }
        return materialized

    def execute(self, environment_id, request, *, owner_id=None):
        environment = self._owned(environment_id, owner_id)
        result = environment.execute(self._materialize(environment, request))
        self.telemetry_records.append(
            {
                "event": "terminal.process.completed",
                "session_id": environment_id,
                "status": result.status,
            }
        )
        return replace(
            result,
            stdout=redact(result.stdout, set(environment.spec.secrets.values())),
            stderr=redact(result.stderr, set(environment.spec.secrets.values())),
        )

    def start(self, environment_id, request, *, owner_id=None):
        environment = self._owned(environment_id, owner_id)
        return environment.start(self._materialize(environment, request))

    def read(self, environment_id, process_id, *, owner_id=None):
        environment = self._owned(environment_id, owner_id)
        return redact(
            environment.read(process_id), set(environment.spec.secrets.values())
        )

    def write(self, environment_id, process_id, data, *, owner_id=None):
        self._owned(environment_id, owner_id).write(process_id, data)

    def wait(self, environment_id, process_id, *, owner_id=None, timeout=None):
        environment = self._owned(environment_id, owner_id)
        result = environment.wait(process_id, timeout)
        return replace(
            result,
            stdout=redact(result.stdout, set(environment.spec.secrets.values())),
            stderr=redact(result.stderr, set(environment.spec.secrets.values())),
        )

    def cancel(self, environment_id, process_id, *, owner_id=None):
        self._owned(environment_id, owner_id).cancel(process_id)

    def execute_transient(self, request, run_id):
        with self._lock:
            environment_id = self._run_environments.get(run_id)
            if environment_id is None:
                environment = self.create(
                    EnvironmentSpec(
                        "default",
                        run_id,
                        getattr(self.backend, "base_snapshot", None) or "",
                        (".",),
                        (),
                        {},
                        owner_id=run_id,
                    )
                )
                environment_id = environment.id
                self._run_environments[run_id] = environment_id
        return self.execute(environment_id, request, owner_id=run_id)

    def destroy(self, environment_id, *, owner_id=None):
        environment = self._owned(environment_id, owner_id)
        snapshot = environment.destroy()
        self._environments.pop(environment_id, None)
        for run_id, owned_id in tuple(self._run_environments.items()):
            if owned_id == environment_id:
                self._run_environments.pop(run_id, None)
        return snapshot

    def destroy_run(self, run_id):
        with self._lock:
            environment_id = self._run_environments.get(run_id)
        if environment_id is None:
            return None
        return self.destroy(environment_id, owner_id=run_id)

    def close(self):
        for environment_id in tuple(self._environments):
            self.destroy(environment_id)


# Backward-compatible name for integrations built against the initial package.
ExecutionEnvironmentManager = TerminalSessionManager


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
