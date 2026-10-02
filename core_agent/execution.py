from __future__ import annotations

import errno
import hashlib
import ipaddress
import json
import math
import os
import pty
import select
import shutil
import signal
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlparse

from .errors import CoreError, ExecutionNotStarted
from .sandbox import SandboxLauncher, SandboxProcess
from .security import redact
from .workspace import ChatWorkspaces, WorkspaceBinding


# Recognised only as whole argv elements: `grep "a|b"` is a pattern, `"|"` on
# its own is a pipe the caller expected a shell to interpret.
SHELL_OPERATORS = frozenset({"&&", "||", "|", ";", ">", ">>", "<", "&"})
SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ash", "ksh", "busybox", "env"})


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
    chat: WorkspaceBinding | None = None
    preserve_workspace: bool = False

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
    _process: subprocess.Popen | SandboxProcess = field(repr=False, default=None)
    _master_fd: int = field(repr=False, default=-1)
    _max_output_bytes: int = field(repr=False, default=1_000_000)
    _started_at: float = field(repr=False, default_factory=time.monotonic)
    _timeout: float | None = field(repr=False, default=None)
    _output: bytearray = field(repr=False, default_factory=bytearray)
    _done: threading.Event = field(repr=False, default_factory=threading.Event)
    _process_done: threading.Event = field(repr=False, default_factory=threading.Event)
    _termination_lock: threading.Lock = field(repr=False, default_factory=threading.Lock)
    _lock: threading.Lock = field(repr=False, default_factory=threading.Lock)
    _truncated: bool = field(repr=False, default=False)
    _timed_out: bool = field(repr=False, default=False)
    _cleanup: str = field(repr=False, default="unconfirmed")
    _error: CoreError | None = field(repr=False, default=None)
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

    def __init__(self, root, *, snapshot_store=None, base_snapshot=None, chat_root=None,
                 launcher=None):
        # Keep the caller-visible path spelling (notably /var vs /private/var on macOS).
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        self.snapshot_store = snapshot_store
        self.base_snapshot = base_snapshot
        self.chats = ChatWorkspaces(chat_root) if chat_root is not None else None
        # An omitted launcher is the explicit low-level test adapter. The app
        # always supplies its preflighted launcher, including in development.
        self.launcher = launcher

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
        if self.chats is not None and spec.chat is not None:
            if spec.tenant_id != spec.chat.tenant_id:
                raise CoreError("WORKSPACE_SCOPE_CONFLICT")
            workspace = self.chats.workspace(spec.chat)
            # A snapshot must never overwrite the live files of an existing chat.
            return _LocalTerminalSession(spec, workspace, persistent=True,
                                         launcher=self.launcher)
        workspace = (
            self.root
            / self._path_component(spec.tenant_id)
            / self._path_component(spec.run_id)
            / self._path_component(spec.workspace_id)
            / "workspace"
        )
        if workspace.parent.exists():
            if not spec.preserve_workspace:
                raise FileExistsError(workspace.parent)
            if not workspace.is_dir() or not (workspace.parent / '.initialized').is_file():
                raise ExecutionNotStarted('WORKSPACE_INITIALIZATION_INCOMPLETE')
        else:
            # Publish workspace and its trusted completion marker together.
            # The marker is outside the directory mounted into the sandbox.
            workspace.parent.parent.mkdir(parents=True, exist_ok=True)
            staged = workspace.parent.parent / ('.initializing-' + str(uuid.uuid4()))
            target = staged / 'workspace'
            target.mkdir(parents=True)
            try:
                snapshot_id = spec.workspace_snapshot or self.base_snapshot
                snapshot = Path(snapshot_id) if snapshot_id else None
                if snapshot and snapshot.is_dir():
                    shutil.copytree(snapshot, target, dirs_exist_ok=True, symlinks=True)
                elif snapshot_id and self.snapshot_store:
                    self.snapshot_store.materialize(snapshot_id, target)
                (staged / '.initialized').write_bytes(b'1')
                # A published parent always contains workspace and marker, so
                # POSIX rename cannot replace another initialized parent.
                staged.rename(workspace.parent)
            except CoreError as error:
                raise ExecutionNotStarted(error.code, error.message, data=error.data) from error
            finally:
                shutil.rmtree(staged, ignore_errors=True)
        return _LocalTerminalSession(spec, workspace, self.snapshot_store,
                                     persistent=spec.preserve_workspace, launcher=self.launcher)


