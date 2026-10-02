import asyncio
import io
import json
import logging
import os
import re
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import httpx

from core_agent.a2a import ATTACHMENTS_ONLY_PROMPT
from tests.app_support import create_app
from core_agent.errors import CoreError
from core_agent.model import CompatibleHttpModel
from core_agent.remote_agents import (
    RemoteAgentCard,
    RemoteAgentConnection,
    build_forwarded_headers,
)
from core_agent.streaming import StreamBuffer, integrate_stream_chunk


BASE_ENVIRONMENT = {
    # patch.dict(clear=True) drops the ambient no_proxy, and urllib would then
    # send the loopback stub calls through the host's system proxy.
    "CORE_AGENT_ENVIRONMENT": "development",
    "NO_PROXY": "*",
    "no_proxy": "*",
    "SESSION_STORAGE_TYPE": "in-memory",
    "LOCAL_APPROVAL_DB_PATH": ":memory:",
    "RUNTIME_MAX_LLM_CALLS": "6",
    "CORE_AGENT_MAX_TOOL_CALLS": "6",
}


class ModelHandler(BaseHTTPRequestHandler):
    """OpenAI-compatible stub that answers with SSE when stream=true."""

    reasoning = "Deciding what to do."
    answer = "Streamed answer."
    tool_call = None
    requests = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(body)
        seen_tool_result = any(
            message.get("role") == "tool" for message in body["messages"]
        )
        wants_tool = type(self).tool_call is not None and not seen_tool_result
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self._frame({"reasoning_content": type(self).reasoning})
        if wants_tool:
            name, arguments = type(self).tool_call
            wire = next(
                item["function"]["name"]
                for item in body["tools"]
                if item["function"]["name"].endswith(name.replace(".", "_"))
            )
            self._frame(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-1",
                            "function": {
                                "name": wire,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ]
                }
            )
        else:
            for word in type(self).answer.split(" "):
                self._frame({"content": word + " "})
        self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _frame(self, delta):
        payload = json.dumps({"choices": [{"delta": delta}]}).encode()
        self.wfile.write(b"data: " + payload + b"\n\n")
        self.wfile.flush()


