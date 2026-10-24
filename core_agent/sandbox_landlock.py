"""Explicit Cloud development backend. No namespaces and no unconfined fallback.

The application must be PID 1 of its dedicated container. Each trusted supervisor
pins a process group until every member is dead; commands cannot change groups.
Landlock confines file access and seccomp confines process control and sockets.
"""
from __future__ import annotations

import ctypes
import dataclasses
import os
from pathlib import Path
import platform
import resource
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

from .errors import CoreError
from .sandbox import (
    SandboxLauncher, SandboxPolicy, SandboxProcess, _CLEANUP_SECONDS,
    _diagnostics, _open_directory, _policy_resource, _receive, _send, _unavailable,
)
from .sandbox_exec import _exec_target


def validate_backend(value, *, cloud, environment):
    selected = value or 'bubblewrap'
    if selected not in {'bubblewrap', 'landlock'}:
        raise CoreError('CONFIG_INVALID', 'SANDBOX_BACKEND must be bubblewrap or landlock')
    if selected == 'landlock' and (not cloud or environment not in {'development', 'production'}):
        raise CoreError('CONFIG_INVALID', 'SANDBOX_BACKEND=landlock requires Cloud.ru development or production')
    return selected


def _validate_workspace(workspace, inputs):
    workspace, inputs = Path(workspace), Path(inputs)
    for path in (workspace, inputs):
        descriptor = _open_directory(path)
        os.close(descriptor)
    if inputs != workspace / 'attachments':
        raise _unavailable('Unsupported protected input layout')
    # Landlock grants are additive: an RW parent cannot exclude a nested child.
    # Until a separate input layout is implemented, refuse, never grant RW input.
    if next(inputs.iterdir(), None) is not None:
        raise _unavailable('Cloud.ru execution with protected attachments is not yet supported')


def _group_members(group, deadline):
    members = []
    with os.scandir('/proc') as entries:
        for count, entry in enumerate(entries):
            if count >= 8192 or time.monotonic() >= deadline:
                raise _unavailable('Process cleanup inspection limit exceeded')
            if not entry.name.isdecimal():
                continue
            try:
                with open(entry.path + '/stat', 'rb') as source:
                    fields = source.read(4096).rsplit(b')', 1)[1].split()
            except FileNotFoundError:
                continue
            if int(fields[2]) == group:
                pid = int(entry.name)
                try:
                    descriptor = os.pidfd_open(pid)
                except ProcessLookupError:
                    continue
                try:
                    # /proc marks an exited main thread Z even while its other
                    # threads still run. A pidfd becomes ready only at group exit.
                    members.append((pid, bool(select.select([descriptor], [], [], 0)[0])))
                finally:
                    os.close(descriptor)
    return members


def _stop_children(group, *, supervisor=False):
    """No wait/reap of the group leader until the last live member is gone."""
    deadline = time.monotonic() + _CLEANUP_SECONDS
    own_pid = os.getpid() if supervisor else None
    while time.monotonic() < deadline:
        members = _group_members(group, deadline)
        live = [pid for pid, dead in members if not dead and pid != own_pid]
        if not live:
            # Reap exact adopted zombies while the leader still pins the PGID.
            for pid, dead in members:
                if pid != group and dead:
                    try:
                        os.waitpid(pid, os.WNOHANG)
                    except ChildProcessError:
                        pass
            return
        if supervisor:
            for pid in live:
                try:
                    descriptor = os.pidfd_open(pid)
                except ProcessLookupError:
                    continue
                try:
                    # A disappearing PID cannot turn into a foreign group target.
                    if os.getpgid(pid) == group:
                        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                finally:
                    os.close(descriptor)
        else:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.01)
    raise _unavailable('Sandbox tree cleanup could not be confirmed')


