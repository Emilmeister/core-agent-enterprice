import io
import json
import logging
import os
import re
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import httpx

from core_agent.a2a import ATTACHMENTS_ONLY_PROMPT
from core_agent.app import create_app
from core_agent.artifact_service import ArtifactService, InMemoryArtifactBackend
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

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
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


class ArtifactServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = ArtifactService(InMemoryArtifactBackend(), max_bytes=64)
        self.scope = {"app_name": "agent", "user_id": "u1", "session_id": "s1"}

    def test_mongodb_backend_stores_and_reads_through_an_injected_collection(self):
        from core_agent.artifact_service import MongoDbArtifactBackend

        class Collection:
            def __init__(self):
                self.documents = {}

            def replace_one(self, key, document, upsert=False):
                self.documents[key["_id"]] = document

            def find_one(self, key):
                return self.documents.get(key["_id"])

            def find(self, query, projection=None):
                pattern = re.compile(query["_id"]["$regex"])
                return [
                    {"_id": key}
                    for key in sorted(self.documents)
                    if pattern.match(key)
                ]

        service = ArtifactService(MongoDbArtifactBackend(collection=Collection()))
        stored = service.save(**self.scope, filename="m.txt", content=b"mongo")
        self.assertEqual(stored.version, 0)
        self.assertEqual(service.load(**self.scope, filename="m.txt")[1], b"mongo")
        self.assertEqual(service.list_keys(**self.scope), (["m.txt"], []))

    def test_every_save_creates_a_new_version_and_never_overwrites(self):
        first = self.service.save(**self.scope, filename="report.txt", content=b"one")
        second = self.service.save(**self.scope, filename="report.txt", content=b"two")
        self.assertEqual((first.version, second.version), (0, 1))
        self.assertEqual(
            self.service.load(**self.scope, filename="report.txt")[1], b"two"
        )
        self.assertEqual(
            self.service.load(**self.scope, filename="report.txt", version=0)[1], b"one"
        )

    def test_user_prefix_is_visible_from_a_different_session(self):
        self.service.save(**self.scope, filename="user:profile.json", content=b"{}")
        self.service.save(**self.scope, filename="notes.txt", content=b"local")
        other = {**self.scope, "session_id": "s2"}
        session_names, user_names = self.service.list_keys(**other)
        self.assertEqual(session_names, [])
        self.assertEqual(user_names, ["user:profile.json"])
        self.assertEqual(
            self.service.load(**other, filename="user:profile.json")[1], b"{}"
        )

    def test_media_type_is_guessed_and_metadata_round_trips(self):
        stored = self.service.save(
            **self.scope,
            filename="data.json",
            content=b"{}",
            metadata={"category": "export"},
        )
        self.assertEqual(stored.media_type, "application/json")
        loaded, _content = self.service.load(**self.scope, filename="data.json")
        self.assertEqual(loaded.metadata, {"category": "export"})

    def test_trust_boundary_rejects_traversal_oversize_and_missing_session(self):
        for filename in ("../escape", "a/b", "", "\x00null"):
            with self.subTest(filename=filename):
                with self.assertRaises(CoreError) as caught:
                    self.service.save(**self.scope, filename=filename, content=b"x")
                self.assertEqual(caught.exception.code, "TOOL_ARGUMENT_INVALID")
        with self.assertRaises(CoreError) as caught:
            self.service.save(**self.scope, filename="big.bin", content=b"x" * 65)
        self.assertEqual(caught.exception.code, "ARTIFACT_TOO_LARGE")
        with self.assertRaises(CoreError) as caught:
            self.service.save(
                app_name="agent",
                user_id="u1",
                session_id="",
                filename="x.txt",
                content=b"x",
            )
        self.assertEqual(caught.exception.code, "CONFIG_INVALID")
        with self.assertRaises(CoreError) as caught:
            self.service.load(**self.scope, filename="missing.txt")
        self.assertEqual(caught.exception.code, "NOT_FOUND")


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
    """Minimal A2A 0.3 JSON-RPC peer used to exercise send_message."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self._json(
            {
                "name": "weather-agent",
                "description": "weather",
                "url": f"http://127.0.0.1:{self.server.server_port}/",
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
        for frame in (
            {
                "kind": "status-update",
                "final": False,
                "status": {
                    "state": "working",
                    "message": {"parts": [{"kind": "text", "text": "looking it up"}]},
                },
            },
            {
                "kind": "status-update",
                "final": True,
                "status": {
                    "state": "completed",
                    "message": {"parts": [{"kind": "text", "text": "24 degrees"}]},
                },
            },
        ):
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
        self.assertEqual([event.text for event in events], ["looking it up", "24 degrees"])
        self.assertEqual([event.final for event in events], [False, True])
        body, headers = RemoteAgentHandler.seen
        self.assertEqual(body["method"], "message/stream")
        self.assertEqual(
            body["params"]["message"]["parts"], [{"kind": "text", "text": "What is the weather?"}]
        )
        self.assertEqual(body["params"]["message"]["contextId"], "c1")
        lowered = {key.lower(): value for key, value in headers.items()}
        self.assertEqual(lowered["x-project-id"], "p1")


class StreamingA2ATests(unittest.IsolatedAsyncioTestCase):
    """The A2A 0.3 JSON-RPC binding must carry ADK-shaped progress frames."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        ModelHandler.reasoning = "Deciding what to do."
        ModelHandler.answer = "Streamed answer."
        ModelHandler.tool_call = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _app(self, **environment):
        patcher = patch.dict(
            os.environ,
            {
                **BASE_ENVIRONMENT,
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
            "method": "message/stream",
            "params": {
                "message": {
                    "kind": "message",
                    "role": "user",
                    "messageId": str(uuid.uuid4()),
                    "parts": [{"kind": "text", "text": prompt}],
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
                                        "A2A-Version": "0.3",
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
    def _parts(frame):
        return ((frame.get("status") or {}).get("message") or {}).get("parts", [])

    async def test_reasoning_streams_as_thought_parts_before_the_terminal_frame(self):
        frames = await self._frames(self._app(), "hello")
        thoughts = [
            part["text"]
            for frame in frames
            for part in self._parts(frame)
            if (part.get("metadata") or {}).get("adk_thought")
        ]
        self.assertIn("Deciding what to do.", thoughts)

        partials = [
            frame
            for frame in frames
            if (((frame.get("status") or {}).get("message") or {}).get("metadata") or {}).get(
                "partial"
            )
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

        terminal = [frame for frame in frames if frame.get("final")]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["status"]["state"], "completed")
        self.assertNotIn(
            True,
            [
                (part.get("metadata") or {}).get("adk_thought")
                for part in self._parts(terminal[0])
            ],
        )

    async def test_tool_calls_and_results_stream_as_adk_data_parts(self):
        ModelHandler.tool_call = ("core.artifact.list", {})
        frames = await self._frames(self._app(), "list my files")
        typed = [
            (part["metadata"]["adk_type"], part["data"])
            for frame in frames
            for part in self._parts(frame)
            if (part.get("metadata") or {}).get("adk_type")
        ]
        self.assertEqual(
            [kind for kind, _data in typed], ["function_call", "function_response"]
        )
        self.assertEqual(typed[0][1]["name"], "core.artifact.list")
        self.assertEqual(typed[1][1]["response"]["status"], "succeeded")

    async def test_artifact_tools_round_trip_through_the_model_catalog(self):
        ModelHandler.tool_call = (
            "core.artifact.save",
            {"filename": "user:notes.txt", "content": "remember"},
        )
        frames = await self._frames(self._app(), "save a note")
        responses = [
            part["data"]["response"]
            for frame in frames
            for part in self._parts(frame)
            if (part.get("metadata") or {}).get("adk_type") == "function_response"
        ]
        self.assertEqual(responses[0]["output"]["version"], 0)
        self.assertEqual(responses[0]["output"]["artifact_name"], "user:notes.txt")

    async def test_send_message_relays_the_remote_agent_progress_and_answer(self):
        peer = ThreadingHTTPServer(("127.0.0.1", 0), RemoteAgentHandler)
        threading.Thread(target=peer.serve_forever, daemon=True).start()
        self.addCleanup(peer.server_close)
        self.addCleanup(peer.shutdown)
        ModelHandler.tool_call = (
            "core.agent.send_message",
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
            if part.get("kind") == "text" and not (part.get("metadata") or {}).get("adk_thought")
        ]
        self.assertIn("looking it up", relayed)
        response = next(
            part["data"]["response"]
            for frame in frames
            for part in self._parts(frame)
            if (part.get("metadata") or {}).get("adk_type") == "function_response"
        )
        self.assertEqual(response["output"]["result"], "24 degrees")
        self.assertTrue(response["output"]["success"])
        _body, headers = RemoteAgentHandler.seen
        lowered = {key.lower(): value for key, value in headers.items()}
        self.assertEqual(lowered["authorization"], "Bearer caller-token")
        self.assertNotIn("cookie", lowered)

    async def test_send_message_is_absent_when_no_remote_agent_is_configured(self):
        app = self._app()
        advertised = {skill.id for skill in app.state.a2a_request_handler._agent_card.skills}
        self.assertNotIn("core.agent.send_message", advertised)
        self.assertNotIn(
            "core.agent.send_message",
            app.state.core_agent.platform_config.allowed_builtin_tools,
        )

    async def test_streaming_can_be_disabled_without_losing_the_result(self):
        frames = await self._frames(self._app(A2A_STREAMING_ENABLED="false"), "hello")
        self.assertEqual(
            [], [frame for frame in frames if self._parts(frame) and not frame.get("final")]
        )
        terminal = [frame for frame in frames if frame.get("final")]
        self.assertEqual(terminal[0]["status"]["state"], "completed")


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
                "kind": "message",
                "role": "user",
                "messageId": str(uuid.uuid4()),
                "parts": [{"kind": "text", "text": "hi"}],
                                            }
            if context_id:
                message["contextId"] = context_id
            return {
                "jsonrpc": "2.0",
                "id": identifier,
                "method": "message/send",
                "params": {"message": message},
                **extra,
            }

        _reported_extra_fields.clear()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://agent.test"
            ) as client:
                headers = {}

                async def send(body):
                    return (await client.post("/", json=body, headers=headers)).json()

                # Duplicated outside and inside: accepted, session from the message.
                with self.assertLogs("core_agent.runtime", "WARNING") as logs:
                    result = await send(
                        envelope("1", {"contextId": "ctx-a"}, context_id="ctx-a")
                    )
                self.assertNotIn("error", result)
                self.assertEqual(result["result"]["contextId"], "ctx-a")
                self.assertIn("contextId", logs.output[0])

                # Only outside: accepted, but never promoted to a session id.
                result = await send(envelope("2", {"contextId": "ctx-b"}))
                self.assertNotEqual(result["result"]["contextId"], "ctx-b")

                # Several unknown members are dropped together.
                result = await send(
                    envelope("3", {"foo": 1, "bar": 2}, context_id="ctx-c")
                )
                self.assertEqual(result["result"]["contextId"], "ctx-c")

                # A clean envelope keeps working and logs nothing new.
                before = set(_reported_extra_fields)
                result = await send(envelope("4", {}, context_id="ctx-d"))
                self.assertEqual(result["result"]["contextId"], "ctx-d")
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
                    found[path] = {
                        item["url"] for item in card["supportedInterfaces"]
                    }
            return found

        # An explicit AGENT_URL wins over any header a caller can forge.
        app = build(AGENT_URL="https://configured.example")
        try:
            found = await urls(app, {"Host": "attacker.example"})
            self.assertEqual(
                list(found.values()), [{"https://configured.example"}] * 2
            )
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
                self.assertEqual(
                    advertised, {("HTTP+JSON", "1.0"), ("JSONRPC", "0.3")}
                )
                for binding, version in sorted(advertised):
                    with self.subTest(binding=binding, version=version):
                        headers = {"A2A-Version": version}
                        if binding == "JSONRPC":
                            response = await client.post(
                                "/",
                                json={
                                    "jsonrpc": "2.0",
                                    "id": "1",
                                    "method": "tasks/get",
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
                        create_app(
                            model=None if "LLM_MODEL" in environment else model
                        )
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
                if "REMOTE_AGENTS" in item and "startup.configuration" not in item
            ]

        unset = startup()
        self.assertEqual(len(unset), 1)
        self.assertTrue(unset[0].startswith("WARNING:"))
        self.assertIn("core.agent.send_message", unset[0])
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
            record["telemetry"]["logs"], "https://collector.test/v1/logs"
        )
        self.assertEqual(
            record["telemetry"]["traces"], "https://collector.test/v1/traces"
        )
        self.assertTrue(record["telemetry"]["credentials_configured"])
        # The value must never appear, in any form.
        self.assertNotIn("sk-never-logged", line)

        record, _ = startup(OTEL_ENDPOINT="https://collector.test")
        self.assertFalse(record["telemetry"]["credentials_configured"])

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

    def test_mcp_connection_failure_names_the_actual_cause(self):
        """One generic code cannot separate a wrong URL from TLS or a dead host."""
        from core_agent.mcp import StreamableHttpMcpConnector

        connector = StreamableHttpMcpConnector(timeout=5)

        def connect(url):
            with self.assertRaises(CoreError) as caught:
                connector.connect(
                    {"name": "mcp", "transport": {"type": "streamable_http", "url": url}}
                )
            self.assertEqual(caught.exception.code, "MCP_CONNECTION_FAILED")
            return str(caught.exception)

        self.assertIn("scheme is not allowed", connect("http://mcp.example.test/mcp"))
        self.assertIn("Connection refused", connect("http://127.0.0.1:1/mcp"))

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
                "remote_agents_configured",
                "remote_agents_connected",
                "artifact_storage",
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

        with patch.dict(
            os.environ,
            {**BASE_ENVIRONMENT, "MCP_ALLOWED_TOOLS": "get_current,weather.get_forecast"},
            clear=True,
        ):
            grouped = _allowed_mcp_tools({"weather", "memory"})
        # A bare name reaches every server; "server.tool" also keeps its scope.
        self.assertIn("get_current", grouped["weather"])
        self.assertIn("get_current", grouped["memory"])
        self.assertIn("get_forecast", grouped["weather"])
        self.assertNotIn("get_forecast", grouped["memory"])

        # The default still names the memory tools and nothing else.
        with patch.dict(os.environ, BASE_ENVIRONMENT, clear=True):
            default = _allowed_mcp_tools({"weather", "memory"})
        self.assertIn("memory.search", default["memory"])
        self.assertEqual(
            [name for name in default["weather"] if not name.startswith("memory.")], []
        )

    def test_subagent_inherits_artifact_and_remote_agent_services(self):
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
            child = parent._child_agent(child_raw, ["core.artifact.save"])

            # Every service backing a delegable tool must reach the child.
            self.assertIs(child.artifact_service, parent.artifact_service)
            self.assertEqual(child.remote_agents, parent.remote_agents)
            self.assertEqual(child.send_message_api_key, "deployment-key")

            # Parent and child share one session scope, so a handover by name works.
            scope = {"app_name": "core-agent", "user_id": "u1", "session_id": "s1"}
            parent.artifact_service.save(**scope, filename="hand.txt", content=b"over")
            self.assertEqual(
                child.artifact_service.load(**scope, filename="hand.txt")[1], b"over"
            )
        finally:
            app.state.close()

    def test_incoming_binary_parts_become_artifacts_or_are_refused(self):
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
        self.assertEqual([part.kind for part in converted.parts], ["text", "file", "url"])
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

        # Switched on, it is stored in session scope and referenced in the prompt.
        app = build(RUNTIME_SAVE_INPUT_BLOBS_AS_ARTIFACTS="true")
        try:
            hostile = parse_run_request(
                message(
                    Part.text("look"),
                    Part.file(b"png", filename="user:profile.json"),
                    Part.file(b"raw", filename="../escape", media_type="image/png"),
                )
            )
            stored = app.state.store_attachments(hostile, "u1", "s1")
            self.assertEqual(stored.attachments, ())
            self.assertIn("profile.json", stored.prompt)
            self.assertIn("attachment-2.png", stored.prompt)
            service = app.state.core_agent.artifact_service
            scope = {"app_name": "core-agent", "user_id": "u1", "session_id": "s1"}
            session_names, user_names = service.list_keys(**scope)
            # The caller-supplied "user:" prefix must not reach the user scope.
            self.assertEqual(user_names, [])
            self.assertEqual(session_names, ["attachment-2.png", "profile.json"])
            self.assertEqual(service.load(**scope, filename="profile.json")[1], b"png")
        finally:
            app.state.close()

    def test_s3_endpoint_and_access_key_follow_the_cloud_ru_profile(self):
        from core_agent.artifact_service import CLOUD_RU_ENDPOINT, S3ArtifactBackend

        def build(**kwargs):
            return S3ArtifactBackend(
                bucket="demo", region="ru-central-1", secret_access_key="s", **kwargs
            )

        # A non-canonical endpoint warns and is overridden, it never fails startup.
        with self.assertLogs("core_agent.runtime", "WARNING") as logs:
            backend = build(endpoint_url="https://my-minio:9000", access_key_id="t:k")
        self.assertEqual(backend._base, CLOUD_RU_ENDPOINT)
        self.assertIn("my-minio", logs.output[0])
        self.assertIn(CLOUD_RU_ENDPOINT, logs.output[0])

        # A trailing slash is normalised silently.
        with self.assertNoLogs("core_agent.runtime", "WARNING"):
            self.assertEqual(
                build(endpoint_url=CLOUD_RU_ENDPOINT + "/", access_key_id="t:k")._base,
                CLOUD_RU_ENDPOINT,
            )

        # Cloud.ru requires exactly one colon with both halves present.
        self.assertEqual(
            build(endpoint_url=CLOUD_RU_ENDPOINT, tenant_id="t", access_key_id="k").access_key_id,
            "t:k",
        )
        for key in ("AKIAKEY", "a:b:c", ":k", "t:"):
            with self.subTest(access_key_id=key):
                with self.assertRaises(CoreError) as caught:
                    build(endpoint_url=CLOUD_RU_ENDPOINT, access_key_id=key)
                self.assertEqual(caught.exception.code, "CONFIG_INVALID")

        # The AWS profile keeps plain access key ids.
        self.assertEqual(build(access_key_id="AKIAKEY").access_key_id, "AKIAKEY")

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

        for environment, expected in (
            ({"LLM_MODEL": ""}, "LLM_MODEL is required"),
            (
                {"LLM_MODEL": "m", "LLM_API_FORMAT": "gemini"},
                "LLM_API_FORMAT must be openai or anthropic",
            ),
            (
                {"LLM_MODEL": "m", "A2A_STREAMING_ENABLED": "maybe"},
                "A2A_STREAMING_ENABLED must be boolean",
            ),
        ):
            with self.subTest(environment=environment):
                with patch.dict(
                    os.environ, {**BASE_ENVIRONMENT, **environment}, clear=True
                ):
                    with self.assertLogs("core_agent.runtime", "ERROR") as logs:
                        with self.assertRaises(SystemExit) as exit_code:
                            main()
                self.assertEqual(exit_code.exception.code, 1)
                self.assertIn(expected, logs.output[0])
                self.assertIn("CONFIG_INVALID", logs.output[0])

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
