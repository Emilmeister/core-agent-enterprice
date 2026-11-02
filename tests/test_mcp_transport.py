"""Regressions for MCP discovery pagination and the MCP transport layer.

Spec anchors: tools.md "Discovery MUST читать все страницы tools/list ...",
agent-configuration.md MCP proxy/client_certificate/TLS section.

All TLS fixtures here are synthetic certificates generated at runtime; the
only patched production hook is the trust factory (`trust_bundle_paths`).
No literal key material appears in this file.
"""

from __future__ import annotations

import base64
import atexit
import datetime
import json
import os
import socket
import socketserver
import ssl
import stat
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from core_agent.errors import CoreError
from core_agent.mcp import StreamableHttpMcpConnector

# ---------------------------------------------------------------- synthetic PKI


def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _issue(cn, *, ca=False, issuer=None, issuer_key=None, client=False):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(_name(cn) if issuer is None else issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress_address("127.0.0.1")), x509.DNSName("localhost")]
            ),
            critical=False,
        )
    )
    if not ca:
        usages = (
            [ExtendedKeyUsageOID.CLIENT_AUTH]
            if client
            else [ExtendedKeyUsageOID.SERVER_AUTH]
        )
        builder = builder.add_extension(
            x509.ExtendedKeyUsage(usages), critical=False
        )
    cert = builder.sign(issuer_key or key, hashes.SHA256())
    return key, cert


def ipaddress_address(text):
    import ipaddress

    return ipaddress.ip_address(text)


def _cert_pem(cert):
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _key_pem(key):
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


CA_KEY, CA_CERT = _issue("synthetic test CA", ca=True)
OTHER_CA_KEY, OTHER_CA_CERT = _issue("synthetic foreign CA", ca=True)
SERVER_KEY, SERVER_CERT = _issue(
    "mcp destination", issuer=CA_CERT.subject, issuer_key=CA_KEY
)
PROXY_KEY, PROXY_CERT = _issue(
    "mcp proxy", issuer=CA_CERT.subject, issuer_key=CA_KEY
)
CLIENT_KEY, CLIENT_CERT = _issue(
    "mcp-client-identity",
    issuer=CA_CERT.subject,
    issuer_key=CA_KEY,
    client=True,
)

_FIXTURES = tempfile.TemporaryDirectory(prefix="core-mcp-tls-")
atexit.register(_FIXTURES.cleanup)
CA_PATH = os.path.join(_FIXTURES.name, "ca.pem")
with open(CA_PATH, "w", encoding="utf-8") as handle:
    handle.write(_cert_pem(CA_CERT))
OTHER_CA_PATH = os.path.join(_FIXTURES.name, "other-ca.pem")
with open(OTHER_CA_PATH, "w", encoding="utf-8") as handle:
    handle.write(_cert_pem(OTHER_CA_CERT))

CLIENT_IDENTITY = {
    "certificate_pem": _cert_pem(CLIENT_CERT),
    "private_key_pem": _key_pem(CLIENT_KEY),
}


def _write_pem(text):
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".pem", delete=False, encoding="utf-8", dir=_FIXTURES.name
    )
    with handle:
        handle.write(text)
    return handle.name


def _server_ssl_context(key=None, cert=None, client_ca=None):
    key = SERVER_KEY if key is None else key
    cert = SERVER_CERT if cert is None else cert
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=_write_pem(_cert_pem(cert)), keyfile=_write_pem(_key_pem(key)))
    if client_ca is not None:
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cafile=client_ca)
    return context


SERVER_CERT_PATH = _write_pem(_cert_pem(SERVER_CERT))
SERVER_KEY_PATH = _write_pem(_key_pem(SERVER_KEY))
PROXY_CERT_PATH = _write_pem(_cert_pem(PROXY_CERT))
PROXY_KEY_PATH = _write_pem(_key_pem(PROXY_KEY))


# ------------------------------------------------------------------ MCP server


def _tools(start, count):
    return [
        {"name": f"tool_{index}", "inputSchema": {"type": "object"}}
        for index in range(start, start + count)
    ]