class StreamedReasoningTests(unittest.TestCase):
    def test_a_reasoning_delta_keeps_the_space_it_arrived_with(self):
        """Trimming each token glues the words of the assembled reasoning."""
        model = CompatibleHttpModel(
            api_format="openai",
            model="m",
            base_url="https://model.test/v1",
            api_key="k",
        )
        words = ["Мне", " нужно", " сначала", " найти", " информацию."]
        frames = [
            b"data: " + json.dumps({"choices": [{"delta": delta}]}).encode() + b"\n\n"
            for delta in [{"reasoning_content": word} for word in words]
        ]
        frames.append(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')
        frames.append(b"data: [DONE]\n\n")
        response = model._stream_openai(iter(frames), lambda *args: None)
        self.assertEqual(
            response["choices"][0]["message"]["reasoning_content"],
            "Мне нужно сначала найти информацию.",
        )

    def test_the_assembled_reasoning_is_still_trimmed_as_a_whole(self):
        model = CompatibleHttpModel(
            api_format="openai",
            model="m",
            base_url="https://model.test/v1",
            api_key="k",
        )
        frames = [
            b"data: "
            + json.dumps(
                {"choices": [{"delta": {"reasoning_content": "  Думаю.  "}}]}
            ).encode()
            + b"\n\n",
            b"data: "
            + json.dumps({"choices": [{"delta": {"content": "Готово."}}]}).encode()
            + b"\n\n",
            b"data: [DONE]\n\n",
        ]
        response = model._stream_openai(iter(frames), lambda *args: None)
        self.assertEqual(model._parse_openai(response, {}).reasoning, "Думаю.")


class ModelTransportFailureTests(unittest.TestCase):
    def _model(self, port, **options):
        return CompatibleHttpModel(
            api_format="openai",
            model="m",
            base_url=f"http://127.0.0.1:{port}/v1",
            api_key="k",
            timeout=0.3,
            **options,
        )

    def _serve(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_port

    def test_a_body_that_stops_arriving_is_a_provider_failure(self):
        """The socket timeout fires on the read, long after the connect succeeded."""

        class StallingHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "4096")
                self.end_headers()
                self.wfile.write(b"{")
                self.wfile.flush()
                time.sleep(2)

        port = self._serve(StallingHandler)
        with self.assertRaises(CoreError) as caught:
            self._model(port).generate(context="c", tools=(), instructions="i")
        self.assertEqual(caught.exception.code, "MODEL_UNAVAILABLE")
        self.assertTrue(caught.exception.retryable)

    def test_a_stream_that_goes_quiet_between_chunks_is_a_provider_failure(self):
        """A bare TimeoutError here kills the A2A Task instead of failing it."""

        class QuietStreamHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                frame = json.dumps({"choices": [{"delta": {"content": "partial"}}]})
                self.wfile.write(f"data: {frame}\n\n".encode())
                self.wfile.flush()
                time.sleep(2)

        port = self._serve(QuietStreamHandler)
        with self.assertRaises(CoreError) as caught:
            self._model(port, stream=True).generate(
                context="c", tools=(), instructions="i"
            )
        self.assertEqual(caught.exception.code, "MODEL_UNAVAILABLE")
        self.assertTrue(caught.exception.retryable)


class StreamBufferTests(unittest.TestCase):
    def test_snapshot_replaces_while_every_delta_is_appended(self):
        self.assertEqual(integrate_stream_chunk("", "abc"), "abc")
        self.assertEqual(integrate_stream_chunk("abc", "def"), "abcdef")
        self.assertEqual(integrate_stream_chunk("abc", "abcdef"), "abcdef")
        self.assertEqual(integrate_stream_chunk("abc", "abc"), "abc")
        # A delta that repeats earlier text must not be mistaken for a snapshot.
        self.assertEqual(integrate_stream_chunk("abcdef", "def"), "abcdefdef")
        self.assertEqual(integrate_stream_chunk("ab", "b"), "abb")

    def test_buffer_emits_only_after_the_threshold_and_flushes_the_remainder(self):
        buffer = StreamBuffer(5)
        self.assertIsNone(buffer.update("abc", ""))
        self.assertEqual(buffer.update("abcdef", ""), ("abcdef", ""))
        self.assertIsNone(buffer.update("abcdef", ""))
        self.assertIsNone(buffer.update("abcdefg", ""))
        self.assertEqual(buffer.flush(), ("abcdefg", ""))
        self.assertIsNone(buffer.flush())




class ForwardedHeaderTests(unittest.TestCase):
    def test_only_the_allowlist_is_forwarded_and_the_api_key_overrides_identity(self):
        incoming = {
            "authorization": "Bearer platform-identity",
            "x-project-id": "p1",
            "cookie": "session=secret",
        }
        self.assertEqual(
            build_forwarded_headers(incoming),
            {"Authorization": "Bearer platform-identity", "X-PROJECT-ID": "p1"},
        )
        self.assertEqual(
            build_forwarded_headers(incoming, api_key="k")["Authorization"],
            "Api-Key k",
        )


class RemoteAgentHandler(BaseHTTPRequestHandler):
    """Minimal A2A 1.0 JSON-RPC peer used to exercise send_message."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self._json(
            {
                "name": "weather-agent",
                "description": "weather",
                "supportedInterfaces": [
                    {
                        "url": f"http://127.0.0.1:{self.server.server_port}/",
                        "protocolBinding": "JSONRPC",
                        "protocolVersion": "1.0",
                    }
                ],
                "capabilities": {"streaming": True},
                "skills": [],
            }
        )

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen = (body, dict(self.headers))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for state, text in (
            ("TASK_STATE_WORKING", "looking it up"),
            ("TASK_STATE_COMPLETED", "24 degrees"),
        ):
            frame = {
                "statusUpdate": {
                    "taskId": "t1",
                    "contextId": "c1",
                    "status": {
                        "state": state,
                        "message": {
                            "role": "ROLE_AGENT",
                            "messageId": str(uuid.uuid4()),
                            "parts": [{"text": text}],
                        },
                    },
                }
            }
            payload = json.dumps({"jsonrpc": "2.0", "id": "1", "result": frame})
            self.wfile.write(f"data: {payload}\n\n".encode())
            self.wfile.flush()

    def _json(self, value):
        payload = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class RemoteAgentConnectionTests(unittest.TestCase):
    def test_streaming_call_yields_progress_then_a_final_answer(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), RemoteAgentHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/"
        connection = RemoteAgentConnection(
            RemoteAgentCard("weather-agent", "weather", url, True, ())
        )
        events = list(
            connection.stream_message(
                task="What is the weather?",
                message_id="m1",
                context_id="c1",
                forwarded_headers={"X-PROJECT-ID": "p1"},
            )
        )
        self.assertEqual(
            [event.text for event in events], ["looking it up", "24 degrees"]
        )
        self.assertEqual([event.final for event in events], [False, True])
        body, headers = RemoteAgentHandler.seen
        self.assertEqual(body["method"], "SendStreamingMessage")
        self.assertEqual(body["params"]["message"]["role"], "ROLE_USER")
        self.assertEqual(
            body["params"]["message"]["parts"], [{"text": "What is the weather?"}]
        )
        self.assertEqual(body["params"]["message"]["contextId"], "c1")
        lowered = {key.lower(): value for key, value in headers.items()}
        self.assertEqual(lowered["x-project-id"], "p1")
        self.assertEqual(lowered["a2a-version"], "1.0")

    def test_peer_without_a_jsonrpc_1_0_interface_is_refused(self):
        from core_agent.remote_agents import _interface_url

        legacy = [{"url": "https://peer", "protocolBinding": "JSONRPC", "protocolVersion": "0.3"}]
        with self.assertRaises(CoreError) as caught:
            _interface_url(legacy)
        self.assertEqual(caught.exception.code, "REMOTE_AGENT_CARD_INVALID")


class StreamingA2ATests(unittest.IsolatedAsyncioTestCase):
    """The A2A 1.0 JSON-RPC binding must carry ADK-typed progress frames."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        ModelHandler.reasoning = "Deciding what to do."
        ModelHandler.answer = "Streamed answer."
        ModelHandler.tool_call = None
        ModelHandler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _app(self, **environment):
        patcher = patch.dict(
            os.environ,
            {
                **BASE_ENVIRONMENT,
                "CORE_AGENT_ENVIRONMENT": "development",
                "LOCAL_WORKSPACE_ROOT": str(Path(self.temp.name) / "workspaces"),
                "A2A_STREAMING_BUFFER_SIZE": "4",
                **environment,
            },
            clear=True,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        model = CompatibleHttpModel(
            api_format="openai",
            model="stream-model",
            base_url=f"http://127.0.0.1:{self.server.server_port}/v1",
            stream=True,
        )
        app = create_app(model=model, base_url="http://agent.test")
        self.addCleanup(app.state.close)
        return app

    async def _frames(self, app, prompt):
        payload = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "SendStreamingMessage",
            "params": {
                "message": {
                    "role": "ROLE_USER",
                    "messageId": str(uuid.uuid4()),
                    "parts": [{"text": prompt}],
                }
            },
        }
        frames = []
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://agent.test",
            timeout=30,
        ) as http:
            async with http.stream(
                "POST",
                "/",
                json=payload,
                headers={
                    "A2A-Version": "1.0",
                    "Accept": "text/event-stream",
                    "Authorization": "Bearer caller-token",
                    "Cookie": "session=secret",
                },
            ) as response:
                self.assertEqual(response.status_code, 200)
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        frame = json.loads(line[5:])
                        self.assertNotIn("error", frame, frame)
                        frames.append(frame["result"])
        return frames

    @staticmethod
    def _status(frame):
        return (frame.get("statusUpdate") or frame.get("task") or {}).get("status") or {}

    @classmethod
    def _parts(cls, frame):
        return (cls._status(frame).get("message") or {}).get("parts", [])

    @classmethod
    def _terminal(cls, frames):
        return [
            frame
            for frame in frames
            if "statusUpdate" in frame
            and cls._status(frame).get("state")
            in {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED"}
        ]

    async def test_guarded_stream_keeps_reasoning_private_and_publishes_final_text(self):
        frames = await self._frames(self._app(), "hello")
        thoughts = [
            part["text"]
            for frame in frames
            for part in self._parts(frame)
            if (part.get("metadata") or {}).get("adk_thought")
        ]
        self.assertEqual(thoughts, [])

        partials = [
            frame
            for frame in frames
            if (
                (self._status(frame).get("message") or {}).get("metadata") or {}
            ).get("partial")
        ]
        self.assertGreater(len(partials), 1)
        snapshots = [
            part["text"]
            for frame in partials
            for part in self._parts(frame)
            if not (part.get("metadata") or {}).get("adk_thought")
        ]
        self.assertEqual(snapshots, sorted(snapshots, key=len))
        self.assertTrue(snapshots[-1].startswith(snapshots[0]))

        terminal = self._terminal(frames)
        self.assertEqual(len(terminal), 1)
        self.assertIs(terminal[0], frames[-1])
        self.assertEqual(self._status(terminal[0])["state"], "TASK_STATE_COMPLETED")
        self.assertNotIn(
            True,
            [
                (part.get("metadata") or {}).get("adk_thought")
                for part in self._parts(terminal[0])
            ],
        )

    async def test_guarded_tool_calls_and_results_remain_private(self):
        ModelHandler.tool_call = ("core_task_list", {})
        frames = await self._frames(self._app(), "list my files")
        typed = [
            (part["metadata"]["adk_type"], part["data"])
            for frame in frames
            for part in self._parts(frame)
            if (part.get("metadata") or {}).get("adk_type")
        ]
        self.assertEqual(typed, [])
        result = next(json.loads(message["content"]) for message in ModelHandler.requests[-1]["messages"]
                      if message.get("role") == "tool")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["tool_name"], "core_task_list")


    async def test_guarded_remote_result_reaches_model_without_raw_progress_relay(self):
        peer = ThreadingHTTPServer(("127.0.0.1", 0), RemoteAgentHandler)
        threading.Thread(target=peer.serve_forever, daemon=True).start()
        self.addCleanup(peer.server_close)
        self.addCleanup(peer.shutdown)
        ModelHandler.tool_call = (
            "core_agent_send_message",
            {"agent_name": "weather-agent", "task": "What is the weather?"},
        )
        frames = await self._frames(
            self._app(REMOTE_AGENTS=f"http://127.0.0.1:{peer.server_port}"),
            "ask the weather agent",
        )
        relayed = [
            part["text"]
            for frame in frames
            for part in self._parts(frame)
            if "text" in part
            and not (part.get("metadata") or {}).get("adk_thought")
        ]
        self.assertNotIn("looking it up", relayed)
        response = next(json.loads(message["content"]) for message in ModelHandler.requests[-1]["messages"]
                        if message.get("role") == "tool")
        self.assertEqual(response["output"]["result"], "24 degrees")
        self.assertTrue(response["output"]["success"])
        _body, headers = RemoteAgentHandler.seen
        lowered = {key.lower(): value for key, value in headers.items()}
        self.assertEqual(lowered["authorization"], "Bearer caller-token")
        self.assertNotIn("cookie", lowered)

    async def test_send_message_is_absent_when_no_remote_agent_is_configured(self):
        app = self._app()
        advertised = {
            skill.id for skill in app.state.a2a_request_handler._agent_card.skills
        }
        self.assertNotIn("core_agent_send_message", advertised)
        self.assertNotIn(
            "core_agent_send_message",
            app.state.core_agent.platform_config.allowed_builtin_tools,
        )

    async def test_streaming_can_be_disabled_without_losing_the_result(self):
        frames = await self._frames(self._app(A2A_STREAMING_ENABLED="false"), "hello")
        self.assertEqual(
            [],
            [
                frame
                for frame in frames
                if self._parts(frame) and frame not in self._terminal(frames)
            ],
        )
        terminal = self._terminal(frames)
        self.assertEqual(self._status(terminal[0])["state"], "TASK_STATE_COMPLETED")

    async def test_unknown_jsonrpc_envelope_fields_are_dropped_not_interpreted(self):
        """A client hedging with a duplicated field must not be rejected outright."""
        from core_agent.a2a_sdk import _reported_extra_fields
        from core_agent.model import ModelResponse, ScriptedModel

        model = ScriptedModel([ModelResponse(message="ok")] * 4)
        model.model = "envelope-model"
        with patch.dict(os.environ, BASE_ENVIRONMENT, clear=True):
            app = create_app(model=model)

        def envelope(identifier, extra, context_id=None):
            message = {
                "role": "ROLE_USER",
                "messageId": str(uuid.uuid4()),
                "parts": [{"text": "hi"}],
            }
            if context_id:
                message["contextId"] = context_id
            return {
                "jsonrpc": "2.0",
                "id": identifier,
                "method": "SendMessage",
                "params": {"message": message},
                **extra,
            }

        _reported_extra_fields.clear()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://agent.test"
            ) as client:
                headers = {"A2A-Version": "1.0"}

                async def send(body):
                    return (await client.post("/", json=body, headers=headers)).json()

                # Duplicated outside and inside: accepted, session from the message.
                with self.assertLogs("core_agent.runtime", "WARNING") as logs:
                    result = await send(
                        envelope("1", {"contextId": "ctx-a"}, context_id="ctx-a")
                    )
                self.assertNotIn("error", result)
                self.assertEqual(result["result"]["task"]["contextId"], "ctx-a")
                self.assertIn("unknown JSON-RPC fields", logs.output[0])
                self.assertNotIn("contextId", logs.output[0])

                # Only outside: accepted, but never promoted to a session id.
                result = await send(envelope("2", {"contextId": "ctx-b"}))
                self.assertNotEqual(result["result"]["task"]["contextId"], "ctx-b")

                # Several unknown members are dropped together.
                result = await send(
                    envelope("3", {"foo": 1, "bar": 2}, context_id="ctx-c")
                )
                self.assertEqual(result["result"]["task"]["contextId"], "ctx-c")

                # A clean envelope keeps working and logs nothing new.
                before = set(_reported_extra_fields)
                result = await send(envelope("4", {}, context_id="ctx-d"))
                self.assertEqual(result["result"]["task"]["contextId"], "ctx-d")
                self.assertEqual(set(_reported_extra_fields), before)
        finally:
            _reported_extra_fields.clear()
            app.state.close()

    async def test_advertised_url_follows_agent_url_then_request_headers(self):
        """A card advertising localhost is discoverable but uncallable."""
        from core_agent.model import ModelResponse, ScriptedModel

        def build(**environment):
            model = ScriptedModel([ModelResponse(message="ok")])
            model.model = "card-model"
            with patch.dict(
                os.environ, {**BASE_ENVIRONMENT, **environment}, clear=True
            ):
                return create_app(model=model)

        async def urls(app, headers):
            found = {}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://internal"
            ) as client:
                for path in (
                    "/.well-known/agent-card.json",
                    "/.well-known/agent.json",
                ):
                    card = (await client.get(path, headers=headers)).json()
                    found[path] = {item["url"] for item in card["supportedInterfaces"]}
            return found

        # Hosting platforms publish the public address under either spelling;
        # a transposed name is otherwise indistinguishable from an unset one.
        app = build(URL_AGENT="https://platform.example")
        try:
            found = await urls(app, {"Host": "attacker.example"})
            self.assertEqual(list(found.values()), [{"https://platform.example"}] * 2)
        finally:
            app.state.close()

        app = build(
            AGENT_URL="https://documented.example",
            URL_AGENT="https://platform.example",
        )
        try:
            found = await urls(app, {"Host": "attacker.example"})
            # The documented name wins when a deployment sets both.
            self.assertEqual(list(found.values()), [{"https://documented.example"}] * 2)
        finally:
            app.state.close()

        # An explicit AGENT_URL wins over any header a caller can forge.
        app = build(AGENT_URL="https://configured.example")
        try:
            found = await urls(app, {"Host": "attacker.example"})
            self.assertEqual(list(found.values()), [{"https://configured.example"}] * 2)
        finally:
            app.state.close()

        app = build(PORT="10000")
        try:
            # Without AGENT_URL the proxy headers decide, then plain Host.
            found = await urls(
                app,
                {
                    "Host": "internal:10000",
                    "X-Forwarded-Host": "agent.cloud.example",
                    "X-Forwarded-Proto": "https",
                },
            )
            self.assertEqual(
                list(found.values()), [{"https://agent.cloud.example"}] * 2
            )
            found = await urls(app, {"Host": "plain.example"})
            self.assertEqual(list(found.values()), [{"http://plain.example"}] * 2)
            # A TLS-terminating proxy that already passes the public name in Host
            # sends no X-Forwarded-Host; the scheme must still be honoured, or the
            # card advertises http:// for an https-only deployment and every peer
            # that follows it is refused as an insecure agent url.
            found = await urls(
                app,
                {"Host": "agent.cloud.example", "X-Forwarded-Proto": "https"},
            )
            self.assertEqual(
                list(found.values()), [{"https://agent.cloud.example"}] * 2
            )
            # An unusable header must not fail discovery; the static value stays.
            for hostile in ("user@evil.example", "has space", ""):
                with self.subTest(host=hostile):
                    found = await urls(app, {"Host": hostile})
                    self.assertEqual(
                        list(found.values()), [{"http://localhost:10000"}] * 2
                    )
        finally:
            app.state.close()

    async def test_every_advertised_interface_accepts_its_own_version(self):
        """The card must not name a (binding, version) pair the endpoint rejects."""
        from core_agent.model import ModelResponse, ScriptedModel

        model = ScriptedModel([ModelResponse(message="ok")])
        model.model = "card-model"
        with patch.dict(os.environ, BASE_ENVIRONMENT, clear=True):
            app = create_app(model=model, base_url="http://agent.test")
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://agent.test"
            ) as client:
                card = (await client.get("/.well-known/agent-card.json")).json()
                advertised = {
                    (item["protocolBinding"], item["protocolVersion"])
                    for item in card["supportedInterfaces"]
                }
                self.assertEqual(advertised, {("HTTP+JSON", "1.0"), ("JSONRPC", "1.0")})
                # No v0.3 card fields beside the 1.0 interfaces.
                for field in ("url", "preferredTransport", "protocolVersion"):
                    self.assertNotIn(field, card)
                legacy = await client.post(
                    "/",
                    json={
                        "jsonrpc": "2.0",
                        "id": "1",
                        "method": "tasks/get",
                        "params": {"id": "missing"},
                    },
                    headers={"A2A-Version": "0.3"},
                )
                # The 0.3 method names are gone: JSON-RPC "method not found".
                self.assertEqual(legacy.json()["error"]["code"], -32601)
                for binding, version in sorted(advertised):
                    with self.subTest(binding=binding, version=version):
                        headers = {"A2A-Version": version}
                        if binding == "JSONRPC":
                            response = await client.post(
                                "/",
                                json={
                                    "jsonrpc": "2.0",
                                    "id": "1",
                                    "method": "GetTask",
                                    "params": {"id": "missing"},
                                },
                                headers=headers,
                            )
                            detail = response.json()["error"]["message"]
                        else:
                            response = await client.get(
                                "/tasks/missing", headers=headers
                            )
                            detail = response.json()["error"]["message"]
                        # Reaching "not found" proves the version was accepted;
                        # a rejected version answers with a version error instead.
                        self.assertNotIn("is not supported by this handler", detail)
                        self.assertIn("not found", detail.lower())
        finally:
            app.state.close()

    async def test_agent_card_is_served_on_both_well_known_paths(self):
        """Registries predating the rename probe /.well-known/agent.json."""
        from core_agent.model import ModelResponse, ScriptedModel

        model = ScriptedModel([ModelResponse(message="ok")])
        model.model = "card-model"
        with patch.dict(os.environ, BASE_ENVIRONMENT, clear=True):
            app = create_app(model=model, base_url="http://agent.test")
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://agent.test"
            ) as client:
                canonical = await client.get("/.well-known/agent-card.json")
                # The platform appends its own identifier to the query string.
                legacy = await client.get(
                    "/.well-known/agent.json", params={"agentId": "0273f25b"}
                )
            self.assertEqual((canonical.status_code, legacy.status_code), (200, 200))
            self.assertEqual(canonical.json(), legacy.json())
        finally:
            app.state.close()


