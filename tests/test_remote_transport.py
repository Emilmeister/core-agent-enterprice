import asyncio
import base64
import hashlib
import io
import json
import threading
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core_agent.errors import CoreError, ExecutionNotStarted
from core_agent.remote_agents import RemoteAgentCard, RemoteAgentConnection, connect_peer
from tests.test_auth import AuthAppTestCase


def task_payload(state="TASK_STATE_WORKING", **changes):
    return {"id": "remote-task", "contextId": "remote-context", "status": {"state": state}, **changes}


class RemoteTransportTests(unittest.TestCase):
    def connection(self, binding="JSONRPC"):
        return RemoteAgentConnection(RemoteAgentCard("peer", "Peer", "https://peer.test/a2a", True, (), binding),
                                     api_key="LEGACY-MUST-NOT-FORWARD")

    def reply(self, payload):
        def respond(request, timeout):
            body = json.loads(request.data) if request.data else {}
            response = {"jsonrpc": "2.0", "id": body["id"], "result": payload} if "jsonrpc" in body else payload
            return io.BytesIO(json.dumps(response).encode())
        return respond

    def test_both_bindings_send_once_nonblocking_with_only_private_peer_headers(self):
        for binding in ("JSONRPC", "HTTP+JSON"):
            with self.subTest(binding=binding), patch("core_agent.remote_agents._OPENER.open", side_effect=self.reply({"task": task_payload()})) as opened:
                connection = self.connection(binding)
                result = connection.send_task(task="Do one task", message_id="stable-message",
                                              headers={"X-Api-Key": "PRIVATE"}, timeout=4)
                request = opened.call_args.args[0]
                self.assertEqual(opened.call_count, 1)
                self.assertEqual(opened.call_args.kwargs["timeout"], 4)
                body = json.loads(request.data)
                if binding == "JSONRPC":
                    self.assertEqual(body["method"], "SendMessage")
                    self.assertEqual(request.full_url, "https://peer.test/a2a")
                    body = body["params"]
                else:
                    self.assertEqual(request.full_url, "https://peer.test/a2a/message:send")
                self.assertEqual(body, {"message": {"role": "ROLE_USER", "messageId": "stable-message", "parts": [{"text": "Do one task"}]},
                                        "configuration": {"returnImmediately": True}})
                headers = {key.lower(): value for key, value in request.header_items()}
                self.assertEqual(headers["x-api-key"], "PRIVATE")
                self.assertNotIn("authorization", headers)
                self.assertEqual(headers["a2a-version"], "1.0")
                self.assertEqual((result.task_id, result.context_id, result.final), ("remote-task", "remote-context", False))
                self.assertNotIn("PRIVATE", repr(connection))

    def test_get_cancel_direct_task_response_and_one_encoded_path_segment(self):
        identity = "remote/../task?#é"
        for binding in ("JSONRPC", "HTTP+JSON"):
            for method in ("get_task", "cancel_task"):
                with self.subTest(binding=binding, method=method), patch("core_agent.remote_agents._OPENER.open", side_effect=self.reply(task_payload("TASK_STATE_CANCELED", id=identity))) as opened:
                    event = getattr(self.connection(binding), method)(task_id=identity, headers={}, timeout=2)
                    self.assertTrue(event.final)
                    self.assertEqual(event.task_id, identity)
                    request = opened.call_args.args[0]
                    if binding == "JSONRPC":
                        body = json.loads(request.data)
                        self.assertEqual(body["method"], "GetTask" if method == "get_task" else "CancelTask")
                        self.assertEqual(body["params"], {"id": identity})
                    else:
                        self.assertEqual(request.full_url, "https://peer.test/a2a/tasks/remote%2F..%2Ftask%3F%23%C3%A9" + (":cancel" if method == "cancel_task" else ""))
                        self.assertEqual(request.method, "GET" if method == "get_task" else "POST")
                    self.assertEqual(opened.call_count, 1)

    def test_human_wait_is_nonterminal_and_file_parts_are_not_dropped_by_status_text(self):
        for state in ("TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"):
            payload = task_payload(state, status={"state": state, "message": {
                "messageId": "status", "role": "ROLE_AGENT", "parts": [{"text": "Waiting for owner"}]}},
                artifacts=[{"artifactId": "file", "parts": [{"raw": "YQ==", "filename": "a.txt", "mediaType": "text/plain"}]}])
            with self.subTest(state=state), patch("core_agent.remote_agents._OPENER.open", side_effect=self.reply({"task": payload})):
                result = self.connection().send_task(task="Task", message_id="m")
                self.assertFalse(result.final)
                self.assertEqual(result.state, state)
                self.assertTrue(result.has_files)
                self.assertEqual(result.parts[-1]["raw"], "YQ==")
                self.assertEqual(result.text, "Waiting for owner")

    def test_immediate_message_is_completed_without_invented_task_ids(self):
        payload = {"message": {"role": "ROLE_AGENT", "messageId": "answer", "parts": [{"text": "done"}]}}
        with patch("core_agent.remote_agents._OPENER.open", side_effect=self.reply(payload)):
            result = self.connection().send_task(task="Task", message_id="m")
        self.assertEqual((result.kind, result.text, result.final, result.task_id), ("message", "done", True, None))

    def test_nonfinite_state_and_invalid_optional_message_ids_are_safe_protocol_errors(self):
        for binding in ("JSONRPC", "HTTP+JSON"):
            for state in (b"Infinity", b"1e999"):
                with self.subTest(binding=binding, state=state):
                    payload = b'{"task":{"id":"task","contextId":"context","status":{"state":' + state + b'}}}'
                    if binding == "JSONRPC":
                        payload = b'{"jsonrpc":"2.0","id":"m","result":' + payload + b'}'
                    with patch("core_agent.remote_agents._OPENER.open", return_value=io.BytesIO(payload)) as opened:
                        with self.assertRaises(CoreError) as caught:
                            self.connection(binding).send_task(task="PRIVATE", message_id="m")
                        self.assertEqual(caught.exception.code, "REMOTE_AGENT_PROTOCOL_ERROR")
                        self.assertNotIn("PRIVATE", str(caught.exception))
                        self.assertEqual(opened.call_count, 1)
            for field in ("taskId", "contextId"):
                for identity in ("\x00", "PRIVATE\n", "\ud800"):
                    with self.subTest(binding=binding, field=field, identity=repr(identity)):
                        payload = {"message": {"role": "ROLE_AGENT", "messageId": "answer",
                                               "parts": [{"text": "done"}], field: identity}}
                        with patch("core_agent.remote_agents._OPENER.open", side_effect=self.reply(payload)) as opened:
                            with self.assertRaises(CoreError) as caught:
                                self.connection(binding).send_task(task="Task", message_id="m")
                            self.assertEqual(caught.exception.code, "REMOTE_AGENT_PROTOCOL_ERROR")
                            self.assertNotIn("PRIVATE", str(caught.exception))
                            self.assertEqual(opened.call_count, 1)

    def test_malformed_responses_ids_and_oversize_fail_safely(self):
        invalid = ({"task": task_payload(id="")}, {"task": task_payload("SECRET")},
                   {"task": task_payload(contextId="")}, {"task": task_payload(artifacts=[{"parts": [{"file": "SECRET"}]}])},
                   {"task": task_payload(), "message": {}}, {}, {"message": {"parts": [{"text": 17}]}})
        for payload in invalid:
            with self.subTest(payload=payload), patch("core_agent.remote_agents._OPENER.open", side_effect=self.reply(payload)):
                with self.assertRaises(CoreError) as caught:
                    self.connection().send_task(task="Task", message_id="m")
                self.assertEqual(caught.exception.code, "REMOTE_AGENT_PROTOCOL_ERROR")
                self.assertNotIn("SECRET", str(caught.exception))
        with patch("core_agent.remote_agents._OPENER.open", return_value=io.BytesIO(b"x" * 257)), patch("core_agent.remote_agents.MAX_RESPONSE_BYTES", 256):
            with self.assertRaises(CoreError) as caught:
                self.connection().send_task(task="Task", message_id="m")
            self.assertEqual(caught.exception.code, "REMOTE_AGENT_RESPONSE_TOO_LARGE")
        with patch("core_agent.remote_agents._OPENER.open", side_effect=self.reply(task_payload(id="other"))):
            with self.assertRaises(CoreError):
                self.connection().get_task(task_id="expected")

    def test_no_mutation_retry_or_secret_echo_after_network_or_rpc_failure(self):
        for method, arguments in (("send_task", {"task": "Task", "message_id": "m"}), ("cancel_task", {"task_id": "id"})):
            with self.subTest(method=method), patch("core_agent.remote_agents._OPENER.open", side_effect=URLError("PRIVATE")) as opened:
                with self.assertRaises(CoreError) as caught:
                    getattr(self.connection(), method)(headers={"Authorization": "PRIVATE"}, **arguments)
                self.assertEqual(opened.call_count, 1)
                self.assertNotIn("PRIVATE", str(caught.exception))
                self.assertFalse(caught.exception.retryable)
        with patch("core_agent.remote_agents._OPENER.open", return_value=io.BytesIO(b'{"jsonrpc":"2.0","id":"m","error":{"code":-32603,"message":"PRIVATE"}}')):
            with self.assertRaises(CoreError) as caught:
                self.connection().send_task(task="Task", message_id="m")
            self.assertNotIn("PRIVATE", str(caught.exception))

    def test_only_get_reports_temporary_http_status_as_retryable(self):
        for binding in ("JSONRPC", "HTTP+JSON"):
            for status in (408, 429, 500, 502, 503, 504, 401, 403, 404):
                for method, arguments in (("send_task", {"task": "Task", "message_id": "m"}),
                        ("get_task", {"task_id": "id"}), ("cancel_task", {"task_id": "id"})):
                    with self.subTest(binding=binding, status=status, method=method), patch(
                            "core_agent.remote_agents._OPENER.open",
                            side_effect=HTTPError("https://peer.test", status, "PRIVATE", {}, None)) as opened:
                        with self.assertRaises(CoreError) as caught:
                            getattr(self.connection(binding), method)(headers={"Authorization": "PRIVATE"}, **arguments)
                        self.assertEqual(caught.exception.retryable, method == "get_task" and status in {408, 429, 500, 502, 503, 504})
                        self.assertNotIn("PRIVATE", str(caught.exception))
                        self.assertEqual(opened.call_count, 1)

    def test_malformed_wire_payloads_are_bounded_generic_errors_without_fallback(self):
        for payload in (b"not json PRIVATE", b"\xff", b"[" * 10000 + b"]" * 10000,
                        b'{"jsonrpc":"2.0","id":"other","result":{}}',
                        b'{"jsonrpc":"1.0","id":"m","result":{}}'):
            with self.subTest(payload=payload[:40]), patch("core_agent.remote_agents._OPENER.open", return_value=io.BytesIO(payload)) as opened:
                with self.assertRaises(CoreError) as caught:
                    self.connection().send_task(task="Task", message_id="m")
                self.assertEqual(caught.exception.code, "REMOTE_AGENT_PROTOCOL_ERROR")
                self.assertNotIn("PRIVATE", str(caught.exception))
                self.assertEqual(opened.call_count, 1)

    def test_dot_remote_ids_do_not_navigate_outside_tasks_path(self):
        for identity, encoded in ((".", "%2E"), ("..", "%2E%2E")):
            with self.subTest(identity=identity), patch("core_agent.remote_agents._OPENER.open", side_effect=self.reply(task_payload(id=identity))) as opened:
                self.connection("HTTP+JSON").get_task(task_id=identity)
                self.assertEqual(opened.call_args.args[0].full_url, "https://peer.test/a2a/tasks/" + encoded)

    def test_invalid_headers_urls_ids_timeouts_are_rejected_before_io(self):
        with patch("core_agent.remote_agents._OPENER.open") as opened:
            for headers in ({"Host": "x"}, {"Content-Length": "1"}, {"Accept": "text/html"},
                            {"A2A-Version": "0.3"}, {"X-Key": "a\r\nb"}, {"X-Key": "x" * 16385},
                            {"X-Key": "☃"}, {"X-Key": ""}, {"X:Key": "secret"}):
                with self.subTest(headers=repr(headers)[:50]), self.assertRaises(CoreError):
                    self.connection().send_task(task="Task", message_id="m", headers=headers)
            for identity in ("", "\x00", "\ud800", None):
                with self.subTest(identity=repr(identity)), self.assertRaises(CoreError):
                    self.connection().get_task(task_id=identity)
            for timeout in (0, -1, True, float("inf"), float("nan")):
                with self.subTest(timeout=timeout), self.assertRaises(CoreError):
                    self.connection().send_task(task="Task", message_id="m", timeout=timeout)
            opened.assert_not_called()

    def test_explicit_ordered_files_include_empty_bytes_in_both_bindings(self):
        files = [{"name": "report.txt", "media_type": "text/plain", "raw": b"a"},
                 {"name": "empty.txt", "media_type": "text/plain", "raw": b""}]
        for binding in ("JSONRPC", "HTTP+JSON"):
            with self.subTest(binding=binding), patch("core_agent.remote_agents._OPENER.open", side_effect=self.reply({"task": task_payload()})) as opened:
                self.connection(binding).send_task(task="Do one task", message_id="m", files=files, attachment_limit_bytes=1)
                body = json.loads(opened.call_args.args[0].data)
                body = body["params"] if binding == "JSONRPC" else body
                self.assertEqual(body, {"message": {"role": "ROLE_USER", "messageId": "m", "parts": [
                    {"text": "Do one task"}, {"filename": "report.txt", "mediaType": "text/plain", "raw": "YQ=="},
                    {"filename": "empty.txt", "mediaType": "text/plain", "raw": ""}]},
                    "configuration": {"returnImmediately": True}})
                self.assertEqual(opened.call_count, 1)

    def test_entire_outbound_batch_and_limit_are_validated_before_io(self):
        valid = {"name": "a.txt", "media_type": "text/plain", "raw": b"a"}
        invalid = [{**valid, "raw": bytearray(b"PRIVATE")}, {**valid, "raw": "PRIVATE"},
                   {**valid, "name": "../PRIVATE"}, {**valid, "name": ""},
                   {**valid, "name": "PRIVATE\\file"}, {**valid, "media_type": "PRIVATE\n"},
                   {**valid, "media_type": 1}, {**valid, "private_ref": "PRIVATE"}, None]
        with patch("core_agent.remote_agents._OPENER.open") as opened:
            for file in invalid:
                with self.subTest(file=repr(file)), self.assertRaises(ExecutionNotStarted) as caught:
                    self.connection().send_task(task="Task", message_id="m", files=[valid, file], attachment_limit_bytes=10)
                self.assertEqual(caught.exception.code, "INVALID_FILE_INPUT")
                self.assertNotIn("PRIVATE", str(caught.exception))
            for limit in (None, True, False, 0, -1, 2147483648, "10", float("inf")):
                with self.subTest(limit=limit), self.assertRaises(ExecutionNotStarted):
                    self.connection().send_task(task="Task", message_id="m", files=[valid], attachment_limit_bytes=limit)
            with self.assertRaises(ExecutionNotStarted) as caught:
                self.connection().send_task(task="Task", message_id="m", files=[valid, valid], attachment_limit_bytes=1)
            self.assertEqual(caught.exception.code, "ATTACHMENTS_TOO_LARGE")
            self.assertEqual(caught.exception.data, {"allowed_bytes": 1, "actual_bytes": 2})
            for files in ({}, iter([valid]), "PRIVATE"):
                with self.subTest(files=repr(files)), self.assertRaises(ExecutionNotStarted):
                    self.connection().send_task(task="Task", message_id="m", files=files, attachment_limit_bytes=10)
            opened.assert_not_called()

    def test_local_send_validation_is_known_not_started_and_encoded_request_is_bounded(self):
        for arguments in ({"task": ""}, {"message_id": "\ud800"}, {"headers": {"Host": "PRIVATE"}}, {"timeout": 0}):
            with self.subTest(arguments=arguments), patch("core_agent.remote_agents._OPENER.open") as opened:
                with self.assertRaises(ExecutionNotStarted):
                    self.connection().send_task(**({"task": "Task", "message_id": "m"} | arguments))
                opened.assert_not_called()
        for binding in ("JSONRPC", "HTTP+JSON"):
            with self.subTest(binding=binding), patch("core_agent.remote_agents.MAX_FILE_RESPONSE_BYTES", 128), patch("core_agent.remote_agents._OPENER.open") as opened:
                with self.assertRaises(ExecutionNotStarted) as caught:
                    self.connection(binding).send_task(task="x" * 200, message_id="m", attachment_limit_bytes=2147483647)
                self.assertEqual(caught.exception.code, "REMOTE_AGENT_REQUEST_TOO_LARGE")
                opened.assert_not_called()

    def test_response_status_artifacts_and_empty_parts_survive_both_bindings_and_polling(self):
        payload = task_payload("TASK_STATE_INPUT_REQUIRED", status={"state": "TASK_STATE_INPUT_REQUIRED", "message": {
            "messageId": "status", "role": "ROLE_AGENT", "parts": [{"text": "Waiting"}, {"raw": "YQ==", "filename": "a.txt"}]}},
            artifacts=[{"artifactId": "empty", "parts": [{"raw": "", "filename": "empty.txt", "mediaType": "text/plain", "metadata": {"source": "peer"}}]}])
        for binding in ("JSONRPC", "HTTP+JSON"):
            for method, arguments in (("send_task", {"task": "Task", "message_id": "m"}),
                    ("get_task", {"task_id": "remote-task"}), ("cancel_task", {"task_id": "remote-task"})):
                reply = {"task": payload} if method == "send_task" else payload
                with self.subTest(binding=binding, method=method), patch("core_agent.remote_agents._OPENER.open", side_effect=self.reply(reply)):
                    event = getattr(self.connection(binding), method)(**arguments, attachment_limit_bytes=1)
                self.assertFalse(event.final)
                self.assertEqual(event.text, "Waiting")
                self.assertEqual(event.parts, tuple(payload["status"]["message"]["parts"] + payload["artifacts"][0]["parts"]))

    def test_response_file_batch_is_validated_before_protobuf_and_no_url_is_fetched(self):
        for binding in ("JSONRPC", "HTTP+JSON"):
            for part, expected in (({"raw": "YR=="}, "REMOTE_AGENT_PROTOCOL_ERROR"),
                    ({"raw": "YQ"}, "REMOTE_AGENT_PROTOCOL_ERROR"), ({"raw": "YQ==\n"}, "REMOTE_AGENT_PROTOCOL_ERROR"),
                    ({"raw": "-_=="}, "REMOTE_AGENT_PROTOCOL_ERROR"), ({"raw": 7}, "REMOTE_AGENT_PROTOCOL_ERROR"),
                    ({"raw": "Yg=="}, "ATTACHMENTS_TOO_LARGE"),
                    ({"url": "https://PRIVATE.test/file"}, "REMOTE_AGENT_PROTOCOL_ERROR")):
                payload = {"message": {"role": "ROLE_AGENT", "messageId": "answer", "parts": [{"raw": "YQ=="}, part]}}
                with self.subTest(binding=binding, part=part), patch("core_agent.remote_agents._OPENER.open", side_effect=self.reply(payload)) as opened, patch("core_agent.remote_agents.ParseDict") as parsed:
                    with self.assertRaises(CoreError) as caught:
                        self.connection(binding).send_task(task="Task", message_id="m", attachment_limit_bytes=1)
                    self.assertEqual(caught.exception.code, expected)
                    self.assertNotIn("PRIVATE", str(caught.exception))
                    self.assertEqual(opened.call_count, 1)
                    parsed.assert_not_called()

    def test_response_json_is_strict_before_protobuf(self):
        valid = '{"task":{"id":"task","contextId":"context","status":{"state":"TASK_STATE_COMPLETED"}}}'
        payloads = [valid.replace('"id":"task"', '"id":"PRIVATE","id":"task"').encode(),
                    valid.encode("utf-16"),
                    valid[:-2].encode() + b',"metadata":{"number":NaN}}}',
                    valid[:-2].encode() + b',"metadata":{"number":1e999}}}',
                    valid[:-2].encode() + b',"metadata":{"nested":' + b'[' * 101 + b'0' + b']' * 101 + b'}}}']
        for binding in ("JSONRPC", "HTTP+JSON"):
            for payload in payloads:
                if binding == "JSONRPC":
                    payload = b'{"jsonrpc":"2.0","id":"m","result":' + payload + b'}'
                with self.subTest(binding=binding, payload=payload[:50]), patch("core_agent.remote_agents._OPENER.open", return_value=io.BytesIO(payload)), patch("core_agent.remote_agents.ParseDict") as parsed:
                    with self.assertRaises(CoreError) as caught:
                        self.connection(binding).send_task(task="Task", message_id="m", attachment_limit_bytes=10)
                    self.assertEqual(caught.exception.code, "REMOTE_AGENT_PROTOCOL_ERROR")
                    self.assertNotIn("PRIVATE", str(caught.exception))
                    parsed.assert_not_called()

    def test_history_is_validated_but_company_limit_counts_only_current_batch(self):
        payload = {"task": task_payload("TASK_STATE_COMPLETED",
            metadata={"raw": "arbitrary metadata, not a FilePart"},
            history=[{"role": "ROLE_USER", "messageId": "old", "parts": [{"raw": "Yg==", "filename": "old.txt"}]}],
            artifacts=[{"artifactId": "current", "parts": [{"raw": "YQ==", "filename": "current.txt"}]}])}
        for binding in ("JSONRPC", "HTTP+JSON"):
            with self.subTest(binding=binding), patch("core_agent.remote_agents._OPENER.open", side_effect=self.reply(payload)):
                event = self.connection(binding).send_task(task="Task", message_id="m", attachment_limit_bytes=1)
                self.assertEqual(event.parts, ({"raw": "YQ==", "filename": "current.txt"},))
                self.assertTrue(event.final)
            for old_parts in ([{"raw": "YR=="}], ["PRIVATE"], [{"file": "PRIVATE"}],
                              [{"raw": "Yg==", "text": "PRIVATE"}], [{"raw": "Yg==", "filename": "\ud800"}]):
                payload["task"]["history"][0]["parts"] = old_parts
                with self.subTest(binding=binding, old_parts=repr(old_parts)), patch("core_agent.remote_agents._OPENER.open", side_effect=self.reply(payload)), patch("core_agent.remote_agents.ParseDict") as parsed:
                    with self.assertRaises(CoreError) as caught:
                        self.connection(binding).send_task(task="Task", message_id="m", attachment_limit_bytes=1)
                    self.assertEqual(caught.exception.code, "REMOTE_AGENT_PROTOCOL_ERROR")
                    self.assertNotIn("PRIVATE", str(caught.exception))
                    parsed.assert_not_called()
            payload["task"]["history"][0]["parts"] = [{"raw": "Yg==", "filename": "old.txt"}]

    def test_file_response_encoded_ceiling_is_independent_of_decoded_limit(self):
        payload = {"task": task_payload(metadata={"padding": "x" * 512})}
        for binding in ("JSONRPC", "HTTP+JSON"):
            with self.subTest(binding=binding), patch("core_agent.remote_agents.MAX_RESPONSE_BYTES", 256), patch("core_agent.remote_agents.MAX_FILE_RESPONSE_BYTES", 1024), patch("core_agent.remote_agents._OPENER.open", side_effect=self.reply(payload)):
                self.assertFalse(self.connection(binding).send_task(task="Task", message_id="m", attachment_limit_bytes=1).final)
                with self.assertRaises(CoreError) as caught:
                    self.connection(binding).send_task(task="Task", message_id="m")
                self.assertEqual(caught.exception.code, "REMOTE_AGENT_RESPONSE_TOO_LARGE")
            with self.subTest(binding=binding), patch("core_agent.remote_agents.MAX_FILE_RESPONSE_BYTES", 256), patch("core_agent.remote_agents._OPENER.open", side_effect=self.reply(payload)):
                with self.assertRaises(CoreError) as caught:
                    self.connection(binding).send_task(task="Task", message_id="m", attachment_limit_bytes=2147483647)
                self.assertEqual(caught.exception.code, "REMOTE_AGENT_RESPONSE_TOO_LARGE")

    def test_malformed_redirect_after_received_send_is_not_known_not_started(self):
        class Peer(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                self.server.received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(302)
                self.send_header("Location", "http://[invalid")
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Peer)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for binding in ("JSONRPC", "HTTP+JSON"):
            server.received = []
            connection = RemoteAgentConnection(RemoteAgentCard("peer", "Peer", f"http://127.0.0.1:{server.server_port}/", False, (), binding))
            with self.subTest(binding=binding), self.assertRaises(CoreError) as caught:
                connection.send_task(task="Mutating task", message_id="m", files=[
                    {"name": "selected.txt", "media_type": "text/plain", "raw": b"x"}], attachment_limit_bytes=1)
            self.assertEqual(len(server.received), 1)
            body = server.received[0]
            body = body["params"] if binding == "JSONRPC" else body
            self.assertEqual(body["message"]["parts"][-1]["raw"], "eA==")
            self.assertEqual(caught.exception.code, "REMOTE_AGENT_DENIED")
            self.assertNotIsInstance(caught.exception, ExecutionNotStarted)
            self.assertFalse(caught.exception.retryable)

    def test_explicit_file_send_get_and_cancel_over_real_loopback_http(self):
        class Peer(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def respond(self, payload):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def task(self, state="TASK_STATE_COMPLETED"):
                return task_payload(state, status={"state": state, "message": {
                    "role": "ROLE_AGENT", "messageId": "status", "parts": [{"text": "done"}]}},
                    artifacts=[{"artifactId": "result", "parts": self.server.parts}])

            def do_GET(self):
                self.respond(self.task())

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                rpc = "jsonrpc" in body
                params = body["params"] if rpc else body
                method = body["method"] if rpc else ("SendMessage" if self.path.endswith("/message:send") else "CancelTask")
                if method == "SendMessage":
                    self.server.parts = params["message"]["parts"][1:]
                    result = {"task": self.task()}
                else:
                    result = self.task("TASK_STATE_CANCELED" if method == "CancelTask" else "TASK_STATE_COMPLETED")
                self.respond({"jsonrpc": "2.0", "id": body["id"], "result": result} if rpc else result)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Peer)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        files = [{"name": "binary.bin", "media_type": "application/octet-stream", "raw": b"\x00\xff"},
                 {"name": "empty.txt", "media_type": "text/plain", "raw": b""}]
        for binding in ("JSONRPC", "HTTP+JSON"):
            connection = RemoteAgentConnection(RemoteAgentCard("peer", "Peer", f"http://127.0.0.1:{server.server_port}/", False, (), binding))
            with self.subTest(binding=binding):
                sent = connection.send_task(task="Read explicit files", message_id="m", files=files, attachment_limit_bytes=2)
                fetched = connection.get_task(task_id=sent.task_id, attachment_limit_bytes=2)
                cancelled = connection.cancel_task(task_id=sent.task_id, attachment_limit_bytes=2)
                self.assertEqual([sent.state, fetched.state, cancelled.state], ["TASK_STATE_COMPLETED", "TASK_STATE_COMPLETED", "TASK_STATE_CANCELED"])
                for event in (sent, fetched, cancelled):
                    self.assertTrue(event.final)
                    self.assertEqual(event.text, "done")
                    self.assertEqual([base64.b64decode(part["raw"]) for part in event.parts[1:]], [b"\x00\xff", b""])