class LandlockProcess(SandboxProcess):
    def __init__(self, launcher, process, channel):
        super().__init__(launcher, process, channel)
        self._scratch = None
        self._tree_stopped = False

    def _launch_resources(self):
        # No finalizer: unconfirmed tree death must preserve these bytes.
        self._scratch = Path(tempfile.mkdtemp(prefix='core-landlock-scratch-'))
        return {'scratch': str(self._scratch)}

    def _cleanup_resources(self):
        # Called only after the tree and its trusted supervisor have stopped.
        # Keep that proof if filesystem cleanup fails: the leader is now reaped
        # and its numeric process group must never be signaled again.
        self._tree_stopped = True
        if self._scratch is None:
            return
        try:
            shutil.rmtree(self._scratch)
        except OSError as error:
            self._launcher._unhealthy = True
            raise CoreError('SIDE_EFFECT_UNKNOWN' if self._released else 'EXECUTION_ENVIRONMENT_UNAVAILABLE',
                'Sandbox scratch cleanup failed; launcher disabled',
                diagnostics=_diagnostics('cleanup', error, self._created_at, 'unconfirmed')) from error
        self._scratch = None

    def _emergency_cleanup(self):
        if self._closed:
            return
        try:
            if not self._tree_stopped:
                # Do not call Popen.poll/wait first: an unreaped leader pins the PGID.
                _stop_children(self.pid)
                self.returncode = self._process.wait(timeout=_CLEANUP_SECONDS)
            self._cleanup_resources()
        except BaseException as error:
            self._launcher._unhealthy = True
            raise CoreError('SIDE_EFFECT_UNKNOWN' if self._released else 'EXECUTION_ENVIRONMENT_UNAVAILABLE',
                'Sandbox tree cleanup failed; launcher disabled',
                diagnostics=_diagnostics('cleanup', error, self._created_at, 'unconfirmed')) from error
        finally:
            self._close()


