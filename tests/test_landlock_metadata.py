import array
import ctypes
import errno
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class NativeMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.platform != 'linux':
            if os.environ.get('CORE_AGENT_REQUIRE_NATIVE_SECCOMP_TESTS') == '1':
                raise AssertionError('Native metadata gate requires Linux')
            raise unittest.SkipTest('Native metadata mediation requires Linux')

    def test_real_memory_copy_only_changes_pinned_workspace_files(self):
        from core_agent.landlock_metadata import MetadataBroker, metadata_syscalls

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, scratch, foreign = (root / name for name in ('work', 'scratch', 'foreign'))
            for path in (workspace, scratch, foreign):
                path.mkdir()
                (path / 'file').write_text('contents')
            foreign_mode = (foreign / 'file').stat().st_mode & 0o777
            (workspace / 'alias').symlink_to(foreign, target_is_directory=True)
            with MetadataBroker(workspace, scratch) as broker:
                number = next(number for number, name in metadata_syscalls().items() if name == 'fchmodat')

                def change(path, mode=0o640, dirfd=-100, flags=0):
                    memory = ctypes.create_string_buffer(os.fsencode(path))
                    arguments = (ctypes.c_uint64 * 6)(dirfd & ((1 << 64) - 1),
                                                       ctypes.addressof(memory), mode, flags, 0, 0)
                    request = SimpleNamespace(pid=os.getpid(),
                                              data=SimpleNamespace(nr=number, args=arguments))
                    return broker(request)

                self.assertEqual(change(workspace / 'file'), 0)
                self.assertEqual((workspace / 'file').stat().st_mode & 0o777, 0o640)
                self.assertEqual(change(scratch / 'file'), 0)
                for path, mode in ((foreign / 'file', 0o777),
                                   (workspace / 'alias' / 'file', 0o777),
                                   (workspace / '..' / 'foreign' / 'file', 0o777),
                                   (workspace / 'file', 0o4755)):
                    with self.subTest(path=path, mode=mode), self.assertRaises(OSError):
                        change(path, mode)
                self.assertEqual((foreign / 'file').stat().st_mode & 0o777, foreign_mode)
                descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    self.assertEqual(change('file', 0o600, dirfd=descriptor), 0)
                finally:
                    os.close(descriptor)
                with self.assertRaises(OSError) as caught:
                    broker(SimpleNamespace(pid=os.getpid(),
                                           data=SimpleNamespace(nr=123456, args=(0,) * 6)))
                self.assertEqual(caught.exception.errno, errno.EPERM)

    def test_timestamps_are_bounded_and_confined_for_path_and_fd_calls(self):
        from core_agent.landlock_metadata import MetadataBroker, metadata_syscalls

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, scratch = root / 'work', root / 'scratch'
            workspace.mkdir()
            scratch.mkdir()
            owned, foreign = workspace / 'file', root / 'foreign'
            for path in (owned, foreign):
                path.write_text('contents')
            original_foreign = foreign.stat().st_mtime_ns
            number = next(number for number, name in metadata_syscalls().items() if name == 'utimensat')
            with MetadataBroker(workspace, scratch) as broker:
                def update(path, values=(123, 456, 789, 123), *, descriptor=-100, flags=0):
                    pathname = ctypes.create_string_buffer(os.fsencode(path)) if path is not None else None
                    timestamps = (ctypes.c_int64 * 4)(*values)
                    request = SimpleNamespace(pid=os.getpid(), data=SimpleNamespace(nr=number,
                        args=(descriptor & ((1 << 64) - 1), ctypes.addressof(pathname) if pathname else 0,
                              ctypes.addressof(timestamps), flags, 0, 0)))
                    return broker(request)

                self.assertEqual(update(owned), 0)
                self.assertEqual(owned.stat().st_atime_ns, 123_000_000_456)
                self.assertEqual(owned.stat().st_mtime_ns, 789_000_000_123)
                self.assertEqual(update(owned, (0, 1073741822, 456, 789)), 0)
                self.assertEqual(owned.stat().st_atime_ns, 123_000_000_456)
                descriptor = os.open(owned, os.O_RDONLY)
                try:
                    self.assertEqual(update(None, descriptor=descriptor), 0)
                finally:
                    os.close(descriptor)
                for path, values, flags in ((foreign, (123, 456, 789, 123), 0),
                                             (owned, (0, -1, 0, 0), 0),
                                             (owned, (0, 0, 0, 0), 0x1000)):
                    with self.assertRaises(OSError):
                        update(path, values, flags=flags)
                self.assertEqual(foreign.stat().st_mtime_ns, original_foreign)

    def test_directory_swap_keeps_mutation_on_pinned_directory(self):
        from core_agent.landlock_metadata import MetadataBroker, metadata_syscalls

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, scratch, foreign = root / 'work', root / 'scratch', root / 'foreign'
            for path in (workspace, scratch, foreign):
                path.mkdir()
            nested = workspace / 'nested'
            nested.mkdir()
            (nested / 'file').write_text('owned')
            (foreign / 'file').write_text('foreign')
            original_mode = (foreign / 'file').stat().st_mode & 0o777
            with MetadataBroker(workspace, scratch) as broker:
                number = next(number for number, name in metadata_syscalls().items() if name == 'fchmodat')
                pathname = ctypes.create_string_buffer(os.fsencode(nested / 'file'))
                request = SimpleNamespace(pid=os.getpid(), data=SimpleNamespace(nr=number,
                    args=((-100) & ((1 << 64) - 1), ctypes.addressof(pathname), 0o600, 0, 0, 0)))
                original_open = os.open
                swapped = False

                def raced_open(path, flags, *args, **kwargs):
                    nonlocal swapped
                    if path == 'file' and not swapped:
                        swapped = True
                        nested.rename(workspace / 'held')
                        nested.symlink_to(foreign, target_is_directory=True)
                    return original_open(path, flags, *args, **kwargs)

                with patch('core_agent.landlock_metadata.os.open', side_effect=raced_open):
                    self.assertEqual(broker(request), 0)
                self.assertTrue(swapped)
                self.assertEqual((workspace / 'held' / 'file').stat().st_mode & 0o777, 0o600)
                self.assertEqual((foreign / 'file').stat().st_mode & 0o777, original_mode)
                with self.assertRaises(OSError):
                    broker(request)

    def test_git_init_and_clone_with_real_seccomp_metadata_notifications(self):
        from core_agent.landlock_metadata import MetadataBroker
        from core_agent.landlock_network import EgressBroker
        from core_agent.landlock_policy import install_seccomp
        from core_agent.sandbox import SandboxPolicy

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, scratch = root / 'work', root / 'scratch'
            workspace.mkdir()
            scratch.mkdir()
            subprocess.run(['git', 'init', '-q', str(workspace / 'source')], check=True)
            subprocess.run(['git', '-C', str(workspace / 'source'), '-c', 'user.name=Probe',
                            '-c', 'user.email=probe@example.invalid', 'commit', '-q',
                            '--allow-empty', '-m', 'probe'], check=True)
            with MetadataBroker(workspace, scratch) as metadata:
                parent, child = socket.socketpair()
                pid = os.fork()
                if pid == 0:
                    parent.close()
                    metadata.close()
                    try:
                        os.setsid()
                        os.chdir(workspace)
                        listener = install_seccomp()
                        child.sendmsg([b'L'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                               array.array('i', [listener]))])
                        os.close(listener)
                        if child.recv(1) != b'G':
                            os._exit(90)
                        child.close()
                        os.execv('/bin/sh', ['sh', '-c',
                                            'git init -q initialized && git clone -q --no-hardlinks source clone'])
                    except BaseException:
                        os._exit(91)
                child.close()
                parent.settimeout(5)
                broker = None
                pidfd = os.pidfd_open(pid)
                try:
                    message, ancillary, _, _ = parent.recvmsg(8, socket.CMSG_SPACE(4))
                    self.assertEqual(message, b'L')
                    descriptors = array.array('i')
                    descriptors.frombytes(ancillary[0][2])
                    broker = EgressBroker(SandboxPolicy(('1.1.1.1',), ('198.19.0.0/16',)),
                                          metadata_handler=metadata)
                    broker.start_notifications(descriptors[0])
                    parent.sendall(b'G')
                    self.assertTrue(select.select([pidfd], [], [], 10)[0], 'Git child did not finish')
                    _, status = os.waitpid(pid, 0)
                    pid = None
                    self.assertEqual(os.waitstatus_to_exitcode(status), 0)
                    self.assertTrue((workspace / 'clone' / '.git' / 'HEAD').is_file())
                finally:
                    parent.close()
                    os.close(pidfd)
                    if pid is not None:
                        try:
                            os.killpg(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            os.kill(pid, signal.SIGKILL)
                        os.waitpid(pid, 0)
                    if broker is not None:
                        broker.close()


if __name__ == '__main__':
    unittest.main()
