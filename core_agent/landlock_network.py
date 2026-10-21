"""Bounded HTTP egress and seccomp socket-FD mediation for Landlock execution.

Untrusted connect() never executes. Each notified TCP socket() receives an
already connected descriptor to this proxy; neither its address pointers nor
its memory are inspected. The proxy resolves and pins all actual destinations.
"""

import ctypes
import errno
import ipaddress
import os
import platform
import re
import secrets
import select
import selectors
import socket
import struct
import threading
import time
from urllib.parse import urlsplit


_HEADER_LIMIT = 32768
_BUFFER_LIMIT = 65536
_DNS_LIMIT = 16384
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')


def _hostname(value):
    if not value or '%' in value or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
        raise PermissionError('Destination denied')
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        value = value.rstrip('.').lower()
        if len(value) > 253 or any(not _LABEL.fullmatch(label) for label in value.split('.')):
            raise PermissionError('Destination denied')
        return value


def _dns_question(host, kind):
    return b''.join(bytes((len(label),)) + label.encode('ascii') for label in host.split('.')) + b'\0' + struct.pack('!HH', kind, 1)


def _dns_name(data, offset):
    labels, visited, end = [], set(), None
    for _ in range(128):
        if offset >= len(data) or offset in visited:
            raise ValueError('Invalid DNS name')
        visited.add(offset)
        size = data[offset]
        if size & 0xc0 == 0xc0:
            if offset + 1 >= len(data):
                raise ValueError('Invalid DNS pointer')
            if end is None:
                end = offset + 2
            offset = ((size & 0x3f) << 8) | data[offset + 1]
            continue
        if size > 63 or offset + 1 + size > len(data):
            raise ValueError('Invalid DNS label')
        offset += 1
        if not size:
            name = b'.'.join(labels).decode('ascii').lower()
            if len(name) > 253:
                raise ValueError('Invalid DNS name')
            return name, end if end is not None else offset
        labels.append(data[offset:offset + size])
        offset += size
    raise ValueError('Invalid DNS compression')


def _dns_answers(data, identifier, host, kind):
    if not 12 <= len(data) <= _DNS_LIMIT:
        raise ValueError('Invalid DNS response')
    received, flags, questions, answers, authority, additional = struct.unpack_from('!6H', data)
    if (received != identifier or not flags & 0x8000 or flags & 0x7a00
            or questions != 1 or answers + authority + additional > 256 or flags & 15 not in (0, 3)):
        raise ValueError('Invalid DNS response')
    question, offset = _dns_name(data, 12)
    if offset + 4 > len(data) or question != host or struct.unpack_from('!HH', data, offset) != (kind, 1):
        raise ValueError('Unexpected DNS question')
    offset += 4
    aliases, addresses = {}, []
    for index in range(answers + authority + additional):
        owner, offset = _dns_name(data, offset)
        if offset + 10 > len(data):
            raise ValueError('Truncated DNS record')
        record_type, record_class, _, size = struct.unpack_from('!HHIH', data, offset)
        offset += 10
        end = offset + size
        if end > len(data):
            raise ValueError('Truncated DNS data')
        if index < answers and record_class == 1:
            if record_type == 5:
                target, consumed = _dns_name(data, offset)
                if consumed != end or (owner in aliases and aliases[owner] != target):
                    raise ValueError('Invalid CNAME')
                aliases[owner] = target
            elif record_type == kind and size == (4 if kind == 1 else 16):
                addresses.append((owner, ipaddress.ip_address(data[offset:end])))
        offset = end
    if offset != len(data):
        raise ValueError('Unexpected DNS data')
    if flags & 15 == 3:
        return []
    allowed, current = {host}, host
    for _ in range(8):
        if current not in aliases:
            break
        current = aliases[current]
        if current in allowed:
            raise ValueError('CNAME cycle')
        allowed.add(current)
    else:
        raise ValueError('CNAME limit')
    return [address for owner, address in addresses if owner in allowed]