class LandlockLauncher(SandboxLauncher):
    isolation_level = 'landlock'
    _supervisor_module = 'core_agent.sandbox_landlock'
    _process_class = LandlockProcess

    def _native_check(self):
        if platform.system() != 'Linux' or platform.machine() not in {'x86_64', 'aarch64'}:
            raise _unavailable('Sandbox requires native Linux amd64 or arm64')
        if os.getpid() != 1:
            raise _unavailable('Cloud.ru Landlock execution requires the agent as container PID 1')
        if os.getuid() == 0 or os.geteuid() == 0:
            raise _unavailable('Cloud.ru Landlock execution requires a non-root user')
        if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
            raise _unavailable('Linux pidfds are required')
        path, _ = _policy_resource()
        info = path.stat()
        if info.st_uid != 0 or info.st_mode & 0o222:
            raise _unavailable('Sandbox policy must be root-owned and read-only')
        with open('/proc/self/status') as source:
            status = dict(line.rstrip().split(':', 1) for line in source if ':' in line)
        if any(int(status[name], 16) for name in ('CapInh', 'CapPrm', 'CapEff', 'CapAmb')):
            raise _unavailable('Cloud.ru Landlock execution requires zero capabilities')
        if int(status['NoNewPrivs']) != 1 or int(status['Seccomp']) != 2:
            raise _unavailable('Cloud.ru Landlock execution requires outer no-new-privileges and seccomp')
        group = Path('/sys/fs/cgroup')
        values = [(group / name).read_text().split() for name in ('cpu.max', 'memory.max', 'pids.max')]
        if any(value == 'max' or int(value) <= 0 for fields in values for value in fields):
            raise _unavailable('Finite cgroup CPU, memory and PID limits are required')

    def preflight(self):
        with tempfile.TemporaryDirectory(prefix='core-landlock-check-') as root:
            workspace = Path(root) / 'workspace'
            workspace.mkdir()
            inputs = workspace / 'attachments'
            inputs.mkdir()
            secret = Path(root) / 'private'
            secret.write_text('private-canary')
            # This reaches the real notify listener and ADDFD, without internet.
            code = '''import errno, os, socket, sys
from pathlib import Path
Path('allowed').write_text('ok')
assert Path('allowed').read_text() == 'ok'
os.chmod('allowed', 0o600)
os.utime('allowed', (1, 1))
assert int(Path('allowed').stat().st_mtime) == 1
for path in (sys.argv[1], '/proc/1/environ'):
    try: open(path, 'rb')
    except OSError as e: assert e.errno in (errno.EACCES, errno.EPERM)
    else: raise AssertionError('private file readable')
for operation in (lambda: os.kill(1, 0), os.setsid,
                  lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)):
    try: operation()
    except OSError as e: assert e.errno in (errno.EACCES, errno.EPERM)
    else: raise AssertionError('process/IPC isolation absent')
s = socket.socket()
s.settimeout(3)
s.connect(('127.0.0.1', 80))
s.sendall(b'CONNECT 127.0.0.1:80 HTTP/1.1\\r\\nHost: 127.0.0.1\\r\\n\\r\\n')
assert b' 403 ' in s.recv(4096)
s.close()
'''
            process = self.start(['/usr/local/bin/python3', '-I', '-S', '-c', code, str(secret)],
                workspace_path=workspace, readonly_input_path=inputs,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                if process.wait(self.policy.start_timeout_seconds) != 0:
                    raise _unavailable('Cloud.ru Landlock enforcement preflight failed')
            finally:
                process.stop()


def _limits(policy):
    for kind, value in ((resource.RLIMIT_CPU, policy.cpu_seconds),
                        (resource.RLIMIT_AS, policy.address_space_bytes),
                        (resource.RLIMIT_NOFILE, policy.max_open_files),
                        (resource.RLIMIT_FSIZE, policy.max_file_bytes),
                        (resource.RLIMIT_NPROC, 128), (resource.RLIMIT_CORE, 0)):
        _, hard = resource.getrlimit(kind)
        value = min(value, hard) if hard != resource.RLIM_INFINITY else value
        resource.setrlimit(kind, (value, value))


def _child(control, status_fd, launch, scratch, broker):
    try:
        # Imports occur before filesystem access is restricted; code is image-owned.
        from .landlock_policy import apply_landlock, install_seccomp
        keep = {0, 1, 2, control.fileno(), status_fd}
        if broker is not None:
            keep.add(broker.fileno())
        for value in os.listdir('/proc/self/fd'):
            descriptor = int(value)
            if descriptor not in keep:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        workspace = Path(launch['workspace'])
        cwd = workspace / launch['cwd']
        # Caller already validated containment; validate again in the helper.
        cwd.resolve(strict=True).relative_to(workspace.resolve(strict=True))
        os.chdir(cwd)
        _limits(SandboxPolicy(**launch['policy']))
        apply_landlock(workspace, scratch)
        listener = install_seccomp()
        _send(control, {'state': 'confined'}, (listener,))
        os.close(listener)
        message, received = _receive(control)
        for descriptor in received:
            os.close(descriptor)
        if message.get('state') != 'release' or not isinstance(message.get('environment'), dict):
            raise _unavailable('Sandbox release authorization missing')
        environment = dict(launch['environment'])
        environment.update(message['environment'])
        environment.update(HOME=str(workspace), TMPDIR=str(scratch),
                           TMP=str(scratch), TEMP=str(scratch))
        argv = launch['argv']
        if broker is not None:
            if len(argv) < 2 or argv[-2] != '/run/core-agent/broker.sock':
                raise _unavailable('Invalid Python broker launch')
            argv[-2] = 'fd:' + str(broker.fileno())
            import fcntl
            fcntl.fcntl(broker.fileno(), fcntl.F_SETFD, 0)
        control.close()
        # EOF on this CLOEXEC pipe is the successful exec acknowledgement.
        os.write(status_fd, _exec_target(argv, environment))
    except BaseException as error:
        try:
            number = getattr(error, 'errno', 0) or 0
            _send(control, {'state': 'failed', 'errno': number if type(number) is int and 0 < number < 4096 else 0})
        except OSError:
            pass
        try:
            os.write(status_fd, f'ERR:{getattr(error, "errno", 0) or 0}'.encode('ascii'))
        except OSError:
            pass
    os._exit(125)


def _supervise(channel):
    from .landlock_metadata import MetadataBroker
    from .landlock_network import EgressBroker
    launch, descriptors = _receive(channel)
    for descriptor in descriptors:
        os.close(descriptor)
    started = time.monotonic()
    broker = network = metadata = child_control = parent_control = None
    child_pid = None
    status_read = status_write = None
    released = False
    stage = 'native_check'
    result = {'state': 'exited', 'returncode': 125}
    try:
        if os.getppid() != launch['parent_pid'] or os.getpgrp() != os.getpid():
            raise _unavailable('Sandbox parent identity changed')
        library = ctypes.CDLL(None, use_errno=True)
        if library.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            raise OSError(ctypes.get_errno(), 'subreaper unavailable')
        _validate_workspace(launch['workspace'], launch['inputs'])
        policy = SandboxPolicy(**launch['policy'])
        scratch = Path(launch['scratch'])
        if launch['broker']:
            broker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            broker.settimeout(max(0.01, launch['deadline'] - time.monotonic()))
            broker.connect(launch['broker'])
            broker.settimeout(None)
        parent_control, child_control = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        status_read, status_write = os.pipe2(os.O_CLOEXEC)
        # No threads exist at fork. Network workers start only in the parent.
        child_pid = os.fork()
        if child_pid == 0:
            _child(child_control, status_write, launch, scratch, broker)
        child_control.close()
        child_control = None
        os.close(status_write)
        status_write = None
        if broker is not None:
            broker.close()
            broker = None
        stage = 'native_check'
        parent_control.settimeout(max(0.01, launch['deadline'] - time.monotonic()))
        message, descriptors = _receive(parent_control)
        if message.get('state') != 'confined' or len(descriptors) != 1:
            for descriptor in descriptors:
                os.close(descriptor)
            number = message.get('errno')
            if type(number) is int and 0 < number < 4096:
                raise OSError(number, 'Sandbox restriction setup failed')
            raise _unavailable('Sandbox restriction handshake failed')
        metadata = MetadataBroker(launch['workspace'], scratch)
        network = EgressBroker(policy, metadata_handler=metadata)
        network.start_notifications(descriptors[0])
        _send(channel, {'state': 'released'})
        released = True
        _send(parent_control, {'state': 'release', 'environment': network.environment})
        parent_control.close()
        parent_control = None
        stage = 'target_exec'
        remaining = max(0, launch['deadline'] - time.monotonic())
        if not select.select([status_read], [], [], remaining)[0]:
            raise _unavailable('Sandbox startup timed out')
        status = os.read(status_read, 128)
        if status:
            released = False
            result['not_executed'] = True
            if status.startswith(b'MISSING:'):
                result['error'] = 'COMMAND_NOT_FOUND'
            elif status.startswith(b'EXEC:'):
                result['error'] = 'COMMAND_EXEC_FAILED'
            else:
                raise _unavailable('Sandbox restriction setup failed')
        else:
            outcome = os.waitid(os.P_PID, child_pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if outcome is not None and outcome.si_code != os.CLD_EXITED:
                raise _unavailable('Sandbox target died during exec acknowledgement')
            _send(channel, {'state': 'started'})
            stage = 'supervisor'
            while True:
                # Keep child zombie until all of its descendants are stopped.
                outcome = os.waitid(os.P_PID, child_pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if outcome is not None:
                    result['returncode'] = outcome.si_status if outcome.si_code == os.CLD_EXITED else -outcome.si_status
                    # EOF can also follow death before exec. Never turn a signal
                    # race into proof that the command completed or may be retried.
                    if outcome.si_code != os.CLD_EXITED:
                        result['error'] = 'SIDE_EFFECT_UNKNOWN'
                    break
                if not network.healthy:
                    raise _unavailable('Sandbox network supervisor failed')
                if select.select([channel], [], [], 0.05)[0]:
                    message, descriptors = _receive(channel)
                    for descriptor in descriptors:
                        os.close(descriptor)
                    if message.get('state') != 'stop':
                        raise _unavailable('Invalid sandbox supervisor message')
                    result['returncode'] = -signal.SIGKILL
                    break
    except BaseException as error:
        result.update(error='SIDE_EFFECT_UNKNOWN' if released else 'EXECUTION_ENVIRONMENT_UNAVAILABLE',
                      not_executed=not released,
                      diagnostics=dataclasses.asdict(_diagnostics(stage, error, started, 'confirmed')))
    finally:
        try:
            _stop_children(os.getpid(), supervisor=True)
        except BaseException as error:
            result.update(error='SIDE_EFFECT_UNKNOWN' if released else 'EXECUTION_ENVIRONMENT_UNAVAILABLE',
                          cleanup_failed=True,
                          diagnostics=dataclasses.asdict(_diagnostics('cleanup', error, started, 'unconfirmed')))
        if network is not None:
            try:
                network.close()
            except BaseException as error:
                result.update(error='SIDE_EFFECT_UNKNOWN' if released else 'EXECUTION_ENVIRONMENT_UNAVAILABLE',
                              cleanup_failed=True,
                              diagnostics=dataclasses.asdict(_diagnostics('cleanup', error, started, 'unconfirmed')))
        if metadata is not None and not result.get('cleanup_failed'):
            metadata.close()
        for value in (broker, parent_control, child_control):
            if value is not None:
                value.close()
        for descriptor in (status_read, status_write):
            if descriptor is not None:
                os.close(descriptor)
        try:
            _send(channel, result)
        except OSError:
            pass
        channel.close()


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--supervise':
        raise SystemExit(2)
    _supervise(socket.socket(fileno=int(sys.argv[2])))
