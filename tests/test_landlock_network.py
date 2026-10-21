import array
import fcntl
import ipaddress
import json
import os
import signal
import socket
import struct
import sys
import threading
import unittest
from unittest.mock import patch

from core_agent.sandbox import SandboxPolicy


class LandlockNetworkTests(unittest.TestCase):
    def setUp(self):
        from core_agent.landlock_network import EgressBroker
        self.policy = SandboxPolicy(('1.1.1.1',), ('198.19.0.0/16',))
        self.broker = EgressBroker(self.policy, timeout_seconds=2)
        self.addCleanup(self.broker.close)

    def request(self, payload):
        connection = socket.create_connection(self.broker.address, timeout=2)
        self.addCleanup(connection.close)
        connection.sendall(payload)
        return connection

    def test_private_and_alternative_literal_destinations_are_denied(self):
        for host in ('127.0.0.1', '169.254.169.254', '10.0.0.1', '[::1]',
                     '[::ffff:127.0.0.1]', '198.19.1.2', '2130706433'):
            with self.subTest(host=host):
                with patch.object(self.broker, '_dns_query', return_value=[]):
                    response = self.request(f'CONNECT {host}:80 HTTP/1.1\r\nHost: x\r\n\r\n'.encode()).recv(1024)
                self.assertTrue(response.startswith(b'HTTP/1.1 403'), response)

    def test_mixed_public_private_dns_answers_fail_closed_before_connect(self):
        with patch.object(self.broker, '_dns_query', return_value=[
                ipaddress.ip_address('1.1.1.1'), ipaddress.ip_address('127.0.0.1')]):
            response = self.request(b'CONNECT example.test:443 HTTP/1.1\r\n\r\n').recv(1024)
        self.assertTrue(response.startswith(b'HTTP/1.1 403'), response)

    def test_dns_uses_only_configured_resolvers_and_rechecks_each_request(self):
        with patch.object(self.broker, '_dns_query', side_effect=[
                [ipaddress.ip_address('1.1.1.1')], [], [ipaddress.ip_address('127.0.0.1')], []]) as query:
            self.assertEqual(self.broker._resolve('public.test'), [ipaddress.ip_address('1.1.1.1')])
            with self.assertRaises(PermissionError):
                self.broker._resolve('public.test')
        self.assertEqual(query.call_count, 4)

    def test_dns_port_and_invalid_proxy_targets_are_denied(self):
        requests = (
            b'CONNECT 1.1.1.1:53 HTTP/1.1\r\n\r\n',
            b'CONNECT user:secret@example.test:443 HTTP/1.1\r\n\r\n',
            b'GET /relative HTTP/1.1\r\nHost: example.test\r\n\r\n',
            b'GET http://example.test/path#fragment HTTP/1.1\r\n\r\n',
            b'CONNECT [fe80::1%lo]:80 HTTP/1.1\r\n\r\n',
            b'GET http://example.test/ HTTP/1.1\r\n Folded: header\r\n\r\n',
            b'GET http://example.test/ HTTP/1.1\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\n',
        )
        for request in requests:
            with self.subTest(request=request):
                response = self.request(request).recv(1024)
                self.assertTrue(response.startswith(b'HTTP/1.1 403'), response)

    def upstream(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(2)
        self.addCleanup(listener.close)
        address = listener.getsockname()
        return listener, lambda *_: socket.create_connection(address, timeout=2)

    def test_connect_streams_bytes_and_half_close(self):
        listener, connect = self.upstream()
        with patch.object(self.broker, '_connect_destination', side_effect=connect):
            client = self.request(b'CONNECT public.test:443 HTTP/1.1\r\n\r\n')
            peer, _ = listener.accept()
            self.addCleanup(peer.close)
            self.assertTrue(client.recv(1024).startswith(b'HTTP/1.1 200'))
            client.sendall(b'request')
            self.assertEqual(peer.recv(7), b'request')
            client.shutdown(socket.SHUT_WR)
            self.assertEqual(peer.recv(1), b'')
            peer.sendall(b'response')
            self.assertEqual(client.recv(8), b'response')

    def test_absolute_http_rewrites_host_strips_proxy_credentials_and_streams_body(self):
        listener, connect = self.upstream()
        with patch.object(self.broker, '_connect_destination', side_effect=connect):
            client = self.request(
                b'POST http://public.test/upload?q=1 HTTP/1.1\r\nHost: internal\r\n'
                b'Proxy-Authorization: secret\r\nContent-Length: 4\r\n\r\ndata')
            peer, _ = listener.accept()
            self.addCleanup(peer.close)
            peer.settimeout(2)
            received = b''
            while not received.endswith(b'data'):
                received += peer.recv(1024)
            self.assertIn(b'POST /upload?q=1 HTTP/1.1\r\n', received)
            self.assertIn(b'Host: public.test\r\n', received)
            self.assertNotIn(b'secret', received)
            self.assertNotIn(b'internal', received)
            peer.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok')
            self.assertIn(b'200 OK', client.recv(1024))

    def test_header_size_bound_and_close_terminate_connections(self):
        client = self.request(b'GET http://public.test/ HTTP/1.1\r\nX: ' + b'x' * 33000)
        self.assertTrue(client.recv(1024).startswith(b'HTTP/1.1 403'))
        client = self.request(b'CONNECT ')
        self.broker.close()
        self.assertFalse(self.broker.healthy)
        try:
            self.assertEqual(client.recv(1), b'')
        except ConnectionResetError:
            pass

    def test_worker_exhaustion_closes_broker_and_releases_connection(self):
        attempted = threading.Event()

        def exhausted():
            attempted.set()
            raise RuntimeError('thread limit')

        with patch.object(threading.Thread, 'start', side_effect=exhausted):
            connection = socket.create_connection(self.broker.address, timeout=2)
            self.addCleanup(connection.close)
            self.assertTrue(attempted.wait(2))
            connection.settimeout(2)
            self.assertEqual(connection.recv(1), b'')
        self.broker.close()
        self.assertFalse(self.broker.healthy)
        self.assertEqual(self.broker._connections, set())

    def test_cleanup_does_not_confirm_while_trusted_worker_is_pending(self):
        entered, release = threading.Event(), threading.Event()

        def pending():
            entered.set()
            release.wait(5)

        self.broker._start_thread(pending)
        self.assertTrue(entered.wait(1))
        try:
            with self.assertRaises(TimeoutError):
                self.broker.close()
        finally:
            release.set()
            self.broker.close()


class DNSResponseTests(unittest.TestCase):
    def test_reply_binding_and_answer_owner(self):
        from core_agent.landlock_network import _dns_answers, _dns_question
        question = _dns_question('example.test', 1)
        response = struct.pack('!6H', 123, 0x8180, 1, 1, 0, 0) + question
        response += b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 30, 4) + b'\x01\x01\x01\x01'
        self.assertEqual(_dns_answers(response, 123, 'example.test', 1), [ipaddress.ip_address('1.1.1.1')])
        for bad in (response[:8], response.replace(b'example', b'private', 1),
                    response[:2] + b'\x01\x80' + response[4:]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    _dns_answers(bad, 123, 'example.test', 1)
        with self.assertRaises(ValueError):
            _dns_answers(response, 124, 'example.test', 1)

    def test_cname_chain_only_accepts_related_addresses(self):
        from core_agent.landlock_network import _dns_answers, _dns_question
        question = _dns_question('example.test', 1)
        target = _dns_question('cdn.test', 1)[:-4]
        answer = b'\xc0\x0c' + struct.pack('!HHIH', 5, 1, 30, len(target)) + target
        answer += target + struct.pack('!HHIH', 1, 1, 30, 4) + b'\x01\x01\x01\x01'
        response = struct.pack('!6H', 123, 0x8180, 1, 2, 0, 0) + question + answer
        self.assertEqual(_dns_answers(response, 123, 'example.test', 1), [ipaddress.ip_address('1.1.1.1')])

    def test_compression_cycle_and_unrelated_answer_are_rejected(self):
        from core_agent.landlock_network import _dns_answers, _dns_question
        header = struct.pack('!6H', 123, 0x8180, 1, 1, 0, 0)
        with self.assertRaises(ValueError):
            _dns_answers(header + b'\xc0\x0c' + struct.pack('!HH', 1, 1), 123, 'example.test', 1)
        response = header + _dns_question('example.test', 1)
        response += _dns_question('unrelated.test', 1)[:-4]
        response += struct.pack('!HHIH', 1, 1, 30, 4) + b'\x01\x01\x01\x01'
        self.assertEqual(_dns_answers(response, 123, 'example.test', 1), [])


class NotificationDispatchTests(unittest.TestCase):
    def test_metadata_reply_is_emulated_and_never_continues_syscall(self):
        from core_agent.landlock_network import EgressBroker, _Notification
        policy = SandboxPolicy(('1.1.1.1',), ('198.19.0.0/16',))
        received = []
        with EgressBroker(policy, metadata_handler=lambda notification: received.append(notification.id) or 0) as broker:
            notification = _Notification()
            notification.id = 123
            calls = []
            with patch('core_agent.landlock_network._ioctl', side_effect=lambda fd, command, value: calls.append((command, value)) or 0):
                broker._metadata_notification(5, notification)
            self.assertEqual(received, [123])
            self.assertEqual([command for command, _ in calls], [0x40082102, 0x40082102, 0xc0182101])
            response = calls[-1][1]
            self.assertEqual((response.id, response.value, response.error, response.flags), (123, 0, 0, 0))

    def test_cancelled_notification_never_calls_metadata_handler(self):
        from core_agent.landlock_network import EgressBroker, _Notification
        policy = SandboxPolicy(('1.1.1.1',), ('198.19.0.0/16',))
        received = []
        with EgressBroker(policy, metadata_handler=lambda notification: received.append(notification.id) or 0) as broker:
            with patch('core_agent.landlock_network._ioctl', side_effect=FileNotFoundError(2, 'cancelled')):
                broker._metadata_notification(5, _Notification())
        self.assertEqual(received, [])


@unittest.skipUnless(sys.platform == 'linux', 'Native Linux seccomp notification test')
class NativeNotificationTests(unittest.TestCase):
    def test_injected_descriptors_flags_dup_and_fork_inherit_forced_proxy(self):
        from core_agent.landlock_network import EgressBroker
        from core_agent.landlock_policy import install_seccomp
        parent, child = socket.socketpair()
        pid = os.fork()
        if pid == 0:
            parent.close()
            stage = 'install'
            try:
                signal.alarm(10)
                listener = install_seccomp()
                stage = 'send_listener'
                child.sendmsg([b'L'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [listener]))])
                os.close(listener)
                ready = json.loads(child.recv(1024))

                def request():
                    connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM | socket.SOCK_NONBLOCK | socket.SOCK_CLOEXEC)
                    assert fcntl.fcntl(connection, fcntl.F_GETFL) & os.O_NONBLOCK
                    assert fcntl.fcntl(connection, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
                    duplicate = socket.socket(fileno=os.dup(connection.fileno()))
                    connection.close()
                    duplicate.settimeout(2)
                    duplicate.connect(('169.254.169.254', 80))
                    assert duplicate.getpeername() == tuple(ready['address'])
                    duplicate.sendall(b'CONNECT 127.0.0.1:80 HTTP/1.1\r\n\r\n')
                    response = duplicate.recv(1024)
                    duplicate.close()
                    assert response.startswith(b'HTTP/1.1 403'), response

                request()
                nested = os.fork()
                if nested == 0:
                    try:
                        request()
                        os._exit(0)
                    except BaseException:
                        os._exit(1)
                _, status = os.waitpid(nested, 0)
                assert os.waitstatus_to_exitcode(status) == 0
                for family, kind in ((socket.AF_INET, socket.SOCK_DGRAM), (socket.AF_UNIX, socket.SOCK_STREAM)):
                    try:
                        socket.socket(family, kind)
                    except PermissionError:
                        pass
                    else:
                        raise AssertionError('unmediated socket was allowed')
                child.sendall(b'OK')
                os._exit(0)
            except BaseException as error:
                child.sendall(('ERROR ' + stage + ' ' + type(error).__name__ + ': ' + str(error)).encode())
                os._exit(1)
        child.close()
        parent.settimeout(10)
        broker = None
        try:
            message, ancillary, _, _ = parent.recvmsg(1024, socket.CMSG_SPACE(4))
            self.assertEqual(message, b'L')
            descriptors = array.array('i')
            descriptors.frombytes(ancillary[0][2])
            broker = EgressBroker(SandboxPolicy(('1.1.1.1',), ('198.19.0.0/16',)), timeout_seconds=2)
            broker.start_notifications(descriptors[0])
            parent.sendall(json.dumps({'address': broker.address}).encode())
            self.assertEqual(parent.recv(2048), b'OK')
            _, status = os.waitpid(pid, 0)
            pid = None
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        finally:
            parent.close()
            if broker is not None:
                broker.close()
            if pid is not None:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(pid, 0)


if __name__ == '__main__':
    unittest.main()