class _McpHandler(BaseHTTPRequestHandler):
    """JSON-RPC MCP responder driven by `server.pages`: (cursor_in, page) list."""

    def log_message(self, *args):
        pass

    def _send(self, status, payload=None, headers=None):
        body = b"" if payload is None else json.dumps(payload).encode()
        if body and self.server.sse:
            body = b'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n' + b"data: " + body + b"\n\n"
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "text/event-stream" if self.server.sse else "application/json")
            self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)
        self.wfile.flush()

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        method = request.get("method")
        params = request.get("params") or {}
        record = {
            "method": method,
            "params": params,
            "headers": {key.lower(): value for key, value in self.headers.items()},
        }
        cert_getter = getattr(self.connection, "getpeercert", None)
        if cert_getter is not None:
            try:
                peer = cert_getter()
            except ValueError:
                peer = None
            if peer:
                record["peer_common_name"] = dict(
                    item for rdn in peer.get("subject", ()) for item in rdn
                ).get("commonName")
        self.server.requests.append(record)
        if method == "initialize":
            return self._send(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "serverInfo": {"name": "fixture", "version": "1"},
                    },
                },
                {"Mcp-Session-Id": self.server.session}
                if self.server.session
                else None,
            )
        if method == "tools/list":
            page = next(
                (page for cursor, page in self.server.pages if cursor == params.get("cursor")),
                None,
            )
            if page is None:
                return self._send(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": request.get("id"),
                        "error": {"code": -32602, "message": "unknown cursor"},
                    },
                )
            if page.get("status"):
                return self._send(page["status"])
            if page.get("delay"):
                time.sleep(page["delay"])
            return self._send(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "result": {key: value for key, value in page.items() if key not in ("status", "delay")},
                },
            )
        return self._send(
            200, {"jsonrpc": "2.0", "id": request.get("id"), "result": {}}
        )


def _start_http_server(pages, *, session=None, ssl_context=None, sse=False):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _McpHandler)
    server.daemon_threads = True
    server.pages = list(pages)
    server.session = session
    server.requests = []
    server.sse = sse
    if ssl_context is not None:
        server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ------------------------------------------------------------- CONNECT proxy


class _ConnectHandler(socketserver.BaseRequestHandler):
    def setup(self):
        if self.server.proxy_ssl_context is not None:
            self.request = self.server.proxy_ssl_context.wrap_socket(
                self.request, server_side=True
            )

    def handle(self):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            data += chunk
        head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        lines = head.split("\r\n")
        method, target, _version = lines[0].split(" ", 2)
        headers = {
            line.split(":", 1)[0].strip().lower(): line.split(":", 1)[1].strip()
            for line in lines[1:]
            if ":" in line
        }
        self.server.connects.append(target)
        self.server.connect_headers.append(headers)
        peer = getattr(self.request, "getpeercert", lambda: None)
        try:
            peer_cert = peer()
        except ValueError:
            peer_cert = None
        self.server.peer_subjects.append(
            dict(item for rdn in (peer_cert or {}).get("subject", ()) for item in rdn).get(
                "commonName"
            )
        )
        expected = self.server.expected_proxy_auth
        if method != "CONNECT" or (
            expected is not None and headers.get("proxy-authorization") != expected
        ):
            self.request.sendall(
                b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                b"Proxy-Authenticate: Basic realm=\"proxy\"\r\n"
                b"Content-Length: 0\r\nConnection: close\r\n\r\n"
            )
            return
        host, _, port = target.rpartition(":")
        upstream = socket.create_connection((host, int(port)))
        self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

        def pump(source, sink):
            try:
                while True:
                    chunk = source.recv(65536)
                    if not chunk:
                        break
                    sink.sendall(chunk)
            except OSError:
                pass
            finally:
                try:
                    sink.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        forward = threading.Thread(target=pump, args=(upstream, self.request), daemon=True)
        forward.start()
        pump(self.request, upstream)
        forward.join(timeout=10)
        upstream.close()


class _ProxyServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    block_on_close = False


