"""Fixed startup probes; never an execution backend or a sandbox fallback."""
import errno
import os
from pathlib import Path
import select
import signal
import subprocess
import time

from .errors import CoreError


_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C", "LC_ALL": "C"}
_STDERR_LIMIT = 4096


def _failure(stderr):
    """Project helper messages onto fixed codes, never returning their text."""
    text = stderr.lower()
    result = {"reason": "unclassified", "operation": "unknown"}
    if any(message in text for message in (b"no permissions to create new namespace",
                                            b"no permissions to create a new namespace")):
        result["reason"] = "permission_denied"
    for fragments, operation in (
        ((b"uid_map", b"uid map"), "uid_mapping"),
        ((b"gid_map", b"gid map", b"setgroups"), "gid_mapping"),
        ((b"mount proc",), "proc_mount"),
        ((b"mount tmpfs",), "tmpfs_mount"),
        ((b"new namespace", b"unshare failed"), "namespace_create"),
        ((b"mount",), "mount"),
        ((b"seccomp",), "seccomp"),
        ((b"execvp", b"failed to execute"), "exec"),
    ):
        if any(fragment in text for fragment in fragments):
            result["operation"] = operation
            break
    for message, number, reason in (
        (b"operation not permitted", errno.EPERM, "permission_denied"),
        (b"permission denied", errno.EACCES, "permission_denied"),
        (b"no space left on device", errno.ENOSPC, "resource_limit"),
        (b"cannot allocate memory", errno.ENOMEM, "resource_limit"),
        (b"too many users", errno.EUSERS, "resource_limit"),
        (b"invalid argument", errno.EINVAL, "invalid_argument"),
        (b"function not implemented", errno.ENOSYS, "unsupported"),
        (b"no such file or directory", errno.ENOENT, "not_found"),
    ):
        if message in text:
            result.update(errno=number, reason=reason)
            break
    return result


def _group_stopped(group, deadline):
    """The unreaped leader pins this PGID while /proc confirms group death."""
    while time.monotonic() < deadline:
        live = False
        try:
            with os.scandir("/proc") as entries:
                for count, entry in enumerate(entries):
                    if count >= 4096 or time.monotonic() >= deadline:
                        return False
                    if not entry.name.isdecimal():
                        continue
                    try:
                        with open(entry.path + "/stat", "rb") as source:
                            fields = source.read(4096).rsplit(b")", 1)[1].split()
                    except FileNotFoundError:
                        continue
                    if int(fields[2]) == group and fields[0] not in (b"Z", b"X"):
                        live = True
            if not live:
                return True
        except (OSError, ValueError, IndexError):
            return False
        time.sleep(min(0.01, max(0, deadline - time.monotonic())))
    return False


def _run_probe(command, *, timeout):
    started = time.monotonic()
    result = {"status": "failed", "cleanup": "not_started"}
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, env=_ENV, cwd="/", close_fds=True,
                                   start_new_session=True)
    except OSError as error:
        result.update(reason="dependency_missing" if error.errno == errno.ENOENT else "os_error")
        if type(error.errno) is int and 0 < error.errno < 4096:
            result["errno"] = error.errno
        return result
    stderr = bytearray()
    try:
        os.set_blocking(process.stderr.fileno(), False)
        deadline = started + timeout
        eof = False
        while True:
            try:
                chunk = os.read(process.stderr.fileno(), _STDERR_LIMIT + 1 - len(stderr))
                eof = not chunk
            except BlockingIOError:
                chunk = b""
            stderr.extend(chunk)
            if len(stderr) > _STDERR_LIMIT:
                result["reason"] = "output_limit"
                break
            exited = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if exited is not None and not chunk:
                if exited.si_code == os.CLD_EXITED and exited.si_status == 0:
                    result.update(status="succeeded", reason="completed")
                else:
                    result.update(_failure(stderr))
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result["reason"] = "timeout"
                break
            select.select([] if eof else [process.stderr], [], [], min(remaining, 0.05))
    except OSError as error:
        result["reason"] = "os_error"
        if type(error.errno) is int and 0 < error.errno < 4096:
            result["errno"] = error.errno
    finally:
        # Only fixed helpers and /usr/bin/true are launched; none creates a new
        # session. Killing their owned group also stops unshare's forked child.
        try:
            cleanup_deadline = time.monotonic() + 1
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stopped = _group_stopped(process.pid, cleanup_deadline)
            process.wait(timeout=max(0, cleanup_deadline - time.monotonic()))
            result["cleanup"] = "confirmed" if stopped else "unconfirmed"
        except (OSError, subprocess.TimeoutExpired):
            result["cleanup"] = "unconfirmed"
        if result["cleanup"] == "unconfirmed":
            result["status"] = "failed"
            if result.get("reason") == "completed":
                result["reason"] = "cleanup_failed"
        process.stderr.close()
    if process.returncode is not None:
        result["returncode"] = process.returncode
    result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return result


def run_probes(diagnostics):
    # Native util-linux probes distinguish creation from UID/GID mapping. These
    # results do not imply bwrap sees the same AppArmor executable profile.
    unshare = ["/usr/bin/unshare", "--user", "--map-root-user", "--fork", "--kill-child"]
    true = ["/usr/bin/true"]
    commands = [
        ("user_namespace", ["/usr/bin/unshare", "--user", *true]),
        ("user_root_mapping", [*unshare, *true]),
        *((name + "_namespace", [*unshare, flag, *true]) for name, flag in (
            ("mount", "--mount"), ("pid", "--pid"), ("ipc", "--ipc"),
            ("network", "--net"), ("uts", "--uts"))),
    ]
    bwrap = ["/usr/bin/bwrap", "--unshare-user", "--uid", "0", "--gid", "0",
             "--cap-drop", "ALL", "--die-with-parent", "--ro-bind", "/usr", "/usr",
             "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib"]
    if Path("/usr/lib64").exists():
        bwrap.extend(["--symlink", "usr/lib64", "/lib64"])
    proc = ["--unshare-pid", "--proc", "/proc"]
    commands.extend([
        ("bwrap_minimal", [*bwrap, *true]),
        ("bwrap_proc", [*bwrap, *proc, *true]),
        ("bwrap_full", [*bwrap, *proc, "--unshare-ipc", "--unshare-net", "--unshare-uts",
                        "--dev", "/dev", "--tmpfs", "/tmp", *true]),
    ])
    deadline = time.monotonic() + 10
    for name, command in commands:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            result = {"status": "skipped", "reason": "budget_exhausted", "cleanup": "not_started"}
        else:
            result = _run_probe(command, timeout=min(2, remaining))
        diagnostics.emit("startup.sandbox_probe", failed=result["status"] != "succeeded",
                         probe=name, **result)
        if result["cleanup"] == "unconfirmed":
            raise CoreError("EXECUTION_ENVIRONMENT_UNAVAILABLE", "Sandbox diagnostic cleanup was not confirmed")