def _receive_exact(connection, size, deadline):
    result = bytearray()
    while len(result) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('DNS deadline')
        connection.settimeout(remaining)
        part = connection.recv(size - len(result))
        if not part:
            raise OSError('Connection closed')
        result.extend(part)
    return bytes(result)


class _SeccompData(ctypes.Structure):
    _fields_ = [('nr', ctypes.c_int), ('arch', ctypes.c_uint32),
                ('ip', ctypes.c_uint64), ('args', ctypes.c_uint64 * 6)]


class _Notification(ctypes.Structure):
    _fields_ = [('id', ctypes.c_uint64), ('pid', ctypes.c_uint32),
                ('flags', ctypes.c_uint32), ('data', _SeccompData)]


class _Response(ctypes.Structure):
    _fields_ = [('id', ctypes.c_uint64), ('value', ctypes.c_int64),
                ('error', ctypes.c_int32), ('flags', ctypes.c_uint32)]


class _AddFD(ctypes.Structure):
    _fields_ = [('id', ctypes.c_uint64), ('flags', ctypes.c_uint32),
                ('source', ctypes.c_uint32), ('target', ctypes.c_uint32),
                ('target_flags', ctypes.c_uint32)]


def _ioctl(descriptor, command, value):
    library = ctypes.CDLL(None, use_errno=True)
    library.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
    library.ioctl.restype = ctypes.c_int
    result = library.ioctl(descriptor, command, ctypes.byref(value))
    if result < 0:
        raise OSError(ctypes.get_errno(), 'Seccomp notification failed')
    return result


