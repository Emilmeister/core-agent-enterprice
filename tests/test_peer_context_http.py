import json
import queue
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core_agent.remote_operations import RemoteA2AExecutor
from core_agent.remote_registry import InMemoryRemoteRegistry
from core_agent.tasks import TaskScheduler


class PeerContextHttpTests(unittest.TestCase):
    def setUp(self):
        self.received = []
        self.responses = queue.Queue()
        test = self

        class Peer(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                test.received.append((self.path, payload, self.headers.get("Authorization")))
                try:
                    result = test.responses.get_nowait()
                except queue.Empty:
                    self.send_error(500, "Unexpected request")
                    return
                if "jsonrpc" in payload:
                    result = {"jsonrpc": "2.0", "id": payload["id"], "result": result}
                encoded = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Peer)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def scheduler(self, binding):
        tenant = str(uuid.uuid4())
        registry = InMemoryRemoteRegistry()
        peer = registry.create(tenant, {"name": "peer", "description": "HTTP fixture", "enabled": True,
            "url": f"http://127.0.0.1:{self.server.server_port}/a2a",
            "header_name": "Authorization", "header_value": "Bearer private-wire-key"}, actor_id="owner")
        scheduler = TaskScheduler()
        scheduler.register("remote_a2a", RemoteA2AExecutor(scheduler, registry))
        self.addCleanup(scheduler.close)
        contract = {"version": 3, "tenant_id": tenant, "peer_id": peer["id"],
            "peer_revision": peer["revision"], "peer_name": peer["name"], "url": peer["url"], "binding": binding,
            "timeout_seconds": 30, "poll_interval_seconds": 5, "outgoing_files": [], "attachment_limit_bytes": 1024,
            "conversation": {"version": 1, "owner_id": "local-owner", "context_id": "local-chat",
                "generation": registry.connection_generation(tenant, peer["id"], peer["revision"])}}
        return scheduler, contract

    @staticmethod
    def response(kind, context_id):
        if kind == "message":
            return {"message": {"messageId": str(uuid.uuid4()), "role": "ROLE_AGENT", "parts": [{"text": "Done"}],
                                **({"contextId": context_id} if context_id is not None else {})}}
        return {"task": {"id": str(uuid.uuid4()), "contextId": context_id,
                         "status": {"state": "TASK_STATE_FAILED" if kind == "failed_task" else "TASK_STATE_COMPLETED"}}}

    def start(self, scheduler, base, response=None):
        if response is not None:
            self.responses.put(response)
        owner = str(uuid.uuid4())
        contract = {**base, "owner_id": owner, "message_id": str(uuid.uuid4()), "task": "New focused task",
                    "caller_scope": {"owner_id": "local-owner", "context_id": "local-chat",
                                     "task_id": str(uuid.uuid4()), "run_id": owner}}
        task = scheduler.start_remote(contract, owner_id=owner, tenant_id=base["tenant_id"], task_id=str(uuid.uuid4()))
        with scheduler._lock:
            workers = tuple(scheduler._threads)
        for worker in workers:
            worker.join(3)
            self.assertFalse(worker.is_alive(), "HTTP operation did not settle")
        return task, scheduler._remote[task.id]["checkpoint"]

    def wire_messages(self, binding, start):
        messages = []
        for path, payload, authorization in self.received[start:]:
            self.assertEqual(authorization, "Bearer private-wire-key")
            self.assertEqual(path, "/a2a" if binding == "JSONRPC" else "/a2a/message:send")
            if binding == "JSONRPC":
                self.assertEqual(payload["method"], "SendMessage")
                payload = payload["params"]
            self.assertTrue(payload["configuration"]["returnImmediately"])
            message = payload["message"]
            self.assertNotIn("taskId", message)
            self.assertNotIn("local-chat", json.dumps(payload))
            messages.append(message)
        self.assertEqual(len({message["messageId"] for message in messages}), len(messages))
        return messages

    def test_new_operations_reuse_task_or_message_context_in_both_bindings(self):
        for binding in ("JSONRPC", "HTTP+JSON"):
            for kind in ("task", "failed_task", "message"):
                with self.subTest(binding=binding, kind=kind):
                    scheduler, contract = self.scheduler(binding)
                    start, remote_context = len(self.received), str(uuid.uuid4())
                    first, first_checkpoint = self.start(scheduler, contract, self.response(kind, remote_context))
                    self.assertEqual(first.state, "failed" if kind == "failed_task" else "completed")
                    self.assertEqual(first_checkpoint["remote_context_id"], remote_context)
                    if kind == "message":
                        self.assertIsNone(first_checkpoint["remote_task_id"])
                    second, checkpoint = self.start(scheduler, contract, self.response("task", remote_context))
                    self.assertEqual(second.state, "completed", getattr(second.error, "code", None))
                    self.assertEqual(checkpoint["remote_context_id"], remote_context)
                    self.assertNotEqual(checkpoint["remote_task_id"], first_checkpoint["remote_task_id"])
                    messages = self.wire_messages(binding, start)
                    self.assertEqual(len(messages), 2)
                    self.assertNotIn("contextId", messages[0])
                    self.assertEqual(messages[1]["contextId"], remote_context)

    def test_direct_message_omitting_context_preserves_it_for_next_operation(self):
        for binding in ("JSONRPC", "HTTP+JSON"):
            with self.subTest(binding=binding):
                scheduler, contract = self.scheduler(binding)
                start, remote_context = len(self.received), str(uuid.uuid4())
                first, _ = self.start(scheduler, contract, self.response("message", remote_context))
                omitted, checkpoint = self.start(scheduler, contract, self.response("message", None))
                self.assertEqual((first.state, omitted.state), ("completed", "completed"))
                self.assertEqual(checkpoint["remote_context_id"], remote_context)
                self.assertIsNone(checkpoint["remote_task_id"])
                last, _ = self.start(scheduler, contract, self.response("task", remote_context))
                self.assertEqual(last.state, "completed", getattr(last.error, "code", None))
                messages = self.wire_messages(binding, start)
                self.assertEqual(len(messages), 3)
                self.assertEqual([message.get("contextId") for message in messages], [None, remote_context, remote_context])

    def test_replacement_context_is_rejected_without_retry_or_losing_saved_context(self):
        for binding in ("JSONRPC", "HTTP+JSON"):
            for kind in ("task", "message"):
                with self.subTest(binding=binding, kind=kind):
                    scheduler, contract = self.scheduler(binding)
                    start, remote_context = len(self.received), str(uuid.uuid4())
                    first, _ = self.start(scheduler, contract, self.response("task", remote_context))
                    self.assertEqual(first.state, "completed")
                    rejected, checkpoint = self.start(scheduler, contract, self.response(kind, str(uuid.uuid4())))
                    self.assertEqual((rejected.state, rejected.error.code), ("failed", "SIDE_EFFECT_UNKNOWN"))
                    self.assertEqual(checkpoint["remote_context_id"], remote_context)
                    self.assertIsNone(checkpoint["remote_task_id"])
                    blocked, _ = self.start(scheduler, contract)
                    self.assertEqual((blocked.state, blocked.error.code), ("failed", "REMOTE_CONTEXT_UNRESOLVED"))
                    messages = self.wire_messages(binding, start)
                    self.assertEqual(len(messages), 2)
                    self.assertEqual(messages[1]["contextId"], remote_context)


if __name__ == "__main__":
    unittest.main()