class PeerDiscoveryTests(unittest.TestCase):
    peer = {"id": "peer-id", "revision": 1, "name": "configured", "url": "https://peer.test/a2a/external", "description": "Trusted peer", "enabled": True}

    def card(self, **changes):
        return {"name": "remote-name", "description": "remote-description", "supportedInterfaces": [{
            "url": self.peer["url"], "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0", **changes}],
            "capabilities": {"streaming": True}, "skills": []}

    def test_discovery_pins_owner_url_name_and_does_not_retain_credentials(self):
        with patch("core_agent.remote_agents._OPENER.open", return_value=io.BytesIO(json.dumps(self.card()).encode())) as opened:
            connection = connect_peer(self.peer, headers={"Authorization": "Bearer PRIVATE"}, timeout=2)
        self.assertEqual(connection.card.url, self.peer["url"])
        self.assertEqual(connection.card.name, "configured")
        self.assertEqual(connection.card.binding, "HTTP+JSON")
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, self.peer["url"] + "/.well-known/agent-card.json")
        self.assertEqual(request.get_header("Authorization"), "Bearer PRIVATE")
        self.assertNotIn("PRIVATE", repr(vars(connection)))

    def test_card_cannot_move_credentials_to_another_authority_or_path(self):
        for url in ("https://evil.test", "https://peer.test/a2a/owner", "https://peer.test:444/a2a/external",
                    "https://user@peer.test/a2a/external", self.peer["url"] + "?secret=x", self.peer["url"] + "\n"):
            with self.subTest(url=url), patch("core_agent.remote_agents._OPENER.open", return_value=io.BytesIO(json.dumps(self.card(url=url)).encode())) as opened:
                with self.assertRaises(CoreError):
                    connect_peer(self.peer, headers={"Authorization": "PRIVATE"})
                self.assertEqual(opened.call_count, 1)

    def test_only_discovery_retries_bounded_read_failures_and_never_redirects(self):
        with patch("core_agent.remote_agents._OPENER.open", side_effect=[URLError("failure"), io.BytesIO(json.dumps(self.card()).encode())]) as opened, patch("core_agent.remote_agents.time.sleep"):
            connect_peer(self.peer, max_retries=1)
            self.assertEqual(opened.call_count, 2)
        with patch("core_agent.remote_agents._OPENER.open", side_effect=URLError("PRIVATE")) as opened, patch("core_agent.remote_agents.time.sleep"):
            with self.assertRaises(CoreError):
                connect_peer(self.peer, max_retries=1)
            self.assertEqual(opened.call_count, 2)
        failure = HTTPError(self.peer["url"], 302, "PRIVATE", {"Location": "https://evil.test"}, io.BytesIO())
        with patch("core_agent.remote_agents._OPENER.open", side_effect=failure) as opened:
            with self.assertRaises(CoreError):
                connect_peer(self.peer, headers={"Authorization": "PRIVATE"})
            self.assertEqual(opened.call_count, 1)

    def test_invalid_configured_destinations_and_disabled_peer_never_send_credentials(self):
        for url in ("https:///missing", "https://peer.test:0", "https://peer.test:70000", "https://user:PRIVATE@peer.test",
                    "https://peer.test?PRIVATE", "https://peer.test#PRIVATE", "https://peer.test/\x80",
                    "https://peer.test/\ud800", "http://remote.test"):
            with self.subTest(url=repr(url)), patch("core_agent.remote_agents._OPENER.open") as opened:
                with self.assertRaises(CoreError) as caught:
                    connect_peer({**self.peer, "url": url}, headers={"Authorization": "PRIVATE"})
                self.assertNotIn("PRIVATE", str(caught.exception))
                opened.assert_not_called()
        with patch("core_agent.remote_agents._OPENER.open") as opened:
            with self.assertRaises(CoreError):
                connect_peer({**self.peer, "enabled": False})
            opened.assert_not_called()

    def test_discovery_rejects_unknown_protocol_and_malformed_body_without_retry(self):
        payloads = [json.dumps(self.card(protocolVersion="0.3")).encode(),
                    json.dumps(self.card(protocolBinding="GRPC")).encode(),
                    b"PRIVATE", b"[" * 10000 + b"]" * 10000]
        for payload in payloads:
            with self.subTest(payload=payload[:50]), patch("core_agent.remote_agents._OPENER.open", return_value=io.BytesIO(payload)) as opened:
                with self.assertRaises(CoreError) as caught:
                    connect_peer(self.peer)
                self.assertEqual(caught.exception.code, "REMOTE_AGENT_CARD_INVALID")
                self.assertNotIn("PRIVATE", str(caught.exception))
                self.assertEqual(opened.call_count, 1)

    def test_registered_url_params_are_part_of_the_pinned_destination(self):
        registered = {**self.peer, "url": self.peer["url"] + ";registered"}
        for params in ("different", "registered"):
            with self.subTest(params=params), patch("core_agent.remote_agents._OPENER.open",
                return_value=io.BytesIO(json.dumps(self.card(url=self.peer["url"] + ";" + params)).encode())) as opened:
                if params == "registered":
                    self.assertEqual(connect_peer(registered).card.url, registered["url"])
                else:
                    with self.assertRaises(CoreError) as caught:
                        connect_peer(registered)
                    self.assertEqual(caught.exception.code, "REMOTE_AGENT_CARD_INVALID")
                self.assertEqual(opened.call_count, 1)

    def test_only_discovery_retries_transient_server_errors(self):
        for status in (500, 502, 503, 504):
            with self.subTest(status=status):
                failure = HTTPError(self.peer["url"], status, "PRIVATE", {}, io.BytesIO())
                with patch("core_agent.remote_agents._OPENER.open", side_effect=[failure, io.BytesIO(json.dumps(self.card()).encode())]) as opened, patch("core_agent.remote_agents.time.sleep"):
                    self.assertEqual(connect_peer(self.peer, max_retries=1).card.binding, "HTTP+JSON")
                    self.assertEqual(opened.call_count, 2)
                connection = RemoteAgentConnection(RemoteAgentCard("peer", "Peer", self.peer["url"], False, ()))
                for method, arguments in (("send_task", {"task": "Task", "message_id": "m"}), ("cancel_task", {"task_id": "id"})):
                    failure = HTTPError(self.peer["url"], status, "PRIVATE", {}, io.BytesIO())
                    with self.subTest(method=method), patch("core_agent.remote_agents._OPENER.open", side_effect=failure) as opened:
                        with self.assertRaises(CoreError) as caught:
                            getattr(connection, method)(**arguments)
                        self.assertFalse(caught.exception.retryable)
                        self.assertNotIn("PRIVATE", str(caught.exception))
                        self.assertEqual(opened.call_count, 1)


