"""Emulate notified chmod/timestamps on pinned workspace files, never CONTINUE.

Only the trusted direct supervisor reads the child's bounded pathname. Every
actual mutation uses its own descriptor opened beneath a pinned allowed root;
no child pointer or pathname is passed back for execution in the kernel.
"""
import ctypes
import errno
import os
import platform
import struct
from pathlib import Path, PurePosixPath

from .landlock_policy import filesystem_rules


def metadata_syscalls():
    return {'x86_64': {90: 'chmod', 268: 'fchmodat', 452: 'fchmodat2',
                      132: 'utime', 235: 'utimes', 261: 'futimesat', 280: 'utimensat'},
            'aarch64': {53: 'fchmodat', 452: 'fchmodat2', 88: 'utimensat'}}.get(platform.machine(), {})


class _IOVec(ctypes.Structure):
    _fields_ = [('base', ctypes.c_void_p), ('length', ctypes.c_size_t)]


class _Timespec(ctypes.Structure):
    _fields_ = [('seconds', ctypes.c_int64), ('nanoseconds', ctypes.c_int64)]


class MetadataBroker:
    def __init__(self, workspace, scratch):
        self._roots = []
        self._syscalls = metadata_syscalls()
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._libc.process_vm_readv.argtypes = [ctypes.c_int, ctypes.POINTER(_IOVec), ctypes.c_ulong,
                                              ctypes.POINTER(_IOVec), ctypes.c_ulong, ctypes.c_ulong]
        self._libc.process_vm_readv.restype = ctypes.c_ssize_t
        self._libc.futimens.argtypes = [ctypes.c_int, ctypes.POINTER(_Timespec)]
        try:
            for path, _ in filesystem_rules(workspace, scratch):
                descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
                self._roots.append((path, descriptor))
        except BaseException:
            self.close()
            raise

    def _read(self, pid, address, size):
        if not pid or not address:
            raise OSError(errno.EFAULT, 'Invalid metadata pointer')
        memory = ctypes.create_string_buffer(size)
        local = _IOVec(ctypes.addressof(memory), len(memory))
        remote = _IOVec(address, len(memory))
        count = self._libc.process_vm_readv(pid, ctypes.byref(local), 1, ctypes.byref(remote), 1, 0)
        if count < 0:
            raise OSError(ctypes.get_errno(), 'Metadata arguments unavailable')
        return memory.raw[:count]

    def _pathname(self, pid, address):
        raw = self._read(pid, address, 4097)
        terminator = raw.find(b'\0')
        if terminator < 0 or terminator > 4095:
            raise OSError(errno.ENAMETOOLONG, 'Metadata pathname exceeds limit')
        if not terminator:
            raise OSError(errno.ENOENT, 'Metadata pathname is empty')
        return os.fsdecode(raw[:terminator])

    def _timestamps(self, pid, address, operation):
        if not address:
            return None
        size = 16 if operation == 'utime' else 32
        raw = self._read(pid, address, size)
        if len(raw) != size:
            raise OSError(errno.EFAULT, 'Incomplete metadata timestamps')
        values = struct.unpack('=2q' if size == 16 else '=4q', raw)
        if operation == 'utime':
            values = (values[0], 0, values[1], 0)
        elif operation in ('utimes', 'futimesat'):
            if not all(0 <= values[index] < 1_000_000 for index in (1, 3)):
                raise OSError(errno.EINVAL, 'Invalid metadata microseconds')
            values = (values[0], values[1] * 1000, values[2], values[3] * 1000)
        if not all(0 <= values[index] < 1_000_000_000 or values[index] in (1073741822, 1073741823)
                   for index in (1, 3)):
            raise OSError(errno.EINVAL, 'Invalid metadata nanoseconds')
        return (_Timespec * 2)(_Timespec(*values[:2]), _Timespec(*values[2:]))

    @staticmethod
    def _descriptor_path(pid, descriptor):
        if descriptor == -100:
            base = os.readlink(f'/proc/{pid}/cwd')
        elif descriptor >= 0:
            base = os.readlink(f'/proc/{pid}/fd/{descriptor}')
        else:
            raise OSError(errno.EBADF, 'Invalid metadata descriptor')
        if not base.startswith('/') or base.endswith(' (deleted)'):
            raise OSError(errno.EACCES, 'Metadata path unavailable')
        return PurePosixPath(base)

    def _open(self, path):
        if '..' in path.parts:
            raise OSError(errno.EACCES, 'Metadata path is outside the workspace')
        for root, root_fd in self._roots:
            if not path.is_relative_to(root):
                continue
            descriptor = os.dup(root_fd)
            try:
                parts = path.relative_to(root).parts
                for index, part in enumerate(parts):
                    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
                    if index < len(parts) - 1:
                        flags |= os.O_DIRECTORY
                    following = os.open(part, flags, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = following
                return descriptor
            except BaseException:
                os.close(descriptor)
                raise
        raise OSError(errno.EACCES, 'Metadata path is outside the workspace')

    def __call__(self, notification):
        operation = self._syscalls.get(notification.data.nr)
        if operation is None:
            raise OSError(errno.EPERM, 'Metadata operation denied')
        arguments = notification.data.args
        absolute_api = operation in ('chmod', 'utime', 'utimes')
        dirfd = -100 if absolute_api else ctypes.c_int32(arguments[0] & 0xffffffff).value
        address, operand = arguments[:2] if absolute_api else arguments[1:3]
        flags = (arguments[3] & 0xffffffff) if operation in ('fchmodat2', 'utimensat') else 0
        if flags not in (0, 0x100):  # AT_SYMLINK_NOFOLLOW; symlink targets still denied.
            raise OSError(errno.EPERM, 'Metadata flags denied')
        changing_mode = operation in ('chmod', 'fchmodat', 'fchmodat2')
        if changing_mode:
            operand &= 0xffffffff
            if operand & ~0o777:
                raise OSError(errno.EPERM, 'Metadata mode denied')
            timestamps = None
        else:
            timestamps = self._timestamps(notification.pid, operand, operation)
        if not address and operation in ('utimensat', 'futimesat') and dirfd >= 0 and not flags:
            path = self._descriptor_path(notification.pid, dirfd)
        else:
            path = PurePosixPath(self._pathname(notification.pid, address))
        if not path.is_absolute():
            path = self._descriptor_path(notification.pid, dirfd) / path
        descriptor = self._open(Path(path))
        try:
            if changing_mode:
                os.fchmod(descriptor, operand)
            elif self._libc.futimens(descriptor, timestamps) < 0:
                raise OSError(ctypes.get_errno(), 'Metadata timestamps failed')
        finally:
            os.close(descriptor)
        return 0

    def close(self):
        for _, descriptor in self._roots:
            os.close(descriptor)
        self._roots.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