def _start_proxy(*, ssl_context=None, expected_proxy_auth=None):
    server = _ProxyServer(("127.0.0.1", 0), _ConnectHandler)
    server.proxy_ssl_context = ssl_context
    server.expected_proxy_auth = expected_proxy_auth
    server.connects = []
    server.connect_headers = []
    server.peer_subjects = []
    server.server_port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ---------------------------------------------------------------- test cases


def _declaration(url, **extra):
    declaration = {
        "name": "srv",
        "transport": {"type": "streamable_http", "url": url},
    }
    transport = extra.pop("transport", None)
    declaration.update(extra)
    if transport:
        declaration["transport"].update(transport)
    return declaration


class TrustPatch(unittest.TestCase):
    def patch_trust(self, *paths):
        handle = mock.patch(
            "core_agent.mcp_transport.trust_bundle_paths", return_value=tuple(paths)
        )
        handle.start()
        self.addCleanup(handle.stop)


class DiscoveryPaginationTests(TrustPatch):
    def setUp(self):
        self.patch_trust(CA_PATH)

    def connect(self, pages, *, session=None, declaration=None, sse=False, **connector_kwargs):
        server = _start_http_server(pages, session=session, sse=sse)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.server = server
        connector_kwargs.setdefault("cold_start_timeout", 0)
        connector = StreamableHttpMcpConnector(timeout=5, **connector_kwargs)
        declaration = declaration or _declaration(
            f"http://127.0.0.1:{server.server_port}/mcp",
            headers={"Authorization": "literal-token-42"},
        )
        return connector, connector.connect(declaration)

    def test_all_pages_are_read_and_merged(self):
        catalog = self.connect(
            [
                (None, {"tools": _tools(0, 2), "nextCursor": "c1"}),
                ("c1", {"tools": _tools(2, 2), "nextCursor": "c2"}),
                ("c2", {"tools": _tools(4, 1)}),
            ]
        )[1]
        self.assertEqual(sorted(catalog), [f"tool_{i}" for i in range(5)])
        list_calls = [r for r in self.server.requests if r["method"] == "tools/list"]
        self.assertEqual(
            [call["params"].get("cursor") for call in list_calls], [None, "c1", "c2"]
        )

    def test_explicit_null_cursor_ends_the_list(self):
        catalog = self.connect(
            [(None, {"tools": _tools(0, 1), "nextCursor": None}), ]
        )[1]
        self.assertEqual(sorted(catalog), ["tool_0"])

    def test_sse_pagination_skips_notifications_and_collects_all_pages(self):
        catalog = self.connect([(None, {"tools": _tools(0, 1), "nextCursor": "next"}),
                                ("next", {"tools": _tools(1, 1)})], sse=True)[1]
        self.assertEqual(sorted(catalog), ["tool_0", "tool_1"])

    def test_session_and_protocol_version_are_shared_across_pages(self):
        self.connect(
            [
                (None, {"tools": _tools(0, 1), "nextCursor": "c1"}),
                ("c1", {"tools": _tools(1, 1)}),
            ],
            session="sess-9",
        )
        list_calls = [r for r in self.server.requests if r["method"] == "tools/list"]
        self.assertEqual(len(list_calls), 2)
        for call in list_calls:
            self.assertEqual(call["headers"].get("mcp-session-id"), "sess-9")
            self.assertEqual(
                call["headers"].get("mcp-protocol-version"), "2025-11-25"
            )

    def test_repeated_cursor_is_rejected(self):
        with self.assertRaises(CoreError) as caught:
            self.connect(
                [
                    (None, {"tools": _tools(0, 1), "nextCursor": "c1"}),
                    ("c1", {"tools": _tools(1, 1), "nextCursor": "c1"}),
                ]
            )
        self.assertEqual(caught.exception.code, "MCP_PROTOCOL_ERROR")

    def test_empty_nonstring_and_oversized_cursors_are_rejected(self):
        for bad in ("", 17, "x" * 4097, "\ud800"):
            with self.subTest(cursor=repr(bad)[:20]):
                with self.assertRaises(CoreError) as caught:
                    self.connect(
                        [(None, {"tools": _tools(0, 1), "nextCursor": bad})]
                    )
                self.assertEqual(caught.exception.code, "MCP_PROTOCOL_ERROR")

    def test_4096_byte_cursor_is_accepted(self):
        big = "x" * 4096
        catalog = self.connect(
            [
                (None, {"tools": _tools(0, 1), "nextCursor": big}),
                (big, {"tools": _tools(1, 1)}),
            ]
        )[1]
        self.assertEqual(sorted(catalog), ["tool_0", "tool_1"])

    def test_page_limit_is_128(self):
        pages = [(None, {"tools": [], "nextCursor": "c1"})]
        for index in range(2, 130):
            pages.append((f"c{index - 1}", {"tools": [], "nextCursor": f"c{index}"}))
        with self.assertRaises(CoreError) as caught:
            self.connect(pages)
        self.assertEqual(caught.exception.code, "MCP_PROTOCOL_ERROR")
        list_calls = [r for r in self.server.requests if r["method"] == "tools/list"]
        self.assertEqual(len(list_calls), 128)

    def test_exactly_128_pages_succeed(self):
        pages = [(None, {"tools": _tools(0, 1), "nextCursor": "c1"})]
        for index in range(2, 129):
            pages.append(
                (
                    f"c{index - 1}",
                    {"tools": _tools(index - 1, 1), "nextCursor": f"c{index}"}
                    if index < 128
                    else {"tools": _tools(index - 1, 1)},
                )
            )
        catalog = self.connect(pages)[1]
        self.assertEqual(len(catalog), 128)

    def test_total_tool_limit_is_10000(self):
        with self.assertRaises(CoreError) as caught:
            self.connect(
                [
                    (None, {"tools": _tools(0, 6000), "nextCursor": "c1"}),
                    ("c1", {"tools": _tools(6000, 4001)}),
                ]
            )
        self.assertEqual(caught.exception.code, "MCP_PROTOCOL_ERROR")

    def test_exactly_10000_tools_succeed(self):
        catalog = self.connect(
            [
                (None, {"tools": _tools(0, 6000), "nextCursor": "c1"}),
                ("c1", {"tools": _tools(6000, 4000)}),
            ]
        )[1]
        self.assertEqual(len(catalog), 10000)

    def test_duplicate_tool_name_across_pages_is_rejected(self):
        with self.assertRaises(CoreError) as caught:
            self.connect(
                [
                    (None, {"tools": _tools(0, 2), "nextCursor": "c1"}),
                    ("c1", {"tools": _tools(1, 2)}),
                ]
            )
        self.assertEqual(caught.exception.code, "MCP_PROTOCOL_ERROR")

    def test_invalid_descriptor_on_a_later_page_is_rejected(self):
        with self.assertRaises(CoreError) as caught:
            self.connect(
                [
                    (None, {"tools": _tools(0, 1), "nextCursor": "c1"}),
                    ("c1", {"tools": [{"name": "no_schema", "inputSchema": "not-a-dict"}]}),
                ]
            )
        self.assertEqual(caught.exception.code, "MCP_PROTOCOL_ERROR")

    def test_later_page_failure_never_publishes_a_partial_catalog(self):
        with self.assertRaises(CoreError) as caught:
            self.connect(
                [
                    (None, {"tools": _tools(0, 3), "nextCursor": "c1"}),
                    ("c1", {"status": 500}),
                ],
                cold_start_timeout=0.2,
            )
        self.assertEqual(caught.exception.code, "MCP_CONNECTION_FAILED")

    def test_deadline_is_shared_across_pages(self):
        server = _start_http_server(
            [
                (None, {"tools": _tools(0, 1), "nextCursor": "c1"}),
                ("c1", {"tools": _tools(1, 1), "delay": 3.0}),
            ]
        )
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        connector = StreamableHttpMcpConnector(timeout=5, cold_start_timeout=0)
        started = time.monotonic()
        with self.assertRaises(CoreError) as caught:
            connector.connect(
                _declaration(f"http://127.0.0.1:{server.server_port}/mcp"),
                deadline=started + 0.6,
            )
        self.assertEqual(caught.exception.code, "MCP_CONNECTION_FAILED")
        self.assertLess(time.monotonic() - started, 2.5)

    def test_cancellation_mid_pagination_stops_everything(self):
        server = _start_http_server(
            [
                (None, {"tools": _tools(0, 1), "nextCursor": "c1"}),
                ("c1", {"tools": _tools(1, 1), "delay": 3.0}),
            ]
        )
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        connector = StreamableHttpMcpConnector(timeout=5, cold_start_timeout=0)
        started = time.monotonic()
        with self.assertRaises(CoreError) as caught:
            connector.connect(
                _declaration(f"http://127.0.0.1:{server.server_port}/mcp"),
                cancel_event=cancel,
            )
        self.assertEqual(caught.exception.code, "TASK_CANCELLED")
        self.assertLess(time.monotonic() - started, 2.5)

    def test_header_values_stay_literal(self):
        self.connect([(None, {"tools": _tools(0, 1)})])
        initialize = self.server.requests[0]
        self.assertEqual(initialize["headers"].get("authorization"), "literal-token-42")