class _LocalTerminalSession:
    def __init__(self, spec, workspace, snapshot_store=None, *, persistent=False,
                 launcher=None):
        self.id = spec.session_id
        self.spec = spec
        self.workspace = workspace
        self._processes = {}
        self._lock = threading.Lock()
        self._closed = False
        self._destroy_lock = threading.Lock()
        self._destroyed = False
        self.snapshot_store = snapshot_store
        self.final_snapshot = None
        self.persistent = persistent
        self.launcher = launcher
        self._cleanup_error = None

    def _contained(self, requested, label):
        candidate = Path(requested)
        candidate = candidate if candidate.is_absolute() else self.workspace / candidate
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self.workspace.resolve())
        except ValueError:
            raise CoreError(
                "TOOL_ARGUMENT_INVALID", f"{label} escapes workspace"
            ) from None
        return resolved

    def _cwd(self, requested):
        resolved = self._contained(requested or ".", "cwd")
        if not resolved.is_dir():
            raise CoreError("TOOL_ARGUMENT_INVALID", "cwd is not a directory")
        return resolved

    def resolve_file(self, requested):
        """An existing file inside this workspace, checked exactly like `cwd`."""
        resolved = self._contained(requested, "path")
        if not resolved.is_file():
            raise CoreError("NOT_FOUND", "no such file in the workspace")
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
        if self.launcher is None:
            temporary.mkdir(exist_ok=True)
        environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin" if self.launcher else os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": "/workspace" if self.launcher else str(self.workspace),
            "TMPDIR": "/tmp" if self.launcher else str(temporary),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }
        environment.update(requested)
        environment.update(request.get("_secret_env", {}))
        return environment

    def start(self, request, *, broker_socket=None, on_start=None):
        argv = request.get("argv")
        if (
            not isinstance(argv, (list, tuple))
            or not argv
            or not all(
                isinstance(value, str) and value and "\0" not in value for value in argv
            )
        ):
            raise CoreError(
                "TOOL_ARGUMENT_INVALID", "argv must be a non-empty list of strings"
            )
        operator = next((value for value in argv[1:] if value in SHELL_OPERATORS), None)
        if operator and Path(argv[0]).name not in SHELLS:
            # Otherwise the operator reaches argv[0] as an argument and the error
            # comes from whichever utility choked on it — `pwd: invalid option`
            # for `["pwd", "&&", "ls", "-la"]` — which points nowhere near the
            # actual mistake.
            raise CoreError(
                "TOOL_ARGUMENT_INVALID",
                f"there is no shell here, so {operator!r} is passed to "
                f"{argv[0]!r} as an argument; use ['sh', '-lc', '<command>']",
            )
        max_output = request.get("max_output_bytes", self.spec.max_output_bytes)
        timeout = request.get("timeout")
        if isinstance(max_output, bool) or not isinstance(max_output, int) or max_output <= 0:
            raise CoreError(
                "TOOL_ARGUMENT_INVALID", "max_output_bytes must be a positive integer"
            )
        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0
        ):
            raise CoreError("TOOL_ARGUMENT_INVALID", "timeout must be a positive number")

        # Start and registration are indivisible with respect to destroy. A
        # closing session must never miss a process still entering its sandbox.
        with self._lock:
            if self._closed:
                raise CoreError("TASK_TERMINAL")
            if self._cleanup_error is not None:
                raise self._cleanup_error
            cwd = self._cwd(request.get("cwd"))
            environment = self._environment(request)
            master_fd, slave_fd = pty.openpty()
            try:
                if self.launcher is not None:
                    inputs = self.workspace / "attachments"
                    inputs.mkdir(exist_ok=True)
                    descriptor = os.open(inputs, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    os.close(descriptor)
                    process = self.launcher.start(
                        list(argv), workspace_path=self.workspace,
                        readonly_input_path=inputs,
                        cwd=cwd.relative_to(self.workspace.resolve()).as_posix(),
                        environment=environment, stdin=slave_fd, stdout=slave_fd,
                        stderr=slave_fd, broker_socket=broker_socket, timeout=timeout,
                    )
                else:
                    process = subprocess.Popen(
                        list(argv), cwd=cwd, env=environment,
                        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                        start_new_session=True, close_fds=True,
                    )
            except BaseException as error:
                os.close(master_fd)
                if isinstance(error, CoreError) and (
                    error.code == "SIDE_EFFECT_UNKNOWN" or getattr(self.launcher, "_unhealthy", False)
                ):
                    self._cleanup_error = error
                if isinstance(error, (OSError, ValueError)):
                    raise CoreError("TOOL_START_FAILED", str(error)) from error
                raise
            finally:
                os.close(slave_fd)
            handle = ProcessHandle(
                id=str(uuid.uuid4()), terminal_session_id=self.id,
                process_group_id=process.pid, _process=process, _master_fd=master_fd,
                _max_output_bytes=max_output,
                _timeout=float(timeout) if timeout is not None else None,
            )
            self._processes[handle.id] = handle
            threading.Thread(target=self._reap, args=(handle,), daemon=True).start()
            threading.Thread(target=self._drain, args=(handle,), daemon=True).start()
        if on_start is not None:
            try:
                on_start(self, handle)
            except BaseException:
                self._terminate(handle)
                raise
        return handle

    def _reap(self, handle):
        try:
            handle._process.wait()
            handle._cleanup = "sandbox_terminated" if self.launcher else "process_exited"
        except Exception as error:
            handle._error = error if isinstance(error, CoreError) else CoreError("SIDE_EFFECT_UNKNOWN")
        finally:
            if handle.state == "running":
                handle.state = "exited" if handle._error is None and handle._process.returncode == 0 else "failed"
            handle._process_done.set()

    def _drain(self, handle):
        try:
            while True:
                if not select.select([handle._master_fd], [], [], 0.1)[0]:
                    if handle._process_done.is_set():
                        break
                    continue
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
        except Exception:
            failure = CoreError("SIDE_EFFECT_UNKNOWN", "Process output failed")
            try:
                self._terminate(handle)
            except CoreError as error:
                failure = error
            handle._error = failure
        finally:
            if handle._error is None:
                handle._process_done.wait()
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
        with handle._termination_lock:
            if handle._error is not None:
                raise handle._error
            if handle._process_done.is_set():
                return
            handle._timed_out = timed_out
            handle.state = "timed_out" if timed_out else "canceled"
            if self.launcher is not None:
                try:
                    handle._process.stop()
                except Exception as error:
                    handle._error = error if isinstance(error, CoreError) else CoreError("SIDE_EFFECT_UNKNOWN")
                    raise handle._error
                handle._cleanup = "sandbox_terminated"
            else:
                try:
                    os.killpg(handle.process_group_id, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                if not handle._process_done.wait(0.1):
                    try:
                        os.killpg(handle.process_group_id, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            if not handle._process_done.wait(3):
                handle._error = CoreError("SIDE_EFFECT_UNKNOWN", "Process cleanup was not confirmed")
                raise handle._error
            if handle._error is not None:
                raise handle._error
            if self.launcher is None:
                handle._cleanup = "process_group_terminated"

    @staticmethod
    def _confirmed(handle):
        if handle._error is not None:
            raise handle._error
        if not handle._done.wait(3):
            raise CoreError("SIDE_EFFECT_UNKNOWN", "Process output did not finish")
        if handle._error is not None:
            raise handle._error

    def wait(self, process_id, timeout=None):
        handle = self._get(process_id)
        effective_timeout = timeout if timeout is not None else handle._timeout
        if not handle._done.wait(effective_timeout):
            self._terminate(handle, timed_out=True)
        self._confirmed(handle)
        if handle._result:
            return handle._result
        output = self.read(process_id)
        handle._result = ExecutionResult(
            exit_code=handle._process.returncode,
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
        self._close_pty(handle)
        return handle._result

    @staticmethod
    def _close_pty(handle):
        with handle._lock:
            descriptor, handle._master_fd = handle._master_fd, -1
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def execute(self, request, *, broker_socket=None, on_start=None):
        handle = self.start(request, broker_socket=broker_socket, on_start=on_start)
        return self.wait(handle.id)

    def cancel(self, process_id):
        handle = self._get(process_id)
        self._terminate(handle)
        self._confirmed(handle)

    def destroy(self):
        with self._destroy_lock:
            if self._destroyed:
                return self.final_snapshot
            with self._lock:
                self._closed = True
                processes = tuple(self._processes.values())
            failure = self._cleanup_error
            for handle in processes:
                try:
                    self._terminate(handle)
                    self._confirmed(handle)
                except CoreError as error:
                    failure = error
                finally:
                    if handle._done.is_set():
                        self._close_pty(handle)
            if failure is not None:
                raise failure
            if self.snapshot_store:
                parent_id = self.spec.workspace_snapshot or None
                self.final_snapshot = self.snapshot_store.publish(
                    self.workspace, parent_id=parent_id
                )
            if not self.persistent:
                shutil.rmtree(self.workspace.parent, ignore_errors=True)
            self._destroyed = True
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
        self.instance_id = str(uuid.uuid4())
        self._environments = {}
        self.telemetry_records = []
        self._run_environments = {}
        self._run_bindings = {}
        self._retained_workspaces = {}
        self._execution_context = ContextVar("terminal_execution_owner", default=None)
        self._active_executions = {}
        self._retired_executions = set()
        self._run_ancestors = {}
        self._execution_seals = {}
        self._retired_intents = set()
        self.validate_workspace_scope = None
        self._lock = threading.RLock()

    @contextmanager
    def execution_scope(self, run_id, worker_id, generation, *, parent_run_id=None,
                        ancestor_run_ids=()):
        """A trusted lease attempt, inherited by the Python broker's context."""
        if not all(isinstance(value, str) and value for value in (run_id, worker_id, generation)):
            raise CoreError("INVALID_TASK_STATE")
        key = (run_id, worker_id, generation)
        ancestors = tuple(ancestor_run_ids)
        if parent_run_id is not None and parent_run_id not in ancestors:
            ancestors = (parent_run_id, *self._run_ancestors.get(parent_run_id, ()), *ancestors)
        if run_id in ancestors:
            raise CoreError("INVALID_TASK_STATE")
        with self._lock:
            previous = self._run_ancestors.get(run_id)
            if previous is not None and previous != ancestors:
                raise CoreError("WORKSPACE_SCOPE_CONFLICT")
            self._run_ancestors[run_id] = ancestors
            if key in self._retired_executions:
                raise CoreError("LEASE_LOST")
            self._active_executions[key] = self._active_executions.get(key, 0) + 1
        token = self._execution_context.set(key)
        try:
            yield
        finally:
            with self._lock:
                remaining = self._active_executions[key] - 1
                if remaining:
                    self._active_executions[key] = remaining
                else:
                    del self._active_executions[key]
            try:
                if not remaining:
                    self.destroy_execution(*key)
            finally:
                self._execution_context.reset(token)
                self.unbind_run(run_id)

    def _execution_key(self, run_id):
        key = self._execution_context.get()
        if key is None:
            return run_id  # Direct low-level callers have no workflow lease.
        if key[0] != run_id or key not in self._active_executions or key in self._retired_executions:
            raise ExecutionNotStarted("LEASE_LOST")
        return key

    def close_execution_tree(self, run_id, intent_id, *, run_ids=(), execution_owners=None):
        """Seal before collecting handles; never hold this lock during teardown.

        Runtime supplies the canonical persisted family, including ancestors
        which have not been resumed in this server process.
        """
        with self._lock:
            if (run_id, intent_id) in self._retired_intents:
                raise CoreError("LEASE_LOST")
            family = {run_id, *run_ids}
            pending = list(family)
            while pending:
                parent = pending.pop()
                for child, ancestors in self._run_ancestors.items():
                    if child not in family and parent in ancestors:
                        family.add(child)
                        pending.append(child)
            for member in family:
                self._execution_seals.setdefault(member, intent_id)
            if execution_owners is None:
                keys = tuple(key for key in self._run_environments if (key[0] if isinstance(key, tuple) else key) in family)
                self._retired_executions.update(key for key in self._active_executions if key[0] in family)
                environments = tuple(environment.id for environment in self._environments.values()
                                     if environment.spec.run_id in family)
            else:
                keys = tuple(tuple(key) for key in execution_owners)
                self._retired_executions.update(keys)
                environments = tuple(self._run_environments[key] for key in keys if key in self._run_environments)
        failure = None
        for environment_id in environments:
            try:
                self.destroy(environment_id)
            except CoreError as error:
                if error.code != "TASK_NOT_FOUND":
                    failure = error
        if failure is not None:
            raise failure
        return keys

    def reopen_execution_tree(self, run_id, intent_id):
        with self._lock:
            # Reopening only permits NEW lease generations. Captured attempts
            # remain retired even after a same-chat follow-up wins completion.
            if self._execution_seals.get(run_id) == intent_id:
                self._retired_intents.add((run_id, intent_id))
                for member in tuple(self._execution_seals):
                    if self._execution_seals[member] == intent_id:
                        del self._execution_seals[member]

    def destroy_execution(self, run_id, worker_id, generation):
        key = (run_id, worker_id, generation)
        with self._lock:
            self._retired_executions.add(key)
            environment_id = self._run_environments.get(key)
        if environment_id is not None:
            try:
                return self.destroy(environment_id, owner_id=run_id)
            except CoreError as error:
                if error.code != "TASK_NOT_FOUND":
                    raise
        return None

    def bind_run(self, run_id, binding):
        """Bind a process owner to its canonical chat, never to model arguments."""
        if not isinstance(binding, WorkspaceBinding):
            raise CoreError("WORKSPACE_SCOPE_REQUIRED")
        if self.validate_workspace_scope is not None:
            self.validate_workspace_scope(binding)
        with self._lock:
            previous = self._run_bindings.get(run_id)
            if previous is not None and previous != binding:
                raise CoreError("WORKSPACE_SCOPE_CONFLICT")
            for environment in self._environments.values():
                if environment.spec.run_id == run_id and environment.spec.chat != binding:
                    raise CoreError("WORKSPACE_SCOPE_CONFLICT")
            self._run_bindings[run_id] = binding

    def unbind_run(self, run_id):
        """Release an attempt's cached scope; recovery binds its durable scope again."""
        with self._lock:
            if any(key[0] == run_id for key in self._active_executions):
                return
            self._run_bindings.pop(run_id, None)

    def create(self, spec):
        sandboxed = isinstance(getattr(self.backend, "launcher", None), SandboxLauncher)
        prepared = replace(
            spec.hardened(),
            session_id=spec.session_id or str(uuid.uuid4()),
            workspace_id=spec.workspace_id or str(uuid.uuid4()),
            isolation_level="bubblewrap" if sandboxed else "process",
            os_security_boundary=sandboxed,
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

    def execute(self, environment_id, request, *, owner_id=None, broker_socket=None,
                on_start=None):
        environment = self._owned(environment_id, owner_id)
        trusted = {}
        if broker_socket is not None:
            trusted["broker_socket"] = broker_socket
        if on_start is not None:
            trusted["on_start"] = on_start
        result = environment.execute(self._materialize(environment, request), **trusted)
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

    def execute_transient(self, request, run_id, *, broker_socket=None, on_start=None):
        with self._lock:
            if any(member in self._execution_seals for member in (run_id, *self._run_ancestors.get(run_id, ()))):
                raise ExecutionNotStarted("TASK_TERMINAL")
            key = self._execution_key(run_id)
            binding = self._run_bindings.get(run_id)
            if binding is None:
                raise CoreError("WORKSPACE_SCOPE_REQUIRED")
            environment_id = self._run_environments.get(key)
            if environment_id is None:
                environment = self.create(
                    EnvironmentSpec(
                        binding.tenant_id,
                        run_id,
                        getattr(self.backend, "base_snapshot", None) or "",
                        (".",),
                        (),
                        {},
                        owner_id=run_id,
                        chat=binding,
                        workspace_id=str(uuid.uuid5(uuid.NAMESPACE_URL, binding.tenant_id + ":" + run_id)) if isinstance(key, tuple) else None,
                        preserve_workspace=isinstance(key, tuple),
                    )
                )
                environment_id = environment.id
                self._run_environments[key] = environment_id
                if isinstance(environment, _LocalTerminalSession) and environment.spec.preserve_workspace and self.backend.chats is None:
                    self._retained_workspaces[run_id] = environment.workspace.parent
        return self.execute(environment_id, request, owner_id=run_id,
                            broker_socket=broker_socket, on_start=on_start)

    def workspace_file(self, run_id, path):
        """The file a run's own tools wrote, for a caller that owns that run."""
        with self._lock:
            environment_id = self._run_environments.get(self._execution_key(run_id))
        environment = self._environments.get(environment_id) if environment_id else None
        if environment is None:
            raise CoreError("NOT_FOUND", "this run has no workspace")
        return environment.resolve_file(path)

    def destroy(self, environment_id, *, owner_id=None):
        environment = self._owned(environment_id, owner_id)
        snapshot = environment.destroy()
        with self._lock:
            self._environments.pop(environment_id, None)
            for key, owned_id in tuple(self._run_environments.items()):
                if owned_id == environment_id:
                    self._run_environments.pop(key, None)
                    self.unbind_run(key[0] if isinstance(key, tuple) else key)
        return snapshot

    def destroy_run(self, run_id):
        key = self._execution_context.get()
        if key is not None and key[0] == run_id:
            return self.destroy_execution(*key)
        with self._lock:
            environment_id = self._run_environments.get(run_id)
            self._run_bindings.pop(run_id, None)
        if environment_id is None:
            return None
        return self.destroy(environment_id, owner_id=run_id)

    def release_run_workspaces(self, run_ids):
        """Remove ephemeral run files after terminal commit; chat files persist."""
        with self._lock:
            for run_id in run_ids:
                path = self._retained_workspaces.get(run_id)
                if path is None or any(environment.spec.run_id == run_id for environment in self._environments.values()):
                    continue
                shutil.rmtree(path, ignore_errors=True)
                self._retained_workspaces.pop(run_id, None)

    def close(self):
        failure = None
        for environment_id in tuple(self._environments):
            try:
                self.destroy(environment_id)
            except CoreError as error:
                failure = error
        launcher = getattr(self.backend, "launcher", None)
        if launcher is not None:
            try:
                launcher.close()
            except CoreError as error:
                failure = error
        if failure is not None:
            raise failure
        self.release_run_workspaces(tuple(self._retained_workspaces))
        self._run_bindings.clear()


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
