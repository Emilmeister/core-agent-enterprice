import base64
import json
import logging
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from a2a.server.context import ServerCallContext
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.types import Message as SdkMessage, Task, TaskState, TaskStatus
from starlette.applications import Starlette
from starlette.routing import Mount

from core_agent.a2a import AgentCard, Artifact
from core_agent.a2a_sdk import build_starlette_app
from core_agent.auth import AuthenticationMiddleware, Principal
from core_agent.errors import CoreError


class A2AInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.messages = []

        def handle(message, _context, _publisher):
            self.messages.append(message)
            return Artifact.text("ok")

        self.options = dict(
            agent_card=AgentCard.minimal("input-test"),
            base_url="http://input.test",
            handler=handle,
            cancel_handler=lambda _context: None,
            resume_handler=lambda *_args: Artifact.text("unused"),
            followup_handler=lambda *_args, **_kwargs: None,
        )
        self.environment = patch.dict(os.environ, {"A2A_MAX_REQUEST_BYTES": "524288"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.app = build_starlette_app(**self.options)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://input.test")
        self.addAsyncCleanup(self.client.aclose)

    def payload(self, parts=None):
        return {"message": {"messageId": "input-id", "role": "ROLE_USER", "parts": parts or [{"text": "hello"}]}}

    def wire(self, binding, payload):
        if binding.startswith("/"):
            return binding, payload
        return "/", {"jsonrpc": "2.0", "id": "rpc-id", "method": binding, "params": payload}

    async def send(self, binding, payload, **kwargs):
        path, body = self.wire(binding, payload)
        return await self.client.post(path, json=body, headers={"A2A-Version": "1.0", **kwargs.pop("headers", {})}, **kwargs)

    def assert_error(self, response, *, rpc=False, code, status=400):
        self.assertEqual(response.status_code, 200 if rpc else status, response.text)
        error = response.json()["error"]
        metadata = (error["data"] if rpc else error["details"])[0]["metadata"]
        self.assertEqual(metadata["code"], code, response.text)
        return metadata

    async def test_noncanonical_second_file_is_rejected_before_sdk_on_all_bindings(self):
        for binding in ("/message:send", "/message:stream", "SendMessage", "SendStreamingMessage"):
            for raw in ("YQ", "YQ=", "YQ===", "YR==", "YQ==\n", "_w==", "!!!!", None, 1):
                with self.subTest(binding=binding, raw=raw):
                    response = await self.send(binding, self.payload([
                        {"text": "do not deliver"}, {"raw": "YQ==", "filename": "first.txt"},
                        {"raw": raw, "filename": "private-name.txt"},
                    ]))
                    self.assert_error(response, rpc=not binding.startswith("/"), code="INVALID_FILE_ENCODING")
                    if not binding.startswith("/"):
                        self.assertEqual(response.json()["id"], "rpc-id")
                        self.assertEqual(response.json()["error"]["code"], -32602)
                    self.assertNotIn("private-name", response.text)
        self.assertEqual(self.messages, [])

    async def test_valid_raw_and_data_metadata_are_not_normalized(self):
        parts = [{"raw": "", "filename": "empty.txt"}, {"raw": "/w==", "filename": "original\u0000.txt", "mediaType": "application/octet-stream"},
                 {"data": {"raw": "YR==", "nested": {"raw": "not base64"}}}]
        response = await self.send("SendMessage", self.payload(parts))
        self.assertNotIn("error", response.json(), response.text)
        self.assertEqual(self.messages[0].attachments[0]["bytes"], b"")
        self.assertEqual(self.messages[0].attachments[1]["bytes"], b"\xff")
        self.assertEqual(self.messages[0].attachments[1]["filename"], "original\u0000.txt")
        self.assertEqual(json.loads(self.messages[0].prompt), {"raw": "YR==", "nested": {"raw": "not base64"}})

    async def test_streamed_actual_bytes_are_bounded_without_content_length(self):
        emitted = []

        async def chunks():
            for index in range(8):
                emitted.append(index)
                yield b" " * 262144

        for path in ("/message:send", "/message:stream", "/"):
            emitted.clear()
            response = await self.client.post(path, content=chunks(), headers={"A2A-Version": "1.0"})
            data = self.assert_error(response, rpc=path == "/", code="REQUEST_TOO_LARGE", status=413)
            self.assertEqual(data["allowed_bytes"], "524288")
            self.assertEqual(data["actual_bytes"], "786432")
            self.assertEqual(emitted, [0, 1, 2])
        self.assertEqual(self.messages, [])

    async def test_content_length_hint_and_compression_reject_before_reading(self):
        async def unread():
            self.fail("Rejected request body was consumed")
            yield b"unused"

        for path in ("/message:send", "/message:stream", "/"):
            response = await self.client.post(path, content=unread(), headers={"Content-Length": "524289"})
            self.assert_error(response, rpc=path == "/", code="REQUEST_TOO_LARGE", status=413)
            response = await self.client.post(path, content=unread(), headers={"Content-Encoding": "gzip"})
            self.assert_error(response, rpc=path == "/", code="CONTENT_TYPE_NOT_SUPPORTED")
        self.assertEqual(self.messages, [])

    async def test_malformed_json_utf8_duplicates_and_depth_have_safe_errors(self):
        bodies = (b"\xff", b"{", b'{"message":{},"message":{}}',
                  b'{"message":{"parts":[{"metadata":{"x":1,"x":2}}]}}',
                  b'{"message":{"parts":[{"data":NaN}]}}', b"[" * 1200 + b"0" + b"]" * 1200)
        for path in ("/message:send", "/message:stream", "/"):
            for body in bodies:
                with self.subTest(path=path, body=body[:50]):
                    response = await self.client.post(path, content=body, headers={"A2A-Version": "1.0"})
                    self.assert_error(response, rpc=path == "/", code="REQUEST_INVALID")
        self.assertEqual(self.messages, [])

    async def test_exact_body_ceiling_and_tolerant_rpc_envelope(self):
        path, payload = self.wire("SendMessage", self.payload())
        payload["contextId"] = "ignored"
        payload["raw"] = "YR=="
        encoded = json.dumps(payload).encode()
        response = await self.client.post(path, content=encoded + b" " * (524288 - len(encoded)), headers={"A2A-Version": "1.0"})
        self.assertNotIn("error", response.json(), response.text)
        self.assertNotIn("ignored", response.text)

    async def test_sdk_debug_logs_omit_request_and_task_payloads(self):
        secret = "private-input-material-never-log"
        raw = base64.b64encode(secret.encode()).decode()
        private_task = Task(history=[SdkMessage(parts=[{"raw": secret.encode()}])])
        def build(request):
            context = DefaultServerCallContextBuilder().build(request)
            context.state["private_task"] = private_task
            return context
        builder = SimpleNamespace(build=build)
        app = build_starlette_app(**self.options, context_builder=builder)
        with self.assertLogs("a2a.server", logging.DEBUG) as logs:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://input.test") as client:
                path, body = self.wire("SendMessage", self.payload([{"raw": raw}]))
                response = await client.post(path, json=body, headers={"A2A-Version": "1.0"})
        self.assertNotIn("error", response.json(), response.text)
        self.assertNotIn(raw, "\n".join(logs.output))
        self.assertNotIn(secret, "\n".join(logs.output))

    async def test_sdk_schema_failure_logs_omit_file_bytes_and_exception_values(self):
        secret = "private-schema-material-never-log"
        raw = base64.b64encode(secret.encode()).decode()
        invalid_parts = ({"raw": raw, "text": secret}, {"raw": raw, "filename": {"private": raw}},
                         {"raw": raw, "mediaType": [raw]}, {"raw": raw, "metadata": [raw]})
        for binding in ("/message:send", "/message:stream", "SendMessage", "SendStreamingMessage"):
            for part in invalid_parts:
                with self.subTest(binding=binding, part=next(key for key in part if key != "raw")):
                    with self.assertLogs("a2a", logging.DEBUG) as logs:
                        response = await self.send(binding, self.payload([part]))
                    self.assertIn(response.status_code, (200, 400))
                    self.assertNotIn(raw, "\n".join(logs.output))
                    self.assertNotIn(secret, "\n".join(logs.output))
            payload = self.payload([{"raw": raw}])
            payload["message"]["role"] = raw
            with self.assertLogs("a2a", logging.DEBUG) as logs:
                await self.send(binding, payload)
            self.assertNotIn(raw, "\n".join(logs.output))
        self.assertEqual(self.messages, [])

    async def test_unknown_rpc_field_names_are_not_logged_or_retained(self):
        from core_agent.a2a_sdk import _reported_extra_fields
        _reported_extra_fields.clear()
        payload = self.payload()
        path, body = self.wire("SendMessage", payload)
        name = "private-top-level-key-never-log"
        body[name] = "ignored"
        with self.assertLogs("core_agent.runtime", "WARNING") as logs:
            response = await self.client.post(path, json=body, headers={"A2A-Version": "1.0"})
        self.assertNotIn("error", response.json())
        self.assertNotIn(name, "\n".join(logs.output))
        self.assertTrue(all(type(marker) is int for marker in _reported_extra_fields))

    async def test_enterprise_raw_reaches_admission_and_url_is_still_refused(self):
        calls = []
        async def admission(*_args):
            calls.append(_args[0])
            raise CoreError("INVALID_FILE_INPUT")

        app = build_starlette_app(**self.options, admission_handler=admission)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://input.test") as client:
            response = await client.post("/message:send", json=self.payload([{"raw": "YQ=="}]), headers={"A2A-Version": "1.0"})
            rejected = await client.post("/message:send", json=self.payload([{"url": "https://example.test/private"}]), headers={"A2A-Version": "1.0"})
        self.assert_error(response, code="INVALID_FILE_INPUT")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].parts[0].raw, b"a")
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(self.messages, [])

    async def test_authentication_and_role_scope_precede_body_consumption(self):
        async def authenticate(_token):
            return Principal("external", "company", False, True)

        app = AuthenticationMiddleware(Starlette(routes=[Mount("/a2a/owner", app=self.app)]),
            authenticator=SimpleNamespace(authenticate=authenticate))

        async def unread():
            self.fail("Unauthorized body was consumed")
            yield b"unused"

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://input.test") as client:
            for headers, status in (({}, 401), ({"Authorization": "Bearer external"}, 403)):
                response = await client.post("/a2a/owner/message:send", content=unread(), headers=headers)
                self.assertEqual(response.status_code, status)
        self.assertEqual(self.messages, [])

    async def test_file_admission_errors_use_bounded_sdk_metadata_on_root_and_followup(self):
        for code in ("INVALID_FILE_INPUT", "INVALID_FILE_ENCODING", "FILE_METADATA_TOO_LARGE", "ATTACHMENTS_TOO_LARGE"):
            async def admission(*_args):
                raise CoreError(code, "private exception details", data={"allowed_bytes": 25000000, "actual_bytes": 26000000, "raw": "private"})

            def followup(*_args, **_kwargs):
                raise CoreError(code, "private exception details", data={"allowed_bytes": 25000000, "actual_bytes": 26000000, "raw": "private"})

            app = build_starlette_app(**{**self.options, "followup_handler": followup}, admission_handler=admission)
            handler = app.state.a2a_request_handler
            task = Task(id="existing", context_id="chat", status=TaskStatus(state=TaskState.TASK_STATE_WORKING))
            await handler.task_store.save(task, ServerCallContext())
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://input.test") as client:
                for binding in ("/message:send", "SendMessage"):
                    for follow in (False, True):
                        payload = self.payload()
                        if follow:
                            payload["message"].update(taskId="existing", contextId="chat")
                        path, body = self.wire(binding, payload)
                        response = await client.post(path, json=body, headers={"A2A-Version": "1.0"})
                        metadata = self.assert_error(response, rpc=not binding.startswith("/"), code=code)
                        self.assertEqual(metadata, {"code": code, "allowed_bytes": "25000000", "actual_bytes": "26000000"})
                        self.assertNotIn("private", response.text)

    async def test_followup_receipt_is_transient_and_reused_without_changing_root(self):
        receipt = {"schema_version": 1, "batch_id": "accepted-batch", "total_bytes": 1,
                   "entries": [{"index": 0, "actual_name": "report_2.txt", "relative_path": "report_2.txt",
                                "size_bytes": 1, "sha256": "a" * 64}]}
        inbox = {"message_id": "input-id", "provenance": {"file_receipt": receipt}}
        app = build_starlette_app(**{**self.options, "followup_handler": lambda *_args, **_kwargs: inbox},
                                  admission_handler=lambda *_args: None)
        handler = app.state.a2a_request_handler
        context = ServerCallContext()
        task = Task(id="existing", context_id="chat", status=TaskStatus(state=TaskState.TASK_STATE_WORKING))
        task.metadata.update({"file_receipt": {"batch_id": "immutable-root"}})
        await handler.task_store.save(task, context)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://input.test") as client:
            for _attempt in range(2):
                payload = self.payload()
                payload["message"].update(taskId="existing", contextId="chat")
                response = await client.post("/message:send", json=payload, headers={"A2A-Version": "1.0"})
                metadata = response.json()["task"]["metadata"]
                self.assertEqual(metadata["accepted_message_id"], "input-id")
                self.assertEqual(metadata["accepted_file_receipt"], receipt)
                self.assertEqual(metadata["file_receipt"], {"batch_id": "immutable-root"})
        stored = await handler.task_store.get("existing", context)
        self.assertEqual(dict(stored.metadata), {"file_receipt": {"batch_id": "immutable-root"}})
        self.assertEqual(list(stored.history), [])

    async def test_request_limit_environment_is_validated_at_composition(self):
        for value in ("524287", "2147483648", "no", "1.5", "-1"):
            with self.subTest(value=value), patch.dict(os.environ, {"A2A_MAX_REQUEST_BYTES": value}), self.assertRaises(CoreError) as raised:
                build_starlette_app(**self.options)
            self.assertEqual(raised.exception.code, "CONFIG_INVALID")

    async def test_default_blank_limit_and_json_nesting_bound(self):
        from core_agent.a2a_input import request_limit
        for value in ("", " ", "\t\n"):
            with patch.dict(os.environ, {"A2A_MAX_REQUEST_BYTES": value}):
                self.assertEqual(request_limit(), 40000000)
        for value in (b"[" * 101 + b"0" + b"]" * 101, b'{"data":1e999}'):
            response = await self.client.post("/message:send", content=value)
            self.assert_error(response, code="REQUEST_INVALID")