class TransportOptionTests(TrustPatch):
    def test_packaged_trust_is_mandatory_and_ignores_environment_ca(self):
        from core_agent import mcp_transport
        with mock.patch.dict(os.environ, {"SSL_CERT_FILE": "/missing/env-ca", "SSL_CERT_DIR": "/missing/env-dir"}):
            roots = mcp_transport.destination_ssl_context().get_ca_certs(binary_form=True)
        fingerprints = {x509.load_der_x509_certificate(root).fingerprint(hashes.SHA256()).hex() for root in roots}
        self.assertIn("d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31", fingerprints)
        with mock.patch.object(mcp_transport, "_PACKAGED_TRUSTED_ROOT", "/missing/packaged-ca"):
            with self.assertRaises(CoreError) as caught:
                mcp_transport.destination_ssl_context()
        self.assertEqual(caught.exception.code, "CONFIG_INVALID")

    def test_direct_connection_ignores_proxy_environment(self):
        self.patch_trust(CA_PATH)
        with mock.patch.dict(
            os.environ,
            {
                "HTTP_PROXY": "http://127.0.0.1:9",
                "HTTPS_PROXY": "http://127.0.0.1:9",
                "ALL_PROXY": "http://127.0.0.1:9",
                "http_proxy": "http://127.0.0.1:9",
                "https_proxy": "http://127.0.0.1:9",
            },
            clear=False,
        ):
            server = _start_http_server([(None, {"tools": _tools(0, 1)})])
            self.addCleanup(server.shutdown)
            self.addCleanup(server.server_close)
            catalog = StreamableHttpMcpConnector(
                timeout=5, cold_start_timeout=0
            ).connect(
                _declaration(f"http://127.0.0.1:{server.server_port}/mcp")
            )
        self.assertEqual(sorted(catalog), ["tool_0"])

    def test_proxy_url_without_credentials_and_invalid_shapes_are_rejected(self):
        from core_agent.mcp_transport import http_client_options

        self.patch_trust(CA_PATH)
        base = _declaration(
            "https://mcp.test/mcp", transport={"proxy_url": "http://127.0.0.1:8080"}
        )
        options, _secrets = http_client_options(base)
        self.assertIs(options["trust_env"], False)
        self.assertEqual(str(options["proxy"].url).rstrip("/"), "http://127.0.0.1:8080")
        self.assertIsNone(options["proxy"].auth)
        # proxy auth without a proxy is never silently accepted.
        with self.assertRaises(CoreError):
            http_client_options(
                _declaration("https://mcp.test/mcp", proxy_auth={"username": "u", "password": "p"})
            )
        # credentials never ride inside the proxy URL as userinfo.
        with self.assertRaises(CoreError):
            http_client_options(
                _declaration(
                    "https://mcp.test/mcp",
                    transport={"proxy_url": "http://user:pass@127.0.0.1:8080"},
                )
            )
        # userinfo/query/fragment and oversized URLs are rejected.
        with self.assertRaises(CoreError):
            http_client_options(
                _declaration(
                    "https://mcp.test/mcp",
                    transport={"proxy_url": "http://127.0.0.1:8080/purpose"},
                )
            )
        with self.assertRaises(CoreError):
            http_client_options(
                _declaration(
                    "https://mcp.test/mcp",
                    transport={"proxy_url": "http://127.0.0.1:8080/" + "q" * 4096},
                )
            )
        with self.assertRaises(CoreError):
            http_client_options(
                _declaration("https://mcp.test/mcp", proxy_auth={"username": "u"})
            )
        # without proxy/identity the options are plain but still trust_env=False.
        plain, secrets = http_client_options(_declaration("https://mcp.test/mcp"))
        self.assertIs(plain["trust_env"], False)
        self.assertNotIn("proxy", plain)
        self.assertEqual(secrets, ())