class ConfigurationTransferTests(unittest.TestCase):
    def test_sampling_parameters_reach_the_provider_body(self):
        model = CompatibleHttpModel(
            api_format="openai",
            model="m",
            base_url="https://provider.test/v1",
            temperature=0.7,
            top_p=0.9,
            top_k=40,
            frequency_penalty=0.1,
            presence_penalty=0.2,
        )
        self.assertEqual(
            {key: model.invocation_parameters[key] for key in ("temperature", "top_k")},
            {"temperature": 0.7, "top_k": 40},
        )

    def test_out_of_range_and_unsupported_sampling_fails_closed(self):
        for name, value in (("temperature", 5.0), ("top_p", 2.0), ("top_k", 0)):
            with self.subTest(name=name):
                with self.assertRaises(CoreError):
                    CompatibleHttpModel(
                        api_format="openai",
                        model="m",
                        base_url="https://provider.test/v1",
                        **{name: value},
                    )
        with self.assertRaises(CoreError):
            CompatibleHttpModel(
                api_format="anthropic",
                model="m",
                base_url="https://provider.test/v1",
                frequency_penalty=0.1,
            )

    def test_prompt_cache_marks_the_instruction_prefix_for_anthropic_only(self):
        cached = CompatibleHttpModel(
            api_format="anthropic",
            model="m",
            base_url="https://provider.test/v1",
            cache_ttl="1h",
            cache_min_tokens=1,
        )
        blocks = cached._system_blocks("instructions")
        self.assertEqual(blocks[0]["cache_control"], {"type": "ephemeral", "ttl": "1h"})
        uncached = CompatibleHttpModel(
            api_format="anthropic", model="m", base_url="https://provider.test/v1"
        )
        self.assertEqual(uncached._system_blocks("instructions"), "instructions")

    def test_agent_card_identity_and_capabilities_come_from_the_environment(self):
        from core_agent.model import ModelResponse, ScriptedModel

        model = ScriptedModel([ModelResponse(message="ok")])
        model.model = "card-model"
        with patch.dict(
            os.environ,
            {
                **BASE_ENVIRONMENT,
                "AGENT_NAME": "transfer-agent",
                "AGENT_DESCRIPTION": "Transferred agent",
                "AGENT_VERSION": "2.4.0",
                "A2A_CAPABILITIES": "tool_calling,multi_turn",
            },
            clear=True,
        ):
            app = create_app(model=model)
        try:
            card = app.state.a2a_request_handler._agent_card
            self.assertEqual(card.name, "transfer-agent")
            self.assertEqual(card.description, "Transferred agent")
            self.assertEqual(card.version, "2.4.0")
            self.assertFalse(card.capabilities.streaming)
        finally:
            app.state.close()

    def test_unknown_storage_and_capability_values_fail_closed(self):
        from core_agent.model import ModelResponse, ScriptedModel

        for environment in (
            {"TASK_STORAGE_TYPE": "redis"},
            {"A2A_CAPABILITIES": "telepathy"},
            {"LLM_MODEL": "m", "THINKING_LEVEL": "ludicrous"},
        ):
            with self.subTest(environment=environment):
                model = ScriptedModel([ModelResponse(message="ok")])
                model.model = "config-model"
                with patch.dict(
                    os.environ, {**BASE_ENVIRONMENT, **environment}, clear=True
                ):
                    with self.assertRaises(CoreError) as caught:
                        create_app(model=None if "LLM_MODEL" in environment else model)
                self.assertEqual(caught.exception.code, "CONFIG_INVALID")

    def test_startup_separates_configured_remote_agents_from_connected(self):
        """An empty connected list hides whether the variable ever arrived."""
        from core_agent.model import ModelResponse, ScriptedModel

        def startup(**environment):
            model = ScriptedModel([ModelResponse(message="ok")])
            model.model = "remote-model"
            with patch.dict(
                os.environ,
                {
                    **BASE_ENVIRONMENT,
                    "REMOTE_AGENTS_MAX_RETRIES": "0",
                    "REMOTE_AGENTS_TIMEOUT": "2",
                    **environment,
                },
                clear=True,
            ):
                with self.assertLogs("core_agent.runtime", "INFO") as logs:
                    app = create_app(model=model)
            app.state.close()
            line = next(
                item for item in logs.output if '"startup.configuration"' in item
            )
            return json.loads(line.split(":", 2)[2]), line

        record, _ = startup()
        self.assertEqual(record["remote_agents_configured"], [])

        # Configured but unreachable must not look like "never configured".
        record, _ = startup(REMOTE_AGENTS="http://127.0.0.1:1")
        self.assertEqual(record["remote_agents_configured"], ["http://127.0.0.1:1"])
        self.assertEqual(record["remote_agents_connected"], [])
        self.assertEqual(
            record["remote_agent_failures"][0]["error_code"],
            "REMOTE_AGENT_UNAVAILABLE",
        )

        # A configured URL may carry credentials; the log must not keep them.
        record, line = startup(REMOTE_AGENTS="https://user:p4ssw0rd@peer.test")
        self.assertEqual(record["remote_agents_configured"], ["https://peer.test"])
        self.assertNotIn("p4ssw0rd", line)

    def test_startup_prints_short_plain_remote_agents_line(self):
        """Collectors drop the long record; the short line must carry the fact alone."""
        from core_agent.model import ModelResponse, ScriptedModel

        def startup(**environment):
            model = ScriptedModel([ModelResponse(message="ok")])
            model.model = "remote-model"
            with patch.dict(
                os.environ,
                {
                    **BASE_ENVIRONMENT,
                    "REMOTE_AGENTS_MAX_RETRIES": "0",
                    "REMOTE_AGENTS_TIMEOUT": "2",
                    "AGENT_NAME": "peer-secret",
                    **environment,
                },
                clear=True,
            ):
                with self.assertLogs("core_agent.runtime", "INFO") as logs:
                    app = create_app(model=model)
            app.state.close()
            return [
                item
                for item in logs.output
                if "REMOTE_AGENTS" in item
                and "startup.configuration" not in item
                # The environment inventory names the variable too; the subject
                # here is the short plain warning, not the inventory line.
                and "startup.env" not in item
            ]

        unset = startup()
        self.assertEqual(len(unset), 1)
        self.assertTrue(unset[0].startswith("WARNING:"))
        self.assertIn("core_agent_send_message", unset[0])
        self.assertIn("not set", unset[0])
        # The state must precede the variable names: a collector that truncates the
        # line still delivers the fact the operator acts on.
        self.assertLess(unset[0].index("not set"), unset[0].index("variables present"))
        # Names of agent-related variables, never their values.
        self.assertIn("AGENT_NAME", unset[0])
        self.assertNotIn("peer-secret", unset[0])

        # A blank value means the platform failed to expand it, not that the
        # operator omitted it; the two need opposite fixes.
        blank = startup(REMOTE_AGENTS="")
        self.assertEqual(len(blank), 1)
        self.assertIn("set but empty", blank[0])

        # A configured URL may carry credentials; the short line must not keep them.
        configured = startup(REMOTE_AGENTS="https://user:p4ssw0rd@peer.test")
        self.assertEqual(len(configured), 1)
        self.assertIn("https://peer.test", configured[0])
        self.assertNotIn("p4ssw0rd", configured[0])

    def test_startup_inventories_the_environment_without_any_value(self):
        """Names and state only: the environment holds every credential there is."""
        from core_agent.model import ModelResponse, ScriptedModel

        model = ScriptedModel([ModelResponse(message="ok")])
        model.model = "inventory-model"
        with patch.dict(
            os.environ,
            {
                **BASE_ENVIRONMENT,
                "LLM_MODEL": "inventory-model",
                "LLM_API_BASE": "https://model.test/v1",
                "LLM_API_KEY": "sk-super-secret-value",
                "REMOTE_AGENTS": "",
                "SOME_PLATFORM_TOKEN": "platform-secret",
            },
            clear=True,
        ):
            with self.assertLogs("core_agent.runtime", "INFO") as captured:
                # The real path, so the model variables are genuinely consulted.
                app = create_app()
        app.state.close()
        del model
        lines = [item for item in captured.output if "startup.env" in item]
        self.assertTrue(lines)
        encoded = "\n".join(lines)

        # Every line is numbered, so a collector dropping some of them shows.
        total = int(re.search(r"startup\.env \d+/(\d+)", lines[0]).group(1))
        self.assertEqual(len(lines), total)
        self.assertEqual(
            [int(re.search(r"startup\.env (\d+)/", item).group(1)) for item in lines],
            list(range(1, total + 1)),
        )
        # Short enough that no single line carries the whole inventory.
        for item in lines:
            self.assertLess(len(item), 400)

        self.assertIn("LLM_API_KEY=set", encoded)
        # Present-but-blank is the case a plain getenv default hides.
        self.assertIn("REMOTE_AGENTS=empty", encoded)
        self.assertIn("MEMORY_STORAGE_TYPE=missing", encoded)
        # A variable the platform sent that nothing reads: this is how a
        # transposed name becomes visible instead of looking like an absent one.
        self.assertIn("not-consulted", encoded)
        self.assertIn("SOME_PLATFORM_TOKEN=set", encoded)

        for secret in ("sk-super-secret-value", "platform-secret"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, encoded)

    def test_inventory_calls_every_variable_the_startup_reads_consulted(self):
        """A name read outside app.py is still read by this startup."""
        import ast

        from core_agent.app import _EXTERNAL_VARIABLES

        root = Path(__file__).resolve().parent.parent / "core_agent"
        # Only the modules app.py hands configuration to. Variables read later,
        # while a tool runs, are not part of what this startup consulted.
        for name in ("database.py", "observability.py"):
            for node in ast.walk(ast.parse((root / name).read_text())):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                target = getattr(node.func, "attr", None)
                if target not in ("getenv", "get"):
                    continue
                if not isinstance(node.args[0], ast.Constant):
                    continue
                variable = node.args[0].value
                if not isinstance(variable, str) or not variable.isupper():
                    continue
                with self.subTest(module=name, variable=variable):
                    self.assertIn(variable, _EXTERNAL_VARIABLES)

    def test_a_tool_name_written_with_dots_still_names_the_same_tool(self):
        """Canonical names lost their dots; deployments were written before that."""
        from core_agent.model import ModelResponse, ScriptedModel

        model = ScriptedModel([ModelResponse(message="ok")])
        model.model = "legacy-model"
        with patch.dict(
            os.environ,
            {
                **BASE_ENVIRONMENT,
                "LLM_MODEL": "legacy-model",
                "CORE_AGENT_ALLOWED_BUILTIN_TOOLS": "core.memory.search,core_python_exec",
            },
            clear=True,
        ):
            app = create_app(model=model)
        try:
            allow = app.state.core_agent.agent_config.to_dict()["tools"]["builtins"][
                "allow"
            ]
        finally:
            app.state.close()
        self.assertEqual(sorted(allow), ["core_memory_search", "core_python_exec"])

        with patch.dict(
            os.environ,
            {
                **BASE_ENVIRONMENT,
                "LLM_MODEL": "legacy-model",
                "CORE_AGENT_ALLOWED_BUILTIN_TOOLS": "core_no_such_tool",
            },
            clear=True,
        ):
            with self.assertRaises(CoreError) as caught:
                create_app(model=ScriptedModel([]))
        self.assertEqual(caught.exception.code, "CONFIG_INVALID")
        # Naming the value is what turns a rejected startup into a fixable one.
        self.assertIn("core_no_such_tool", str(caught.exception))

    def test_an_argument_the_model_left_null_is_an_argument_it_omitted(self):
        openai = CompatibleHttpModel(api_format="openai", model="m")
        response = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {
                                    "name": "core_terminal_exec",
                                    "arguments": '{"argv": ["ls"], "cwd": null}',
                                },
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
        parsed = openai._parse_openai(
            response, {"core_terminal_exec": "core_terminal_exec"}
        )
        self.assertEqual(parsed.tool_requests[0].arguments, {"argv": ["ls"]})

    def test_startup_reports_otlp_endpoints_and_never_the_key(self):
        """A 403 on one signal is only actionable with the resolved address."""
        from core_agent.model import ModelResponse, ScriptedModel

        def startup(**environment):
            model = ScriptedModel([ModelResponse(message="ok")])
            model.model = "otlp-model"
            with patch.dict(
                os.environ, {**BASE_ENVIRONMENT, **environment}, clear=True
            ):
                with self.assertLogs("core_agent.runtime", "INFO") as logs:
                    app = create_app(model=model)
            app.state.close()
            line = next(
                item for item in logs.output if '"startup.configuration"' in item
            )
            return json.loads(line.split(":", 2)[2]), line

        record, line = startup(
            OTEL_ENDPOINT="https://collector.test", OTEL_API_KEY="sk-never-logged"
        )
        self.assertEqual(
            record["telemetry"]["traces"], "https://collector.test/v1/traces"
        )
        # A base URL is not a claim that the backend takes every signal; the
        # managed collector that takes traces answers 403 on logs.
        self.assertIsNone(record["telemetry"]["logs"])
        self.assertIsNone(record["telemetry"]["metrics"])
        self.assertTrue(record["telemetry"]["credentials_configured"])
        # The value must never appear, in any form.
        self.assertNotIn("sk-never-logged", line)

        # An explicit per-signal endpoint is the operator saying it is accepted.
        explicit, _ = startup(
            OTEL_ENDPOINT="https://collector.test",
            OTEL_EXPORTER_OTLP_LOGS_ENDPOINT="https://logs.test/v1/logs",
        )
        self.assertEqual(explicit["telemetry"]["logs"], "https://logs.test/v1/logs")

        record, _ = startup(OTEL_ENDPOINT="https://collector.test")
        self.assertFalse(record["telemetry"]["credentials_configured"])

    def test_telemetry_can_be_switched_off_and_named_by_the_platform(self):
        from core_agent.observability import Telemetry

        with patch.dict(
            os.environ,
            {"OTEL_ENDPOINT": "https://collector.test", "ENABLE_OTEL": "false"},
            clear=True,
        ):
            self.assertIsNone(Telemetry.otlp_from_env())
        with patch.dict(
            os.environ,
            {
                "OTEL_ENDPOINT": "https://collector.test",
                "OTEL_PROJECT_NAME": "platform-project",
                "OTEL_SERVICE_NAME": "legacy-name",
            },
            clear=True,
        ):
            telemetry = Telemetry.otlp_from_env()
        resource = telemetry.exporter.trace_provider.resource
        # The platform publishes the project name; the standard name is a synonym.
        self.assertEqual(resource.attributes["service.name"], "platform-project")

    def test_embedding_base_falls_back_to_the_model_gateway(self):
        from core_agent.model import ModelResponse, ScriptedModel

        def build(**environment):
            model = ScriptedModel([ModelResponse(message="ok")])
            model.model = "embed-model"
            with patch.dict(
                os.environ,
                {
                    **BASE_ENVIRONMENT,
                    "LLM_MODEL": "m",
                    "LLM_API_BASE": "https://gateway.test/v1",
                    "LLM_API_KEY": "sk-model",
                    **environment,
                },
                clear=True,
            ):
                return create_app(model=model)

        app = build(EMBEDDING_MODEL="bge-m3", EMBEDDING_API_KEY="sk-embed")
        try:
            provider = app.state.core_agent.memory_registry.embedding_provider
            self.assertEqual(provider.endpoint, "https://gateway.test/v1/embeddings")
        finally:
            app.state.close()

        # The key is never inherited: rights on embeddings may differ.
        app = build(EMBEDDING_MODEL="bge-m3")
        try:
            self.assertIsNone(app.state.core_agent.memory_registry.embedding_provider)
        finally:
            app.state.close()


    def test_mcp_server_answering_with_an_event_stream_connects(self):
        """A Streamable HTTP server may answer JSON or SSE; both must work."""
        from core_agent.mcp import StreamableHttpMcpConnector

        tools = [
            {
                "name": "get_forecast",
                "description": "f",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ]

        class SseMcpHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                method = request.get("method")
                if method == "initialize":
                    result = {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "weather", "version": "1"},
                    }
                elif method == "tools/list":
                    result = {"tools": tools}
                else:
                    result = {}
                body = json.dumps(
                    {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(f"event: message\ndata: {body}\n\n".encode())
                self.wfile.flush()

        server = ThreadingHTTPServer(("127.0.0.1", 0), SseMcpHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        catalog = StreamableHttpMcpConnector(timeout=5).connect(
            {
                "name": "mcp",
                "transport": {
                    "type": "streamable_http",
                    "url": f"http://127.0.0.1:{server.server_port}/mcp",
                },
            }
        )
        self.assertEqual(sorted(catalog), ["get_forecast"])

    def test_progress_notifications_do_not_become_the_tool_result(self):
        """A slow tool narrates first; the answer is the frame carrying our id."""
        from core_agent.mcp import StreamableHttpMcpConnector

        class NarratingMcpHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                method = request.get("method")
                if method == "initialize":
                    result = {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "search", "version": "1"},
                    }
                elif method == "tools/list":
                    result = {"tools": [{"name": "search_web", "inputSchema": {}}]}
                else:
                    result = {"content": [{"type": "text", "text": "leaderboard"}]}
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if method == "tools/call":
                    for frame in (
                        {
                            "jsonrpc": "2.0",
                            "method": "notifications/progress",
                            "params": {"progress": 1},
                        },
                        # A server request of its own, with an id that is not ours.
                        {"jsonrpc": "2.0", "id": "server-1", "method": "ping"},
                    ):
                        self.wfile.write(f"data: {json.dumps(frame)}\n\n".encode())
                    self.wfile.flush()
                body = json.dumps(
                    {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
                )
                self.wfile.write(f"event: message\ndata: {body}\n\n".encode())
                self.wfile.flush()

        server = ThreadingHTTPServer(("127.0.0.1", 0), NarratingMcpHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connector = StreamableHttpMcpConnector(timeout=5)
        declaration = {
            "name": "mcp",
            "transport": {
                "type": "streamable_http",
                "url": f"http://127.0.0.1:{server.server_port}/mcp",
            },
        }
        connector.connect(declaration)
        result = connector.call("mcp", "search_web", {"query": "terminal bench"})
        # Before the id match this returned {} — reported to the model as a
        # successful search that found nothing.
        self.assertEqual(result, {"content": [{"type": "text", "text": "leaderboard"}]})

    def test_mcp_accepts_every_published_protocol_revision(self):
        """A server picks the revision; refusing a working one hides it for nothing."""
        from core_agent.mcp import MCP_PROTOCOL_VERSIONS, StreamableHttpMcpConnector

        def serve(version):
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_POST(self):
                    request = json.loads(
                        self.rfile.read(int(self.headers["Content-Length"]))
                    )
                    if request.get("method") == "initialize":
                        result = {
                            "protocolVersion": version,
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "w", "version": "1"},
                        }
                    elif request.get("method") == "tools/list":
                        result = {"tools": [{"name": "get_forecast"}]}
                    else:
                        result = {}
                    body = json.dumps(
                        {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body.encode())

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            return {
                "name": "mcp",
                "transport": {
                    "type": "streamable_http",
                    "url": f"http://127.0.0.1:{server.server_port}/mcp",
                },
            }

        for version in MCP_PROTOCOL_VERSIONS:
            with self.subTest(version=version):
                catalog = StreamableHttpMcpConnector(timeout=5).connect(serve(version))
                self.assertEqual(sorted(catalog), ["get_forecast"])

        with self.assertRaises(CoreError) as caught:
            StreamableHttpMcpConnector(timeout=5).connect(serve("2099-01-01"))
        self.assertEqual(caught.exception.code, "MCP_PROTOCOL_ERROR")
        # The refusal must name both sides, not just say "unsupported".
        self.assertIn("2099-01-01", str(caught.exception))
        self.assertIn(MCP_PROTOCOL_VERSIONS[0], str(caught.exception))

    def test_mcp_session_id_is_returned_on_every_later_request(self):
        """A strict Streamable HTTP server rejects requests without its session."""
        from core_agent.mcp import StreamableHttpMcpConnector

        session = "sess-abc-123"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                method = request.get("method")
                if method != "initialize" and (
                    self.headers.get("Mcp-Session-Id") != session
                ):
                    self.send_response(400)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if method == "initialize":
                    result = {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "w", "version": "1"},
                    }
                elif method == "tools/list":
                    result = {"tools": [{"name": "get_forecast"}]}
                else:
                    result = {}
                body = json.dumps(
                    {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                if method == "initialize":
                    self.send_header("Mcp-Session-Id", session)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        catalog = StreamableHttpMcpConnector(timeout=5).connect(
            {
                "name": "mcp",
                "transport": {
                    "type": "streamable_http",
                    "url": f"http://127.0.0.1:{server.server_port}/mcp",
                },
            }
        )
        self.assertEqual(sorted(catalog), ["get_forecast"])

    def test_mcp_session_id_is_isolated_between_runs(self):
        """A second run must not replace the first run's server session."""
        from core_agent.mcp import StreamableHttpMcpConnector

        calls = []
        sessions = iter(("session-one", "session-two"))

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                method = request.get("method")
                session = self.headers.get("Mcp-Session-Id")
                calls.append((method, session))
                if method == "initialize":
                    assigned = next(sessions)
                    result = {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "scoped", "version": "1"},
                    }
                elif method == "tools/list":
                    assigned = None
                    result = {"tools": [{"name": "search", "inputSchema": {}}]}
                else:
                    assigned = None
                    result = {"session": session}
                body = json.dumps(
                    {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                if assigned:
                    self.send_header("Mcp-Session-Id", assigned)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connector = StreamableHttpMcpConnector(timeout=5)
        first_connector = connector.for_run()
        second_connector = connector.for_run()
        declaration = {
            "name": "mcp",
            "transport": {
                "type": "streamable_http",
                "url": f"http://127.0.0.1:{server.server_port}/mcp",
            },
        }

        first_connector.connect(declaration)
        second_connector.connect(declaration)
        first = first_connector.call("mcp", "search", {})
        second = second_connector.call("mcp", "search", {})

        self.assertEqual(first["session"], "session-one")
        self.assertEqual(second["session"], "session-two")
        self.assertEqual(
            calls[-2:],
            [
                ("tools/call", "session-one"),
                ("tools/call", "session-two"),
            ],
        )

    def test_mcp_malformed_catalog_is_a_permanent_protocol_error(self):
        from core_agent.mcp import StreamableHttpMcpConnector

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                result = (
                    {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "bad", "version": "1"},
                    }
                    if request.get("method") == "initialize"
                    else {"tools": [{"inputSchema": {}}]}
                )
                body = json.dumps(
                    {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connector = StreamableHttpMcpConnector(timeout=5, cold_start_timeout=5)
        with self.assertRaises(CoreError) as caught:
            connector.connect(
                {
                    "name": "mcp",
                    "transport": {
                        "type": "streamable_http",
                        "url": f"http://127.0.0.1:{server.server_port}/mcp",
                    },
                }
            )

        self.assertEqual(caught.exception.code, "MCP_PROTOCOL_ERROR")
        self.assertFalse(caught.exception.retryable)

    def test_mcp_connection_failure_names_the_actual_cause(self):
        """One generic code cannot separate a wrong URL from TLS or a dead host."""
        from core_agent.mcp import StreamableHttpMcpConnector

        connector = StreamableHttpMcpConnector(timeout=5, cold_start_timeout=0)

        def connect(url):
            with self.assertRaises(CoreError) as caught:
                connector.connect(
                    {
                        "name": "mcp",
                        "transport": {"type": "streamable_http", "url": url},
                    }
                )
            self.assertEqual(caught.exception.code, "MCP_CONNECTION_FAILED")
            return str(caught.exception)

        self.assertIn("scheme is not allowed", connect("http://mcp.example.test/mcp"))
        self.assertIn("Connection refused", connect("http://127.0.0.1:1/mcp"))

    def test_mcp_cold_start_retries_discovery_and_drops_the_stale_session(self):
        """A replacement instance cannot know the session owned by the scaled-down one."""
        from core_agent.mcp import StreamableHttpMcpConnector

        calls = []

        class WakingMcpHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                method = request.get("method")
                calls.append((method, self.headers.get("Mcp-Session-Id")))
                if len(calls) == 1:
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if method == "initialize":
                    result = {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "waking", "version": "1"},
                    }
                elif method == "tools/list":
                    result = {"tools": [{"name": "search", "inputSchema": {}}]}
                else:
                    result = {}
                body = json.dumps(
                    {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                if method == "initialize":
                    self.send_header("Mcp-Session-Id", "fresh-session")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), WakingMcpHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connector = StreamableHttpMcpConnector(timeout=5, cold_start_timeout=5)
        connector._sessions["mcp"] = "stale-session"
        declaration = {
            "name": "mcp",
            "transport": {
                "type": "streamable_http",
                "url": f"http://127.0.0.1:{server.server_port}/mcp",
            },
        }

        with patch("core_agent.mcp.time.sleep", return_value=None):
            catalog = connector.connect(declaration)

        self.assertEqual(catalog, {"search": {}})
        self.assertEqual([method for method, _session in calls[:2]], ["initialize"] * 2)
        self.assertEqual([session for _method, session in calls[:2]], [None, None])
        self.assertEqual(
            calls[2:],
            [
                ("notifications/initialized", "fresh-session"),
                ("tools/list", "fresh-session"),
            ],
        )

    def test_mcp_cold_start_does_not_retry_permanent_http_failure(self):
        from core_agent.mcp import StreamableHttpMcpConnector

        attempts = []
        real_client = httpx.AsyncClient

        def denied(request):
            return httpx.Response(401, request=request)

        def client(**kwargs):
            attempts.append(kwargs["timeout"].connect)
            return real_client(transport=httpx.MockTransport(denied), **kwargs)

        connector = StreamableHttpMcpConnector(timeout=30, cold_start_timeout=300)
        with (
            patch("core_agent.mcp.httpx.AsyncClient", side_effect=client),
            patch("core_agent.mcp.time.sleep") as sleep,
            self.assertRaises(CoreError) as caught,
        ):
            connector.connect(
                {
                    "name": "mcp",
                    "transport": {"type": "streamable_http", "url": "https://mcp.test"},
                }
            )

        self.assertEqual(caught.exception.code, "MCP_CONNECTION_FAILED")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(attempts, [30])
        sleep.assert_not_called()

    def test_mcp_explicit_zero_deadline_runs_one_attempt_without_retry(self):
        from core_agent.mcp import StreamableHttpMcpConnector

        attempts = []
        real_client = httpx.AsyncClient

        def unavailable(request):
            attempts.append(request)
            return httpx.Response(503, request=request)

        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(unavailable), **kwargs)

        connector = StreamableHttpMcpConnector(timeout=30, cold_start_timeout=300)
        with (
            patch("core_agent.mcp.httpx.AsyncClient", side_effect=client),
            patch("core_agent.mcp.time.sleep") as sleep,
            self.assertRaises(CoreError) as caught,
        ):
            connector.connect(
                {
                    "name": "mcp",
                    "transport": {
                        "type": "streamable_http",
                        "url": "https://mcp.test/mcp",
                    },
                },
                deadline=0.0,
            )

        self.assertEqual(caught.exception.code, "MCP_CONNECTION_FAILED")
        self.assertEqual(len(attempts), 1)
        sleep.assert_not_called()

    def test_mcp_cold_start_does_not_retry_invalid_json(self):
        from core_agent.mcp import StreamableHttpMcpConnector

        attempts = []
        real_client = httpx.AsyncClient

        def invalid(request):
            attempts.append("initialize")
            return httpx.Response(
                200,
                content=b"not-json",
                headers={"Content-Type": "application/json"},
                request=request,
            )

        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(invalid), **kwargs)

        connector = StreamableHttpMcpConnector(timeout=30, cold_start_timeout=300)
        with (
            patch("core_agent.mcp.httpx.AsyncClient", side_effect=client),
            patch("core_agent.mcp.time.sleep") as sleep,
            self.assertRaises(CoreError) as caught,
        ):
            connector.connect(
                {
                    "name": "mcp",
                    "transport": {"type": "streamable_http", "url": "https://mcp.test"},
                }
            )

        self.assertEqual(caught.exception.code, "MCP_PROTOCOL_ERROR")
        self.assertEqual(attempts, ["initialize"])
        sleep.assert_not_called()

    def test_mcp_cold_start_retries_a_remote_protocol_disconnect(self):
        from core_agent.mcp import StreamableHttpMcpConnector

        attempts = []
        real_client = httpx.AsyncClient

        def handler(request):
            method = json.loads(request.content)["method"]
            attempts.append(method)
            if len(attempts) == 1:
                raise httpx.RemoteProtocolError("peer closed early", request=request)
            result = (
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mcp", "version": "1"},
                }
                if method == "initialize"
                else {"tools": [{"name": "search", "inputSchema": {}}]}
                if method == "tools/list"
                else {}
            )
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": json.loads(request.content).get("id"),
                    "result": result,
                },
                request=request,
            )

        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        connector = StreamableHttpMcpConnector(timeout=5, cold_start_timeout=5)
        with (
            patch("core_agent.mcp.httpx.AsyncClient", side_effect=client),
            patch("core_agent.mcp.time.sleep", return_value=None),
        ):
            catalog = connector.connect(
                {
                    "name": "mcp",
                    "transport": {"type": "streamable_http", "url": "https://mcp.test"},
                }
            )

        self.assertEqual(catalog, {"search": {}})
        self.assertEqual(attempts[:2], ["initialize", "initialize"])

    def test_sync_mcp_connector_can_be_called_from_an_active_event_loop(self):
        from core_agent.mcp import StreamableHttpMcpConnector

        real_client = httpx.AsyncClient

        def handler(request):
            payload = json.loads(request.content)
            result = (
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mcp", "version": "1"},
                }
                if payload["method"] == "initialize"
                else {"tools": [{"name": "search", "inputSchema": {}}]}
                if payload["method"] == "tools/list"
                else {}
            )
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": payload.get("id"), "result": result},
                request=request,
            )

        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        async def connect():
            return StreamableHttpMcpConnector(timeout=5, cold_start_timeout=0).connect(
                {
                    "name": "mcp",
                    "transport": {"type": "streamable_http", "url": "https://mcp.test"},
                }
            )

        with patch("core_agent.mcp.httpx.AsyncClient", side_effect=client):
            catalog = asyncio.run(connect())
        self.assertEqual(catalog, {"search": {}})

    def test_mcp_connector_close_does_not_invalidate_active_discovery(self):
        from core_agent.mcp import StreamableHttpMcpConnector

        started = threading.Event()
        release = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                method = payload["method"]
                if method == "initialize":
                    started.set()
                    release.wait(1)
                    result = {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "mcp", "version": "1"},
                    }
                elif method == "tools/list":
                    result = {"tools": [{"name": "search", "inputSchema": {}}]}
                else:
                    result = {}
                body = json.dumps(
                    {"jsonrpc": "2.0", "id": payload.get("id"), "result": result}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connector = StreamableHttpMcpConnector(timeout=5, cold_start_timeout=0)
        results = []

        def connect():
            try:
                results.append(
                    connector.connect(
                        {
                            "name": "mcp",
                            "transport": {
                                "type": "streamable_http",
                                "url": f"http://127.0.0.1:{server.server_port}/mcp",
                            },
                        }
                    )
                )
            except Exception as error:
                results.append(error)

        thread = threading.Thread(target=connect)
        thread.start()
        self.assertTrue(started.wait(1))
        connector.close()
        release.set()
        thread.join(1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [{"search": {}}])

    def test_mcp_cold_start_deadline_caps_the_network_attempt(self):
        from core_agent.mcp import StreamableHttpMcpConnector

        timeouts = []
        real_client = httpx.AsyncClient

        def unavailable(request):
            raise httpx.ConnectError("still starting", request=request)

        def client(**kwargs):
            timeouts.append(kwargs["timeout"].connect)
            return real_client(transport=httpx.MockTransport(unavailable), **kwargs)

        connector = StreamableHttpMcpConnector(timeout=30, cold_start_timeout=0.05)
        with (
            patch("core_agent.mcp.httpx.AsyncClient", side_effect=client),
            self.assertRaises(CoreError) as caught,
        ):
            connector.connect(
                {
                    "name": "mcp",
                    "transport": {"type": "streamable_http", "url": "https://mcp.test"},
                }
            )

        self.assertEqual(len(timeouts), 1)
        self.assertGreater(timeouts[0], 0)
        self.assertLessEqual(timeouts[0], 0.05)
        self.assertEqual(caught.exception.code, "MCP_CONNECTION_FAILED")
        self.assertEqual(caught.exception.data["reason"], "cold_start_timeout")

    def test_mcp_cold_start_does_not_turn_cancel_polling_into_a_one_second_timeout(
        self,
    ):
        from core_agent.mcp import StreamableHttpMcpConnector

        class SlowHealthyHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                if request.get("method") == "initialize":
                    time.sleep(1.05)
                    result = {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "slow", "version": "1"},
                    }
                elif request.get("method") == "tools/list":
                    result = {"tools": [{"name": "search", "inputSchema": {}}]}
                else:
                    result = {}
                body = json.dumps(
                    {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except BrokenPipeError:
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHealthyHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connector = StreamableHttpMcpConnector(timeout=5, cold_start_timeout=3)

        catalog = connector.connect(
            {
                "name": "mcp",
                "transport": {
                    "type": "streamable_http",
                    "url": f"http://127.0.0.1:{server.server_port}/mcp",
                },
            },
            cancel_event=threading.Event(),
        )

        self.assertEqual(catalog, {"search": {}})

    def test_mcp_cancel_interrupts_a_trickled_response_body(self):
        from core_agent.mcp import StreamableHttpMcpConnector

        first_byte = threading.Event()

        class TricklingHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request.get("id"),
                        "result": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {"tools": {}},
                        },
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    for byte in body:
                        self.wfile.write(bytes((byte,)))
                        self.wfile.flush()
                        first_byte.set()
                        time.sleep(0.02)
                except BrokenPipeError:
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), TricklingHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connector = StreamableHttpMcpConnector(timeout=5, cold_start_timeout=5)
        cancel = threading.Event()
        errors = []

        def connect():
            try:
                connector.connect(
                    {
                        "name": "mcp",
                        "transport": {
                            "type": "streamable_http",
                            "url": f"http://127.0.0.1:{server.server_port}/mcp",
                        },
                    },
                    cancel_event=cancel,
                )
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=connect)
        thread.start()
        self.assertTrue(first_byte.wait(1))
        started = time.monotonic()
        cancel.set()
        thread.join(0.75)

        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 0.75)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], CoreError)
        self.assertEqual(errors[0].code, "TASK_CANCELLED")

    def test_mcp_cold_start_deadline_interrupts_a_trickled_response_body(self):
        from core_agent.mcp import StreamableHttpMcpConnector

        class TricklingHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request.get("id"),
                        "result": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {"tools": {}},
                        },
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    for byte in body:
                        self.wfile.write(bytes((byte,)))
                        self.wfile.flush()
                        time.sleep(0.02)
                except BrokenPipeError:
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), TricklingHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connector = StreamableHttpMcpConnector(timeout=5, cold_start_timeout=0.2)
        started = time.monotonic()

        with self.assertRaises(CoreError) as caught:
            connector.connect(
                {
                    "name": "mcp",
                    "transport": {
                        "type": "streamable_http",
                        "url": f"http://127.0.0.1:{server.server_port}/mcp",
                    },
                }
            )

        self.assertLess(time.monotonic() - started, 0.75)
        self.assertEqual(caught.exception.code, "MCP_CONNECTION_FAILED")
        self.assertEqual(caught.exception.data["reason"], "cold_start_timeout")

    def test_mcp_cold_start_timeout_comes_from_the_environment(self):
        from core_agent.model import ModelResponse, ScriptedModel

        for configured, expected in ((None, 300.0), ("17.5", 17.5), ("0", 0.0)):
            environment = dict(BASE_ENVIRONMENT)
            if configured is not None:
                environment["MCP_COLD_START_TIMEOUT_SECONDS"] = configured
            model = ScriptedModel([ModelResponse(message="ok")])
            model.model = "cold-start-config"
            with self.subTest(configured=configured):
                with patch.dict(os.environ, environment, clear=True):
                    app = create_app(model=model)
                try:
                    self.assertEqual(
                        app.state.core_agent.mcp_connector.cold_start_timeout,
                        expected,
                    )
                finally:
                    app.state.close()

    def test_mcp_cold_start_timeout_rejects_non_finite_or_negative_values(self):
        from core_agent.model import ModelResponse, ScriptedModel

        for value in ("-1", "nan", "inf", "not-a-number"):
            model = ScriptedModel([ModelResponse(message="unused")])
            model.model = "cold-start-config"
            with (
                self.subTest(value=value),
                patch.dict(
                    os.environ,
                    {**BASE_ENVIRONMENT, "MCP_COLD_START_TIMEOUT_SECONDS": value},
                    clear=True,
                ),
                self.assertRaises(CoreError) as caught,
            ):
                create_app(model=model)
            self.assertEqual(caught.exception.code, "CONFIG_INVALID")

    def test_mcp_request_timeouts_must_be_finite_and_positive(self):
        from core_agent.mcp import StreamableHttpMcpConnector

        for field in ("timeout", "sse_read_timeout"):
            for value in (0, -1, float("nan"), float("inf"), "not-a-number"):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(CoreError) as caught:
                        StreamableHttpMcpConnector(**{field: value})
                    self.assertEqual(caught.exception.code, "CONFIG_INVALID")

    def test_mcp_custom_headers_cannot_override_transport_state(self):
        from core_agent.mcp import StreamableHttpMcpConnector

        for name in (
            "content-type",
            "ACCEPT",
            "mcp-method",
            "MCP-NAME",
            "mcp-session-id",
            "mcp-protocol-version",
        ):
            with self.subTest(name=name):
                with self.assertRaises(CoreError) as caught:
                    StreamableHttpMcpConnector(headers={name: "attacker-controlled"})
                self.assertEqual(caught.exception.code, "CONFIG_INVALID")

    def test_startup_record_survives_late_logging_setup(self):
        """The ASGI server configures logging after the app is built."""
        from core_agent.model import ModelResponse, ScriptedModel

        logger = logging.getLogger("core_agent.runtime")
        saved = list(logger.handlers)
        for handler in saved:
            logger.removeHandler(handler)
        stream = io.StringIO()
        try:
            model = ScriptedModel([ModelResponse(message="ok")])
            model.model = "late-logging"
            with patch.dict(os.environ, BASE_ENVIRONMENT, clear=True):
                app = create_app(model=model)
            # create_app must own the handler; attaching one afterwards is too late.
            self.assertTrue(logger.handlers)
            logger.handlers[0].stream = stream
            app.state.close()
        finally:
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
            for handler in saved:
                logger.addHandler(handler)

    def test_startup_records_describe_the_configuration_without_secrets(self):
        """A missing capability must be explainable from the logs alone."""
        from core_agent.model import ModelResponse, ScriptedModel

        model = ScriptedModel([ModelResponse(message="ok")])
        model.model = "diag-model"
        with patch.dict(
            os.environ,
            {**BASE_ENVIRONMENT, "LLM_API_KEY": "sk-must-not-appear"},
            clear=True,
        ):
            with self.assertLogs("core_agent.runtime", "INFO") as logs:
                app = create_app(model=model)
        try:
            startup = next(
                json.loads(line.split(":", 2)[2])
                for line in logs.output
                if '"startup.configuration"' in line
            )
            for key in (
                "runtime_mode",
                "builtin_tools",
                "mcp_servers",
                "mcp_allowed_tools",
                "mcp_read_only_tools",
                "remote_agents_configured",
                "remote_agents_connected",
                "session_storage",
                "model",
            ):
                self.assertIn(key, startup)
            self.assertNotIn("sk-must-not-appear", json.dumps(startup))

            with self.assertLogs("core_agent.runtime", "INFO") as logs:
                app.state.core_agent._resolve_capabilities(
                    type("R", (), {"prompt": "x"})()
                )
            resolved = next(
                json.loads(line.split(":", 2)[2])
                for line in logs.output
                if '"capabilities.resolved"' in line
            )
            # Discovered vs allowed per server is the whole point of the record.
            self.assertIn("mcp", resolved)
            self.assertIn("model_tool_catalog", resolved)
        finally:
            app.state.close()

    def test_mcp_allowlist_takes_bare_names_and_warns_on_a_silent_server(self):
        """A connected server exposing nothing looks healthy but gives nothing."""
        from core_agent.app import _allowed_mcp_tools
        from core_agent.mcp import InMemoryMcpConnector
        from core_agent.model import ModelResponse, ScriptedModel

        with patch.dict(
            os.environ,
            {
                **BASE_ENVIRONMENT,
                "MCP_ALLOWED_TOOLS": "get_current,weather.get_forecast",
            },
            clear=True,
        ):
            grouped = _allowed_mcp_tools({"weather", "docs"})
        # A bare name reaches every server; "server.tool" also keeps its scope.
        self.assertIn("get_current", grouped["weather"])
        self.assertIn("get_current", grouped["docs"])
        self.assertIn("get_forecast", grouped["weather"])
        self.assertNotIn("get_forecast", grouped["docs"])

        # Without an allowlist no tool is allowed anywhere: nothing is allowed by
        # default, so a connected server must say so instead of looking healthy.
        with patch.dict(os.environ, BASE_ENVIRONMENT, clear=True):
            self.assertEqual(
                _allowed_mcp_tools({"weather", "docs"}),
                {
                    "weather": [],
                    "docs": [],
                },
            )

        model = ScriptedModel([ModelResponse(message="ok")])
        model.model = "silent-mcp-model"
        connector = InMemoryMcpConnector(
            catalogs={"docs": {"search": {"type": "object"}}}
        )
        with patch.dict(
            os.environ,
            {**BASE_ENVIRONMENT, "MCP_URL": "https://docs.test/docs"},
            clear=True,
        ):
            app = create_app(model=model, mcp_connector=connector)
        try:
            with self.assertLogs("core_agent.runtime", "WARNING") as logs:
                app.state.core_agent._resolve_capabilities(
                    type("R", (), {"prompt": "x"})()
                )
            self.assertEqual(connector.connections, ("docs",))
            silent = [
                line for line in logs.output if "no tool of it is allowed" in line
            ]
            self.assertEqual(len(silent), 1)
            # The warning must name the server and what could be allowed.
            self.assertIn("'docs'", silent[0])
            self.assertIn("search", silent[0])
        finally:
            app.state.close()

    def test_mcp_read_only_policy_comes_from_deployment_configuration(self):
        from core_agent.mcp import InMemoryMcpConnector
        from core_agent.model import ModelResponse, ScriptedModel

        model = ScriptedModel([ModelResponse(message="ok")])
        model.model = "read-only-policy"
        with patch.dict(
            os.environ,
            {
                **BASE_ENVIRONMENT,
                "MCP_URL": "https://docs.test/docs",
                "MCP_ALLOWED_TOOLS": "search",
                "MCP_READ_ONLY_TOOLS": "search",
            },
            clear=True,
        ):
            app = create_app(model=model, mcp_connector=InMemoryMcpConnector())
        try:
            self.assertEqual(
                app.state.core_agent.platform_mcp[0]["read_only_tools"], ["search"]
            )
        finally:
            app.state.close()

    def test_scoped_mcp_read_only_policy_does_not_leak_to_another_server(self):
        from core_agent.app import _read_only_mcp_tools

        with patch.dict(
            os.environ,
            {**BASE_ENVIRONMENT, "MCP_READ_ONLY_TOOLS": "docs.search"},
            clear=True,
        ):
            grouped = _read_only_mcp_tools({"docs", "evil"})

        self.assertEqual(grouped["docs"], ["search"])
        self.assertEqual(grouped["evil"], [])

    def test_subagent_inherits_remote_agent_services_without_named_storage(self):
        """A delegated tool whose service is missing answers CAPABILITY_DISABLED."""
        from core_agent.model import ModelResponse, ScriptedModel

        peer = ThreadingHTTPServer(("127.0.0.1", 0), RemoteAgentHandler)
        threading.Thread(target=peer.serve_forever, daemon=True).start()
        self.addCleanup(peer.server_close)
        self.addCleanup(peer.shutdown)
        model = ScriptedModel([ModelResponse(message="ok")])
        model.model = "child-model"
        with patch.dict(
            os.environ,
            {
                **BASE_ENVIRONMENT,
                "REMOTE_AGENTS": f"http://127.0.0.1:{peer.server_port}",
                "SEND_MESSAGE_API_KEY": "deployment-key",
            },
            clear=True,
        ):
            app = create_app(model=model)
        try:
            parent = app.state.core_agent
            self.assertTrue(parent.remote_agents)
            child_raw = parent.agent_config.to_dict()
            child = parent._child_agent(child_raw, ["core_agent_send_message"])

            # Every service backing a delegable tool must reach the child.
            self.assertFalse(hasattr(parent, "artifact_service"))
            self.assertFalse(hasattr(child, "artifact_service"))
            self.assertEqual(child.remote_agents, parent.remote_agents)
            self.assertEqual(child.send_message_api_key, "deployment-key")

        finally:
            app.state.close()

    def test_incoming_binary_parts_are_preserved_then_explicitly_refused_without_workspace_admission(self):
        from core_agent.a2a import Message, Part, parse_run_request
        from core_agent.model import ModelResponse, ScriptedModel

        metadata = {}
        extensions = ()

        def message(*parts):
            return Message("user", parts, extensions, metadata, context_id="s1")

        # The SDK conversion must carry raw and url parts through at all.
        from a2a.types import Message as SdkMessage, Part as SdkPart, Role
        from core_agent.a2a_sdk import CoreAgentExecutor

        converted = CoreAgentExecutor._from_sdk_message(
            SdkMessage(
                message_id="m1",
                role=Role.ROLE_USER,
                parts=[
                    SdkPart(text="hi"),
                    SdkPart(raw=b"\x89PNG", media_type="image/png", filename="c.png"),
                    SdkPart(url="https://example.test/r.pdf"),
                ],
            )
        )
        self.assertEqual(
            [part.kind for part in converted.parts], ["text", "file", "url"]
        )
        self.assertEqual(converted.parts[1].data["bytes"], b"\x89PNG")

        # A URL part is refused outright: fetching it would be SSRF.
        with self.assertRaises(CoreError) as caught:
            parse_run_request(message(Part("url", "https://attacker.test/x")))
        self.assertEqual(caught.exception.code, "CONTENT_TYPE_NOT_SUPPORTED")

        # Binary parts survive parsing instead of being dropped, and an
        # attachment-only message still yields a usable prompt.
        parsed = parse_run_request(
            message(Part.text("look"), Part.file(b"\x89PNG", filename="chart.png"))
        )
        self.assertEqual(parsed.prompt, "look")
        self.assertEqual(parsed.attachments[0]["filename"], "chart.png")
        self.assertEqual(
            parse_run_request(message(Part.file(b"x", filename="a.bin"))).prompt,
            ATTACHMENTS_ONLY_PROMPT,
        )

        def build(**environment):
            model = ScriptedModel([ModelResponse(message="ok")])
            model.model = "blob-model"
            with patch.dict(
                os.environ, {**BASE_ENVIRONMENT, **environment}, clear=True
            ):
                return create_app(model=model)

        # Switched off, an attachment is refused rather than silently ignored.
        app = build()
        try:
            with self.assertRaises(CoreError) as caught:
                app.state.store_attachments(parsed, "u1", "s1")
            self.assertEqual(caught.exception.code, "CONTENT_TYPE_NOT_SUPPORTED")
        finally:
            app.state.close()

    def test_blank_variables_fall_back_to_their_documented_defaults(self):
        """Compose substitutes "" for an unresolved ${VAR}; that must mean unset."""
        from core_agent.app import _model

        blank = dict.fromkeys(
            (
                "LLM_API_FORMAT",
                "LLM_TIMEOUT",
                "LLM_MAX_TOKENS",
                "LLM_CONTEXT_WINDOW",
                "THINKING_LEVEL",
                "A2A_STREAMING_ENABLED",
                "CONTEXT_CACHE_TTL_SECONDS",
            ),
            "",
        )
        with patch.dict(
            os.environ,
            {
                **BASE_ENVIRONMENT,
                **blank,
                "LLM_MODEL": "blank-defaults",
                "LLM_API_BASE": "https://provider.test/v1",
            },
            clear=True,
        ):
            model = _model()
        self.assertEqual(model.api_format, "openai")
        self.assertEqual(model.max_tokens, 4096)
        self.assertEqual(model.context_window, 128000)

    def test_config_error_reports_the_setting_instead_of_a_traceback(self):
        """A misconfigured deployment must exit 1 with one readable line."""
        from core_agent.app import main

        cases = [
            ({"LLM_MODEL": ""}, "LLM_MODEL is required"),
            (
                {"LLM_MODEL": "m", "LLM_API_FORMAT": "gemini"},
                "LLM_API_FORMAT must be openai or anthropic",
            ),
            (
                {"LLM_MODEL": "m", "A2A_STREAMING_ENABLED": "maybe"},
                "A2A_STREAMING_ENABLED must be boolean",
            ),
        ]
        cases.extend((
            {"LLM_MODEL": "m", "CORE_AGENT_MEMORY": "disabled", name: "invalid-number-canary"},
            name + " must be numeric",
        ) for name in ("LLM_TIMEOUT", "LLM_MAX_TOKENS", "LLM_CONTEXT_WINDOW",
                       "RUNTIME_MAX_LLM_CALLS", "EVENTS_COMPACTION_INTERVAL", "PORT",
                       "A2A_STREAMING_BUFFER_SIZE", "MAX_CHUNK_SIZE"))
        cases.append(({"LLM_MODEL": "m", "LOG_LEVEL": "invalid-level-canary"}, "LOG_LEVEL must be a valid logging level"))
        cases.append(({"LLM_MODEL": "m", "CORE_AGENT_MEMORY": "disabled",
                       "REMOTE_AGENTS": "https://peer.example/a2a",
                       "REMOTE_AGENTS_RETRYABLE_STATUS_CODES": "invalid-code-canary"},
                      "REMOTE_AGENTS_RETRYABLE_STATUS_CODES must contain integers"))
        cases.extend((
            {"LLM_MODEL": "m", "LLM_TIMEOUT": "invalid-number-canary", "LOG_LEVEL": level},
            "LLM_TIMEOUT must be numeric",
        ) for level in ("CRITICAL", "FATAL"))
        cases.extend((
            {"LLM_MODEL": "m", "CORE_AGENT_MEMORY": "disabled", name: "private-value-canary"},
            expected,
        ) for name, expected in (("TASK_STORAGE_TYPE", "unsupported TASK_STORAGE_TYPE"),
                                 ("A2A_CAPABILITIES", "unknown A2A_CAPABILITIES")))
        for environment, expected in cases:
            with self.subTest(environment=environment):
                with patch.dict(
                    os.environ, {**BASE_ENVIRONMENT, **environment}, clear=True
                ), patch("core_agent.app.create_app", side_effect=create_app), patch("uvicorn.run") as listener:
                    with self.assertLogs("core_agent.runtime", "ERROR") as logs:
                        with self.assertRaises(SystemExit) as exit_code:
                            main()
                self.assertEqual(exit_code.exception.code, 1)
                self.assertIn(expected, logs.output[0])
                self.assertIn("CONFIG_INVALID", logs.output[0])
                self.assertNotIn("canary", "\n".join(logs.output))
                listener.assert_not_called()

    def test_model_timeout_rejects_nonfinite_values(self):
        from core_agent.app import _model

        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {
                    **BASE_ENVIRONMENT, "LLM_MODEL": "m", "LLM_TIMEOUT": value,
                }, clear=True), self.assertRaises(CoreError) as caught:
                    _model()
                self.assertEqual(caught.exception.code, "CONFIG_INVALID")
                self.assertEqual(caught.exception.message, "LLM_TIMEOUT must be finite")

    def test_thinking_disabled_turns_the_effort_off_explicitly(self):
        with patch.dict(
            os.environ,
            {**BASE_ENVIRONMENT, "LLM_MODEL": "m", "THINKING_ENABLED": "false"},
            clear=True,
        ):
            from core_agent.app import _model

            self.assertEqual(_model().reasoning_effort, "none")


if __name__ == "__main__":
    unittest.main()
