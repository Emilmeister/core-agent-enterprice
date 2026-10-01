import asyncio
import io
import json
import threading
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from core_agent.errors import CoreError
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
        with patch("core_agent.remote_agents._OPENER.open", return_value=io.BytesIO(b"x" * 33)), patch("core_agent.remote_agents.MAX_RESPONSE_BYTES", 32):
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
