import dataclasses
import errno
import importlib.util
import ipaddress
import io
import json
import os
import socket
import subprocess
import threading
import time
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from core_agent.errors import CoreError, ExecutionNotStarted


class SandboxIntegrationTests(unittest.TestCase):
    def setUp(self):
        from core_agent.execution import EnvironmentSpec, LocalTerminalBackend, TerminalSessionManager

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.launcher = Mock(_unhealthy=False)
        self.process = Mock(pid=12345, returncode=None)
        self.finished = threading.Event()

        def wait():
            if not self.finished.wait(5):
                raise AssertionError('test did not complete its process')
            return self.process.returncode

        def stop():
            self.process.returncode = -9
            self.finished.set()
            return -9

        self.process.wait.side_effect = wait
        self.process.stop.side_effect = stop
        self.launcher.start.return_value = self.process
        self.manager = TerminalSessionManager(LocalTerminalBackend(self.root, launcher=self.launcher))
        self.session = self.manager.create(EnvironmentSpec(
            'tenant', 'run', '', ('.',), (), {'SELECTED': 'approved', 'OTHER': 'hidden'},
            environment_allowlist=('VISIBLE',),
        ))

    def test_terminal_uses_trusted_paths_clean_environment_and_confirmed_cleanup(self):
        (self.session.workspace / 'nested').mkdir()
        with patch.dict(os.environ, {'PLATFORM_SECRET': 'hidden'}):
            handle = self.manager.start(self.session.id, {
                'argv': ['echo', 'ok'], 'cwd': 'nested', 'env': {'VISIBLE': 'yes'},
                'secret_refs': ['SELECTED'], '_broker_socket': '/untrusted',
            })
        called = self.launcher.start.call_args
        self.assertEqual(called.args, (['echo', 'ok'],))
        values = called.kwargs
        self.assertEqual(values['workspace_path'], self.session.workspace)
        self.assertEqual(values['readonly_input_path'], self.session.workspace / 'attachments')
        self.assertEqual(values['cwd'], 'nested')
        self.assertIsNone(values['broker_socket'])
        self.assertEqual(values['environment']['HOME'], '/workspace')
        self.assertEqual(values['environment']['TMPDIR'], '/tmp')
        self.assertEqual(values['environment']['SELECTED'], 'approved')
        self.assertNotIn('OTHER', values['environment'])
        self.assertNotIn('PLATFORM_SECRET', values['environment'])
        self.manager.cancel(self.session.id, handle.id)
        result = self.manager.wait(self.session.id, handle.id)
        self.assertEqual(result.cleanup, 'sandbox_terminated')
        self.assertEqual(result.exit_code, -9)
        self.manager.close()
        self.launcher.close.assert_called_once()

    def test_wait_failure_survives_thread_and_prevents_snapshot_and_delete(self):
        error = CoreError('SIDE_EFFECT_UNKNOWN')
        self.process.wait.side_effect = error
        snapshots = self.session.snapshot_store = Mock()
        handle = self.manager.start(self.session.id, {'argv': ['true']})
        with self.assertRaises(CoreError) as caught:
            self.manager.wait(self.session.id, handle.id)
        self.assertIs(caught.exception, error)
        with self.assertRaises(CoreError):
            self.manager.destroy(self.session.id)
        snapshots.publish.assert_not_called()
        self.assertTrue(self.session.workspace.exists())
        self.assertIn(self.session.id, self.manager._environments)

    def test_supervisor_os_error_keeps_private_diagnosis_across_execution_thread(self):
        from core_agent.errors import SandboxDiagnostics
        error = OSError(errno.EBADF, 'private-token-/private/company')
        diagnostic = error.diagnostics = SandboxDiagnostics(stage='supervisor', reason='os_error',
            errno=errno.EBADF, elapsed_ms=3, cleanup='confirmed')
        self.process.wait.side_effect = error
        handle = self.manager.start(self.session.id, {'argv': ['true']})
        with self.assertRaises(CoreError) as caught:
            self.manager.wait(self.session.id, handle.id)
        self.assertEqual(caught.exception.code, 'SIDE_EFFECT_UNKNOWN')
        self.assertIs(caught.exception.diagnostics, diagnostic)
        self.assertEqual(caught.exception.data, {})
        self.assertNotIn('private-token', str(caught.exception))

    def test_failed_stop_never_claims_cleanup_or_removes_workspace(self):
        error = CoreError('EXECUTION_ENVIRONMENT_UNAVAILABLE')
        self.process.stop.side_effect = error
        handle = self.manager.start(self.session.id, {'argv': ['sleep', '10']})
        with self.assertRaises(CoreError) as caught:
            self.manager.wait(self.session.id, handle.id, timeout=.01)
        self.assertIs(caught.exception, error)
        self.assertEqual(handle._cleanup, 'unconfirmed')
        with self.assertRaises(CoreError):
            self.manager.destroy(self.session.id)
        self.assertTrue(self.session.workspace.exists())
        with self.assertRaises(CoreError):
            self.manager.close_execution_tree('run', 'cleanup-intent')
        with self.assertRaises(CoreError) as blocked:
            self.manager.execute_transient({'argv': ['true']}, 'run')
        self.assertEqual(blocked.exception.code, 'TASK_TERMINAL')
        self.finished.set()
        handle._done.wait(1)

    def test_destroy_waits_for_inflight_start_and_stops_registered_process(self):
        started, release = threading.Event(), threading.Event()

        def launch(*args, **kwargs):
            started.set()
            self.assertTrue(release.wait(2))
            return self.process

        self.launcher.start.side_effect = launch
        with ThreadPoolExecutor(2) as pool:
            starting = pool.submit(self.session.start, {'argv': ['sleep', '10']})
            self.assertTrue(started.wait(1))
            destroying = pool.submit(self.session.destroy)
            self.assertFalse(destroying.done())
            release.set()
            handle = starting.result(2)
            destroying.result(2)
        self.process.stop.assert_called_once()
        self.assertTrue(handle._done.is_set())
        self.assertFalse(self.session.workspace.exists())

    def test_attachments_symlink_is_rejected_before_launcher(self):
        (self.session.workspace / 'attachments').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(CoreError):
            self.manager.start(self.session.id, {'argv': ['true']})
        self.launcher.start.assert_not_called()
        self.session.destroy()

    def test_concurrent_destroy_publishes_one_unchanged_snapshot(self):
        publishing, release, repeated = threading.Event(), threading.Event(), threading.Event()
        publications = []
        (self.session.workspace / 'saved.txt').write_text('preserved')
        def publish(workspace, **kwargs):
            publications.append(tuple(path.name for path in workspace.iterdir()))
            publishing.set()
            if len(publications) == 1:
                self.assertTrue(release.wait(2))
            else:
                repeated.set()
            return 'snapshot'
        self.session.snapshot_store = Mock(publish=publish)
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.session.destroy)
            self.assertTrue(publishing.wait(1))
            second = pool.submit(self.session.destroy)
            repeated.wait(.05)
            release.set()
            self.assertEqual(first.result(2), 'snapshot')
            self.assertEqual(second.result(2), 'snapshot')
        self.assertEqual(len(publications), 1)
        self.assertIn('saved.txt', publications[0])

    def test_python_mount_and_stop_hook_are_trusted_arguments(self):
        from core_agent.python_exec import execute_python

        broker = Mock(path=self.root / 'broker.sock', token='test-token')
        manager = Mock()
        callback = Mock()
        with patch('core_agent.python_exec.PythonToolBroker') as factory:
            factory.return_value.__enter__.return_value = broker
            execute_python(manager, run_id='run', code='pass', tool_names=(),
                           dispatch=None, on_start=callback)
        request = manager.execute_transient.call_args.args[0]
        self.assertEqual(request['argv'][0], '/usr/local/bin/python3')
        self.assertEqual(request['argv'][-2], '/run/core-agent/broker.sock')
        self.assertNotIn(str(broker.path), request['argv'])
        self.assertEqual(manager.execute_transient.call_args.kwargs,
                         {'broker_socket': broker.path, 'on_start': callback})
        self.session.destroy()

    def test_every_serving_profile_fails_closed_before_agent_or_recovery(self):
        from core_agent.app import create_app
        from core_agent.model import ScriptedModel

        for mode in ('development', 'test', 'production'):
            with self.subTest(mode=mode):
                model = ScriptedModel([])
                model.model = 'startup-test'
                values = {'CORE_AGENT_ENVIRONMENT': mode,
                          'PUSH_NOTIFICATION_ENCRYPTION_KEY': 'configured',
                          'SANDBOX_DNS_SERVERS': '1.1.1.1',
                          'SANDBOX_DENIED_CIDRS': '10.0.0.0/8'}
                with patch.dict(os.environ, values, clear=True), \
                        patch('core_agent.app.AuthSettings.from_environment', return_value=None), \
                        patch('core_agent.app._state', return_value={'database': None}), \
                        patch('platform.system', return_value='Darwin'), \
                        patch('core_agent.app._agent') as compose, \
                        patch('core_agent.sandbox.subprocess.Popen') as spawn, \
                        patch('core_agent.runtime.CoreAgent.recover_workflows') as recovery:
                    with self.assertRaises(CoreError) as caught:
                        create_app(model=model)
                self.assertEqual(caught.exception.code, 'EXECUTION_ENVIRONMENT_UNAVAILABLE')
                compose.assert_not_called()
                spawn.assert_not_called()
                recovery.assert_not_called()
                self.assertFalse(model.calls)

    def test_actual_composition_terminal_python_and_background_errors_have_private_operator_diagnostics(self):
        from core_agent.errors import SandboxDiagnostics
        from core_agent.guardrails import GuardrailClassifier
        from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
        from tests.app_support import ClearGuardrailModel, UnsandboxedTestLauncher, create_app

        for tool in ('core_terminal_exec', 'core_python_exec', 'core_task_start'):
            arguments = {'argv': ['private-argv-canary']}
            if tool == 'core_python_exec':
                arguments = {'code': "print('private-code-canary')"}
            elif tool == 'core_task_start':
                arguments = {'tool': 'core_terminal_exec', 'arguments': arguments, 'required': False}
            class AwaitFailureModel(ScriptedModel):
                def generate(inner, **kwargs):
                    if tool == 'core_task_start' and len(inner.calls) == 1:
                        output = next(json.loads(item['content'])['output'] for item in kwargs['messages']
                                      if item.get('tool_call_id') == 'execute')
                        task = agent.task_scheduler.wait(output['task_id'], timeout=5)
                        self.assertEqual(task.state, 'failed')
                        self.assertEqual(task.error.code, 'EXECUTION_ENVIRONMENT_UNAVAILABLE')
                    return super().generate(**kwargs)
            model = AwaitFailureModel([
                ModelResponse(tool_requests=(ToolRequest('execute', tool, arguments),)),
                ModelResponse(message='failure handled'),
            ])
            model.model = 'scripted-diagnostic-proof'
            launcher = UnsandboxedTestLauncher()
            diagnostic = SandboxDiagnostics(stage='target_exec', reason='os_error', errno=errno.ENOENT,
                                            elapsed_ms=2, cleanup='confirmed')
            failure = ExecutionNotStarted('EXECUTION_ENVIRONMENT_UNAVAILABLE', 'Sandbox did not start',
                                          diagnostics=diagnostic)
            classifier = GuardrailClassifier(ClearGuardrailModel())
            classify = classifier.classify
            lock = threading.Lock()
            def available_classifier(*args, **kwargs):
                with lock:
                    return classify(*args, **kwargs)
            environment = {'CORE_AGENT_ENVIRONMENT': 'development', 'CORE_AGENT_MEMORY': 'disabled',
                'SESSION_STORAGE_TYPE': 'in-memory', 'LOCAL_WORKSPACE_ROOT': str(self.root / tool / 'scratch'),
                'CHAT_WORKSPACE_ROOT': str(self.root / tool / 'chats'),
                'CORE_AGENT_ALLOWED_BUILTIN_TOOLS': 'core_terminal_exec,core_python_exec,core_task_start,core_task_wait,core_task_get'}
            with self.subTest(tool=tool), patch.dict(os.environ, environment, clear=True), \
                    patch.object(classifier, 'classify', side_effect=available_classifier), \
                    patch.object(launcher, 'start', side_effect=failure) as start:
                app = create_app(model=model, sandbox_launcher=launcher, guardrail_classifier=classifier)
                try:
                    agent = app.state.core_agent
                    result = agent.run({'prompt': 'exercise execution refusal'}, identity='owner',
                                       tenant_id='company', session_id=tool)
                    self.assertEqual(result.message, 'failure handled')
                    start.assert_called_once()
                    exporter = app.state.telemetry.exporter
                    spans = [span for span in exporter.spans
                             if span.attributes.get('core_agent.sandbox.stage') == 'target_exec']
                    self.assertTrue(spans, 'actual execution lost its private operator diagnosis')
                    self.assertTrue(all(span.attributes['core_agent.sandbox.errno'] == errno.ENOENT for span in spans))
                    self.assertTrue(any(body == 'sandbox.failed' for body, _ in exporter.logs))
                    exported = json.dumps([span.attributes for span in exporter.spans]) + str(exporter.logs)
                    self.assertNotIn('private-argv-canary', exported)
                    self.assertNotIn('private-code-canary', exported)
                    self.assertNotIn('core_agent.sandbox', json.dumps(result.__dict__, default=str))
                finally:
                    app.state.close()