class EgressBroker:
    def __init__(self, policy, *, metadata_handler=None, max_connections=16, timeout_seconds=30):
        if not 1 <= max_connections <= 128 or not 0 < timeout_seconds <= 600:
            raise ValueError('Invalid broker limits')
        self.policy = policy
        self.metadata_handler = metadata_handler
        self.timeout = timeout_seconds
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._connections = set()
        self._threads = set()
        self._capacity = threading.BoundedSemaphore(max_connections)
        self._notification_fd = None
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.bind(('127.0.0.1', 0))
        self._server.listen(max_connections)
        self._server.settimeout(.2)
        self.address = self._server.getsockname()
        proxy = f'http://127.0.0.1:{self.address[1]}'
        self.environment = {name: proxy for name in (
            'http_proxy', 'https_proxy', 'all_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY')}
        self.environment.update(no_proxy='', NO_PROXY='')
        try:
            self._start_thread(self._accept)
        except Exception:
            self.close()
            raise

    @property
    def healthy(self):
        return not self._closed.is_set()

    def _start_thread(self, target, *arguments):
        def run():
            try:
                target(*arguments)
            except Exception:
                self.close()
            finally:
                with self._lock:
                    self._threads.discard(threading.current_thread())
        thread = threading.Thread(target=run, daemon=True)
        with self._lock:
            if self._closed.is_set():
                raise OSError('Broker closed')
            self._threads.add(thread)
            try:
                thread.start()
            except RuntimeError:
                self._threads.discard(thread)
                raise

    def _track(self, connection):
        with self._lock:
            if self._closed.is_set():
                connection.close()
                raise OSError('Broker closed')
            self._connections.add(connection)
        return connection

    def _release(self, connection):
        with self._lock:
            self._connections.discard(connection)
        connection.close()

    def _accept(self):
        while not self._closed.is_set():
            try:
                connection, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                self.close()
                return
            if not self._capacity.acquire(blocking=False):
                connection.close()
                continue
            try:
                self._track(connection)
                self._start_thread(self._serve, connection)
            except (OSError, RuntimeError):
                self._release(connection)
                self._capacity.release()
                self.close()
                return

    def _dns_query(self, host, kind, *, deadline=None):
        deadline = deadline or (time.monotonic() + min(self.timeout, 10))
        query_id = secrets.randbits(16)
        query = struct.pack('!6H', query_id, 0x100, 1, 0, 0, 0) + _dns_question(host, kind)
        for resolver in self.policy.dns_servers[:4]:
            address = ipaddress.ip_address(resolver)
            if not self.policy.permits_destination(address):
                raise PermissionError('Resolver denied')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            connection = self._track(socket.socket(socket.AF_INET if address.version == 4 else socket.AF_INET6))
            try:
                attempt_deadline = min(deadline, time.monotonic() + 3)
                connection.settimeout(min(remaining, 3))
                connection.connect((str(address), 53))
                connection.sendall(struct.pack('!H', len(query)) + query)
                length = struct.unpack('!H', _receive_exact(connection, 2, attempt_deadline))[0]
                if not 12 <= length <= _DNS_LIMIT:
                    raise ValueError('Invalid DNS response size')
                return _dns_answers(_receive_exact(connection, length, attempt_deadline), query_id, host, kind)
            except (OSError, ValueError):
                continue
            finally:
                self._release(connection)
        raise OSError('DNS unavailable')

    def _resolve(self, host):
        host = _hostname(host)
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            deadline = time.monotonic() + min(self.timeout, 10)
            addresses = []
            for kind in (1, 28):
                addresses.extend(self._dns_query(host, kind, deadline=deadline))
        if not addresses or len(addresses) > 32 or any(not self.policy.permits_destination(value) for value in addresses):
            raise PermissionError('Destination denied')
        return list(dict.fromkeys(addresses))

    def _connect_destination(self, host, port):
        if not 1 <= port <= 65535 or port == 53:
            raise PermissionError('Destination denied')
        addresses = self._resolve(host)
        deadline = time.monotonic() + min(self.timeout, 10)
        for address in addresses:
            # Recheck the exact numeric destination immediately before connect;
            # never re-resolve the hostname in libc or inherit environment proxies.
            if not self.policy.permits_destination(address):
                raise PermissionError('Destination denied')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            connection = self._track(socket.socket(socket.AF_INET if address.version == 4 else socket.AF_INET6))
            try:
                connection.settimeout(remaining)
                connection.connect((str(address), port))
                if ipaddress.ip_address(connection.getpeername()[0]) != address:
                    raise PermissionError('Unexpected destination')
                return connection
            except PermissionError:
                self._release(connection)
                raise
            except OSError:
                self._release(connection)
        raise OSError('Destination unavailable')

    def _request(self, client):
        data = bytearray()
        deadline = time.monotonic() + self.timeout
        while b'\r\n\r\n' not in data:
            remaining = deadline - time.monotonic()
            if len(data) >= _HEADER_LIMIT or remaining <= 0:
                raise PermissionError('Request limit')
            client.settimeout(remaining)
            part = client.recv(min(4096, _HEADER_LIMIT - len(data)))
            if not part:
                raise PermissionError('Incomplete request')
            data.extend(part)
        header, initial = bytes(data).split(b'\r\n\r\n', 1)
        lines = header.decode('ascii').split('\r\n')
        first = lines.pop(0).split(' ')
        if len(first) != 3:
            raise PermissionError('Invalid request line')
        method, target, version = first
        if not _TOKEN.fullmatch(method) or version not in ('HTTP/1.0', 'HTTP/1.1') or any(ord(c) <= 32 for c in target):
            raise PermissionError('Invalid request line')
        headers = []
        for line in lines:
            name, separator, value = line.partition(':')
            if not separator or not _TOKEN.fullmatch(name) or any(ord(c) < 32 and c != '\t' or ord(c) == 127 for c in value):
                raise PermissionError('Invalid header')
            headers.append((name, value.strip()))
        lengths = [value for name, value in headers if name.lower() == 'content-length']
        encodings = [value for name, value in headers if name.lower() == 'transfer-encoding']
        if (len(lengths) > 1 or len(encodings) > 1 or (lengths and encodings)
                or (lengths and not re.fullmatch('[0-9]{1,20}', lengths[0]))
                or (encodings and encodings[0].lower() != 'chunked')):
            raise PermissionError('Invalid request framing')
        parsed = urlsplit('//' + target if method == 'CONNECT' else target)
        if (parsed.username is not None or parsed.password is not None or parsed.fragment
                or not parsed.hostname or '%' in parsed.netloc or '\\' in parsed.netloc):
            raise PermissionError('Invalid destination')
        host = _hostname(parsed.hostname)
        if method == 'CONNECT':
            if parsed.path or parsed.query or parsed.port is None:
                raise PermissionError('Invalid CONNECT')
            port = parsed.port
            forward = None
        else:
            if parsed.scheme != 'http':
                raise PermissionError('Unsupported proxy scheme')
            port = parsed.port if parsed.port is not None else 80
            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query
            authority = f'[{host}]' if ':' in host else host
            if port != 80:
                authority += ':' + str(port)
            removed = {'host', 'connection', 'proxy-connection', 'proxy-authorization', 'proxy-authenticate'}
            for name, value in headers:
                if name.lower() == 'connection':
                    removed.update(part.strip().lower() for part in value.split(','))
            kept = [(name, value) for name, value in headers if name.lower() not in removed]
            forward = (f'{method} {path} {version}\r\nHost: {authority}\r\nConnection: close\r\n'
                       + ''.join(f'{name}: {value}\r\n' for name, value in kept) + '\r\n').encode('ascii')
        if not 1 <= port <= 65535 or port == 53:
            raise PermissionError('Destination denied')
        return host, port, forward, initial

    def _serve(self, client):
        upstream = None
        established = False
        try:
            host, port, forward, initial = self._request(client)
            upstream = self._connect_destination(host, port)
            if forward is None:
                client.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            else:
                upstream.sendall(forward)
            established = True
            self._relay(client, upstream, initial)
        except (OSError, ValueError, UnicodeError) as error:
            if not established:
                try:
                    client.settimeout(.1)
                    status = b'403 Forbidden' if isinstance(error, (PermissionError, ValueError, UnicodeError)) else b'502 Bad Gateway'
                    client.sendall(b'HTTP/1.1 ' + status + b'\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                except OSError:
                    pass
        finally:
            if upstream is not None:
                self._release(upstream)
            self._release(client)
            self._capacity.release()

    def _relay(self, client, upstream, initial):
        peers = {client: upstream, upstream: client}
        pending = {client: bytearray(), upstream: bytearray(initial)}
        reading = {client, upstream}
        shutdown = set()
        for connection in peers:
            connection.setblocking(False)
        deadline = time.monotonic() + self.timeout
        with selectors.DefaultSelector() as selector:
            while not self._closed.is_set():
                for connection, other in peers.items():
                    if other not in reading and not pending[connection] and connection not in shutdown:
                        connection.shutdown(socket.SHUT_WR)
                        shutdown.add(connection)
                    events = (selectors.EVENT_READ if connection in reading and len(pending[other]) < _BUFFER_LIMIT else 0)
                    if pending[connection]:
                        events |= selectors.EVENT_WRITE
                    try:
                        selector.unregister(connection)
                    except KeyError:
                        pass
                    if events:
                        selector.register(connection, events)
                if not selector.get_map():
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Proxy idle timeout')
                for key, events in selector.select(min(remaining, .2)):
                    connection = key.fileobj
                    other = peers[connection]
                    if events & selectors.EVENT_READ:
                        try:
                            data = connection.recv(min(16384, _BUFFER_LIMIT - len(pending[other])))
                        except BlockingIOError:
                            data = None
                        if data:
                            pending[other].extend(data)
                            deadline = time.monotonic() + self.timeout
                        elif data == b'':
                            reading.discard(connection)
                    if events & selectors.EVENT_WRITE:
                        try:
                            sent = connection.send(pending[connection])
                        except BlockingIOError:
                            sent = 0
                        if sent:
                            del pending[connection][:sent]
                            deadline = time.monotonic() + self.timeout

    def start_notifications(self, listener_fd):
        """Take ownership of an installed filter's listener FD; never CONTINUE."""
        if (self._notification_fd is not None or platform.system() != 'Linux'
                or platform.machine() not in ('x86_64', 'aarch64')):
            raise ValueError('Notification listener unavailable')
        self._notification_fd = listener_fd
        try:
            os.set_inheritable(listener_fd, False)
            self._start_thread(self._notifications, listener_fd)
        except Exception:
            self.close()
            raise

    def _metadata_notification(self, descriptor, notification):
        try:
            identifier = ctypes.c_uint64(notification.id)
            _ioctl(descriptor, 0x40082102, identifier)
            result, error_code = 0, 0
            try:
                if self.metadata_handler is None:
                    raise PermissionError(errno.EACCES, 'Operation denied')
                result = self.metadata_handler(notification)
                if result != 0:
                    raise ValueError('Invalid metadata result')
            except OSError as error:
                error_code = -(error.errno if error.errno and 0 < error.errno < 4096 else errno.EACCES)
            _ioctl(descriptor, 0x40082102, identifier)
            _ioctl(descriptor, 0xc0182101, _Response(notification.id, result, error_code, 0))
        except OSError as error:
            if error.errno != errno.ENOENT:
                raise

    def _notifications(self, descriptor):
        syscall, architecture = {'x86_64': (41, 0xc000003e), 'aarch64': (198, 0xc00000b7)}[platform.machine()]
        poll = select.poll()
        poll.register(descriptor, select.POLLIN)
        try:
            while not self._closed.is_set():
                ready = poll.poll(200)
                if not ready:
                    continue
                if ready[0][1] & (select.POLLERR | select.POLLHUP | select.POLLNVAL):
                    return
                notification = _Notification()
                try:
                    _ioctl(descriptor, 0xc0502100, notification)
                except OSError as error:
                    if error.errno in (errno.EINTR, errno.ENOENT):
                        continue
                    raise
                family, kind, protocol = notification.data.args[:3]
                if notification.data.arch == architecture and notification.data.nr != syscall:
                    self._metadata_notification(descriptor, notification)
                    continue
                allowed = (notification.data.arch == architecture and notification.data.nr == syscall
                           and family in (socket.AF_INET, socket.AF_INET6)
                           and kind & ~(socket.SOCK_CLOEXEC | socket.SOCK_NONBLOCK) == socket.SOCK_STREAM
                           and protocol in (0, socket.IPPROTO_TCP))
                connection = None
                try:
                    if not allowed:
                        raise PermissionError(errno.EPERM, 'Socket denied')
                    connection = socket.create_connection(self.address, timeout=1)
                    connection.setblocking(not bool(kind & socket.SOCK_NONBLOCK))
                    injected = _AddFD(notification.id, 2, connection.fileno(), 0,
                                      os.O_CLOEXEC if kind & socket.SOCK_CLOEXEC else 0)
                    _ioctl(descriptor, 0x40182103, injected)
                except OSError as error:
                    if error.errno != errno.ENOENT:
                        # An unsupported ADDFD is fatal, never a plain-socket fallback.
                        response = _Response(notification.id, 0, -errno.EACCES, 0)
                        try:
                            _ioctl(descriptor, 0xc0182101, response)
                        except OSError as reply_error:
                            if reply_error.errno != errno.ENOENT:
                                raise
                        if allowed:
                            raise
                finally:
                    if connection is not None:
                        connection.close()
        except (OSError, ValueError):
            pass
        finally:
            self.close()

    def close(self):
        with self._lock:
            first = not self._closed.is_set()
            self._closed.set()
            connections = tuple(self._connections)
            threads = tuple(self._threads)
            descriptor, self._notification_fd = self._notification_fd, None
        if first:
            self._server.close()
            for connection in connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
            if descriptor is not None:
                os.close(descriptor)
        # A worker can fail closed, but must not join other workers which are
        # themselves unwinding close(). The supervisor performs the bounded join.
        if threading.current_thread() in threads:
            return
        deadline = time.monotonic() + 2
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            raise TimeoutError('Network broker cleanup is unconfirmed')

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