class AuthenticatedPeerTransportTests(AuthAppTestCase):
    async def test_both_bindings_admit_only_explicit_files_and_empty_bytes_with_real_sdk(self):
        loop = asyncio.get_running_loop()
        replies = []

        async def request_remote(request):
            response = await self.http.request(request.method, request.full_url,
                content=request.data, headers=dict(request.header_items()))
            self.assertEqual(response.status_code, 200)
            replies.append(response.json())
            return io.BytesIO(response.content)

        def open_remote(request, timeout):
            return asyncio.run_coroutine_threadsafe(request_remote(request), loop).result(timeout=timeout)

        files = [{"name": "report.txt", "media_type": "text/plain", "raw": b"a"},
                 {"name": "report.txt", "media_type": "text/plain", "raw": b""}]
        with patch("core_agent.remote_agents._OPENER.open", side_effect=open_remote):
            for binding in ("JSONRPC", "HTTP+JSON"):
                connection = RemoteAgentConnection(RemoteAgentCard("peer", "Peer", "https://agent.example.test/a2a/external/", False, (), binding))
                event = await asyncio.to_thread(connection.send_task, task="Read files", message_id="files-" + binding,
                    files=files, attachment_limit_bytes=1, headers={"Authorization": "Bearer external-a"})
                payload = replies[-1]["result"] if binding == "JSONRPC" else replies[-1]
                self.assertEqual(event.task_id, payload["task"]["id"])
                receipt = payload["task"]["metadata"]["file_receipt"]
                self.assertEqual([entry["size_bytes"] for entry in receipt["entries"]], [1, 0])
                self.assertEqual(len({entry["actual_name"] for entry in receipt["entries"]}), 2)
                self.assertEqual([entry["sha256"] for entry in receipt["entries"]], [hashlib.sha256(file["raw"]).hexdigest() for file in files])

    async def test_real_http_json_card_nonblocking_send_get_cancel(self):
        loop = asyncio.get_running_loop()
        seen = []

        async def request_remote(request):
            seen.append(request)
            response = await self.http.request(request.method, request.full_url,
                                               content=request.data, headers=dict(request.header_items()))
            if response.status_code >= 400:
                raise HTTPError(request.full_url, response.status_code, "remote failure", {}, io.BytesIO(response.content))
            return io.BytesIO(response.content)

        def open_remote(request, timeout):
            return asyncio.run_coroutine_threadsafe(request_remote(request), loop).result(timeout=timeout)

        started, release = threading.Event(), threading.Event()
        generate = self.model.generate

        def blocked_model(**kwargs):
            started.set()
            release.wait(10)
            return generate(**kwargs)

        self.model.generate = blocked_model
        self.addCleanup(release.set)
        peer = {"id": "peer", "revision": 1, "name": "app", "description": "App", "enabled": True,
                "url": "https://agent.example.test/a2a/external"}
        headers = {"Authorization": "Bearer external-a"}
        with patch("core_agent.remote_agents._OPENER.open", side_effect=open_remote):
            connection = await asyncio.to_thread(connect_peer, peer, headers=headers, timeout=5)
            self.assertEqual(connection.card.binding, "HTTP+JSON")
            sent = await asyncio.to_thread(connection.send_task, task="Test", message_id="remote-send", headers=headers)
            self.assertFalse(sent.final)
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            fetched = await asyncio.to_thread(connection.get_task, task_id=sent.task_id, headers=headers)
            self.assertEqual(fetched.task_id, sent.task_id)
            self.assertFalse(fetched.final)
            agent = self.app.state.core_agent
            original = agent.signal_task_cancel

            def release_on_cancel(task_id):
                original(task_id)
                release.set()

            with patch.object(agent, "signal_task_cancel", side_effect=release_on_cancel):
                cancelled = await asyncio.to_thread(connection.cancel_task, task_id=sent.task_id, headers=headers)
            self.assertEqual(cancelled.state, "TASK_STATE_CANCELED")
            self.assertTrue(cancelled.final)
        self.assertEqual(len(seen), 4)
