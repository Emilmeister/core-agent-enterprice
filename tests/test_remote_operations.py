import io
import base64
import json
import os
import tempfile
import threading
import unittest
import uuid
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError
from unittest.mock import patch

from core_agent.artifacts import InMemoryArtifactStore, PostgresArtifactStore
from core_agent.database import PostgresDatabase
from core_agent.errors import CoreError, ExecutionNotStarted
from core_agent.postgres_tasks import PostgresTaskScheduler
from core_agent.remote_agents import RemoteAgentConnection, RemoteEvent
from core_agent.remote_registry import InMemoryRemoteRegistry
from core_agent.response_files import ResponseFileService
from core_agent.tasks import REMOTE_TASK_PENDING, TaskScheduler, _remote_contract, remote_timeout_result
from core_agent.workflow import InMemoryWorkflowStore, WorkflowRecord
from core_agent.workspace import ChatWorkspaces, WorkspaceBinding

try:
    from core_agent.remote_operations import RemoteA2AExecutor
except ModuleNotFoundError:
    RemoteA2AExecutor = None


class RemoteOperationTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(RemoteA2AExecutor, "Durable A2A executor is missing")
        self.now = 1000.0
        self.tenant, self.owner = "company", "root-run"
        self.scheduler = TaskScheduler(clock=lambda: self.now)
        self.addCleanup(self.scheduler.close)
        self.registry = InMemoryRemoteRegistry()
        self.peer = self.registry.create(self.tenant, {
            "name": "delivery", "url": "https://peer.example/a2a", "description": "Delivery",
            "enabled": True, "header_name": "Authorization", "header_value": "Bearer private-key",
        }, actor_id="owner")
        self.contract = {
            "version": 1, "tenant_id": self.tenant, "owner_id": self.owner,
            "peer_id": self.peer["id"], "peer_revision": self.peer["revision"],
            "peer_name": self.peer["name"], "url": self.peer["url"], "binding": "HTTP+JSON",
            "message_id": str(uuid.uuid4()), "task": "Deliver order", "timeout_seconds": 30,
            "poll_interval_seconds": 5,
        }
        self.calls, self.responses = [], []
        self.executor = RemoteA2AExecutor(self.scheduler, self.registry)
        self.scheduler.register("remote_a2a", self.executor)
        transport = patch("core_agent.remote_operations.RemoteAgentConnection", side_effect=self.connection)
        transport.start()
        self.addCleanup(transport.stop)

    def connection(self, card, **options):
        def request(method, **arguments):
            self.calls.append((method, card, arguments))
            self.assertTrue(self.responses, "Unexpected network request")
            value = self.responses.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value() if callable(value) else value
        return SimpleNamespace(send_task=lambda **kw: request("Send", **kw),
                               get_task=lambda **kw: request("Get", **kw),
                               cancel_task=lambda **kw: request("Cancel", **kw))

    def event(self, state="WORKING", *, text="progress", kind="task", files=False):
        parts = ({"raw": "eA==", "filename": "result.txt"},) if files else ({"text": text},)
        return RemoteEvent(kind, "TASK_STATE_" + state if state else None, text,
                           state in {"COMPLETED", "FAILED", "CANCELED", "REJECTED"} or kind == "message",
                           parts, "remote-task" if kind == "task" else None,
                           "remote-context" if kind == "task" else None)

    def settle(self):
        with self.scheduler._lock:
            threads = tuple(self.scheduler._threads)
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive(), "Worker did not release its claim")

    def start(self, **changes):
        task = self.scheduler.start_remote({**self.contract, **changes}, owner_id=self.owner,
                                          tenant_id=self.tenant, task_id=str(uuid.uuid4()))
        self.settle()
        return task

    def recover(self, advance=5):
        self.now += advance
        count = self.scheduler.recover()
        self.settle()
        return count

    def outbound_files(self, *, child=False):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        workspaces = ChatWorkspaces(directory.name)
        workflows = InMemoryWorkflowStore()
        root = workflows.create(WorkflowRecord(str(uuid.uuid4()), str(uuid.uuid4()), "source-chat",
            self.tenant, "source-person", None, "CREATED", 1, {"prompt": "root"}, {}))
        source = (workflows.create(WorkflowRecord(str(uuid.uuid4()), str(uuid.uuid4()), root.context_id,
            root.tenant_id, root.owner_id, root.run_id, "CREATED", 1, {"prompt": "child"}, {})) if child else root)
        self.owner = source.run_id
        binding = WorkspaceBinding(source.tenant_id, source.owner_id, source.context_id)
        folder = workspaces.workspace(binding)
        (folder / "binary.bin").write_bytes(b"\0\xff\x80")
        (folder / "empty.txt").write_bytes(b"")
        store = (PostgresArtifactStore(self.database, directory.name) if hasattr(self, "database")
                 else InMemoryArtifactStore())
        service = ResponseFileService(workspaces, store)
        refs = service.prepare(binding, ["binary.bin", "empty.txt"], task_id=source.task_id,
            run_id=source.run_id, limit_bytes=3)
        self.contract = {**self.contract, "version": 2, "owner_id": source.run_id,
            "caller_scope": {key: getattr(source, key) for key in ("owner_id", "context_id", "task_id", "run_id")},
            "attachment_limit_bytes": 3, "outgoing_files": list(refs)}
        self.executor = RemoteA2AExecutor(self.scheduler, self.registry, service)
        self.scheduler._handlers["remote_a2a"] = self.executor
        return folder, refs, service, source

    def test_v2_contract_validates_exact_source_scope_refs_and_uniform_pinned_limit(self):
        for child in (False, True):
            with self.subTest(child=child):
                _folder, refs, _service, source = self.outbound_files(child=child)
                try:
                    _remote_contract(self.contract, self.tenant, self.owner)
                except CoreError as error:
                    self.fail("Valid originating WorkflowRecord contract rejected: " + error.code)
                self.assertEqual(self.contract["caller_scope"]["task_id"], source.task_id)
                for field in ("tenant_id", "owner_id", "context_id", "task_id", "run_id"):
                    forged = [refs[0], {**refs[1], field: "foreign"}]
                    with self.assertRaises(CoreError) as error:
                        _remote_contract({**self.contract, "outgoing_files": forged}, self.tenant, self.owner)
                    self.assertEqual(error.exception.code, "CHECKPOINT_INVALID")
                for changes in ({"version": 3}, {"version": True}, {"attachment_limit_bytes": True},
                        {"attachment_limit_bytes": 4}, {"attachment_limit_bytes": 2},
                        {"outgoing_files": tuple(refs)}, {"outgoing_files": None},
                        {"caller_scope": {**self.contract["caller_scope"], "root_run_id": "grant"}}):
                    with self.assertRaises(CoreError) as error:
                        _remote_contract({**self.contract, **changes}, self.tenant, self.owner)
                    self.assertEqual(error.exception.code, "CHECKPOINT_INVALID")
        self.assertEqual(self.calls, [])

    def test_v1_contract_rejects_v2_fields_and_unknown_versions(self):
        for changes in ({"caller_scope": {}}, {"outgoing_files": []}, {"attachment_limit_bytes": 3}, {"version": 7}):
            with self.assertRaises(CoreError) as error:
                _remote_contract({**self.contract, **changes}, self.tenant, self.owner)
            self.assertEqual(error.exception.code, "CHECKPOINT_INVALID")
        self.assertEqual(self.calls, [])

    def test_v2_send_and_recovery_use_frozen_source_files_and_pinned_limit(self):
        folder, refs, _service, source = self.outbound_files(child=True)
        (folder / "binary.bin").write_bytes(b"changed source")
        (folder / "empty.txt").unlink()
        self.responses.extend([self.event(), self.event("COMPLETED")])
        task = self.start()
        self.recover()
        self.assertEqual(task.state, "completed")
        sent = self.calls[0][2]
        self.assertEqual(sent["files"], ({"name": "binary.bin", "media_type": "application/octet-stream", "raw": b"\0\xff\x80"},
            {"name": "empty.txt", "media_type": "text/plain", "raw": b""}))
        self.assertEqual(sent["attachment_limit_bytes"], 3)
        self.assertEqual(self.calls[1][2]["attachment_limit_bytes"], 3)
        self.assertNotIn("files", self.calls[1][2])
        self.assertEqual(self.scheduler._remote[task.id]["contract"]["outgoing_files"], list(refs))
        self.assertEqual(self.contract["caller_scope"]["run_id"], source.run_id)
        self.assertNotEqual(sent["message_id"], source.task_id)

    def test_v2_corrupt_last_blob_and_missing_service_fail_before_send_marker(self):
        _folder, refs, service, _source = self.outbound_files()
        original = service.artifact_store.get
        def corrupt_last(tenant, blob_id, **kwargs):
            metadata, content = original(tenant, blob_id, **kwargs)
            return metadata, b"changed" if blob_id == refs[-1]["blob_id"] else content
        with patch.object(service.artifact_store, "get", side_effect=corrupt_last):
            task = self.start()
        self.assertEqual((task.state, task.error.code), ("failed", "ARTIFACT_INTEGRITY_FAILED"))
        self.assertFalse(self.scheduler._remote[task.id]["checkpoint"]["send_started"])
        self.executor.response_files_service = None
        missing = self.start()
        self.assertEqual((missing.state, missing.error.code), ("failed", "ARTIFACT_INTEGRITY_FAILED"))
        self.assertFalse(self.scheduler._remote[missing.id]["checkpoint"]["send_started"])
        self.assertEqual(self.calls, [])

    def test_v2_cancel_uses_pinned_limit_and_received_files_still_fail_explicitly(self):
        self.outbound_files()
        self.responses.extend([self.event(), self.event("CANCELED"), self.event("COMPLETED", files=True)])
        task = self.start()
        self.scheduler.cancel(task.id, owner_id=self.owner, tenant_id=self.tenant)
        self.recover(0)
        self.assertEqual(task.state, "canceled")
        self.assertEqual(self.calls[1][2]["attachment_limit_bytes"], 3)
        rejected = self.start()
        self.assertEqual((rejected.state, rejected.error.code), ("failed", "REMOTE_FILES_UNSUPPORTED"))

    def test_v2_early_files_preserve_working_and_human_waits_without_import_or_relay(self):
        self.outbound_files()
        previews = [replace(self.event(state, files=True), parts=({
            "raw": "eA==", "filename": "PRIVATE-preview.txt", "mediaType": "text/plain"},))
            for state in ("WORKING", "INPUT_REQUIRED", "AUTH_REQUIRED")]
        self.responses.extend([*previews, self.event("COMPLETED", text="Finished normally")])
        task = self.start()
        for offset, state in enumerate(("WORKING", "INPUT_REQUIRED", "AUTH_REQUIRED")):
            with self.subTest(state=state):
                if offset:
                    self.assertEqual(self.recover(), 1)
                self.assertEqual(task.state, "working", getattr(task.error, "code", None))
                self.assertEqual(task.result, {"agent_name": "delivery", "remote_state": "TASK_STATE_" + state})
                checkpoint = self.scheduler._remote[task.id]["checkpoint"]
                self.assertEqual(checkpoint["deadline"], 1030)
                self.assertEqual((checkpoint["remote_task_id"], checkpoint["remote_context_id"]),
                    ("remote-task", "remote-context"))
                self.assertNotIn("PRIVATE-preview", json.dumps({"checkpoint": checkpoint, "result": task.result}))
                self.assertNotIn("eA==", json.dumps(task.result))
                self.assertEqual(self.scheduler.mailbox(self.owner, self.tenant).poll(), ())
        self.assertEqual(self.recover(), 1)
        self.assertEqual((task.state, task.result["text"]), ("completed", "Finished normally"))
        self.assertEqual([call[0] for call in self.calls], ["Send", "Get", "Get", "Get"])
        self.assertTrue(all(call[2]["task_id"] == "remote-task" for call in self.calls[1:]))
        self.assertEqual(self.scheduler._remote[task.id]["checkpoint"]["deadline"], 1030)
        self.assertNotIn("PRIVATE-preview", json.dumps(task.result))
        self.assertEqual(len(self.scheduler.mailbox(self.owner, self.tenant).poll()), 1)

    def test_v1_early_files_keep_explicit_unsupported_failure(self):
        for state in ("WORKING", "INPUT_REQUIRED", "AUTH_REQUIRED"):
            with self.subTest(state=state):
                self.responses.append(self.event(state, files=True))
                task = self.start()
                self.assertEqual((task.state, task.error.code), ("failed", "REMOTE_FILES_UNSUPPORTED"))

    def test_actual_malformed_redirect_after_received_send_stays_unknown_in_both_bindings(self):
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
        peer = self.registry.create(self.tenant, {
            "name": "redirect-peer", "description": "Fixture", "url": f"http://127.0.0.1:{server.server_port}/",
            "enabled": True, "header_name": "Authorization", "header_value": "Bearer private-key",
        }, actor_id="owner")
        self.contract.update(peer_id=peer["id"], peer_revision=peer["revision"], peer_name=peer["name"], url=peer["url"])
        self.outbound_files()
        for binding in ("JSONRPC", "HTTP+JSON"):
            server.received = []
            with self.subTest(binding=binding), patch("core_agent.remote_operations.RemoteAgentConnection", RemoteAgentConnection):
                task = self.start(binding=binding, message_id=str(uuid.uuid4()))
                self.assertEqual((task.state, task.error.code), ("failed", "SIDE_EFFECT_UNKNOWN"))
                self.assertEqual(task.result["remote_outcome"], "unknown")
                self.assertNotIn("private-key", json.dumps(task.result))
                self.assertEqual(len(server.received), 1)
                body = server.received[0]
                body = body["params"] if binding == "JSONRPC" else body
                self.assertEqual([base64.b64decode(part["raw"]) for part in body["message"]["parts"][1:]],
                    [b"\0\xff\x80", b""])
                self.assertEqual(self.recover(), 0)
                self.assertEqual(len(server.received), 1)

    def test_transport_proven_predispatch_error_is_known_failure_after_send_marker(self):
        self.responses.append(ExecutionNotStarted("ATTACHMENTS_TOO_LARGE", data={"allowed_bytes": 1, "actual_bytes": 2}))
        task = self.start()
        self.assertEqual((task.state, task.error.code), ("failed", "ATTACHMENTS_TOO_LARGE"))
        self.assertNotEqual(task.result.get("remote_outcome"), "unknown")
        self.assertEqual(self.recover(), 0)

    def test_v2_frozen_files_use_actual_wire_in_both_bindings(self):
        folder, _refs, _service, source = self.outbound_files(child=True)
        (folder / "binary.bin").unlink()
        (folder / "empty.txt").write_bytes(b"changed")
        for binding in ("JSONRPC", "HTTP+JSON"):
            wire = []
            def response(request, timeout):
                payload = json.loads(request.data)
                body = payload["params"] if binding == "JSONRPC" else payload
                wire.append(body)
                message = body["message"]
                self.assertEqual(set(message), {"role", "messageId", "parts"})
                self.assertEqual(message["parts"][0], {"text": "Deliver order"})
                files = message["parts"][1:]
                self.assertEqual([base64.b64decode(part["raw"]) for part in files], [b"\0\xff\x80", b""])
                self.assertEqual([part["filename"] for part in files], ["binary.bin", "empty.txt"])
                self.assertTrue(all(set(part) == {"raw", "filename", "mediaType"} for part in files))
                self.assertNotIn(source.run_id, json.dumps(body))
                result = {"task": {"id": "wire-task", "contextId": "wire-context",
                    "status": {"state": "TASK_STATE_COMPLETED"}}}
                if binding == "JSONRPC":
                    result = {"jsonrpc": "2.0", "id": payload["id"], "result": result}
                return io.BytesIO(json.dumps(result).encode())
            with self.subTest(binding=binding), patch("core_agent.remote_operations.RemoteAgentConnection", RemoteAgentConnection), \
                    patch("core_agent.remote_agents._OPENER.open", side_effect=response):
                task = self.start(binding=binding, message_id=str(uuid.uuid4()))
                self.assertEqual(task.state, "completed", getattr(task.error, "code", None))
                self.assertEqual(len(wire), 1)

    def test_send_releases_worker_and_recovery_only_gets_pinned_remote_task(self):
        self.responses.extend([self.event(), self.event("COMPLETED", text="Delivered")])
        task = self.start()
        self.assertEqual(task.state, "working")
        self.assertEqual(self.scheduler._active, set())
        checkpoint = self.scheduler._remote[task.id]["checkpoint"]
        self.assertEqual((checkpoint["deadline"], checkpoint["next_poll_at"]), (1030, 1005))
        self.assertEqual((checkpoint["remote_task_id"], checkpoint["remote_context_id"]),
                         ("remote-task", "remote-context"))
        self.assertEqual(self.recover(0), 0)
        self.assertEqual(self.recover(), 1)
        self.assertEqual(task.state, "completed")
        self.assertEqual(task.result, {"agent_name": "delivery", "remote_state": "TASK_STATE_COMPLETED", "text": "Delivered"})
        self.assertEqual([call[0] for call in self.calls], ["Send", "Get"])
        sent = self.calls[0][2]
        self.assertEqual(sent["message_id"], self.contract["message_id"])
        self.assertEqual(sent["headers"], {"Authorization": "Bearer private-key"})
        self.assertEqual(set(sent), {"task", "message_id", "headers", "timeout"})
        self.assertEqual(self.calls[1][2]["task_id"], "remote-task")
        self.assertEqual(len(self.scheduler.mailbox(self.owner, self.tenant).poll()), 1)
        self.assertEqual(self.recover(), 0)

    def test_foreign_human_and_auth_waits_remain_working(self):
        self.responses.extend([self.event("INPUT_REQUIRED"), self.event("AUTH_REQUIRED"),
                               self.event("COMPLETED")])
        task = self.start()
        self.assertEqual(task.state, "working")
        self.assertEqual(task.result, {"agent_name": "delivery", "remote_state": "TASK_STATE_INPUT_REQUIRED"})
        self.recover()
        self.assertEqual(task.state, "working")
        self.assertEqual(task.result, {"agent_name": "delivery", "remote_state": "TASK_STATE_AUTH_REQUIRED"})
        self.assertEqual(self.scheduler.mailbox(self.owner, self.tenant).poll(), ())
        self.recover()
        self.assertEqual(task.state, "completed")

    def test_timeout_wins_over_inflight_response_and_no_network_reopens(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def delayed_result():
            entered.set()
            release.wait(3)
            return self.event("COMPLETED", text="late answer")
        self.responses.extend([self.event(), delayed_result])
        task = self.start()
        self.now += 5
        self.scheduler.recover()
        self.assertTrue(entered.wait(3))
        self.now = 1031
        self.assertEqual(self.scheduler.expire_remote(), 1)
        release.set()
        self.settle()
        self.assertEqual(task.state, "failed")
        self.assertEqual(task.error.code, "REMOTE_OPERATION_TIMEOUT")
        self.assertEqual(task.result, remote_timeout_result(self.contract))
        self.assertNotIn("late answer", str(task.result))
        self.scheduler.cancel(task.id, owner_id=self.owner, tenant_id=self.tenant)
        self.assertEqual(self.recover(), 0)
        self.assertEqual([call[0] for call in self.calls], ["Send", "Get"])
        self.assertEqual(len(self.scheduler.mailbox(self.owner, self.tenant).poll()), 1)

    def test_unknown_send_outcome_is_reconciliation_without_retry(self):
        self.responses.append(CoreError("REMOTE_AGENT_UNAVAILABLE", "private-key", retryable=True))
        task = self.start()
        self.assertEqual(task.state, "failed")
        self.assertEqual(task.error.code, "SIDE_EFFECT_UNKNOWN")
        self.assertEqual(task.result["remote_outcome"], "unknown")
        self.assertNotIn("private-key", str(task.result))
        self.assertEqual(self.recover(), 0)
        self.assertEqual(len(self.calls), 1)

    def test_recovery_of_send_marker_without_remote_id_does_not_send(self):
        def interrupted(claim, cancel):
            current = self.scheduler.read_remote_claim(claim)
            self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"],
                                              checkpoint={**current["checkpoint"], "send_started": True})
            return REMOTE_TASK_PENDING
        self.scheduler._handlers["remote_a2a"] = interrupted
        task = self.start()
        self.scheduler._handlers["remote_a2a"] = self.executor
        self.recover(0)
        self.assertEqual(task.error.code, "SIDE_EFFECT_UNKNOWN")
        self.assertEqual(self.calls, [])

    def test_explicit_cancel_before_send_does_not_dispatch(self):
        self.scheduler._handlers["remote_a2a"] = lambda *_: REMOTE_TASK_PENDING
        task = self.start()
        self.scheduler.cancel(task.id, owner_id=self.owner, tenant_id=self.tenant)
        self.scheduler._handlers["remote_a2a"] = self.executor
        self.recover(0)
        self.assertEqual(task.state, "canceled")
        self.assertEqual(self.calls, [])

    def test_cancel_known_task_once_and_unknown_cancel_never_retries(self):
        self.responses.extend([self.event(), CoreError("REMOTE_AGENT_UNAVAILABLE", retryable=True)])
        task = self.start()
        self.scheduler.cancel(task.id, owner_id=self.owner, tenant_id=self.tenant)
        self.recover(0)
        self.assertEqual(task.state, "failed")
        self.assertEqual(task.error.code, "SIDE_EFFECT_UNKNOWN")
        self.assertTrue(self.scheduler._remote[task.id]["checkpoint"]["cancel_started"])
        self.assertEqual(self.recover(), 0)
        self.assertEqual([call[0] for call in self.calls], ["Send", "Cancel"])

    def test_recovery_of_cancel_marker_never_repeats_cancel(self):
        self.responses.append(self.event())
        task = self.start()
        def interrupted(claim, cancel):
            current = self.scheduler.read_remote_claim(claim)
            self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"],
                                              checkpoint={**current["checkpoint"], "cancel_started": True})
            return REMOTE_TASK_PENDING
        self.scheduler.cancel(task.id, owner_id=self.owner, tenant_id=self.tenant)
        self.scheduler._handlers["remote_a2a"] = interrupted
        self.recover(0)
        self.scheduler._handlers["remote_a2a"] = self.executor
        self.recover(0)
        self.assertEqual(task.state, "failed")
        self.assertEqual(task.error.code, "SIDE_EFFECT_UNKNOWN")
        self.assertEqual([call[0] for call in self.calls], ["Send"])

    def test_registry_rotation_and_disable_preserve_approved_binding(self):
        self.responses.extend([self.event(), self.event("COMPLETED")])
        task = self.start()
        changed = self.registry.update(self.tenant, self.peer["id"], {
            "url": "https://different.example/a2a", "header_value": "Bearer rotated-key",
            "description": "Changed", "enabled": True, "header_name": "Authorization",
        }, expected_revision=1, actor_id="owner")
        self.registry.disable(self.tenant, self.peer["id"], expected_revision=changed["revision"], actor_id="owner")
        self.recover()
        self.assertEqual(task.state, "completed")
        for _, card, arguments in self.calls:
            self.assertEqual(card.url, self.contract["url"])
            self.assertEqual(arguments["headers"], {"Authorization": "Bearer private-key"})

    def test_destination_mismatch_and_missing_secret_fail_before_dispatch(self):
        task = self.start(url="https://wrong.example/a2a")
        self.assertEqual((task.state, task.error.code), ("failed", "REMOTE_AGENT_DENIED"))
        self.assertFalse(self.scheduler._remote[task.id]["checkpoint"]["send_started"])
        with patch.object(self.registry, "resolve_headers", side_effect=CoreError("REMOTE_AGENT_SECRET_UNAVAILABLE")):
            task = self.start()
        self.assertEqual(task.error.code, "REMOTE_AGENT_SECRET_UNAVAILABLE")
        self.assertEqual(self.calls, [])

    def test_immediate_message_completes_without_inventing_task_ids(self):
        self.responses.append(self.event(None, kind="message", text="Immediate answer"))
        task = self.start()
        self.assertEqual(task.state, "completed")
        self.assertIsNone(self.scheduler._remote[task.id]["checkpoint"]["remote_task_id"])
        self.assertEqual(self.recover(), 0)

    def test_files_are_rejected_explicitly_and_reflected_credentials_redacted(self):
        self.responses.append(self.event("COMPLETED", text="private-key Bearer private-key"))
        completed = self.start()
        self.assertEqual(completed.state, "completed")
        self.assertNotIn("private-key", str(completed.result))
        self.responses.append(self.event("COMPLETED", files=True))
        rejected = self.start()
        self.assertEqual((rejected.state, rejected.error.code), ("failed", "REMOTE_FILES_UNSUPPORTED"))
        self.assertNotIn("eA==", str(rejected.result))

    def test_read_retry_keeps_original_deadline_and_shutdown_does_not_cancel(self):
        self.responses.extend([self.event(), CoreError("REMOTE_AGENT_UNAVAILABLE", retryable=True)])
        task = self.start()
        self.recover()
        self.assertEqual(task.state, "working")
        self.assertEqual(self.scheduler._remote[task.id]["checkpoint"]["deadline"], 1030)
        self.scheduler.close()
        self.assertEqual(self.recover(), 0)
        self.assertEqual(task.state, "working")
        self.assertEqual([call[0] for call in self.calls], ["Send", "Get"])

    def test_claim_and_deadline_are_rechecked_after_resolving_credentials(self):
        self.responses.append(self.event())
        task = self.start()
        resolve = self.registry.resolve_headers
        def delayed_headers(*args, **kwargs):
            self.now = 1031
            return resolve(*args, **kwargs)
        with patch.object(self.registry, "resolve_headers", side_effect=delayed_headers):
            self.recover()
        self.assertEqual(task.error.code, "REMOTE_OPERATION_TIMEOUT")
        self.assertEqual([call[0] for call in self.calls], ["Send"])

    def test_confirmed_cancel_and_terminal_remote_errors_keep_the_actual_outcome(self):
        for remote_state, local_state, error_code in (("CANCELED", "canceled", None),
                ("COMPLETED", "completed", None), ("FAILED", "failed", "REMOTE_TASK_FAILED"),
                ("REJECTED", "failed", "REMOTE_TASK_REJECTED")):
            with self.subTest(remote_state=remote_state):
                self.responses.extend([self.event(), self.event(remote_state)])
                task = self.start()
                self.scheduler.cancel(task.id, owner_id=self.owner, tenant_id=self.tenant)
                self.recover(0)
                self.assertEqual((task.state, getattr(task.error, "code", None)), (local_state, error_code))
                self.assertEqual([call[0] for call in self.calls[-2:]], ["Send", "Cancel"])

    def test_cancel_during_send_commits_remote_id_then_cancels_on_the_next_claim(self):
        def cancel_while_sending():
            task = self.scheduler.list(owner_id=self.owner, tenant_id=self.tenant)[0]
            self.scheduler.cancel(task.id, owner_id=self.owner, tenant_id=self.tenant)
            return self.event()
        self.responses.extend([cancel_while_sending, self.event("CANCELED")])
        task = self.start()
        self.assertEqual(task.state, "working")
        self.assertEqual([call[0] for call in self.calls], ["Send"])
        self.recover(0)
        self.assertEqual(task.state, "canceled")
        self.assertEqual([call[0] for call in self.calls], ["Send", "Cancel"])

    def test_unknown_credential_error_is_safe_and_never_dispatches(self):
        with patch.object(self.registry, "resolve_headers", side_effect=RuntimeError("private-key")):
            task = self.start()
        self.assertEqual(task.state, "failed")
        self.assertEqual(task.error.code, "REMOTE_AGENT_UNAVAILABLE")
        self.assertNotIn("private-key", str(task.result))
        self.assertEqual(self.calls, [])

    def test_structured_parts_are_not_silently_dropped(self):
        self.responses.append(RemoteEvent("message", None, "", True, ({"data": {"result": 42}},)))
        task = self.start()
        self.assertEqual((task.state, getattr(task.error, "code", None)), ("failed", "REMOTE_PARTS_UNSUPPORTED"))

    def test_reflected_credentials_in_remote_ids_never_enter_checkpoint(self):
        for kind, state, files in (("task", "WORKING", False), ("task", "COMPLETED", False),
                                   ("message", None, False), ("task", "COMPLETED", True)):
            for key in ("task_id", "context_id"):
                for secret in ("private-key", "Bearer private-key"):
                    with self.subTest(kind=kind, state=state, files=files, key=key, secret=secret):
                        event = self.event(state, kind=kind, files=files)
                        self.responses.append(replace(event, **{key: secret}))
                        task = self.start()
                        self.assertEqual((task.state, getattr(task.error, "code", None)), ("failed", "SIDE_EFFECT_UNKNOWN"))
                        self.assertEqual(task.result["remote_outcome"], "unknown")
                        self.assertNotIn("private-key", repr(self.scheduler._remote[task.id]["checkpoint"]))
                        self.assertNotIn("private-key", repr(task.result))

    def test_reflected_credentials_in_get_ids_cannot_replace_known_identity(self):
        self.responses.extend([self.event(), replace(self.event("COMPLETED"), context_id="private-key")])
        task = self.start()
        self.recover()
        self.assertEqual((task.state, getattr(task.error, "code", None)), ("failed", "REMOTE_AGENT_PROTOCOL_ERROR"))
        self.assertNotIn("private-key", repr(self.scheduler._remote[task.id]["checkpoint"]))
        self.assertNotIn("private-key", repr(task.result))

    def test_actual_wire_send_get_and_temporary_http_retry_in_both_bindings(self):
        for binding in ("JSONRPC", "HTTP+JSON"):
            wire = []
            def response(request, timeout):
                payload = json.loads(request.data) if request.data else {}
                method = payload.get("method") or ("GetTask" if request.method == "GET" else "SendMessage")
                wire.append((method, request, payload, timeout))
                task = next(task for task in self.scheduler.list(owner_id=self.owner, tenant_id=self.tenant)
                            if task.state == "working")
                checkpoint = self.scheduler._remote[task.id]["checkpoint"]
                self.assertTrue(checkpoint["send_started"], "Intent must be durable before physical Send")
                self.assertEqual(checkpoint["deadline"], self.now - (len(wire) - 1) * 5 + 30)
                if len(wire) == 2:
                    raise HTTPError(request.full_url, 503, "PRIVATE failure", {}, None)
                remote = {"id": "wire-task", "contextId": "wire-context",
                          "status": {"state": "TASK_STATE_WORKING" if method == "SendMessage" else "TASK_STATE_COMPLETED"}}
                if method == "SendMessage":
                    body = payload["params"] if binding == "JSONRPC" else payload
                    self.assertEqual(body["configuration"], {"returnImmediately": True})
                    self.assertEqual(set(body["message"]), {"role", "messageId", "parts"})
                    self.assertNotEqual(body["message"]["messageId"], self.owner)
                    self.assertIsNone(checkpoint["remote_task_id"])
                    result = {"task": remote}
                else:
                    self.assertEqual(checkpoint["remote_task_id"], "wire-task")
                    remote["artifacts"] = [{"artifactId": "answer", "parts": [{"text": "Delivered"}]}]
                    result = remote
                headers = {key.lower(): value for key, value in request.header_items()}
                self.assertEqual(headers["authorization"], "Bearer private-key")
                self.assertLessEqual(timeout, 30)
                if binding == "JSONRPC":
                    result = {"jsonrpc": "2.0", "id": payload["id"], "result": result}
                return io.BytesIO(json.dumps(result).encode())
            with self.subTest(binding=binding), patch("core_agent.remote_operations.RemoteAgentConnection", RemoteAgentConnection), patch(
                    "core_agent.remote_agents._OPENER.open", side_effect=response):
                task = self.start(binding=binding, message_id=str(uuid.uuid4()))
                self.recover()
                self.assertEqual(task.state, "working")
                self.recover()
                self.assertEqual((task.state, task.result["text"]), ("completed", "Delivered"))
                self.assertEqual([call[0] for call in wire], ["SendMessage", "GetTask", "GetTask"])
                self.assertEqual(self.scheduler._remote[task.id]["checkpoint"]["deadline"], self.now + 20)


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "TEST_DATABASE_URL required for remote outbound durability")
class PostgresRemoteOutboundTests(unittest.TestCase):
    connection = RemoteOperationTests.connection
    event = RemoteOperationTests.event
    settle = RemoteOperationTests.settle
    outbound_files = RemoteOperationTests.outbound_files

    def setUp(self):
        RemoteOperationTests.setUp(self)
        self.database = PostgresDatabase(os.environ["TEST_DATABASE_URL"], min_size=0, max_size=1)
        self.addCleanup(self.database.close)
        self.task_ids, self.blob_ids = [], []
        self.addCleanup(self.cleanup_rows)
        self.scheduler = PostgresTaskScheduler(self.database)
        self.addCleanup(self.scheduler.close)
        self.executor = RemoteA2AExecutor(self.scheduler, self.registry)
        self.scheduler.register("remote_a2a", self.executor)

    def cleanup_rows(self):
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM core_notifications WHERE task_id = ANY(%s)", (self.task_ids,))
            connection.execute("DELETE FROM core_background_tasks WHERE id = ANY(%s)", (self.task_ids,))
            connection.execute("DELETE FROM core_artifacts WHERE tenant_id=%s AND id = ANY(%s)", (self.tenant, self.blob_ids))

    def start(self):
        task = self.scheduler.start_remote(self.contract, owner_id=self.owner, tenant_id=self.tenant, task_id=str(uuid.uuid4()))
        self.task_ids.append(task.id)
        self.settle()
        return self.scheduler.get(task.id, owner_id=self.owner, tenant_id=self.tenant)

    def test_native_v2_root_and_actual_child_send_frozen_blobs_and_reject_corrupt_last_before_marker(self):
        for child in (False, True):
            with self.subTest(child=child):
                folder, refs, service, source = self.outbound_files(child=child)
                self.blob_ids.extend(ref["blob_id"] for ref in refs)
                (folder / "binary.bin").unlink()
                (folder / "empty.txt").write_bytes(b"changed")
                self.responses.append(self.event("COMPLETED"))
                task = self.start()
                self.assertEqual(task.state, "completed", getattr(task.error, "code", None))
                self.assertEqual([part["raw"] for part in self.calls[-1][2]["files"]], [b"\0\xff\x80", b""])
                with self.database.pool.connection() as connection:
                    row = connection.execute("SELECT contract,checkpoint FROM core_background_tasks WHERE id=%s AND tenant_id=%s",
                        (task.id, self.tenant)).fetchone()
                self.assertEqual(row["contract"]["caller_scope"]["run_id"], source.run_id)
                self.assertEqual(row["contract"]["caller_scope"]["task_id"], source.task_id)
                self.assertEqual(row["contract"]["outgoing_files"], list(refs))
                self.assertTrue(row["checkpoint"]["send_started"])
                calls = len(self.calls)
                original = service.artifact_store.get
                def corrupt_last(tenant, blob_id, **kwargs):
                    metadata, content = original(tenant, blob_id, **kwargs)
                    return metadata, b"changed" if blob_id == refs[-1]["blob_id"] else content
                with patch.object(service.artifact_store, "get", side_effect=corrupt_last):
                    rejected = self.start()
                self.assertEqual((rejected.state, rejected.error.code), ("failed", "ARTIFACT_INTEGRITY_FAILED"))
                with self.database.pool.connection() as connection:
                    checkpoint = connection.execute("SELECT checkpoint FROM core_background_tasks WHERE id=%s AND tenant_id=%s",
                        (rejected.id, self.tenant)).fetchone()["checkpoint"]
                self.assertFalse(checkpoint["send_started"])
                self.assertEqual(len(self.calls), calls)
