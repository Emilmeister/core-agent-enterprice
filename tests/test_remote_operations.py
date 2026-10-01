import io
import json
import threading
import unittest
import uuid
from dataclasses import replace
from types import SimpleNamespace
from urllib.error import HTTPError
from unittest.mock import patch

from core_agent.errors import CoreError
from core_agent.remote_agents import RemoteAgentConnection, RemoteEvent
from core_agent.remote_registry import InMemoryRemoteRegistry
from core_agent.tasks import REMOTE_TASK_PENDING, TaskScheduler, remote_timeout_result

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