class TlsTransportTests(TrustPatch):
    def test_two_connections_keep_their_own_proxy_and_credentials(self):
        self.patch_trust(CA_PATH)
        connector = StreamableHttpMcpConnector(timeout=5, cold_start_timeout=0)
        pairs = []
        for name in ("first", "second"):
            expected = "Basic " + base64.b64encode(f"{name}:{name}-password".encode()).decode()
            proxy = _start_proxy(expected_proxy_auth=expected)
            destination = _start_http_server([(None, {"tools": [{"name": name, "inputSchema": {}}]})],
                                             ssl_context=_server_ssl_context())
            for server in (proxy, destination):
                self.addCleanup(server.shutdown)
                self.addCleanup(server.server_close)
            declaration = _declaration(f"https://127.0.0.1:{destination.server_port}/mcp",
                transport={"proxy_url": f"http://127.0.0.1:{proxy.server_port}"},
                proxy_auth={"username": name, "password": name + "-password"})
            declaration["name"] = name
            pairs.append((declaration, proxy, destination, expected))
            self.assertEqual(set(connector.connect(declaration)), {name})
        for declaration, proxy, destination, expected in pairs:
            connector.call(declaration["name"], declaration["name"], {})
            self.assertEqual(set(proxy.connects), {f"127.0.0.1:{destination.server_port}"})
            self.assertTrue(all(headers.get("proxy-authorization") == expected for headers in proxy.connect_headers))

    def test_wrong_authority_is_rejected_without_retry_churn(self):
        self.patch_trust(CA_PATH)
        foreign_context = _server_ssl_context(
            *_issue("mcp destination", issuer=OTHER_CA_CERT.subject, issuer_key=OTHER_CA_KEY)
        )
        server = _start_http_server([], ssl_context=foreign_context)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        started = time.monotonic()
        with self.assertRaises(CoreError) as caught:
            StreamableHttpMcpConnector(timeout=5, cold_start_timeout=300).connect(
                _declaration(f"https://127.0.0.1:{server.server_port}/mcp")
            )
        self.assertEqual(caught.exception.code, "MCP_CONNECTION_FAILED")
        self.assertFalse(caught.exception.retryable)
        self.assertLess(time.monotonic() - started, 5)

    def test_http_connect_proxy_with_basic_auth_reaches_the_destination(self):
        for password in ("proxy-secret-77", ""):
            with self.subTest(empty_password=not password):
                self._check_http_proxy(password)

    def _check_http_proxy(self, password):
        self.patch_trust(CA_PATH)
        expected = "Basic " + base64.b64encode(f"ops:{password}".encode()).decode()
        proxy = _start_proxy(expected_proxy_auth=expected)
        self.addCleanup(proxy.shutdown)
        self.addCleanup(proxy.server_close)
        # HTTPS destination: only then does an HTTP proxy carry the traffic
        # through a real CONNECT tunnel instead of absolute-URI forwarding.
        destination = _start_http_server(
            [(None, {"tools": _tools(0, 2)})], ssl_context=_server_ssl_context()
        )
        self.addCleanup(destination.shutdown)
        self.addCleanup(destination.server_close)
        connector = StreamableHttpMcpConnector(timeout=5, cold_start_timeout=0)
        catalog = connector.connect(
            _declaration(
                f"https://127.0.0.1:{destination.server_port}/mcp",
                transport={"proxy_url": f"http://127.0.0.1:{proxy.server_port}"},
                proxy_auth={"username": "ops", "password": password},
                headers={"Authorization": "literal-token-42"},
            )
        )
        self.assertEqual(sorted(catalog), ["tool_0", "tool_1"])
        self.assertEqual(set(proxy.connects), {f"127.0.0.1:{destination.server_port}"})
        self.assertGreaterEqual(len(proxy.connects), 1)
        self.assertTrue(
            all(
                headers.get("proxy-authorization") == expected
                for headers in proxy.connect_headers
            )
        )
        # Proxy credentials never travel as ordinary headers to the target.
        for request in destination.requests:
            self.assertNotIn("proxy-authorization", request["headers"])
            self.assertEqual(request["headers"].get("authorization"), "literal-token-42")

    def test_proxy_rejecting_auth_fails_fast_without_leaking_the_password(self):
        self.patch_trust(CA_PATH)
        proxy = _start_proxy(expected_proxy_auth="Basic " + base64.b64encode(b"ops:right").decode())
        self.addCleanup(proxy.shutdown)
        self.addCleanup(proxy.server_close)
        destination = _start_http_server(
            [(None, {"tools": _tools(0, 1)})], ssl_context=_server_ssl_context()
        )
        self.addCleanup(destination.shutdown)
        self.addCleanup(destination.server_close)
        with self.assertRaises(CoreError) as caught:
            StreamableHttpMcpConnector(timeout=5, cold_start_timeout=300).connect(
                _declaration(
                    f"https://127.0.0.1:{destination.server_port}/mcp",
                    transport={"proxy_url": f"http://127.0.0.1:{proxy.server_port}"},
                    proxy_auth={"username": "ops", "password": "wrong-password"},
                )
            )
        self.assertEqual(caught.exception.code, "MCP_CONNECTION_FAILED")
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn("wrong-password", str(caught.exception))

    def test_https_proxy_gets_trusted_ca_but_never_the_client_identity(self):
        self.patch_trust(CA_PATH)
        proxy_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        proxy_context.load_cert_chain(certfile=PROXY_CERT_PATH, keyfile=PROXY_KEY_PATH)
        proxy_context.verify_mode = ssl.CERT_OPTIONAL
        proxy_context.load_verify_locations(cafile=CA_PATH)
        proxy = _start_proxy(ssl_context=proxy_context)
        self.addCleanup(proxy.shutdown)
        self.addCleanup(proxy.server_close)
        destination_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        destination_context.load_cert_chain(certfile=SERVER_CERT_PATH, keyfile=SERVER_KEY_PATH)
        destination_context.verify_mode = ssl.CERT_REQUIRED
        destination_context.load_verify_locations(cafile=CA_PATH)
        destination = _start_http_server([], ssl_context=destination_context)
        self.addCleanup(destination.shutdown)
        self.addCleanup(destination.server_close)
        connector = StreamableHttpMcpConnector(timeout=10, cold_start_timeout=0)
        # Destination demands a client certificate; a client that offered none
        # to the proxy is exactly what makes this a separation test.
        with self.assertRaises(CoreError):
            connector.connect(
                _declaration(
                    f"https://127.0.0.1:{destination.server_port}/mcp",
                    transport={"proxy_url": f"https://127.0.0.1:{proxy.server_port}"},
                )
            )
        # The proxy TLS saw no certificate (identity stayed destination-side).
        self.assertEqual(proxy.peer_subjects, [None])
        self.assertEqual(proxy.connects, [f"127.0.0.1:{destination.server_port}"])
        # With the identity configured the handshake completes end to end.
        catalog_pages = [(None, {"tools": _tools(0, 1)})]
        destination.pages = catalog_pages
        catalog = connector.connect(
            _declaration(
                f"https://127.0.0.1:{destination.server_port}/mcp",
                transport={"proxy_url": f"https://127.0.0.1:{proxy.server_port}"},
                client_certificate=CLIENT_IDENTITY,
            )
        )
        self.assertEqual(sorted(catalog), ["tool_0"])
        self.assertIn("mcp-client-identity", [r.get("peer_common_name") for r in destination.requests])
        # Every proxy TLS session — from both connects — saw no client identity.
        self.assertTrue(all(subject is None for subject in proxy.peer_subjects))
        self.assertGreaterEqual(len(proxy.peer_subjects), 2)
        # URL schemes are case-insensitive and must select the same proxy trust.
        uppercase = _declaration(
            f"https://127.0.0.1:{destination.server_port}/mcp",
            transport={"proxy_url": f"HTTPS://127.0.0.1:{proxy.server_port}"},
            client_certificate=CLIENT_IDENTITY,
        )
        self.assertEqual(connector.connect(uppercase), catalog)
        self.assertTrue(all(subject is None for subject in proxy.peer_subjects))

    def test_cleanup_failure_is_reported_without_starting_a_request(self):
        import shutil
        from core_agent.mcp_transport import destination_ssl_context

        self.patch_trust(CA_PATH)
        original = shutil.rmtree

        def cleanup(path):
            original(path)
            raise OSError("cleanup-canary")

        with mock.patch("core_agent.mcp_transport.shutil.rmtree", side_effect=cleanup):
            with self.assertRaises(CoreError) as caught:
                destination_ssl_context(CLIENT_IDENTITY)
        self.assertEqual(caught.exception.code, "MCP_CONNECTION_FAILED")
        self.assertNotIn("cleanup-canary", str(caught.exception))

    def test_identity_temp_files_are_private_and_removed_even_on_failure(self):
        self.patch_trust(CA_PATH)
        destination_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        destination_context.load_cert_chain(certfile=SERVER_CERT_PATH, keyfile=SERVER_KEY_PATH)
        destination_context.verify_mode = ssl.CERT_REQUIRED
        destination_context.load_verify_locations(cafile=CA_PATH)
        server = _start_http_server([(None, {"tools": _tools(0, 1)})], ssl_context=destination_context)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

        seen = {}
        original = ssl.SSLContext.load_cert_chain

        def spy(self, certfile, keyfile=None, password=None):
            if keyfile is not None:
                seen["directory"] = os.path.dirname(keyfile)
                seen["key_mode"] = stat.S_IMODE(os.stat(keyfile).st_mode)
                seen["directory_mode"] = stat.S_IMODE(os.stat(seen["directory"]).st_mode)
            return original(self, certfile, keyfile, password)

        with mock.patch.object(ssl.SSLContext, "load_cert_chain", spy):
            catalog = StreamableHttpMcpConnector(timeout=5, cold_start_timeout=0).connect(
                _declaration(
                    f"https://127.0.0.1:{server.server_port}/mcp",
                    client_certificate=CLIENT_IDENTITY,
                )
            )
        self.assertEqual(sorted(catalog), ["tool_0"])
        self.assertEqual(seen["key_mode"], 0o600)
        self.assertEqual(seen["directory_mode"], 0o700)
        self.assertFalse(os.path.exists(seen["directory"]))

    def test_broken_identity_fails_and_still_removes_temporary_files(self):
        self.patch_trust(CA_PATH)
        server = _start_http_server([(None, {"tools": _tools(0, 1)})])
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

        seen = {}
        original = ssl.SSLContext.load_cert_chain

        def spy(self, certfile, keyfile=None, password=None):
            if keyfile is not None:
                seen["directory"] = os.path.dirname(keyfile)
            return original(self, certfile, keyfile, password)

        broken = {
            "certificate_pem": CLIENT_IDENTITY["certificate_pem"],
            # Not loadable key material: loading must fail, and the private
            # files must still disappear.
            "private_key_pem": _cert_pem(SERVER_CERT),
        }
        with mock.patch.object(ssl.SSLContext, "load_cert_chain", spy):
            with self.assertRaises(CoreError):
                StreamableHttpMcpConnector(timeout=5, cold_start_timeout=0).connect(
                    _declaration(
                        f"https://127.0.0.1:{server.server_port}/mcp",
                        client_certificate=broken,
                    )
                )
        self.assertIn("directory", seen)
        self.assertFalse(os.path.exists(seen["directory"]))


if __name__ == "__main__":
    unittest.main()