class SandboxTests(unittest.TestCase):
    def sandbox(self):
        self.assertIsNotNone(importlib.util.find_spec('core_agent.sandbox'),
                             'the fail-closed launcher is missing')
        from core_agent import sandbox
        return sandbox

    def policy(self, **settings):
        return self.sandbox().SandboxPolicy(
            dns_servers=('1.1.1.1', '2606:4700:4700::1111'),
            denied_cidrs=('10.0.0.0/8', '2606:4700:abcd::11/128'), **settings)

    def test_configuration_rejects_unsafe_network_and_unbounded_limits(self):
        sandbox = self.sandbox()
        for value in (0, -1, True, float('inf'), float('nan')):
            with self.subTest(limit=value), self.assertRaises(CoreError) as caught:
                self.policy(cpu_seconds=value)
            self.assertEqual(caught.exception.code, 'CONFIG_INVALID')
        for case, settings in (
                ('missing DNS', {'SANDBOX_DENIED_CIDRS': '10.0.0.0/8'}),
                ('missing CIDR', {'SANDBOX_DNS_SERVERS': '1.1.1.1'}),
                ('loopback DNS', {'SANDBOX_DNS_SERVERS': '127.0.0.1',
                                  'SANDBOX_DENIED_CIDRS': '10.0.0.0/8'})):
            with self.subTest(configuration=case):
                with self.assertRaises(CoreError) as caught:
                    sandbox.SandboxPolicy.from_environment(settings)
                self.assertEqual(caught.exception.code, 'CONFIG_INVALID')
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.policy().cpu_seconds = 999

    def test_public_filter_excludes_translation_reserved_and_deployment_ranges(self):
        policy = self.policy()
        denied = ('0.0.0.0', '10.2.3.4', '100.64.0.1', '127.0.0.1',
                  '169.254.169.254', '172.16.1.1', '192.168.1.1',
                  '192.0.0.9', '198.18.0.1', '224.0.0.1', '255.255.255.255',
                  '::1', '::ffff:8.8.8.8', '64:ff9b::a00:1', '2001::1',
                  '2002:a00:1::1', '3fff::1', 'fc00::1', 'fe80::1',
                  'ff02::1', '2606:4700:abcd::11')
        for address in denied:
            with self.subTest(address=address):
                self.assertFalse(policy.permits_destination(ipaddress.ip_address(address)))
        for address in ('1.1.1.1', '8.8.8.8', '2606:4700:4700::1111'):
            self.assertTrue(policy.permits_destination(ipaddress.ip_address(address)))
        rules = policy.nft_rules()
        self.assertEqual(rules.count('policy drop;'), 3)
        self.assertIn('ip6 daddr', rules)
        self.assertIn('udp dport 53', rules)

    def test_unsupported_platform_never_runs_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / 'attachments'
            inputs.mkdir()
            marker = root / 'executed'
            with patch('platform.system', return_value='Darwin'):
                with self.assertRaises(CoreError) as caught:
                    self.sandbox().SandboxLauncher(self.policy()).start(
                        ['/bin/sh', '-c', 'touch executed'], workspace_path=root,
                        readonly_input_path=inputs, environment={}, cwd='.')
            self.assertEqual(caught.exception.code, 'EXECUTION_ENVIRONMENT_UNAVAILABLE')
            self.assertFalse(marker.exists())

    def test_symlink_workspace_and_cwd_escape_are_rejected(self):
        sandbox = self.sandbox()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / 'real').mkdir()
            (root / 'alias').symlink_to(root / 'real', target_is_directory=True)
            with self.assertRaises((OSError, CoreError)):
                sandbox._open_directory(root / 'alias')
            for cwd in ('../secret', '/tmp', 'nested/../../secret'):
                with self.assertRaises(CoreError):
                    sandbox._workspace_cwd(cwd)
            descriptor = sandbox._open_directory(root / 'real')
            os.close(descriptor)

    def test_dns_and_cidrs_require_literal_unscoped_strings(self):
        sandbox = self.sandbox()
        for dns, cidrs in (((16843009,), ('10.0.0.0/8',)),
                           (('2606:4700:4700::1111%lo',), ('10.0.0.0/8',)),
                           (('1.1.1.1',), (167772160,))):
            with self.subTest(dns=dns, cidrs=cidrs):
                with self.assertRaises(CoreError) as caught:
                    sandbox.SandboxPolicy(dns_servers=dns, denied_cidrs=cidrs)
                self.assertEqual(caught.exception.code, 'CONFIG_INVALID')

    def test_native_failure_and_invalid_launch_never_spawn(self):
        sandbox = self.sandbox()
        launcher = sandbox.SandboxLauncher(self.policy())
        with patch.object(sandbox, '_check_native', side_effect=FileNotFoundError), \
                patch.object(sandbox.subprocess, 'Popen') as spawn:
            with self.assertRaises(CoreError) as caught:
                launcher.start(['true'], workspace_path='/unused', readonly_input_path='/unused/attachments')
            self.assertEqual(caught.exception.code, 'EXECUTION_ENVIRONMENT_UNAVAILABLE')
            self.assertIsInstance(caught.exception, ExecutionNotStarted)
            spawn.assert_not_called()
        for argv, cwd, timeout in (('sh', '.', None), (['true'], None, None),
                                    (['true'], '.', float('nan')), (['true'], '.', True)):
            with self.subTest(argv=argv, cwd=cwd, timeout=timeout), \
                    patch.object(sandbox, '_check_native'), \
                    patch.object(sandbox.subprocess, 'Popen') as spawn:
                with self.assertRaises(CoreError) as caught:
                    launcher.start(argv, cwd=cwd, timeout=timeout,
                                   workspace_path='/unused', readonly_input_path='/unused/attachments')
                self.assertEqual(caught.exception.code, 'INVALID_ARGUMENT')
                spawn.assert_not_called()

    def test_native_failure_has_private_finite_diagnostics_without_exception_text(self):
        sandbox = self.sandbox()
        launcher = sandbox.SandboxLauncher(self.policy())
        canary = 'private-token-/private/company/workspace'
        with patch.object(sandbox, '_check_native', side_effect=OSError(errno.ENOENT, canary)), \
                patch.object(sandbox.subprocess, 'Popen') as spawn:
            with self.assertRaises(ExecutionNotStarted) as caught:
                launcher.start(['private-argv'], workspace_path='/unused', readonly_input_path='/unused/attachments')
        error = caught.exception
        self.assertEqual(error.code, 'EXECUTION_ENVIRONMENT_UNAVAILABLE')
        self.assertEqual(error.data, {})
        self.assertNotIn(canary, str(error))
        diagnostic = getattr(error, 'diagnostics', None)
        self.assertIsNotNone(diagnostic, 'native failure lost its safe private diagnosis')
        self.assertEqual(diagnostic.stage, 'native_check')
        self.assertEqual(diagnostic.reason, 'os_error')
        self.assertEqual(diagnostic.errno, errno.ENOENT)
        self.assertNotIn(canary, str(diagnostic.as_dict()))
        spawn.assert_not_called()

    def test_queue_timeout_has_bounded_diagnostics_and_never_spawns(self):
        sandbox = self.sandbox()
        launcher = sandbox.SandboxLauncher(self.policy(max_concurrent_processes=1, start_timeout_seconds=.01))
        launcher._semaphore.acquire()
        self.addCleanup(launcher._semaphore.release)
        with patch.object(sandbox, '_check_native'), patch.object(sandbox.subprocess, 'Popen') as spawn:
            with self.assertRaises(ExecutionNotStarted) as caught:
                launcher.start(['true'], workspace_path='/unused', readonly_input_path='/unused/attachments')
        diagnostic = getattr(caught.exception, 'diagnostics', None)
        self.assertIsNotNone(diagnostic, 'queue timeout lost its safe private diagnosis')
        self.assertEqual((diagnostic.stage, diagnostic.reason), ('queue', 'timeout'))
        self.assertEqual(diagnostic.cleanup, 'not_started')
        self.assertIs(type(diagnostic.elapsed_ms), int)
        self.assertGreaterEqual(diagnostic.elapsed_ms, 0)
        self.assertLess(diagnostic.elapsed_ms, 2000)
        self.assertEqual((diagnostic.slots_active, diagnostic.slots_limit), (1, 1))
        self.assertEqual(caught.exception.data, {})
        spawn.assert_not_called()

    def test_supervisor_failure_reports_safe_stage_without_material_or_raw_error(self):
        sandbox = self.sandbox()
        channel = Mock()
        policy = self.policy()
        launch = {'workspace': '/private/company/workspace', 'inputs': '/private/company/workspace/attachments',
                  'policy': dataclasses.asdict(policy), 'parent_pid': 999,
                  'deadline': time.monotonic() + 10}
        with patch.object(sandbox, '_receive', return_value=(launch, [])), \
                patch.object(sandbox.ctypes, 'CDLL', return_value=Mock(prctl=Mock(return_value=0))), \
                patch.object(sandbox.os, 'getppid', return_value=999), \
                patch.object(sandbox, '_open_directory', side_effect=OSError(errno.EACCES, 'private-token')), \
                patch.object(sandbox, '_send') as send:
            sandbox._supervise(channel)
        frame = send.call_args.args[1]
        self.assertEqual(frame['error'], 'EXECUTION_ENVIRONMENT_UNAVAILABLE')
        self.assertTrue(frame['not_executed'])
        self.assertFalse(frame['cleanup_failed'])
        diagnostic = frame.get('diagnostics')
        self.assertIsNotNone(diagnostic, 'supervisor discarded the concrete failure stage')
        self.assertEqual((diagnostic['stage'], diagnostic['reason']), ('namespace', 'os_error'))
        self.assertEqual(diagnostic['errno'], errno.EACCES)
        self.assertEqual(diagnostic['cleanup'], 'confirmed')
        self.assertNotIn('private-token', str(frame))
        self.assertNotIn('/private/company', str(frame))

    def test_stop_interrupts_concurrent_wait(self):
        sandbox = self.sandbox()
        launcher = sandbox.SandboxLauncher(self.policy())
        launcher._semaphore.acquire()
        parent, child = socket.socketpair()
        self.addCleanup(child.close)
        native = Mock(pid=100, returncode=0)
        native.wait.return_value = 0
        handle = sandbox.SandboxProcess(launcher, native, parent)
        handle._started = True
        entered = threading.Event()
        original_receive = sandbox._receive

        def receive(channel):
            entered.set()
            return original_receive(channel)

        with patch.object(sandbox, '_receive', side_effect=receive), ThreadPoolExecutor(2) as pool:
            waiting = pool.submit(handle.wait, 1)
            entered.wait(.1)
            stopping = pool.submit(handle.stop, .5)
            child.settimeout(.25)
            try:
                message, _ = original_receive(child)
                self.assertEqual(message['state'], 'stop')
            finally:
                sandbox._send(child, {'state': 'exited', 'returncode': -9})
            self.assertEqual(stopping.result(2), -9)
            self.assertEqual(waiting.result(2), -9)
            self.assertEqual(handle.stop(), -9)

    def test_supervisor_wait_os_error_has_private_diagnostics_without_changing_exception(self):
        sandbox = self.sandbox()
        launcher = sandbox.SandboxLauncher(self.policy())
        launcher._semaphore.acquire()
        parent, child = socket.socketpair()
        self.addCleanup(child.close)
        native = Mock(pid=100, returncode=0)
        native.poll.return_value = 0
        handle = sandbox.SandboxProcess(launcher, native, parent)
        self.addCleanup(handle._close)
        failure = OSError(errno.EBADF, 'private-token-/private/company')
        with patch.object(sandbox.select, 'select', side_effect=failure):
            with self.assertRaises(OSError) as caught:
                handle.wait()
        self.assertIs(caught.exception, failure)
        diagnostic = getattr(failure, 'diagnostics', None)
        self.assertIsNotNone(diagnostic, 'supervisor I/O failure discarded its private diagnosis')
        self.assertEqual((diagnostic.stage, diagnostic.reason, diagnostic.errno), ('supervisor', 'os_error', errno.EBADF))
        self.assertNotIn('private-token', str(diagnostic.as_dict()))

    def test_cleanup_failure_disables_launcher(self):
        sandbox = self.sandbox()
        launcher = sandbox.SandboxLauncher(self.policy())
        launcher._semaphore.acquire()
        parent, child = socket.socketpair()
        self.addCleanup(child.close)
        native = Mock(pid=100)
        native.wait.side_effect = subprocess.TimeoutExpired('supervisor', 1)
        handle = sandbox.SandboxProcess(launcher, native, parent)
        with self.assertRaises(CoreError) as caught:
            handle._emergency_cleanup()
        self.assertTrue(launcher._unhealthy)
        self.assertTrue(handle._closed)
        diagnostic = getattr(caught.exception, 'diagnostics', None)
        self.assertIsNotNone(diagnostic, 'unconfirmed cleanup lost its finite private diagnosis')
        self.assertEqual((diagnostic.stage, diagnostic.reason, diagnostic.cleanup),
                         ('cleanup', 'timeout', 'unconfirmed'))
        self.assertEqual(caught.exception.data, {})

    def test_private_gate_rejects_eof_short_wrong_token_and_timeout(self):
        from core_agent import sandbox_exec
        token = b'a' * 32
        for data in (b'', token[:-1], b'b' * 32, None):
            with self.subTest(data=data):
                reader, writer = os.pipe()
                try:
                    if data is not None:
                        os.write(writer, data)
                        os.close(writer)
                        writer = -1
                    with self.assertRaises(ValueError):
                        sandbox_exec._authorize(reader, token, time.monotonic() + .02)
                finally:
                    os.close(reader)
                    if writer >= 0:
                        os.close(writer)
        reader, writer = os.pipe()
        try:
            os.write(writer, token)
            sandbox_exec._authorize(reader, token, time.monotonic() + .1)
        finally:
            os.close(reader)
            os.close(writer)

    def test_target_exec_distinguishes_missing_command_from_interpreter_permissions_and_format(self):
        from core_agent import sandbox_exec
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            interpreter = root / 'missing-interpreter'
            script = root / 'script'
            script.write_text(f'#!{interpreter}\n')
            script.chmod(0o700)
            denied = root / 'denied'
            denied.write_text('#!/bin/sh\n')
            denied.chmod(0o600)
            invalid = root / 'invalid'
            invalid.write_text('invalid executable\n')
            invalid.chmod(0o700)
            for command, expected in ((str(root / 'missing'), b'MISSING:2'), ('missing', b'MISSING:2'),
                                      (str(script), b'EXEC:2'), ('script', b'EXEC:2'),
                                      (str(denied), f'EXEC:{errno.EACCES}'.encode()),
                                      (str(invalid), f'EXEC:{errno.ENOEXEC}'.encode())):
                with self.subTest(command=command):
                    self.assertEqual(sandbox_exec._exec_target([command], {'PATH': directory}), expected)

    def test_startup_failure_after_release_stays_unknown(self):
        sandbox = self.sandbox()
        for explicit_no_exec in (False, True):
            with self.subTest(no_exec=explicit_no_exec):
                launcher = sandbox.SandboxLauncher(self.policy())
                parent, child = socket.socketpair()
                messages = [{'state': 'released'},
                            {'state': 'exited', 'error': 'EXECUTION_ENVIRONMENT_UNAVAILABLE',
                             'not_executed': explicit_no_exec}]
                with patch.object(sandbox, '_check_native'), \
                        patch.object(sandbox, '_send'), \
                        patch.object(sandbox.socket, 'socketpair', return_value=(parent, child)), \
                        patch.object(sandbox.subprocess, 'Popen', return_value=Mock(pid=100, returncode=125)), \
                        patch.object(sandbox.SandboxProcess, '_message', side_effect=messages):
                    with self.assertRaises(CoreError) as caught:
                        launcher.start(['true'], workspace_path='/unused', readonly_input_path='/unused/attachments')
                    self.assertEqual(caught.exception.code, 'EXECUTION_ENVIRONMENT_UNAVAILABLE'
                                     if explicit_no_exec else 'SIDE_EFFECT_UNKNOWN')

    def test_command_failure_preserves_safe_message_and_no_exec_proof(self):
        sandbox = self.sandbox()
        for code, expected in (
                ('COMMAND_NOT_FOUND', 'Command was not found in the sandbox PATH or at the requested path'),
                ('COMMAND_EXEC_FAILED', 'Command could not execute; check its permissions, format, and interpreter')):
            with self.subTest(code=code):
                launcher = sandbox.SandboxLauncher(self.policy())
                parent, child = socket.socketpair()
                messages = [{'state': 'released'}, {'state': 'exited', 'error': code,
                            'not_executed': True, 'diagnostics': {'stage': 'target_exec',
                            'reason': 'os_error', 'errno': errno.ENOENT, 'elapsed_ms': 7, 'cleanup': 'confirmed'}}]
                with patch.object(sandbox, '_check_native'), patch.object(sandbox, '_send'), \
                        patch.object(sandbox.socket, 'socketpair', return_value=(parent, child)), \
                        patch.object(sandbox.subprocess, 'Popen', return_value=Mock(pid=100, returncode=125)), \
                        patch.object(sandbox.SandboxProcess, '_message', side_effect=messages):
                    with self.assertRaises(ExecutionNotStarted) as caught:
                        launcher.start(['private-argv'], workspace_path='/unused', readonly_input_path='/unused/attachments')
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.message, expected)
                self.assertEqual(caught.exception.data, {})
                self.assertFalse(launcher._unhealthy)
                self.assertFalse(launcher._processes)

    def test_private_supervisor_diagnosis_preserves_release_and_no_exec_proof(self):
        sandbox = self.sandbox()
        for explicit_no_exec in (False, True):
            with self.subTest(no_exec=explicit_no_exec):
                launcher = sandbox.SandboxLauncher(self.policy())
                parent, child = socket.socketpair()
                messages = [{'state': 'released'}, {'state': 'exited',
                    'error': 'EXECUTION_ENVIRONMENT_UNAVAILABLE', 'not_executed': explicit_no_exec,
                    'diagnostics': {'stage': 'target_exec', 'reason': 'os_error', 'errno': errno.ENOENT,
                                    'elapsed_ms': 7, 'cleanup': 'confirmed'}}]
                with patch.object(sandbox, '_check_native'), patch.object(sandbox, '_send'), \
                        patch.object(sandbox.socket, 'socketpair', return_value=(parent, child)), \
                        patch.object(sandbox.subprocess, 'Popen', return_value=Mock(pid=100, returncode=125)), \
                        patch.object(sandbox.SandboxProcess, '_message', side_effect=messages):
                    with self.assertRaises(CoreError) as caught:
                        launcher.start(['private-argv'], workspace_path='/unused', readonly_input_path='/unused/attachments')
                self.assertEqual(caught.exception.code, 'EXECUTION_ENVIRONMENT_UNAVAILABLE'
                                 if explicit_no_exec else 'SIDE_EFFECT_UNKNOWN')
                self.assertEqual(isinstance(caught.exception, ExecutionNotStarted), explicit_no_exec)
                self.assertEqual(caught.exception.data, {})
                diagnostic = getattr(caught.exception, 'diagnostics', None)
                self.assertIsNotNone(diagnostic, 'wrapping the failure discarded its private diagnosis')
                self.assertEqual(diagnostic.as_dict(), {'stage': 'target_exec', 'reason': 'os_error',
                    'errno': errno.ENOENT, 'elapsed_ms': 7, 'cleanup': 'confirmed'})

    def test_invalid_private_frame_cannot_export_extra_fields_or_paths(self):
        sandbox = self.sandbox()
        launcher = sandbox.SandboxLauncher(self.policy())
        parent, child = socket.socketpair()
        messages = [{'state': 'exited', 'error': 'EXECUTION_ENVIRONMENT_UNAVAILABLE', 'not_executed': True,
            'diagnostics': {'stage': '/private/company', 'reason': 'private-token', 'elapsed_ms': True,
                            'cleanup': 'confirmed', 'stderr': 'private-token'}}]
        with patch.object(sandbox, '_check_native'), patch.object(sandbox, '_send'), \
                patch.object(sandbox.socket, 'socketpair', return_value=(parent, child)), \
                patch.object(sandbox.subprocess, 'Popen', return_value=Mock(pid=100, returncode=125)), \
                patch.object(sandbox.SandboxProcess, '_message', side_effect=messages):
            with self.assertRaises(ExecutionNotStarted) as caught:
                launcher.start(['true'], workspace_path='/unused', readonly_input_path='/unused/attachments')
        diagnostic = getattr(caught.exception, 'diagnostics', None)
        self.assertIsNotNone(diagnostic, 'invalid private metadata must produce a finite protocol diagnosis')
        self.assertEqual((diagnostic.stage, diagnostic.reason), ('supervisor', 'protocol_invalid'))
        self.assertEqual(caught.exception.data, {})
        self.assertNotIn('private-token', str(diagnostic.as_dict()))
        self.assertNotIn('/private/company', str(diagnostic.as_dict()))

    def test_private_diagnostic_type_is_immutable_and_rejects_unbounded_values(self):
        from core_agent import errors
        diagnostic_type = getattr(errors, 'SandboxDiagnostics', None)
        self.assertIsNotNone(diagnostic_type, 'typed private sandbox diagnostics are missing')
        values = {'stage': 'network', 'reason': 'child_exit', 'returncode': 125,
                  'elapsed_ms': 4, 'cleanup': 'confirmed'}
        diagnostic = diagnostic_type(**values)
        self.assertEqual(diagnostic.as_dict(), values)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            diagnostic.stage = 'cleanup'
        for changes in ({'stage': '/private/company'}, {'reason': 'private-token'},
                        {'elapsed_ms': True}, {'elapsed_ms': float('nan')}, {'elapsed_ms': -1},
                        {'errno': 0}, {'errno': 4096}, {'returncode': 256}, {'cleanup': 'private-token'},
                        {'pids_current': -1}, {'memory_current_bytes': 2**63}, {'stderr': 'private-token'}):
            with self.subTest(fields=tuple(changes)):
                with self.assertRaises((TypeError, ValueError)):
                    diagnostic_type(**{**values, **changes})
        class UntrustedString(str):
            pass
        for field in ('stage', 'reason', 'cleanup'):
            with self.subTest(field=field), self.assertRaises((TypeError, ValueError)):
                diagnostic_type(**{**values, field: UntrustedString(values[field])})
        error = CoreError('EXECUTION_ENVIRONMENT_UNAVAILABLE', diagnostics=diagnostic)
        self.assertIs(error.diagnostics, diagnostic)
        self.assertEqual(error.data, {})
        untrusted = CoreError('EXECUTION_ENVIRONMENT_UNAVAILABLE', diagnostics={'stderr': 'private-token'})
        self.assertIsNone(untrusted.diagnostics)
        self.assertEqual(untrusted.data, {})

    def test_optional_resource_snapshot_is_numeric_bounded_and_ignores_unlimited_or_unavailable(self):
        sandbox = self.sandbox()
        values = {'memory.current': '128\n', 'memory.max': 'max\n', 'pids.current': '12\n',
                  'pids.max': '64\n', 'memory.events': 'oom 3\noom_kill 2\n'}
        def source(path, **kwargs):
            self.assertTrue(path.startswith('/sys/fs/cgroup/'))
            return io.StringIO(values[Path(path).name])
        with patch('builtins.open', side_effect=source):
            snapshot = sandbox._resource_stats()
        self.assertEqual(snapshot, {'memory_current_bytes': 128, 'pids_current': 12,
                                    'pids_limit': 64, 'oom_kill_count': 2})
        values.update({'memory.current': '9' * 40, 'pids.current': 'private-token', 'pids.max': '0',
                       'memory.events': 'oom_kill private-token'})
        with patch('builtins.open', side_effect=source):
            self.assertEqual(sandbox._resource_stats(), {})
        with patch('builtins.open', side_effect=PermissionError('private-token-/private/company')):
            self.assertEqual(sandbox._resource_stats(), {})

    def test_known_timeout_errno_remains_available_without_exception_text(self):
        diagnostic = self.sandbox()._diagnostics('network', TimeoutError(errno.ETIMEDOUT, 'private-token'),
                                                time.monotonic(), 'confirmed')
        self.assertEqual(diagnostic.reason, 'timeout')
        self.assertEqual(diagnostic.errno, errno.ETIMEDOUT)
        self.assertNotIn('private-token', str(diagnostic.as_dict()))

    def test_private_diagnostic_cannot_claim_cleanup_after_reported_failure(self):
        frame = {'cleanup_failed': True, 'diagnostics': {'stage': 'network', 'reason': 'child_exit',
                  'elapsed_ms': 1, 'cleanup': 'confirmed'}}
        diagnostic = self.sandbox()._frame_diagnostics(frame, time.monotonic())
        self.assertEqual((diagnostic.stage, diagnostic.reason, diagnostic.cleanup),
                         ('supervisor', 'protocol_invalid', 'unconfirmed'))

    def test_close_attempts_every_owned_process_after_cleanup_error(self):
        launcher = self.sandbox().SandboxLauncher(self.policy())
        first, second = Mock(), Mock()
        first.stop.side_effect = CoreError('SIDE_EFFECT_UNKNOWN')
        launcher._processes.update((first, second))
        with self.assertRaises(CoreError):
            launcher.close()
        first.stop.assert_called_once()
        second.stop.assert_called_once()

    def test_cancel_pending_launch_never_waits_for_a_started_frame(self):
        sandbox = self.sandbox()
        launcher = sandbox.SandboxLauncher(self.policy())
        launcher._semaphore.acquire()
        parent, child = socket.socketpair()
        self.addCleanup(child.close)
        native = Mock(pid=100, returncode=-9)
        native.poll.return_value = None
        handle = sandbox.SandboxProcess(launcher, native, parent)
        with patch.object(handle, 'wait', side_effect=AssertionError('pending launch must be killed')):
            self.assertEqual(handle.stop(), -9)
        native.kill.assert_called_once()

    def test_missing_native_binary_and_bad_policy_integrity_fail_closed(self):
        sandbox = self.sandbox()
        for missing in ('bwrap', 'nft', 'slirp4netns', 'nsenter'):
            with self.subTest(binary=missing), \
                    patch.object(sandbox.platform, 'system', return_value='Linux'), \
                    patch.object(sandbox.platform, 'machine', return_value='x86_64'), \
                    patch.object(sandbox.os, 'pidfd_open', create=True), \
                    patch.object(sandbox.signal, 'pidfd_send_signal', create=True), \
                    patch.object(sandbox.shutil, 'which', side_effect=lambda name, path: None if name == missing else '/bin/' + name):
                with self.assertRaises(CoreError) as caught:
                    sandbox._check_native()
                self.assertEqual(caught.exception.code, 'EXECUTION_ENVIRONMENT_UNAVAILABLE')
        with patch.object(sandbox, '_POLICY_SHA256', '0' * 64):
            with self.assertRaises(CoreError) as caught:
                sandbox._policy_resource()
            self.assertEqual(caught.exception.code, 'EXECUTION_ENVIRONMENT_UNAVAILABLE')


if __name__ == '__main__':
    unittest.main()
