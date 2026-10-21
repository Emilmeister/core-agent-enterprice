"""Unprivileged native restrictions, installed only in a disposable child.

Landlock ABI 4 does not isolate signals, metadata, or UNIX sockets. The seccomp
policy therefore intentionally denies job control, process signaling, most
metadata changes, and new UNIX sockets. chmod is emulated by a trusted broker.
Network sockets are supplied by the trusted notification broker; connect never
executes in the kernel. Read-only inputs must be outside both writable trees.
"""
import ctypes
import errno
import fcntl
import json
import os
import platform
import socket
import stat
import sys
from pathlib import Path


_FS_ALL = (1 << 15) - 1  # ABI 4: EXECUTE through TRUNCATE, including REFER.
_FS_READ = (1 << 2) | (1 << 3)
_FS_RUNTIME = _FS_READ | 1
_FS_WRITE = _FS_ALL & ~((1 << 6) | (1 << 9) | (1 << 11))
_ALLOW, _NOTIFY, _ERRNO = 0x7fff0000, 0x7fc00000, 0x00050000
_SYSTEM_PATHS = (
    '/usr', '/bin', '/sbin', '/lib', '/lib64', '/etc/ld.so.cache',
    '/etc/ld.so.conf', '/etc/ld.so.conf.d', '/etc/ssl/certs', '/etc/alternatives',
    '/etc/localtime', '/etc/nsswitch.conf', '/etc/passwd', '/etc/group',
)
_DENIED = {
    'clone', 'clone3', 'socket', 'socketpair', 'ptrace', 'process_vm_readv',
    'process_vm_writev', 'process_madvise', 'pidfd_open', 'pidfd_getfd',
    'pidfd_send_signal', 'io_uring_setup', 'io_uring_enter', 'io_uring_register',
    'unshare', 'setns', 'mount', 'umount2', 'pivot_root', 'chroot', 'bpf', 'keyctl',
    'accept', 'accept4', 'bind', 'connect', 'listen', 'fcntl', 'ioctl',
    'kill', 'tkill', 'tgkill', 'rt_sigqueueinfo', 'rt_tgsigqueueinfo',
    'setsid', 'setpgid', 'prctl', 'prlimit64',
    'sched_setaffinity', 'sched_setattr', 'sched_setparam', 'sched_setscheduler',
    'setpriority', 'setuid', 'setgid', 'setgroups', 'setreuid', 'setregid',
    'setresuid', 'setresgid', 'setfsuid', 'setfsgid',
    'chmod', 'fchmod', 'fchmodat', 'fchmodat2',
    'chown', 'lchown', 'fchown', 'fchownat',
    'utime', 'utimes', 'futimesat', 'utimensat',
    'getxattr', 'lgetxattr', 'fgetxattr', 'listxattr', 'llistxattr', 'flistxattr',
    'setxattr', 'lsetxattr', 'fsetxattr', 'removexattr', 'lremovexattr', 'fremovexattr',
    'inotify_init', 'inotify_init1', 'inotify_add_watch', 'inotify_rm_watch',
}
_POLICY_PATH = Path('/opt/core-agent/security/sandbox-policy.json')
if not _POLICY_PATH.exists():
    _POLICY_PATH = Path(__file__).with_name('sandbox-policy.json')
# Load trusted configuration before filesystem restrictions hide server paths.
_SYSCALLS = frozenset(json.loads(_POLICY_PATH.read_text())['syscalls'])


def _native():
    if (sys.platform != 'linux' or platform.machine() not in {'x86_64', 'aarch64'}
            or ctypes.sizeof(ctypes.c_void_p) != 8):
        raise OSError(errno.ENOSYS, 'Native Linux sandbox is unavailable')
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.argtypes = [ctypes.c_long]
    libc.syscall.restype = ctypes.c_long
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                          ctypes.c_ulong, ctypes.c_ulong]
    return libc


def _checked(value, *, library=False):
    if value < 0:
        raise OSError(-value if library else ctypes.get_errno(), 'Sandbox policy failed')
    return value


def _directory(value):
    path = Path(value).absolute()
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise ValueError('Sandbox roots must be real directories')
    return path.resolve(strict=True)


def filesystem_rules(workspace, scratch, *, readonly_paths=()):
    """Reject overlapping grants; Landlock permissions cannot subtract a subtree."""
    writable = (_directory(workspace), _directory(scratch))
    readonly = tuple(_directory(path) for path in readonly_paths)
    roots = (*writable, *readonly)
    for index, root in enumerate(roots):
        if root == Path('/'):
            raise ValueError('Sandbox root cannot be the filesystem root')
        for other in roots[index + 1:]:
            if root.is_relative_to(other) or other.is_relative_to(root):
                raise ValueError('Sandbox filesystem grants must be disjoint')
    return [(path, _FS_WRITE) for path in writable] + [(path, _FS_READ) for path in readonly]


