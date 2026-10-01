"""Real Linux gate, run in the production-equivalent dedicated test Pod.

CORE_AGENT_REQUIRE_SANDBOX_TESTS=1 makes missing native capabilities/fixtures fail.
SANDBOX_TEST_NETWORK_FIXTURE is bounded JSON: control_url serves per-address
{host: {tcp: count, udp: count}}; targets have name, host, tcp_port, udp_port,
allowed. deploy/kubernetes/network-fixture.py implements HTTP/UDP receivers and
DNS rebinding. https_url uses a publicly trusted certificate. No mock proves
an isolation success; supervisor hooks below only inject failures into real runs.
"""
import contextlib
import dataclasses
import ipaddress
import json
import os
import platform
import select
import signal
import socket
import subprocess
import tempfile
import textwrap
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from core_agent import sandbox
from core_agent.errors import CoreError
from core_agent.guardrails import GuardrailClassifier
from core_agent.model import ModelResponse


class ClearGuardrailModel:
    """Keep native isolation probes independent of an external LLM service."""
    def generate(self, **kwargs):
        return ModelResponse(message='{"verdict":"clear"}', finish_reason='stop')

    def count_tokens(self, text):
        return max(1, (len(text.encode('utf-8')) + 2) // 3)


class SandboxLinuxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        required = os.environ.get('CORE_AGENT_REQUIRE_SANDBOX_TESTS') == '1'
        if platform.system() != 'Linux':
            if required:
                raise AssertionError('Mandatory sandbox gate requires native Linux')
            raise unittest.SkipTest('Real sandbox requires Linux; no isolation was verified')
        if not required and not os.environ.get('SANDBOX_DNS_SERVERS'):
            raise unittest.SkipTest('Run with the dedicated Linux sandbox fixture')
        cls.policy = sandbox.SandboxPolicy.from_environment()
        launcher = sandbox.SandboxLauncher(cls.policy)
        try:
            launcher.preflight()
        finally:
            launcher.close()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='sandbox-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.inputs = self.workspace / 'attachments'
        self.inputs.mkdir()
        self.launcher = sandbox.SandboxLauncher(self.policy)
        self.addCleanup(self.launcher.close)

    def start(self, code, **kwargs):
        return self.launcher.start(
            ['/usr/local/bin/python3', '-P', '-c', textwrap.dedent(code)],
            workspace_path=self.workspace, readonly_input_path=self.inputs,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)

    def run_code(self, code, timeout=15, **kwargs):
        handle = self.start(code, **kwargs)
        self.addCleanup(handle.stop)
        self.assertEqual(handle.wait(timeout), 0)

    def test_real_app_terminal_python_and_background_share_required_launcher(self):
        from core_agent.app import create_app
        from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
        from core_agent.workspace import WorkspaceBinding

        # Exercise the actual composition root. Portable unit adapters never
        # prove that model calls enter namespaces or lose server credentials.
        environment = {
            'CORE_AGENT_ENVIRONMENT': 'development',
            'CORE_AGENT_MEMORY': 'disabled',
            'SESSION_STORAGE_TYPE': 'in-memory',
            'LOCAL_WORKSPACE_ROOT': str(self.root / 'scratch'),
            'CHAT_WORKSPACE_ROOT': str(self.root / 'chats'),
            'CORE_AGENT_ALLOWED_BUILTIN_TOOLS': 'core_terminal_exec,core_python_exec,core_task_start,core_task_wait,core_task_get',
            'SANDBOX_DNS_SERVERS': ','.join(self.policy.dns_servers),
            'SANDBOX_DENIED_CIDRS': ','.join(self.policy.denied_cidrs),
            'PLATFORM_CANARY': 'server-only',
        }
        probe = (
            "import os,pathlib; "
            "assert os.getcwd()=='/workspace'; "
            "assert os.environ['HOME']=='/workspace'; "
            "assert os.environ['TMPDIR']=='/tmp'; "
            "assert 'PLATFORM_CANARY' not in os.environ; "
            "assert not pathlib.Path('/app').exists(); "
            "assert not pathlib.Path('/var/run/secrets').exists(); "
        )
        for tool in ('core_terminal_exec', 'core_python_exec', 'core_task_start'):
            code = probe + f"pathlib.Path('{tool}').write_text('isolated')"
            arguments = {'argv': ['/usr/local/bin/python3', '-P', '-c', code]}
            if tool == 'core_python_exec':
                arguments = {'code': code}
            elif tool == 'core_task_start':
                arguments = {'tool': 'core_terminal_exec', 'arguments': arguments, 'required': True}
            class AwaitBackgroundModel(ScriptedModel):
                def generate(self, **kwargs):
                    if tool == 'core_task_start' and len(self.calls) == 1:
                        output = next(json.loads(item['content'])['output']
                                      for item in kwargs['messages']
                                      if item.get('tool_call_id') == 'execute')
                        task = agent.task_scheduler.wait(output['task_id'], timeout=20)
                        if task.state != 'completed':
                            raise AssertionError(f'Background execution failed: {task.state}')
                    return super().generate(**kwargs)

            model = AwaitBackgroundModel([
                ModelResponse(tool_requests=(ToolRequest('execute', tool, arguments),)),
                ModelResponse(message='completed'),
            ])
            model.model = 'native-sandbox-test'
            with self.subTest(tool=tool), patch.dict(os.environ, environment, clear=True):
                app = create_app(model=model, guardrail_classifier=GuardrailClassifier(ClearGuardrailModel()))
                try:
                    agent = app.state.core_agent
                    manager = agent.tool_runtime.environment_manager
                    self.assertIsInstance(manager.backend.launcher, sandbox.SandboxLauncher)
                    agent.run({'prompt': 'execute the probe'}, identity='owner',
                              tenant_id='company', session_id=tool)
                    workspace = manager.backend.chats.workspace(WorkspaceBinding('company', 'owner', tool))
                    self.assertEqual((workspace / tool).read_text(), 'isolated')
                finally:
                    app.state.close()

    def test_python_nested_hitl_stops_namespace_and_resumes_only_frozen_call(self):
        from core_agent.app import create_app
        from core_agent.interactions import InMemoryInteractionStore, tool_origin
        from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
        from core_agent.workflow import SuspendedRun
        from core_agent.workspace import WorkspaceBinding

        environment = {
            'CORE_AGENT_ENVIRONMENT': 'development', 'CORE_AGENT_MEMORY': 'disabled',
            'SESSION_STORAGE_TYPE': 'in-memory', 'LOG_LEVEL': 'ERROR',
            'LOCAL_WORKSPACE_ROOT': str(self.root / 'scratch'),
            'CHAT_WORKSPACE_ROOT': str(self.root / 'chats'),
            'CORE_AGENT_ALLOWED_BUILTIN_TOOLS': 'core_python_exec,core_task_list',
            'SANDBOX_DNS_SERVERS': ','.join(self.policy.dns_servers),
            'SANDBOX_DENIED_CIDRS': ','.join(self.policy.denied_cidrs),
        }
        code = textwrap.dedent(r"""
            import os, pathlib, time
            with pathlib.Path('prefix').open('a') as marker:
                marker.write('once\n')
            print('prefix output', flush=True)
            if os.fork() == 0:
                os.setsid()
                time.sleep(2)
                pathlib.Path('escaped-descendant').write_text('must not survive')
                os._exit(0)
            try:
                tools.call('core_task_list', {})
            except BaseException:
                pathlib.Path('caught').write_text('must not receive a catchable error')
            pathlib.Path('remainder').write_text('must not run')
        """)
        for decision in ('allowed', 'rejected', 'timeout'):
            with self.subTest(decision=decision), patch.dict(os.environ, environment, clear=True):
                model = ScriptedModel([
                    ModelResponse(tool_requests=(ToolRequest('python', 'core_python_exec', {'code': code}),)),
                    ModelResponse(message='done'),
                ])
                model.model = 'native-python-wait-test'
                app = create_app(model=model, guardrail_classifier=GuardrailClassifier(ClearGuardrailModel()))
                try:
                    agent = app.state.core_agent
                    agent.guardrail_classifier.clock = lambda: agent.workflow_store.current_time()
                    manager = agent.tool_runtime.environment_manager
                    self.assertIsInstance(manager.backend.launcher, sandbox.SandboxLauncher)
                    agent.interaction_store = InMemoryInteractionStore(agent.workflow_store)
                    policy = agent.interaction_store.get_policy('company', 'core_python_exec', tool_origin('core_python_exec'))
                    agent.interaction_store.update_policy('company', policy.canonical_name, policy.origin,
                        mode='allow', guardrails_exempt=False, expected_revision=policy.revision, actor_id='owner')
                    dispatched = []
                    agent.tool_runtime.handlers['core_task_list'] = lambda *_: dispatched.append('once') or {'confirmed': True}
                    sleeping = agent.run({'prompt': 'nested action'}, task_id=decision,
                        identity='owner', tenant_id='company', session_id=decision)
                    self.assertIsInstance(sleeping, SuspendedRun)
                    workspace = manager.backend.chats.workspace(WorkspaceBinding('company', 'owner', decision))
                    self.assertEqual((workspace / 'prefix').read_text(), 'once\n')
                    self.assertFalse((workspace / 'remainder').exists())
                    self.assertFalse((workspace / 'caught').exists())
                    self.assertFalse(manager.backend.launcher._processes)
                    self.assertEqual(dispatched, [])
                    wait = agent.workflow_store.get_wait(sleeping.wait_id, tenant_id='company')
                    self.assertEqual(wait.continuation['phase'], 'python_nested')
                    if decision == 'timeout':
                        agent.workflow_store.clock = lambda: wait.deadline + 1
                        agent.workflow_store.expire_waits()
                    else:
                        agent.workflow_store.resolve_wait(wait.wait_id, tenant_id='company',
                            outcome={'reason': decision}, actor_id='owner')
                    agent._runtime_cache.clear()
                    result = agent.resume_task(decision)
                    self.assertEqual(result.message, 'done')
                    self.assertEqual(result.usage.tool_calls, 2)
                    self.assertEqual(dispatched, ['once'] if decision == 'allowed' else [])
                    self.assertIn('PYTHON_CONTINUATION_INTERRUPTED', model.calls[-1].context)
                    self.assertIn('prefix output', model.calls[-1].context)
                    time.sleep(2.1)
                    self.assertEqual((workspace / 'prefix').read_text(), 'once\n')
                    for marker in ('remainder', 'caught', 'escaped-descendant'):
                        self.assertFalse((workspace / marker).exists(), marker)
                finally:
                    app.state.close()

    def wait_file(self, path, timeout=5):
        deadline = time.monotonic() + timeout
        while not path.exists():
            if time.monotonic() >= deadline:
                self.fail(f'Missing expected sandbox output: {path.name}')
            time.sleep(.02)

    def assert_stopped(self, pidfds):
        self.assertEqual(len(select.select(pidfds, [], [], 5)[0]), len(pidfds))

    @contextlib.contextmanager
    def supervisor_hook(self, source):
        """Fault injection into the real isolated supervisor, never a fake launch."""
        original = subprocess.Popen

        def launch(argv, **kwargs):
            if len(argv) > 4 and argv[1:4] == ['-I', '-m', 'core_agent.sandbox']:
                program = (
                    'import os,signal,socket,sys\nfrom core_agent import sandbox\n'
                    + textwrap.dedent(source)
                    + '\nwith socket.socket(fileno=int(sys.argv[1])) as channel:\n'
                    + '    sandbox._supervise(channel)\n')
                argv = [argv[0], '-I', '-c', program, argv[-1]]
            return original(argv, **kwargs)

        with patch.object(sandbox.subprocess, 'Popen', side_effect=launch):
            yield

    def test_files_namespaces_descriptors_and_input_mount(self):
        credentials = patch.dict(os.environ, {'PLATFORM_CANARY': 'server-private-marker',
                                             'PYTHONPATH': str(self.workspace),
                                             'LD_PRELOAD': '/untrusted-library.so'})
        credentials.start()
        self.addCleanup(credentials.stop)
        secret = self.root / 'server-secret'
        secret.write_text('server-private-marker')
        other = self.root / 'other-chat'
        other.mkdir()
        (other / 'secret').write_text('other-private-marker')
        (self.inputs / 'accepted').write_text('input')
        (self.workspace / 'escape').symlink_to(secret)
        host_ns = {name: os.readlink(f'/proc/self/ns/{name}')
                   for name in ('mnt', 'pid', 'ipc', 'net', 'uts', 'user')}
        self.run_code(f'''
            import json,os,pathlib,shutil,socket
            root=pathlib.Path('/workspace')
            assert os.environ['TASK_CANARY']=='approved'
            assert 'LD_PRELOAD' not in os.environ and 'PYTHONPATH' not in os.environ
            for value in {repr([str(secret), str(other), '/app', '/var/run', '/run/secrets', '/workspace/escape'])}:
                assert not pathlib.Path(value).exists(), value
            for name, host in {host_ns!r}.items():
                assert os.readlink('/proc/self/ns/'+name) != host, name
            status=dict(line.split(':',1) for line in pathlib.Path('/proc/self/status').read_text().splitlines())
            assert all(int(status[key],16)==0 for key in ('CapInh','CapPrm','CapEff','CapBnd','CapAmb'))
            assert int(status['NoNewPrivs'])==1 and int(status['Seccomp'])==2
            for item in pathlib.Path('/proc/self/fd').iterdir():
                if int(item.name)>2:
                    try: os.fstat(int(item.name))
                    except OSError: continue
                    raise AssertionError('inherited descriptor: '+item.name)
            for proc in pathlib.Path('/proc').iterdir():
                if not proc.name.isdigit(): continue
                for suffix in ('root'+{str(secret)!r}, 'environ'):
                    try: data=(proc/suffix).read_bytes()
                    except OSError: continue
                    assert b'PLATFORM_CANARY' not in data and b'server-private-marker' not in data
            assert (root/'attachments/accepted').read_text()=='input'
            for operation in (lambda: (root/'attachments/accepted').write_text('changed'),
                              lambda: (root/'attachments').rename(root/'moved')):
                try: operation()
                except OSError: pass
                else: raise AssertionError('input mount is writable/replaceable')
            shutil.copyfile(root/'attachments/accepted',root/'editable')
            (root/'editable').write_text('edited')
            (root/'ok').write_text('ok')
        ''', environment={'TASK_CANARY': 'approved'})
        self.assertEqual((self.inputs / 'accepted').read_text(), 'input')
        self.assertEqual((self.workspace / 'editable').read_text(), 'edited')

    def test_two_live_workspaces_do_not_share_files(self):
        other = self.root / 'other'
        (other / 'attachments').mkdir(parents=True)
        first = self.start('''
            import pathlib,time
            pathlib.Path('first').write_text('owned')
            while not pathlib.Path('done').exists(): time.sleep(.02)
            assert not pathlib.Path('second').exists()
        ''')
        self.addCleanup(first.stop)
        self.wait_file(self.workspace / 'first')
        second = self.launcher.start(
            ['/usr/local/bin/python3', '-P', '-c',
             "import pathlib; assert not pathlib.Path('first').exists(); pathlib.Path('second').write_text('owned')"],
            workspace_path=other, readonly_input_path=other / 'attachments')
        self.addCleanup(second.stop)
        self.assertEqual(second.wait(10), 0)
        (self.workspace / 'done').touch()
        self.assertEqual(first.wait(10), 0)

    def test_inner_seccomp_denies_escape_and_keeps_threads_fork_exec(self):
        self.run_code('''
            import ctypes,ctypes.util,errno,fcntl,os,pathlib,socket,subprocess,threading
            libc=ctypes.CDLL(None,use_errno=True)
            seccomp=ctypes.CDLL('libseccomp.so.2')
            seccomp.seccomp_syscall_resolve_name.argtypes=[ctypes.c_char_p]
            # Invalid arguments still MUST receive the filter's EPERM, before
            # the kernel can report EBADF/EFAULT/EINVAL for these calls.
            for name in ('unshare','setns','mount','umount2','pivot_root','open_tree',
                         'move_mount','fsopen','fsconfig','fsmount','fspick','mount_setattr',
                         'ptrace','process_vm_readv','process_vm_writev','pidfd_getfd',
                         'open_by_handle_at','bpf','perf_event_open','keyctl',
                         'init_module','finit_module','delete_module',
                         'io_uring_setup','io_uring_enter','io_uring_register'):
                number=seccomp.seccomp_syscall_resolve_name(name.encode())
                if number < 0: continue  # syscall absent from the native ABI
                ctypes.set_errno(0)
                assert libc.syscall(number,-1,0,0,0,0,0)==-1, name
                assert ctypes.get_errno()==errno.EPERM,(name,ctypes.get_errno())
            for flag in (0x80,0x20000,0x2000000,0x4000000,0x8000000,0x10000000,0x20000000,0x40000000):
                number=seccomp.seccomp_syscall_resolve_name(b'clone')
                assert libc.syscall(number,flag|17,0,0,0,0)==-1
                assert ctypes.get_errno()==errno.EPERM
            number=seccomp.seccomp_syscall_resolve_name(b'clone3')
            if number>=0:
                assert libc.syscall(number,0,0)==-1 and ctypes.get_errno()==errno.ENOSYS
            for family,kind,protocol in ((socket.AF_PACKET,socket.SOCK_RAW,0),
                    (socket.AF_NETLINK,socket.SOCK_RAW,0),(socket.AF_INET,socket.SOCK_RAW,1)):
                try: socket.socket(family,kind,protocol)
                except PermissionError: pass
                else: raise AssertionError('privileged socket allowed')
            fd=os.open('/dev/null',os.O_RDONLY)
            try:
                for command in (0x5412,0x541c,0x100005412,0x10000541c):
                    number=seccomp.seccomp_syscall_resolve_name(b'ioctl')
                    assert libc.syscall(number,fd,ctypes.c_ulong(command),0)==-1
                    assert ctypes.get_errno()==errno.EACCES
            finally: os.close(fd)
            values=[]
            worker=threading.Thread(target=lambda:values.append('thread'))
            worker.start();worker.join();assert values==['thread']
            assert subprocess.check_output(['/bin/sh','-c','printf child'])==b'child'
            pid=os.fork()
            if pid==0: os._exit(0)
            assert os.waitpid(pid,0)[1]==0
        ''')

    def test_rlimits_are_enforced_and_cannot_be_raised(self):
        self.launcher.close()
        self.launcher = sandbox.SandboxLauncher(dataclasses.replace(
            self.policy, cpu_seconds=1, address_space_bytes=128 * 1024 * 1024,
            max_open_files=64, max_file_bytes=4096))
        self.addCleanup(self.launcher.close)
        self.run_code('''
            import errno,os,resource,signal
            for kind,value in ((resource.RLIMIT_CPU,1),(resource.RLIMIT_AS,134217728),
                               (resource.RLIMIT_NOFILE,64),(resource.RLIMIT_FSIZE,4096),(resource.RLIMIT_CORE,0)):
                assert resource.getrlimit(kind)==(value,value)
                try: resource.setrlimit(kind,(value+1,value+1))
                except (ValueError,PermissionError): pass
                else: raise AssertionError('raised hard limit')
            try: allocation=bytearray(256*1024*1024)
            except MemoryError: pass
            else: raise AssertionError('address-space limit missing')
            descriptors=[]
            try:
                for _ in range(65): descriptors.append(os.open('/dev/null',os.O_RDONLY))
            except OSError as error: assert error.errno==errno.EMFILE
            else: raise AssertionError('descriptor limit missing')
            finally:
                for descriptor in descriptors: os.close(descriptor)
            signal.signal(signal.SIGXFSZ,signal.SIG_IGN)
            with open('bounded','wb',buffering=0) as output:
                output.write(b'a'*4096)
                try: output.write(b'b')
                except OSError as error: assert error.errno==errno.EFBIG
                else: raise AssertionError('file-size limit missing')
        ''')
        cpu = self.start('while True: pass')
        self.addCleanup(cpu.stop)
        self.assertNotEqual(cpu.wait(8), 0)

    def test_normal_exit_cancel_and_helper_death_remove_daemon_descendants(self):
        for outcome in ('normal', 'cancel', 'slirp', 'supervisor'):
            with self.subTest(outcome=outcome):
                marker = self.workspace / 'alive'
                marker.unlink(missing_ok=True)
                handle = self.start(f'''
                    import os,pathlib,time
                    pid=os.fork()
                    if pid==0:
                        os.setsid()
                        if os.fork(): os._exit(0)
                        for fd in (0,1,2): os.close(fd)
                        while True:
                            pathlib.Path('alive').write_text(str(time.monotonic_ns()))
                            time.sleep(.02)
                    while not pathlib.Path('alive').exists(): time.sleep(.01)
                    {'raise SystemExit(23)' if outcome == 'normal' else 'time.sleep(60)'}
                ''')
                self.addCleanup(handle.stop)
                self.wait_file(marker)
                descriptors = [os.dup(fd) for fd in handle._pidfds]
                try:
                    if outcome == 'normal':
                        self.assertEqual(handle.wait(5), 23)
                    elif outcome == 'cancel':
                        with self.assertRaises(subprocess.TimeoutExpired):
                            handle.wait(.01)
                        handle.stop(.5)
                        handle.stop(.5)
                    else:
                        if outcome == 'slirp':
                            signal.pidfd_send_signal(descriptors[-1], signal.SIGKILL)
                        else:
                            handle._process.kill()
                        with self.assertRaises(CoreError) as caught:
                            handle.wait(5)
                        self.assertEqual(caught.exception.code, 'SIDE_EFFECT_UNKNOWN')
                    self.assert_stopped(descriptors)
                    before = marker.read_bytes()
                    time.sleep(.1)
                    self.assertEqual(marker.read_bytes(), before)
                finally:
                    for fd in descriptors:
                        os.close(fd)

    def test_bootstrap_failure_or_supervisor_death_never_releases_target(self):
        hooks = (
            "def failed(): raise RuntimeError('seccomp failure')\nsandbox._seccomp_fd=failed\n",
            "original=sandbox.subprocess.run\n"
            "def failed(argv,**kwargs):\n"
            "    if 'nft' in argv: raise RuntimeError('nft failure')\n"
            "    return original(argv,**kwargs)\n"
            "sandbox.subprocess.run=failed\n",
            "original=sandbox.subprocess.Popen\n"
            "def failed(argv,**kwargs):\n"
            "    if 'slirp4netns' in argv: raise RuntimeError('slirp failure')\n"
            "    return original(argv,**kwargs)\n"
            "sandbox.subprocess.Popen=failed\n",
            "original=sandbox._read_deadline\n"
            "def failed(fd,length,deadline):\n"
            "    if length==5: os.kill(os.getpid(),signal.SIGKILL)\n"
            "    return original(fd,length,deadline)\n"
            "sandbox._read_deadline=failed\n",
            "original=sandbox._send\n"
            "def failed(channel,message,descriptors=()):\n"
            "    if message['state']=='released': os.kill(os.getpid(),signal.SIGKILL)\n"
            "    return original(channel,message,descriptors)\n"
            "sandbox._send=failed\n",
        )
        for index, hook in enumerate(hooks):
            (self.workspace / 'forbidden').unlink(missing_ok=True)
            with self.subTest(stage=index), self.supervisor_hook(hook):
                with self.assertRaises(CoreError) as caught:
                    self.start("from pathlib import Path; Path('forbidden').touch()")
                self.assertEqual(caught.exception.code, 'EXECUTION_ENVIRONMENT_UNAVAILABLE')
                self.assertFalse((self.workspace / 'forbidden').exists())
                self.assertFalse(self.launcher._processes)

    def test_exec_failure_is_predispatch_and_keeps_known_error(self):
        with self.assertRaises(CoreError) as caught:
            self.launcher.start(['/missing/executable'], workspace_path=self.workspace,
                                readonly_input_path=self.inputs)
        self.assertEqual(caught.exception.code, 'EXECUTION_ENVIRONMENT_UNAVAILABLE')

    def test_broker_mount_exposes_only_the_current_socket(self):
        own, foreign = socket.socket(socket.AF_UNIX), socket.socket(socket.AF_UNIX)
        self.addCleanup(own.close)
        self.addCleanup(foreign.close)
        own.bind(str(self.root / 'own.sock'))
        foreign.bind(str(self.root / 'foreign.sock'))
        own.listen(1)
        foreign.listen(1)
        handle = self.start(f'''
            import pathlib,socket
            assert not pathlib.Path({str(self.root / 'foreign.sock')!r}).exists()
            sock=socket.socket(socket.AF_UNIX)
            sock.connect('/run/core-agent/broker.sock')
            sock.sendall(b'current-run')
            assert sock.recv(16)==b'ok'
            sock.close()
        ''', broker_socket=self.root / 'own.sock')
        self.addCleanup(handle.stop)
        own.settimeout(5)
        connection, _ = own.accept()
        with connection:
            self.assertEqual(connection.recv(16), b'current-run')
            connection.sendall(b'ok')
        self.assertEqual(handle.wait(5), 0)
        self.assertFalse(select.select([foreign], [], [], 0)[0])

    def test_public_dual_stack_and_forbidden_receivers(self):
        raw = os.environ.get('SANDBOX_TEST_NETWORK_FIXTURE', '')
        self.assertTrue(raw, 'Missing controlled dual-stack/receiver-counter fixture')
        self.assertLessEqual(len(raw), 65536)
        fixture = json.loads(raw)
        targets = fixture['targets']
        self.assertTrue({4, 6}.issubset({ipaddress.ip_address(t['host']).version
                                       for t in targets if t['allowed']}))
        self.assertTrue({4, 6}.issubset({ipaddress.ip_address(t['host']).version
                                       for t in targets if not t['allowed']}))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def counters():
            with opener.open(fixture['control_url'], timeout=5) as response:
                return json.load(response)

        reset = fixture['control_url'].rsplit('/', 1)[0] + '/reset'
        with opener.open(urllib.request.Request(reset, data=b'', method='POST'), timeout=5):
            pass
        # Every prohibited receiver is demonstrably alive outside the sandbox.
        for target in targets:
            host = '[' + target['host'] + ']' if ':' in target['host'] else target['host']
            with opener.open(f'http://{host}:{target["tcp_port"]}/', timeout=3) as response:
                self.assertEqual(json.load(response)['address'], target['host'])
            family = socket.AF_INET6 if ':' in target['host'] else socket.AF_INET
            with socket.socket(family, socket.SOCK_DGRAM) as control:
                control.settimeout(3)
                control.connect((target['host'], target['udp_port']))
                control.send(b'control')
                self.assertEqual(json.loads(control.recv(512))['address'], target['host'])
        before = counters()
        self.run_code(f'''
            import ipaddress,json,socket,urllib.request
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({{}}))
            for target in {targets!r}:
                for transport,kind in (('tcp',socket.SOCK_STREAM),('udp',socket.SOCK_DGRAM)):
                    family=socket.AF_INET6 if ':' in target['host'] else socket.AF_INET
                    with socket.socket(family,kind) as client:
                        client.settimeout(.5)
                        try:
                            client.connect((target['host'],target[transport+'_port']))
                            if transport=='tcp':
                                client.sendall(b'GET / HTTP/1.0\\r\\nHost: test\\r\\n\\r\\n')
                                response=b''
                                while True:
                                    part=client.recv(4096)
                                    if not part: break
                                    response+=part
                                result=json.loads(response.partition(b'\\r\\n\\r\\n')[2])
                            else:
                                client.send(b'sandbox')
                                result=json.loads(client.recv(512))
                        except OSError:
                            assert not target['allowed'],target
                        else:
                            assert target['allowed'] and result['address']==target['host'],target
            public4=next(t for t in {targets!r} if t['allowed'] and ':' not in t['host'])
            urls=[f"http://{{public4['host']}}:{{public4['tcp_port']}}/redirect?v={{v}}" for v in (4,6)]
            urls += ['http://private.test:18080/','http://protected.test:18080/',
                     'http://[::ffff:10.77.0.1]:18080/',
                     'http://'+str(int(ipaddress.ip_address('10.77.0.1')))+':18080/']
            for url in urls:
                try: opener.open(url,timeout=.5)
                except OSError: pass
                else: raise AssertionError('protected redirect/DNS/alternate address connected: '+url)
            with opener.open('http://rebind.test:18080/',timeout=3) as response:
                assert response.status==200
            try: opener.open('http://rebind.test:18080/',timeout=.5)
            except OSError: pass
            else: raise AssertionError('DNS rebinding bypassed destination enforcement')
            with opener.open({fixture['https_url']!r},timeout=10) as response:
                assert response.status==200
        ''', timeout=max(20, len(targets) * 2 + 15), environment={})
        after = counters()
        for target in targets:
            for transport in ('tcp', 'udp'):
                delta = after[target['host']][transport] - before[target['host']][transport]
                self.assertGreater(delta, 0) if target['allowed'] else self.assertEqual(delta, 0)

    def test_pod_pid_limit_with_bounded_fork(self):
        self.assertEqual(os.environ.get('SANDBOX_TEST_DEDICATED_POD'), '1',
                         'PID ceiling check requires an explicitly dedicated disposable Pod')
        # Container cgroup namespaces hide the tighter aggregate Pod parent.
        # Never scale this probe to a possibly much larger visible limit.
        visible_ceiling = int(Path('/sys/fs/cgroup/pids.max').read_text().strip())
        self.assertGreater(visible_ceiling, 0)
        self.run_code('''
            import errno,os,signal
            children=[]
            limited=False
            try:
                for _ in range(513):
                    try: child=os.fork()
                    except OSError as error:
                        assert error.errno==errno.EAGAIN
                        limited=True
                        break
                    if child==0:
                        signal.pause()
                        os._exit(0)
                    children.append(child)
                assert limited,'Pod pids limit was not enforced'
            finally:
                for child in children: os.kill(child,signal.SIGKILL)
                for child in children: os.waitpid(child,0)
        ''', timeout=20)


if __name__ == '__main__':
    unittest.main()
