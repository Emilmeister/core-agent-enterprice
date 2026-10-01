"""Fail-closed Linux command launcher. Runtime supplies already authorized paths.

The private supervisor runs single-threaded; no server-side preexec_fn or shell.
Bubblewrap's EOF-permissive bootstrap barrier is followed by a secret exec gate.
"""
from __future__ import annotations

import array
import ctypes
import ctypes.util
import dataclasses
import errno
import fcntl
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import secrets
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time

from .errors import CoreError, ExecutionNotStarted

_POLICY_SHA256 = '8ef9479132f923e4c2f485ae4c06c98ddce9ad9448ce48232cc199663beefe2e'
_HELPER_ENV = {'PATH': '/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'C.UTF-8'}
_MAX_CONFIG = 65536
_CLEANUP_SECONDS = 3.0


def _unavailable(message='Required Linux sandbox is unavailable'):
    return CoreError('EXECUTION_ENVIRONMENT_UNAVAILABLE', message)


def _policy_resource():
    candidates = (Path('/opt/core-agent/security/sandbox-policy.json'),
                  Path(__file__).with_name('sandbox-policy.json'))
    for path in candidates:
        if path.is_file():
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != _POLICY_SHA256:
                raise _unavailable('Sandbox policy integrity check failed')
            return path, json.loads(data)
    raise _unavailable('Sandbox policy resource is missing')


@dataclasses.dataclass(frozen=True)
class SandboxPolicy:
    dns_servers: tuple[str, ...]
    denied_cidrs: tuple[str, ...]
    start_timeout_seconds: float = 10.0
    max_concurrent_processes: int = 4
    cpu_seconds: int = 60
    address_space_bytes: int = 2147483648
    max_open_files: int = 256
    max_file_bytes: int = 104857600

    def __post_init__(self):
        for field in dataclasses.fields(self):
            if field.name in ('dns_servers', 'denied_cidrs'):
                value = getattr(self, field.name)
                if not isinstance(value, (tuple, list)) or not value:
                    raise CoreError('CONFIG_INVALID', 'Sandbox network settings are required')
                object.__setattr__(self, field.name, tuple(value))
                continue
            value = getattr(self, field.name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0
                    or (field.name != 'start_timeout_seconds' and not isinstance(value, int))):
                raise CoreError('CONFIG_INVALID', 'Sandbox limits must be finite and positive')
        try:
            for value in self.denied_cidrs:
                if not isinstance(value, str) or '%' in value:
                    raise ValueError('CIDR must be a literal string')
                ipaddress.ip_network(value, strict=True)
            for value in self.dns_servers:
                if not isinstance(value, str) or '%' in value:
                    raise ValueError('DNS must be an unscoped literal string')
                if not self.permits_destination(ipaddress.ip_address(value)):
                    raise ValueError('DNS must be an allowed public address')
        except (ValueError, TypeError) as error:
            raise CoreError('CONFIG_INVALID', 'Invalid sandbox CIDR or DNS address') from error

    @classmethod
    def from_environment(cls, environment=None):
        values = os.environ if environment is None else environment
        kwargs = {}
        for field in dataclasses.fields(cls):
            key = 'SANDBOX_' + field.name.upper()
            value = values.get(key)
            if field.name in ('dns_servers', 'denied_cidrs'):
                kwargs[field.name] = tuple(part.strip() for part in (value or '').split(','))
            elif value is not None:
                try:
                    kwargs[field.name] = (float(value) if field.name == 'start_timeout_seconds'
                                          else int(value))
                except (TypeError, ValueError) as error:
                    raise CoreError('CONFIG_INVALID', 'Invalid sandbox limit') from error
        return cls(**kwargs)

    def denied_networks(self):
        _, resource = _policy_resource()
        return tuple(ipaddress.ip_network(cidr) for cidr in
                     (*resource['ipv4_denied'], *resource['ipv6_denied'], *self.denied_cidrs))

    def permits_destination(self, address):
        return not any(address.version == network.version and address in network
                       for network in self.denied_networks())

    def nft_rules(self):
        denies = self.denied_networks()
        lines = ['table inet core_agent {',
                 ' chain input { type filter hook input priority 0; policy drop;',
                 '  iifname "lo" accept',
                 '  ct state established,related accept',
                 '  iifname "tap0" ip6 hoplimit 255 icmpv6 type { nd-router-advert, nd-neighbor-solicit, nd-neighbor-advert } accept',
                 ' }', ' chain forward { type filter hook forward priority 0; policy drop; }',
                 ' chain output { type filter hook output priority 0; policy drop;',
                 '  oifname "lo" accept',
                 '  oifname "tap0" ip6 hoplimit 255 icmpv6 type { nd-router-solicit, nd-neighbor-solicit, nd-neighbor-advert } accept']
        for version, family in ((4, 'ip'), (6, 'ip6')):
            nets = sorted({str(network) for network in denies if network.version == version})
            lines.append(f'  {family} daddr {{ {", ".join(nets)} }} counter reject')
            dns = [str(ipaddress.ip_address(value)) for value in self.dns_servers
                   if ipaddress.ip_address(value).version == version]
            if dns:
                for protocol in ('tcp', 'udp'):
                    lines.append(f'  {family} daddr {{ {", ".join(dns)} }} {protocol} dport 53 accept')
        lines.extend(['  tcp dport 53 counter reject', '  udp dport 53 counter reject',
                      '  meta nfproto ipv4 meta l4proto { tcp, udp } counter accept',
                      '  ip6 daddr 2000::/3 meta l4proto { tcp, udp } counter accept', ' }', '}'])
        return '\n'.join(lines) + '\n'


def _workspace_cwd(value):
    if not isinstance(value, str) or not value:
        raise CoreError('INVALID_ARGUMENT', 'Working directory must be workspace-relative')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or '\x00' in value:
        raise CoreError('INVALID_ARGUMENT', 'Working directory must be workspace-relative')
    return '/workspace' + (('/' + str(path)) if str(path) != '.' else '')


def _open_directory(path):
    """Pin every component, rejecting links, including links in ancestors."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise _unavailable('Workspace path must be absolute')
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                              dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _check_native():
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'aarch64'):
        raise _unavailable('Sandbox requires native Linux amd64 or arm64')
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise _unavailable('Linux pidfds are required')
    for executable in ('bwrap', 'slirp4netns', 'nft', 'nsenter'):
        if not shutil.which(executable, path=_HELPER_ENV['PATH']):
            raise _unavailable('Sandbox native executable is missing')
    if not ctypes.util.find_library('seccomp'):
        raise _unavailable('libseccomp is required')
    path, _ = _policy_resource()
    mode = path.stat()
    if mode.st_uid != 0 or mode.st_mode & 0o222:
        raise _unavailable('Sandbox policy must be root-owned and read-only')
    # The current cgroup may omit a tighter parent limit, but never invent a limit.
    group = Path('/sys/fs/cgroup')
    try:
        memory = (group / 'memory.max').read_text().strip()
        pids = (group / 'pids.max').read_text().strip()
        cpu, period = (group / 'cpu.max').read_text().split()
        if any(value == 'max' or int(value) <= 0 for value in (memory, pids, cpu, period)):
            raise ValueError('unbounded cgroup')
    except (OSError, ValueError) as error:
        raise _unavailable('Finite cgroup CPU, memory and PID limits are required') from error
    result = subprocess.run(['bwrap', '--help'], env=_HELPER_ENV, capture_output=True,
                            timeout=2, check=True)
    if b'--ro-bind-fd' not in result.stdout:
        raise _unavailable('Bubblewrap with directory FD binding is required')


class _ArgCompare(ctypes.Structure):
    _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_uint),
                ('a', ctypes.c_uint64), ('b', ctypes.c_uint64)]


def _seccomp_fd():
    library = ctypes.CDLL(ctypes.util.find_library('seccomp'), use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                              ctypes.c_int, ctypes.c_uint,
                                              ctypes.POINTER(_ArgCompare)]
    library.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    allow = 0x7fff0000
    context = library.seccomp_init(0x00050000 | errno.EPERM)
    if not context:
        raise _unavailable('Cannot create seccomp policy')
    descriptor = -1
    try:
        def rule(name, comparisons=(), action=allow):
            number = library.seccomp_syscall_resolve_name(name.encode('ascii'))
            if number == -1:  # A syscall absent from this native ABI remains denied.
                return
            args = (_ArgCompare * len(comparisons))(*(_ArgCompare(*item) for item in comparisons))
            if library.seccomp_rule_add_array(context, action, number, len(args), args) < 0:
                raise _unavailable('Cannot compile seccomp rule')

        _, resource = _policy_resource()
        for name in resource['syscalls']:
            rule(name)
        # Includes CLONE_NEWTIME; clone3's pointer cannot be inspected by BPF.
        rule('clone', [(0, 7, 0x7e020080, 0)])
        rule('clone3', action=0x00050000 | errno.ENOSYS)
        for family in (socket.AF_UNIX, socket.AF_INET, socket.AF_INET6):
            types = (1, 2, 5) if family == socket.AF_UNIX else (1, 2)
            for kind in types:
                rule('socket', [(0, 4, family, 0), (1, 7, 0xf, kind)])
        for kind in (1, 2, 5):
            rule('socketpair', [(0, 4, socket.AF_UNIX, 0), (1, 7, 0xf, kind)])
        # libseccomp cannot compare the same argument twice in one rule.
        # ERRNO has precedence over ALLOW; EACCES differs from default EPERM.
        rule('ioctl')
        for request in (0x5412, 0x541c):
            rule('ioctl', [(1, 7, 0xffffffff, request)], action=0x00050000 | errno.EACCES)
        descriptor = os.memfd_create('core-agent-seccomp', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        if library.seccomp_export_bpf(context, descriptor) < 0:
            raise _unavailable('Cannot export seccomp policy')
        os.lseek(descriptor, 0, os.SEEK_SET)
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS,
                    fcntl.F_SEAL_SEAL | fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK)
        return descriptor
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    finally:
        library.seccomp_release(context)


def _send(channel, message, descriptors=()):
    data = json.dumps(message, separators=(',', ':')).encode()
    if len(data) > _MAX_CONFIG:
        raise _unavailable('Sandbox launch description exceeds limit')
    ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', descriptors))] if descriptors else []
    if channel.sendmsg([data], ancillary) != len(data):
        raise _unavailable('Incomplete sandbox control message')


def _receive(channel):
    data, ancillary, flags, _ = channel.recvmsg(_MAX_CONFIG + 1, socket.CMSG_SPACE(16))
    descriptors = []
    for level, kind, payload in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            received = array.array('i')
            received.frombytes(payload[:len(payload) - len(payload) % received.itemsize])
            for descriptor in received:
                os.set_inheritable(descriptor, False)
            descriptors.extend(received)
    if not data or len(data) > _MAX_CONFIG or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
        for descriptor in descriptors:
            os.close(descriptor)
        raise _unavailable('Sandbox supervisor disconnected')
    try:
        return json.loads(data), descriptors
    except BaseException:
        for descriptor in descriptors:
            os.close(descriptor)
        raise


def _kill_pidfd(descriptor):
    try:
        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _confirm_dead(descriptors, timeout):
    pending = set(descriptors)
    deadline = time.monotonic() + timeout
    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _unavailable('Sandbox tree cleanup could not be confirmed')
        ready, _, _ = select.select(list(pending), [], [], remaining)
        pending.difference_update(ready)


class SandboxProcess:
    def __init__(self, launcher, process, channel):
        self._launcher, self._process, self._channel = launcher, process, channel
        self._pidfds = []
        self._released = False
        self._started = False
        self._closed = False
        self.returncode = None
        self.pid = process.pid
        self._lock = threading.RLock()

    def _message(self, deadline):
        remaining = deadline - time.monotonic() if deadline is not None else None
        if remaining is not None and remaining <= 0:
            raise subprocess.TimeoutExpired('sandbox', 0)
        self._channel.settimeout(remaining)
        try:
            message, descriptors = _receive(self._channel)
        except TimeoutError as error:
            raise subprocess.TimeoutExpired('sandbox', remaining) from error
        with self._lock:
            if self._closed:
                for descriptor in descriptors:
                    os.close(descriptor)
                raise _unavailable('Sandbox launch was cancelled')
            self._pidfds.extend(descriptors)
        return message

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                if self._closed:
                    return self.returncode
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            # Never hold the state lock while waiting: stop/close must interrupt us.
            try:
                ready = select.select([self._channel], [], [], remaining)[0]
            except (OSError, ValueError):
                with self._lock:
                    if self._closed:
                        return self.returncode
                raise
            with self._lock:
                if self._closed:
                    return self.returncode
                if not ready:
                    raise subprocess.TimeoutExpired('sandbox', timeout)
                try:
                    message = self._message(None)
                    if message['state'] != 'exited':
                        raise _unavailable('Unexpected sandbox completion')
                    self.returncode = message['returncode']
                    self._process.wait(timeout=_CLEANUP_SECONDS)
                    _confirm_dead(self._pidfds, _CLEANUP_SECONDS)
                    if message.get('cleanup_failed'):
                        self._launcher._unhealthy = True
                    self._close()
                    if message.get('error'):
                        raise CoreError(message['error'], 'Sandbox execution failed')
                    return self.returncode
                except BaseException as error:
                    self._emergency_cleanup()
                    if isinstance(error, CoreError) and error.code == 'SIDE_EFFECT_UNKNOWN':
                        raise
                    raise CoreError('SIDE_EFFECT_UNKNOWN' if self._released else 'EXECUTION_ENVIRONMENT_UNAVAILABLE',
                                    'Sandbox supervisor failed') from error

    def poll(self):
        if self._closed:
            return self.returncode
        if select.select([self._channel], [], [], 0)[0]:
            return self.wait(timeout=0.01)
        return None

    def stop(self, grace_seconds=_CLEANUP_SECONDS):
        with self._lock:
            if self._closed:
                return self.returncode
            if not self._started:
                self._emergency_cleanup()
                return self.returncode
            try:
                _send(self._channel, {'state': 'stop'})
            except OSError:
                self._emergency_cleanup()
                return self.returncode
        try:
            return self.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            with self._lock:
                if not self._closed:
                    self._emergency_cleanup()
            return self.returncode

    def _emergency_cleanup(self):
        if self._closed:
            return
        try:
            for descriptor in self._pidfds:
                _kill_pidfd(descriptor)
            if self._process.poll() is None:
                self._process.kill()
            self._process.wait(timeout=_CLEANUP_SECONDS)
            _confirm_dead(self._pidfds, _CLEANUP_SECONDS)
            if self.returncode is None:
                self.returncode = self._process.returncode
        except BaseException:
            self._launcher._unhealthy = True
            raise _unavailable('Sandbox tree cleanup failed; launcher disabled')
        finally:
            self._close()

    def _close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._channel.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._channel.close()
        for descriptor in self._pidfds:
            os.close(descriptor)
        self._pidfds.clear()
        self._launcher._semaphore.release()
        with self._launcher._lock:
            self._launcher._processes.discard(self)


class SandboxLauncher:
    def __init__(self, policy):
        self.policy = policy
        self._semaphore = threading.BoundedSemaphore(policy.max_concurrent_processes)
        self._lock = threading.Lock()
        self._processes = set()
        self._unhealthy = False
        self._closed = False

    def start(self, argv, *, workspace_path, readonly_input_path, cwd='.', environment=None,
              stdin=None, stdout=None, stderr=None, broker_socket=None, timeout=None):
        try:
            _workspace_cwd(cwd)
            if (not isinstance(argv, (list, tuple)) or not argv or not argv[0]
                    or not all(isinstance(value, str) and '\x00' not in value for value in argv)):
                raise CoreError('INVALID_ARGUMENT', 'Command argv is invalid')
            if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                        or not math.isfinite(timeout) or timeout <= 0):
                raise CoreError('INVALID_ARGUMENT', 'Launch timeout must be finite and positive')
            environment = dict(environment or {})
            for key, value in environment.items():
                if not isinstance(key, str) or not key or '=' in key or '\x00' in key or not isinstance(value, str) or '\x00' in value:
                    raise CoreError('INVALID_ARGUMENT', 'Process environment is invalid')
            deadline = time.monotonic() + min(self.policy.start_timeout_seconds,
                                             timeout if timeout is not None else self.policy.start_timeout_seconds)
            if self._unhealthy or self._closed:
                raise _unavailable('Sandbox launcher is unavailable')
            try:
                _check_native()
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                raise _unavailable() from error
            if not self._semaphore.acquire(timeout=max(0, deadline - time.monotonic())):
                raise _unavailable('Sandbox concurrency limit timed out')
        except CoreError as error:
            raise ExecutionNotStarted(error.code, error.message, retryable=error.retryable,
                                      data=error.data) from error
        parent = child = None
        handle = None
        try:
            with self._lock:
                if self._closed or self._unhealthy:
                    raise _unavailable('Sandbox launcher is unavailable')
                parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                process = subprocess.Popen([sys.executable, '-I', '-m', 'core_agent.sandbox', '--supervise', str(child.fileno())],
                                           env=_HELPER_ENV, pass_fds=(child.fileno(),),
                                           stdin=stdin, stdout=stdout, stderr=stderr, start_new_session=True)
                child.close()
                handle = SandboxProcess(self, process, parent)
                self._processes.add(handle)
            _send(parent, {'argv': list(argv), 'workspace': str(workspace_path),
                           'inputs': str(readonly_input_path), 'cwd': cwd, 'environment': environment,
                           'broker': str(broker_socket) if broker_socket is not None else None,
                           'policy': dataclasses.asdict(self.policy), 'deadline': deadline,
                           'parent_pid': os.getpid()})
            while True:
                message = handle._message(deadline)
                if message['state'] == 'owned':
                    continue
                if message['state'] == 'released':
                    handle._released = True
                    continue
                if message['state'] == 'started':
                    with handle._lock:
                        if handle._closed:
                            raise _unavailable('Sandbox launch was cancelled')
                        handle._started = True
                    return handle
                if message.get('not_executed'):
                    handle._released = False
                if message.get('cleanup_failed'):
                    self._unhealthy = True
                raise CoreError(message.get('error', 'EXECUTION_ENVIRONMENT_UNAVAILABLE'),
                                'Sandbox did not start')
        except BaseException as error:
            if child is not None:
                child.close()
            if handle:
                handle._emergency_cleanup()
            else:
                if parent is not None:
                    parent.close()
                self._semaphore.release()
            if handle and handle._released:
                raise CoreError('SIDE_EFFECT_UNKNOWN', 'Sandbox startup outcome is unknown') from error
            if isinstance(error, CoreError):
                raise ExecutionNotStarted(error.code, error.message, retryable=error.retryable,
                                          data=error.data) from error
            if not isinstance(error, Exception):
                raise
            raise ExecutionNotStarted('EXECUTION_ENVIRONMENT_UNAVAILABLE',
                                      'Required Linux sandbox is unavailable') from error

    def preflight(self):
        with tempfile.TemporaryDirectory(prefix='core-agent-preflight-') as root:
            workspace = Path(root)
            inputs = workspace / 'attachments'
            inputs.mkdir()
            process = self.start(['/usr/local/bin/python3', '-I', '-S', '-c', 'pass'],
                                 workspace_path=workspace, readonly_input_path=inputs,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if process.wait(timeout=self.policy.start_timeout_seconds) != 0:
                raise _unavailable('Sandbox preflight failed')

    def close(self):
        with self._lock:
            self._closed = True
            processes = tuple(self._processes)
        failure = None
        for process in processes:
            try:
                process.stop()
            except CoreError as error:
                failure = error
        if failure:
            raise failure


def _read_deadline(descriptor, length, deadline):
    result = b''
    while len(result) < length:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([descriptor], [], [], remaining)[0]:
            raise _unavailable('Sandbox startup timed out')
        part = os.read(descriptor, length - len(result))
        if not part:
            break
        result += part
    return result


def _supervise(channel):
    launch, received = _receive(channel)
    for descriptor in received:
        os.close(descriptor)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) or os.getppid() != launch['parent_pid']:
        raise _unavailable('Sandbox parent is gone')
    policy = SandboxPolicy(**launch['policy'])
    deadline = launch['deadline']
    owned_fds, pidfds, children = [], [], []
    released, returncode, failure, cleanup_failed = False, 125, None, False

    def protect_helper():
        # Only this isolated single-threaded supervisor uses preexec_fn.
        # Pin parent death before exec, including the window before bwrap setup.
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) or os.getppid() != supervisor_pid:
            os._exit(125)

    supervisor_pid = os.getpid()

    def own(fd):
        owned_fds.append(fd)
        return fd

    def pipe():
        return tuple(own(fd) for fd in os.pipe2(os.O_CLOEXEC))

    def close(fd):
        os.close(fd)
        owned_fds.remove(fd)

    def run_helper(argv, **kwargs):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _unavailable('Sandbox startup timed out')
        return subprocess.run(argv, env=_HELPER_ENV, check=True, timeout=remaining,
                              capture_output=True, preexec_fn=protect_helper, **kwargs)

    try:
        workspace = own(_open_directory(launch['workspace']))
        if Path(launch['inputs']) != Path(launch['workspace']) / 'attachments':
            raise _unavailable('Input directory must be workspace attachments')
        inputs = own(os.open('attachments', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=workspace))
        trampoline = own(os.open(Path(__file__).with_name('sandbox_exec.py'), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC))
        seccomp = own(_seccomp_fd())
        resolver = own(os.memfd_create('sandbox-resolver', os.MFD_CLOEXEC))
        os.write(resolver, ''.join(f'nameserver {ip}\n' for ip in policy.dns_servers).encode())
        os.lseek(resolver, 0, os.SEEK_SET)
        info_r, info_w = pipe()
        bootstrap_r, bootstrap_w = pipe()
        exec_r, exec_w = pipe()
        ready_r, ready_w = pipe()
        slirp_r, slirp_w = pipe()
        exit_r, exit_w = pipe()
        config = own(os.memfd_create('sandbox-command', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING))
        token = secrets.token_bytes(32)
        target_env = {**launch['environment'], **_HELPER_ENV,
                      'HOME': '/workspace', 'TMPDIR': '/tmp'}
        payload = json.dumps({'argv': launch['argv'], 'environment': target_env,
                              'token': token.hex(), 'limits': dataclasses.asdict(policy),
                              'deadline': deadline}).encode()
        if len(payload) > _MAX_CONFIG:
            raise _unavailable('Target configuration exceeds limit')
        os.write(config, payload)
        os.lseek(config, 0, os.SEEK_SET)
        fcntl.fcntl(config, fcntl.F_ADD_SEALS, fcntl.F_SEAL_SEAL | fcntl.F_SEAL_WRITE |
                    fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK)
        command = ['bwrap', '--unshare-user', '--uid', '0', '--gid', '0',
                   '--unshare-pid', '--unshare-ipc', '--unshare-net', '--unshare-uts',
                   '--cap-drop', 'ALL', '--die-with-parent', '--new-session',
                   '--ro-bind', '/usr', '/usr', '--symlink', 'usr/bin', '/bin',
                   '--symlink', 'usr/sbin', '/sbin', '--symlink', 'usr/lib', '/lib',
                   '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
                   '--ro-bind', '/etc/ssl/certs', '/etc/ssl/certs',
                   '--ro-bind-data', str(resolver), '/etc/resolv.conf',
                   '--bind-fd', str(workspace), '/workspace',
                   '--ro-bind-fd', str(inputs), '/workspace/attachments',
                   '--ro-bind-fd', str(trampoline), '/run/core-agent/sandbox_exec.py',
                   '--chdir', _workspace_cwd(launch['cwd']),
                   '--info-fd', str(info_w), '--block-fd', str(bootstrap_r),
                   '--seccomp', str(seccomp)]
        if Path('/usr/lib64').exists():
            command.extend(['--symlink', 'usr/lib64', '/lib64'])
        if launch['broker']:
            broker_parent = own(_open_directory(Path(launch['broker']).parent))
            broker = own(os.open(Path(launch['broker']).name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 dir_fd=broker_parent))
            if not stat.S_ISSOCK(os.fstat(broker).st_mode):
                raise _unavailable('Python broker must be a socket')
            command.extend(['--ro-bind-fd', str(broker), '/run/core-agent/broker.sock'])
        command.extend(['/usr/local/bin/python3', '-I', '-S', '/run/core-agent/sandbox_exec.py',
                        str(config), str(exec_r), str(ready_w)])
        passed = [fd for fd in owned_fds if fd not in (info_r, bootstrap_w, exec_w, ready_r,
                                                       slirp_r, slirp_w, exit_r, exit_w)]
        bwrap = subprocess.Popen(command, env=_HELPER_ENV, pass_fds=passed,
                                 preexec_fn=protect_helper)
        children.append(bwrap)
        for fd in (info_w, bootstrap_r, exec_r, ready_w):
            close(fd)
        info = b''
        while b'}' not in info:
            part = _read_deadline(info_r, 1, deadline)
            if not part or len(info) >= 4096:
                raise _unavailable('Bubblewrap namespace setup failed')
            info += part
        pid = json.loads(info)['child-pid']
        initfd = own(os.pidfd_open(pid))
        pidfds.append(initfd)
        # Validate ownership while the live pidfd and bootstrap barrier pin the child.
        parent_pid = int(Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[1])
        if parent_pid != bwrap.pid:
            raise _unavailable('Unexpected namespace owner')
        userns = own(os.open(f'/proc/{pid}/ns/user', os.O_RDONLY | os.O_CLOEXEC))
        netns = own(os.open(f'/proc/{pid}/ns/net', os.O_RDONLY | os.O_CLOEXEC))
        _send(channel, {'state': 'owned'}, [initfd])
        nsenter = ['nsenter', '--preserve-credentials', f'--user=/proc/self/fd/{userns}',
                   f'--net=/proc/self/fd/{netns}', 'nft']
        run_helper([*nsenter, '-f', '-'], input=policy.nft_rules().encode(), pass_fds=(userns, netns))
        readback = run_helper([*nsenter, '-j', 'list', 'table', 'inet', 'core_agent'], pass_fds=(userns, netns))
        state = json.loads(readback.stdout)['nftables']
        chains = {item['chain']['name']: item['chain'] for item in state if 'chain' in item}
        if any(chains.get(name, {}).get('policy') != 'drop' for name in ('input', 'output', 'forward')):
            raise _unavailable('Firewall readback did not confirm default-deny')
        rules = [item['rule'] for item in state if 'rule' in item]
        if len(rules) < 8:
            raise _unavailable('Firewall readback is incomplete')
        slirp = subprocess.Popen(['slirp4netns', '--configure', '--disable-host-loopback',
                                  '--disable-dns', '--enable-ipv6', '--enable-sandbox', '--enable-seccomp',
                                  '--netns-type=path', f'--userns-path=/proc/self/fd/{userns}',
                                  '--exit-fd', str(exit_r), '--ready-fd', str(slirp_w),
                                  f'/proc/self/fd/{netns}', 'tap0'],
                                 env=_HELPER_ENV, pass_fds=(userns, netns, exit_r, slirp_w),
                                 preexec_fn=protect_helper,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        children.append(slirp)
        slirpfd = own(os.pidfd_open(slirp.pid))
        pidfds.append(slirpfd)
        _send(channel, {'state': 'owned'}, [slirpfd])
        close(exit_r)
        close(slirp_w)
        if _read_deadline(slirp_r, 1, deadline) != b'1' or slirp.poll() is not None:
            raise _unavailable('Sandbox network setup failed')
        # Releasing this barrier only starts the trusted isolated trampoline.
        if os.write(bootstrap_w, b'1') != 1:
            raise _unavailable('Incomplete bootstrap release')
        if _read_deadline(ready_r, 5, deadline) != b'READY' or slirp.poll() is not None:
            raise _unavailable('Sandbox target setup failed')
        _send(channel, {'state': 'released'})
        if os.write(exec_w, token) != 32:
            raise _unavailable('Incomplete target release')
        released = True
        # READY's descriptor is CLOEXEC; EOF confirms exec, ERR confirms no exec.
        status = _read_deadline(ready_r, 32, deadline)
        if status.startswith(b'ERR:'):
            released = False
            raise _unavailable('Sandbox target exec failed')
        if status:
            raise _unavailable('Invalid target exec status')
        _send(channel, {'state': 'started'})
        bwrapfd = own(os.pidfd_open(bwrap.pid))
        ready, _, _ = select.select([channel, bwrapfd, slirpfd], [], [])
        if bwrapfd in ready:
            returncode = bwrap.wait()
        elif slirpfd in ready:
            failure = 'SIDE_EFFECT_UNKNOWN'
        else:
            returncode = -signal.SIGKILL
    except BaseException:
        failure = 'SIDE_EFFECT_UNKNOWN' if released else 'EXECUTION_ENVIRONMENT_UNAVAILABLE'
    finally:
        try:
            # Never close bootstrap/exec gates until namespace death is confirmed.
            for descriptor in pidfds:
                _kill_pidfd(descriptor)
            for process in children:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=_CLEANUP_SECONDS)
            _confirm_dead(pidfds, _CLEANUP_SECONDS)
        except BaseException:
            cleanup_failed = True
            failure = 'SIDE_EFFECT_UNKNOWN' if released else 'EXECUTION_ENVIRONMENT_UNAVAILABLE'
        for descriptor in owned_fds:
            os.close(descriptor)
        try:
            _send(channel, {'state': 'exited', 'returncode': returncode, 'error': failure,
                            'not_executed': not released, 'cleanup_failed': cleanup_failed})
        except OSError:
            pass


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--supervise':
        raise SystemExit(2)
    with socket.socket(fileno=int(sys.argv[2])) as control:
        _supervise(control)
