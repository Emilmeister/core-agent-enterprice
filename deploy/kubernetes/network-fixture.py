"""Real receiver and DNS fixtures, run only in the disposable kind node netns."""

import ipaddress
import json
import socket
import socketserver
import struct
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ADDRESSES = {
    "public": ("93.184.216.34", "2606:4700:abcd::10"),
    "protected": ("93.184.216.35", "2606:4700:abcd::11"),
    "private": ("10.77.0.1", "fd77::1"),
}
COUNTS = {address: {"tcp": 0, "udp": 0}
          for pair in ADDRESSES.values() for address in pair}
DNS_QUERIES = {}
LOCK = threading.Lock()


class Receiver(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def respond(self, value, status=200):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        address, port = self.server.server_address[:2]
        path = urlsplit(self.path)
        if port == 18082:
            with LOCK:
                self.respond(COUNTS if path.path == "/counts" else {"ready": True})
            return
        with LOCK:
            COUNTS[address]["tcp"] += 1
        if path.path == "/redirect":
            family = parse_qs(path.query).get("v", ["4"])[0]
            target = ADDRESSES["private"][family == "6"]
            if ":" in target:
                target = f"[{target}]"
            self.send_response(302)
            self.send_header("Location", f"http://{target}:18080/")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.respond({"address": address, "transport": "tcp"})

    def do_POST(self):
        if self.server.server_address[1] != 18082 or self.path != "/reset":
            self.respond({"error": "not found"}, 404)
            return
        with LOCK:
            for values in COUNTS.values():
                values.update(tcp=0, udp=0)
            DNS_QUERIES.clear()
        self.respond({"reset": True})


class Datagram(socketserver.BaseRequestHandler):
    def handle(self):
        _data, stream = self.request
        address = self.server.server_address[0]
        with LOCK:
            COUNTS[address]["udp"] += 1
        stream.sendto(json.dumps({"address": address, "transport": "udp"}).encode(),
                      self.client_address)


def dns_response(query):
    if len(query) < 17 or struct.unpack("!H", query[4:6])[0] != 1:
        return b""
    offset, labels = 12, []
    while offset < len(query) and query[offset]:
        size = query[offset]
        if size > 63 or offset + size + 1 >= len(query):
            return b""
        labels.append(query[offset + 1:offset + size + 1].decode("ascii"))
        offset += size + 1
    offset += 1
    if offset + 4 > len(query):
        return b""
    qtype, qclass = struct.unpack("!HH", query[offset:offset + 4])
    name = ".".join(labels).lower()
    records = []
    if qtype in (1, 28) and qclass == 1:
        family = qtype == 28
        category = name.removesuffix(".test")
        if category == "rebind":
            with LOCK:
                previous = DNS_QUERIES.get(qtype, 0)
                DNS_QUERIES[qtype] = previous + 1
            category = "public" if previous == 0 else "private"
        if category in ADDRESSES:
            records = [ADDRESSES[category][family]]
        else:
            try:
                answers = socket.getaddrinfo(name, None,
                    socket.AF_INET6 if family else socket.AF_INET, socket.SOCK_STREAM)
                records = list(dict.fromkeys(item[4][0] for item in answers))[:8]
            except socket.gaierror:
                pass
    header = query[:2] + struct.pack("!5H", 0x8180, 1, len(records), 0, 0)
    response = header + query[12:offset + 4]
    for address in records:
        packed = ipaddress.ip_address(address).packed
        response += b"\xc0\x0c" + struct.pack("!HHIH", qtype, 1, 0, len(packed)) + packed
    return response


class DnsDatagram(socketserver.BaseRequestHandler):
    def handle(self):
        query, stream = self.request
        response = dns_response(query)
        if response:
            stream.sendto(response, self.client_address)


class DnsStream(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        prefix = self.rfile.read(2)
        if len(prefix) != 2:
            return
        size = struct.unpack("!H", prefix)[0]
        response = dns_response(self.rfile.read(size))
        self.wfile.write(struct.pack("!H", len(response)) + response)


def serve(address, port, handler, *, udp=False):
    base = socketserver.ThreadingUDPServer if udp else (
        ThreadingHTTPServer if handler is Receiver else socketserver.ThreadingTCPServer)

    class Server(base):
        address_family = socket.AF_INET6 if ":" in address else socket.AF_INET
        allow_reuse_address = True
        daemon_threads = True

    server = Server((address, port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--environment":
        node = json.loads(Path(sys.argv[2]).read_text())[0]
        network = node["NetworkSettings"]["Networks"]["kind"]
        node_address = ipaddress.ip_address(network["IPAddress"])
        denied = ["10.244.0.0/16", "10.96.0.0/16", "fd00:10:244::/56", "fd00:10:96::/112"]
        for field in ("IPAddress", "GlobalIPv6Address"):
            if network.get(field):
                address = ipaddress.ip_address(network[field])
                denied.append(f"{address}/{address.max_prefixlen}")
        denied.extend(["93.184.216.35/32", "2606:4700:abcd::11/128"])
        fixture = {
            "control_url": f"http://{node_address}:18082/counts",
            "https_url": "https://example.com/",
            "targets": [
                {"host": address, "allowed": category == "public", "tcp_port": 18080, "udp_port": 18081}
                for category, pair in ADDRESSES.items() for address in pair
            ],
        }
        print("SANDBOX_DNS_SERVERS=" + ADDRESSES["public"][0])
        print("SANDBOX_DENIED_CIDRS=" + ",".join(denied))
        print("SANDBOX_TEST_DEDICATED_POD=1")
        print("SANDBOX_TEST_NETWORK_FIXTURE=" + json.dumps(fixture, separators=(",", ":")))
        sys.exit(0)
    for pair in ADDRESSES.values():
        for address in pair:
            serve(address, 18080, Receiver)
            serve(address, 18081, Datagram, udp=True)
    serve(ADDRESSES["public"][0], 53, DnsDatagram, udp=True)
    serve(ADDRESSES["public"][0], 53, DnsStream)
    serve(sys.argv[1], 18082, Receiver)
    print("network fixture ready", flush=True)
    threading.Event().wait()
