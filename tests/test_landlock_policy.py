"""Native policy checks run in disposable child processes, never the test runner."""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


class LandlockPathTests(unittest.TestCase):
    def test_readonly_inputs_cannot_be_nested_in_writable_roots(self):
        from core_agent.landlock_policy import filesystem_rules

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace, scratch = root / 'workspace', root / 'scratch'
            workspace.mkdir()
            scratch.mkdir()
            inputs = workspace / 'attachments'
            inputs.mkdir()
            with self.assertRaises(ValueError):
                filesystem_rules(workspace, scratch, readonly_paths=(inputs,))

    def test_writable_roots_and_readonly_inputs_are_disjoint(self):
        from core_agent.landlock_policy import filesystem_rules

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name in ('workspace', 'scratch', 'inputs'):
                (root / name).mkdir()
            rules = filesystem_rules(root / 'workspace', root / 'scratch',
                                     readonly_paths=(root / 'inputs',))
            rights = dict(rules)
            self.assertTrue(rights[root / 'workspace'] & (1 << 1))
            self.assertFalse(rights[root / 'inputs'] & (1 << 1))
            with self.assertRaises(ValueError):
                filesystem_rules(root, root / 'scratch')
            alias = root / 'alias'
            alias.symlink_to(root / 'inputs', target_is_directory=True)
            with self.assertRaises(ValueError):
                filesystem_rules(root / 'workspace', root / 'scratch', readonly_paths=(alias,))


class NativeSeccompTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.platform != 'linux':
            if os.environ.get('CORE_AGENT_REQUIRE_NATIVE_SECCOMP_TESTS') == '1':
                raise AssertionError('Native seccomp gate requires Linux')
            raise unittest.SkipTest('Native Linux seccomp is not available')

    def run_child(self, code):
        result = subprocess.run([sys.executable, '-c', textwrap.dedent(code)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_filter_blocks_process_escape_signals_metadata_and_direct_network(self):
        self.run_child('''
            import errno, fcntl, os, socket, tempfile, threading
            from core_agent.landlock_policy import install_seccomp
            def denied(operation, expected=errno.EPERM):
                try:
                    operation()
                except OSError as error:
                    assert error.errno == expected, (error.errno, expected)
                else:
                    raise AssertionError('forbidden operation succeeded')
            with tempfile.TemporaryDirectory() as directory:
                path = directory + '/file'
                descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
                os.chmod(path, 0o600)
                listener = install_seccomp()
                assert listener >= 0
                os.close(listener)
                denied(lambda: os.kill(os.getppid(), 0))
                denied(lambda: os.setpgid(0, 0))
                denied(os.setsid)
                denied(lambda: os.chmod(path, 0o777), errno.ENOSYS)
                denied(lambda: os.fchmod(descriptor, 0o4755))
                os.fchmod(descriptor, 0o640)
                denied(lambda: os.utime(path, None), errno.ENOSYS)
                denied(lambda: fcntl.fcntl(descriptor, fcntl.F_SETOWN, os.getppid()))
                denied(lambda: fcntl.fcntl(descriptor, fcntl.F_SETFL, os.O_ASYNC))
                denied(lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
                denied(lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
                denied(lambda: socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM))
                denied(lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM), errno.ENOSYS)
                left, right = socket.socketpair()
                left.send(b'allowed')
                assert right.recv(7) == b'allowed'
                left.close()
                right.close()
                os.write(descriptor, b'allowed')
                os.close(descriptor)
                completed = []
                worker = threading.Thread(target=lambda: completed.append(True))
                worker.start()
                worker.join()
                assert completed == [True]
                child = os.fork()
                if child == 0:
                    denied(lambda: os.kill(os.getppid(), 0))
                    os._exit(0)
                assert os.waitpid(child, 0)[1] == 0
        ''')

    def test_shell_exec_and_pipes_remain_available(self):
        output = self.run_child('''
            import os
            from core_agent.landlock_policy import install_seccomp
            os.close(install_seccomp())
            os.execv('/bin/sh', ['sh', '-c', 'printf allowed | cat'])
        ''')
        self.assertEqual(output, 'allowed')


class NativeLandlockTests(unittest.TestCase):
    def test_filesystem_enforcement_and_inheritance(self):
        required = os.environ.get('CORE_AGENT_REQUIRE_LANDLOCK_TESTS') == '1'
        if sys.platform != 'linux':
            if required:
                self.fail('Native Landlock gate requires Linux')
            self.skipTest('Native Linux Landlock is not available')
        code = '''
            import errno, os, tempfile
            from pathlib import Path
            from core_agent.landlock_policy import apply_landlock
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name in ('workspace', 'scratch', 'inputs', 'foreign'):
                    (root / name).mkdir()
                (root / 'inputs' / 'source').write_text('read only')
                (root / 'foreign' / 'secret').write_text('secret')
                try:
                    abi = apply_landlock(root / 'workspace', root / 'scratch',
                                         readonly_paths=(root / 'inputs',))
                except OSError as error:
                    if error.errno in (errno.ENOSYS, errno.EOPNOTSUPP):
                        os._exit(77)
                    raise
                assert abi >= 4
                assert (root / 'inputs' / 'source').read_text() == 'read only'
                (root / 'workspace' / 'result').write_text('allowed')
                (root / 'scratch' / 'temporary').write_text('allowed')
                for path, mode in ((root / 'foreign' / 'secret', 'r'),
                                   (root / 'inputs' / 'source', 'w')):
                    try:
                        path.open(mode)
                    except PermissionError:
                        pass
                    else:
                        raise AssertionError('filesystem isolation bypass')
                (root / 'workspace' / 'alias').symlink_to(root / 'foreign')
                child = os.fork()
                if child == 0:
                    try:
                        (root / 'workspace' / 'alias' / 'secret').read_text()
                    except PermissionError:
                        os._exit(0)
                    os._exit(1)
                assert os.waitpid(child, 0)[1] == 0
                os._exit(0)
        '''
        result = subprocess.run([sys.executable, '-c', textwrap.dedent(code)],
                                capture_output=True, text=True, timeout=15)
        if result.returncode == 77 and not required:
            self.skipTest('Landlock ABI >= 4 is unavailable; no enforcement proof')
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