def apply_landlock(workspace, scratch, *, readonly_paths=()):
    """Restrict this thread and descendants; return the required ABI (at least 4)."""
    rules = filesystem_rules(workspace, scratch, readonly_paths=readonly_paths)
    libc = _native()
    abi = _checked(libc.syscall(444, ctypes.c_void_p(), ctypes.c_size_t(0), ctypes.c_uint(1)))
    if abi < 4:
        raise OSError(errno.EOPNOTSUPP, 'Landlock ABI 4 is required')
    # Both TCP operations are denied. Only already-connected descriptors from
    # the trusted broker can carry network traffic after seccomp is installed.
    attributes = (ctypes.c_uint64 * 2)(_FS_ALL, 3)
    ruleset = _checked(libc.syscall(444, ctypes.byref(attributes),
                                  ctypes.c_size_t(ctypes.sizeof(attributes)), ctypes.c_uint(0)))

    class PathBeneath(ctypes.Structure):
        _pack_ = 1
        _fields_ = [('allowed_access', ctypes.c_uint64), ('parent_fd', ctypes.c_int32)]

    try:
        for value in _SYSTEM_PATHS:
            path = Path(value)
            if not path.exists():
                continue
            path = path.resolve(strict=True)
            info = path.stat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError('Sandbox runtime must be root-owned and immutable')
            rights = _FS_RUNTIME if stat.S_ISDIR(info.st_mode) else _FS_RUNTIME & ~(1 << 3)
            rules.append((path, rights))
        rules.extend((Path(path), (1 << 1) | (1 << 2))
                     for path in ('/dev/null', '/dev/zero', '/dev/random', '/dev/urandom'))
        for path, rights in rules:
            descriptor = os.open(path, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                attributes = PathBeneath(rights, descriptor)
                _checked(libc.syscall(445, ctypes.c_int(ruleset), ctypes.c_uint(1),
                                      ctypes.byref(attributes), ctypes.c_uint(0)))
            finally:
                os.close(descriptor)
        _checked(libc.prctl(38, 1, 0, 0, 0))  # PR_SET_NO_NEW_PRIVS
        _checked(libc.syscall(446, ctypes.c_int(ruleset), ctypes.c_uint(0)))
    finally:
        os.close(ruleset)
    return abi


class _Compare(ctypes.Structure):
    _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_int),
               ('datum_a', ctypes.c_uint64), ('datum_b', ctypes.c_uint64)]


def install_seccomp():
    """Install the native default-deny filter and return its notification FD.

    The caller must pass the listener to the trusted broker before releasing
    application code. Install in a single-threaded child, never the server.
    """
    libc = _native()
    if os.getuid() == 0 or os.geteuid() == 0:
        raise OSError(errno.EPERM, 'Native sandbox requires an unprivileged UID')
    library = ctypes.CDLL('libseccomp.so.2', use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    library.seccomp_release.restype = None
    library.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                             ctypes.c_int, ctypes.c_uint, ctypes.POINTER(_Compare)]
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_notify_fd.argtypes = [ctypes.c_void_p]
    context = library.seccomp_init(_ERRNO | errno.EPERM)
    if not context:
        raise OSError(errno.ENOMEM, 'Cannot create sandbox policy')

    def rule(name, comparisons=(), action=_ALLOW):
        number = library.seccomp_syscall_resolve_name(name.encode('ascii'))
        if number < 0:  # Absent native syscalls remain denied, including compat ABIs.
            return
        arguments = (_Compare * len(comparisons))(*(_Compare(*item) for item in comparisons))
        _checked(library.seccomp_rule_add_array(context, action, number,
                                               len(arguments), arguments), library=True)

    try:
        for name in _SYSCALLS:
            if name not in _DENIED:
                rule(name)
        # CLONE_PARENT and every namespace flag are denied. clone3 arguments
        # are pointers and cannot be inspected by classic BPF; libc may fallback.
        rule('clone', [(0, 7, 0x7e028080, 0)])
        rule('clone3', action=_ERRNO | errno.ENOSYS)
        stream_mask = 0xffffffff & ~(socket.SOCK_CLOEXEC | socket.SOCK_NONBLOCK)
        for family in (socket.AF_INET, socket.AF_INET6):
            for protocol in (0, socket.IPPROTO_TCP):
                rule('socket', [(0, 4, family, 0), (1, 7, stream_mask, socket.SOCK_STREAM),
                                (2, 4, protocol, 0)], action=_NOTIFY)
        rule('socketpair', [(0, 4, socket.AF_UNIX, 0),
                            (1, 7, stream_mask, socket.SOCK_STREAM), (2, 4, 0, 0)])
        rule('connect', action=_ERRNO)  # Return success without examining or using sockaddr.
        for name in ('chmod', 'fchmodat', 'fchmodat2', 'utime', 'utimes', 'futimesat', 'utimensat'):
            rule(name, action=_NOTIFY)
        # No read-only same-UID inputs may be granted with this policy. Runtime
        # files are root-owned, and descriptors to other chats are never inherited.
        rule('fchmod', [(1, 7, 0xffffffff & ~0o777, 0)])
        rule('prlimit64', [(0, 4, 0, 0)])  # Only the calling process, including reads.
        for request in (3, 15, 16, 21, 29, 30, 39):
            rule('prctl', [(0, 4, request, 0)])
        # Never allow F_SETOWN/F_SETOWN_EX/F_SETSIG or O_ASYNC: those can signal
        # other same-UID processes even when every signal syscall is denied.
        for operation in (0, 1, 2, 3, 5, 6, 7, 36, 37, 38, 1030, 1031, 1032, 1033, 1034):
            rule('fcntl', [(1, 4, operation, 0)])
        rule('fcntl', [(1, 4, 4, 0), (2, 7, os.O_ASYNC, 0)])
        # Exact terminal/readiness operations only; no socket ownership ioctls,
        # TIOCSTI/TIOCLINUX, controlling-terminal changes or filesystem ioctls.
        for request in (0x5401, 0x5402, 0x5403, 0x5404, 0x5409, 0x540a, 0x540b,
                        0x540f, 0x5411, 0x5413, 0x5414, 0x541b, 0x5421, 0x5424, 0x5429):
            rule('ioctl', [(1, 7, 0xffffffff, request)])
        _checked(libc.prctl(38, 1, 0, 0, 0))
        _checked(library.seccomp_load(context), library=True)
        listener = _checked(library.seccomp_notify_fd(context), library=True)
        fcntl.fcntl(listener, fcntl.F_SETFD, fcntl.FD_CLOEXEC)
        return listener
    finally:
        library.seccomp_release(context)
