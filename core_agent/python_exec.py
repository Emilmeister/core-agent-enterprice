from __future__ import annotations

from contextvars import copy_context

import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sys
import tempfile
import threading

from .errors import CoreError, ExecutionNotStarted


# Sized so the largest artifact the storage accepts still fits in one frame
# as base64: a smaller limit makes core_artifact_save fail from here for a
# file it accepts from anywhere else.
MAX_RPC_BYTES = 140_000_000

RUNNER = r'''
import json
import socket
import sys
import uuid

MAX_RPC_BYTES = 140_000_000


def receive(stream):
    line = stream.readline(MAX_RPC_BYTES + 1)
    if not line or len(line) > MAX_RPC_BYTES or not line.endswith(b"\n"):
        raise RuntimeError("python tool broker disconnected")
    return json.loads(line)


def send(stream, value):
    encoded = json.dumps(value, separators=(",", ":")).encode() + b"\n"
    if len(encoded) > MAX_RPC_BYTES:
        raise RuntimeError("python tool request is too large")
    stream.write(encoded)
    stream.flush()


class ToolCallError(RuntimeError):
    def __init__(self, tool_name, code, message):
        self.tool_name = tool_name
        self.code = code
        super().__init__(f"{tool_name}: {code}: {message}")


class Tools:
    def __init__(self, stream, names):
        self._stream = stream
        self.names = tuple(names)

    def call(self, name, arguments=None, **kwargs):
        if arguments is not None and kwargs:
            raise TypeError("pass arguments or keyword arguments, not both")
        arguments = kwargs if arguments is None else arguments
        if name not in self.names or not isinstance(arguments, dict):
            raise ToolCallError(str(name), "CAPABILITY_DISABLED", "tool is unavailable")
        request_id = str(uuid.uuid4())
        send(
            self._stream,
            {"id": request_id, "name": name, "arguments": arguments},
        )
        response = receive(self._stream)
        if response.get("id") != request_id:
            raise RuntimeError("python tool broker response mismatch")
        if not response.get("ok"):
            error = response.get("error", {})
            raise ToolCallError(
                name,
                error.get("code", "TOOL_EXECUTION_FAILED"),
                error.get("message", "tool call failed"),
            )
        return response.get("output")


with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
    connection.connect(sys.argv[1])
    with connection.makefile("rwb") as stream:
        send(stream, {"token": sys.argv[2]})
        initial = receive(stream)
        namespace = {
            "__name__": "__main__",
            "ToolCallError": ToolCallError,
            "tools": Tools(stream, initial["tools"]),
        }
        exec(compile(initial["code"], "<core_python_exec>", "exec"), namespace, namespace)
'''


class PythonContinuationStopped(BaseException):
    """Host-only control signal: never serialize a catchable Python RPC failure."""

    def __init__(self, *, cleanup_failed=False):
        self.cleanup_failed = cleanup_failed


class PythonToolBroker:
    def __init__(self, code, tool_names, dispatch):
        self.directory = None
        try:
            self.code = code
            self.tool_names = tuple(sorted(tool_names))
            self.dispatch = dispatch
            self.token = secrets.token_hex(32)
            self.directory = Path(tempfile.mkdtemp(prefix="core-python-", dir="/tmp"))
            self.path = self.directory / "broker.sock"
            self._stop = threading.Event()
            self._connection = None
            self._hold_reply = False
            self._dispatch_done = threading.Event()
            self._dispatch_done.set()
            self._listener = None
            context = copy_context()
            self._thread = threading.Thread(target=context.run, args=(self._serve,), daemon=True)
        except Exception as error:
            if self.directory is not None:
                shutil.rmtree(self.directory, ignore_errors=True)
            raise ExecutionNotStarted("TOOL_START_FAILED", "Python broker could not start") from error

    def __enter__(self):
        try:
            self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._listener.bind(str(self.path))
            os.chmod(self.path, 0o600)
            self._listener.listen(1)
            self._listener.settimeout(0.1)
            self._thread.start()
        except Exception as error:
            if self._listener is not None:
                self._listener.close()
            shutil.rmtree(self.directory, ignore_errors=True)
            raise ExecutionNotStarted("TOOL_START_FAILED", "Python broker could not start") from error
        return self

    @staticmethod
    def _receive(stream):
        line = stream.readline(MAX_RPC_BYTES + 1)
        if not line:
            return None
        if len(line) > MAX_RPC_BYTES or not line.endswith(b"\n"):
            raise CoreError("TOOL_ARGUMENT_INVALID", "python RPC frame is too large")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise CoreError("TOOL_ARGUMENT_INVALID", "invalid python RPC frame") from error
        if not isinstance(value, dict):
            raise CoreError("TOOL_ARGUMENT_INVALID", "invalid python RPC frame")
        return value

    @staticmethod
    def _send(stream, value):
        encoded = (
            json.dumps(value, separators=(",", ":"), default=str).encode() + b"\n"
        )
        if len(encoded) > MAX_RPC_BYTES:
            raise CoreError("TOOL_OUTPUT_TOO_LARGE")
        stream.write(encoded)
        stream.flush()

    def _serve(self):
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self._connection = connection
            try:
                self._handle(connection)
            except Exception:
                pass
            finally:
                connection.close()
                self._connection = None
            return

    def _handle(self, connection):
        with connection.makefile("rwb") as stream:
            hello = self._receive(stream)
            if hello is None or not secrets.compare_digest(
                str(hello.get("token", "")), self.token
            ):
                raise CoreError("POLICY_DENIED")
            self._send(
                stream,
                {"code": self.code, "tools": self.tool_names},
            )
            seen = set()
            while not self._stop.is_set():
                request = self._receive(stream)
                if request is None:
                    return
                request_id = request.get("id")
                try:
                    name = request.get("name")
                    arguments = request.get("arguments")
                    if (
                        not isinstance(request_id, str)
                        or name not in self.tool_names
                        or not isinstance(arguments, dict)
                    ):
                        raise CoreError("CAPABILITY_DISABLED")
                    if request_id in seen or len(seen) >= 10000:
                        raise CoreError("TOOL_ARGUMENT_INVALID", "Duplicate or excessive Python broker request")
                    seen.add(request_id)
                    self._dispatch_done.clear()
                    try:
                        output = self.dispatch(name, arguments, request_id)
                    except PythonContinuationStopped as stopped:
                        self._hold_reply = stopped.cleanup_failed
                        raise
                    finally:
                        self._dispatch_done.set()
                    response = {"id": request_id, "ok": True, "output": output}
                    try:
                        self._send(stream, response)
                    except CoreError as error:
                        if error.code != "TOOL_OUTPUT_TOO_LARGE":
                            raise
                        self._send(
                            stream,
                            {
                                "id": request_id,
                                "ok": False,
                                "error": {"code": error.code, "message": str(error)},
                            },
                        )
                except PythonContinuationStopped:
                    if self._hold_reply:
                        # Unknown cleanup is reconciliation, never a catchable
                        # EOF/error to a possibly live interpreter. Stop accepting
                        # calls and retain this connection until its peer exits.
                        while connection.recv(4096):
                            pass
                    return
                except CoreError as error:
                    self._send(
                        stream,
                        {
                            "id": request_id,
                            "ok": False,
                            "error": {"code": error.code, "message": str(error)[:1000]},
                        },
                    )
                except Exception as error:
                    self._send(
                        stream,
                        {
                            "id": request_id,
                            "ok": False,
                            "error": {
                                "code": "TOOL_EXECUTION_FAILED",
                                "message": type(error).__name__,
                            },
                        },
                    )

    def __exit__(self, *_error):
        self._stop.set()
        try:
            self._listener.close()
        except OSError:
            pass
        self._dispatch_done.wait(12)
        if self._connection is not None and not self._hold_reply:
            try:
                self._connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._thread.join(timeout=1)
        shutil.rmtree(self.directory, ignore_errors=True)


def _interpreter():
    """The interpreter a terminal `pip install` writes to, not the agent's own.

    The agent virtualenv has no pip and no writable site-packages, so a package
    installed from `core_terminal_exec` is invisible to it. The failure is
    silent in the worst way — the install reports success and the next import
    does not find the module — so both tools must share one interpreter, and the
    one that can be installed into is the one outside the virtualenv. Its own
    bin directory leads the image PATH, hence searching without it rather than
    asking `which` and getting the virtualenv straight back.
    """
    # The directory, then resolve it: resolving the executable first follows the
    # symlink out of the virtualenv and into the very interpreter being avoided.
    own = Path(sys.executable).parent.resolve()
    search = os.pathsep.join(
        entry
        for entry in os.environ.get("PATH", os.defpath).split(os.pathsep)
        if entry and Path(entry).resolve() != own
    )
    for name in ("python3", "python"):
        found = shutil.which(name, path=search)
        if found:
            return found
    # No interpreter besides the virtualenv: it is the deployment's only one.
    return sys.executable


def execute_python(
    environment_manager,
    *,
    run_id,
    code,
    tool_names,
    dispatch,
    cwd=None,
    timeout=30,
    max_output_bytes=100_000,
    on_start=None,
):
    sandboxed = getattr(getattr(environment_manager, "backend", None), "launcher", None) is not None
    with PythonToolBroker(code, tool_names, dispatch) as broker:
        request = {
            "argv": [
                "/usr/local/bin/python3" if sandboxed else _interpreter(),
                # `-P` only, never `-I`: isolated mode also drops user
                # site-packages and PYTHONPATH, which is precisely what a
                # `pip install` from the terminal writes to. `-P` alone keeps
                # the working directory out of sys.path, so a workspace file
                # named like a stdlib module cannot break the runner itself.
                "-P",
                "-u",
                "-c",
                RUNNER,
                "/run/core-agent/broker.sock" if sandboxed else str(broker.path),
                broker.token,
            ],
            "timeout": timeout,
            "max_output_bytes": max_output_bytes,
        }
        if cwd is not None:
            request["cwd"] = cwd
        trusted = {"broker_socket": broker.path} if sandboxed else {}
        if on_start is not None:
            trusted["on_start"] = on_start
        return environment_manager.execute_transient(request, run_id, **trusted)
