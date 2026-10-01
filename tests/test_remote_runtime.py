"""Trusted remote dispatch through the authenticated composition root."""
import asyncio
import copy
import json
import threading
from contextvars import copy_context
from types import SimpleNamespace
from unittest.mock import patch

from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
from core_agent.errors import CoreError
from core_agent.execution import ExecutionResult
from core_agent.python_exec import PythonContinuationStopped
from core_agent.tools import ToolCall
from core_agent.remote_agents import RemoteAgentCard, RemoteAgentConnection, RemoteEvent
from tests.test_auth import AuthAppTestCase


class RemoteRuntimeTests(AuthAppTestCase):
    async def asyncSetUp(self):
        with patch("core_agent.runtime.CoreAgent.recover_workflows", return_value=()), patch(
                "core_agent.app._remote_agents", side_effect=AssertionError("Enterprise used legacy ENV discovery")):
            await super().asyncSetUp()
        self.agent = self.app.state.core_agent
        self.tenant = self.app.state.authenticator.settings.tenant
        self.registry = self.app.state.remote_registry_store
        self.peer = self.registry.create(self.tenant, {"name": "delivery", "url": "https://peer.example/a2a",
            "description": "Delivery status", "enabled": True, "header_name": "Authorization"}, actor_id="alice")
        self.discovery = patch("core_agent.remote_agents.connect_peer", side_effect=lambda peer, **kwargs:
            RemoteAgentConnection(RemoteAgentCard(peer["name"], peer["description"], peer["url"], False, (), "HTTP+JSON")))
        self.discovery.start()
        self.addCleanup(self.discovery.stop)
        self.send = patch.object(RemoteAgentConnection, "send_task", return_value=RemoteEvent(
            "message", None, "delivered", True, ({"text": "delivered"},)))
        self.sent = self.send.start()
        self.addCleanup(self.send.stop)

    def script(self, *responses):
        model = ScriptedModel(responses)
        generate = model.generate
        def call(**kwargs):
            # Let the independently scheduled immediate peer result settle before finalization.
            if model.calls:
                with self.agent.task_scheduler._lock:
                    workers = tuple(self.agent.task_scheduler._threads)
                for worker in workers:
                    worker.join(3)
            return generate(**kwargs)
        model.generate = call
        self.agent.model = model
        return model

    def request(self, call_id="send"):
        return ModelResponse(tool_requests=(ToolRequest(call_id, "core_agent_send_message",
            {"agent_name": "delivery", "task": "Deliver parcel"}),))

    async def test_registry_dispatch_returns_durable_handle_and_never_forwards_caller_ids(self):
        model = self.script(self.request(), ModelResponse(message="done"))
        task = await self.submit("external-a", "message-one", "chat-one")
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertIn("core_agent_send_message", model.calls[0].tools)
        record = self.agent.workflow_store.lookup_task(task["id"])
        handles = self.agent.task_scheduler.list(owner_id=record.run_id, tenant_id=self.tenant)
        self.assertEqual(len(handles), 1)
        self.assertEqual(handles[0].result["text"], "delivered")
        self.assertEqual(self.sent.call_count, 1)
        wire = self.sent.call_args.kwargs
        self.assertEqual(set(wire), {"task", "message_id", "headers", "timeout"})
        self.assertEqual(wire["headers"], {})
        self.assertNotEqual(wire["message_id"], "message-one")
        self.assertNotIn("external-a", json.dumps(record.snapshot))

    async def test_card_and_model_catalog_follow_scoped_registry(self):
        response = await self.http.get("/a2a/owner/.well-known/agent-card.json", headers=self.headers("owner-a"))
        self.assertIn("core_agent_send_message", {skill["id"] for skill in response.json().get("skills", [])})
        self.assertEqual(self.agent._remote_peers("another-company"), {})
        self.registry.disable(self.tenant, self.peer["id"], expected_revision=1, actor_id="alice")
        response = await self.http.get("/a2a/owner/.well-known/agent-card.json", headers=self.headers("owner-a"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("core_agent_send_message", {skill["id"] for skill in response.json().get("skills", [])})
        model = self.script(ModelResponse(message="done"))
        await self.submit("owner-a", "message-empty", "chat-empty")
        self.assertNotIn("core_agent_send_message", model.calls[0].tools)

    async def test_unavailable_peer_is_omitted_and_stale_send_has_structured_failure(self):
        with patch("core_agent.remote_agents.connect_peer", side_effect=CoreError("REMOTE_AGENT_UNAVAILABLE")):
            model = self.script(self.request(), ModelResponse(message="unavailable"))
            task = await self.submit("owner-a", "unavailable", "unavailable-chat")
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertNotIn("core_agent_send_message", model.calls[0].tools)
        self.assertIn("TOOL_UNAVAILABLE", str(model.calls[1].messages))
        self.assertEqual(self.sent.call_count, 0)

    async def test_only_pinned_private_peer_headers_reach_send(self):
        self.registry.update(self.tenant, self.peer["id"], {"url": self.peer["url"], "description": "Delivery status",
            "enabled": True, "header_name": "Authorization", "header_value": "Bearer peer-private-secret"},
            expected_revision=1, actor_id="alice")
        self.script(self.request(), ModelResponse(message="done"))
        task = await self.submit("external-a", "private-peer", "private-peer-chat")
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(self.sent.call_args.kwargs["headers"], {"Authorization": "Bearer peer-private-secret"})
        record = self.agent.workflow_store.lookup_task(task["id"])
        self.assertNotIn("peer-private-secret", json.dumps(record.snapshot))

    async def test_coordinator_expiry_resolves_existing_wait_without_more_network(self):
        now = [1800000000.0]
        self.agent.task_scheduler.clock = lambda: now[0]
        self.sent.return_value = RemoteEvent("task", "TASK_STATE_INPUT_REQUIRED", "owner needed", False, (), "remote-id", "remote-chat")
        model = self.script(self.request(), ModelResponse(message="unused"), ModelResponse(message="timeout explained"))
        original = model.generate
        def next_call(**kwargs):
            if len(model.calls) == 1:
                with self.agent.task_scheduler._lock:
                    threads = tuple(self.agent.task_scheduler._threads)
                for thread in threads:
                    thread.join(3)
                handle = next(iter(self.agent.task_scheduler._remote))
                model._responses[0] = ModelResponse(tool_requests=(ToolRequest("wait", "core_task_wait", {"task_id": handle}),))
            return original(**kwargs)
        model.generate = next_call
        task = await self.submit("owner-a", "expire", "expire-chat")
        record = self.agent.workflow_store.lookup_task(task["id"])
        self.assertEqual(record.state, "WAITING_TASK")
        wait_id = record.snapshot["wait_id"]
        now[0] += 86401
        with patch.object(RemoteAgentConnection, "get_task", side_effect=AssertionError("expired polling")), patch.object(
                RemoteAgentConnection, "cancel_task", side_effect=AssertionError("expired cancellation")), patch.object(
                self.agent, "_launch_recovery", return_value=False):
            await asyncio.to_thread(self.agent._recover_workflows_once)
            wait = self.agent.workflow_store.get_wait(wait_id, tenant_id=self.tenant, owner_id=record.owner_id)
            self.assertEqual(wait.outcome["result"]["error"], "REMOTE_OPERATION_TIMEOUT")
            await asyncio.to_thread(self.agent.resume_task, task["id"])
        self.assertEqual(self.sent.call_count, 1)
        self.assertIn("REMOTE_OPERATION_TIMEOUT", str(model.calls[-1].messages))

    async def test_hitl_pins_revision_and_recovery_reuses_admission(self):
        from core_agent.interactions import tool_origin
        policy = self.agent.interaction_store.get_policy(self.tenant, "core_agent_send_message", tool_origin("core_agent_send_message"))
        self.agent.interaction_store.update_policy(self.tenant, "core_agent_send_message", policy.origin,
            mode="require_hitl", guardrails_exempt=False, expected_revision=policy.revision, actor_id="alice")
        self.script(self.request(), ModelResponse(message="done"))
        task = await self.submit("owner-a", "message-hitl", "chat-hitl")
        waiting = await self.http.get("/api/interactions", headers=self.headers("owner-a"), params={"task_id": task["id"]})
        approval = waiting.json()["interactions"][0]
        self.assertEqual(approval["subject"]["remote_binding"]["peer_revision"], 1)
        self.assertEqual(self.sent.call_count, 0)
        self.registry.disable(self.tenant, self.peer["id"], expected_revision=1, actor_id="alice")
        allowed = await self.http.post(f"/api/hitl/{approval['wait_id']}/decision", headers=self.headers("owner-a"),
            json={"decision": "allow", "subject_digest": approval["subject_digest"]})
        self.assertEqual(allowed.status_code, 200, allowed.text)
        await asyncio.to_thread(self.agent.resume_task, task["id"])
        await asyncio.to_thread(self.agent.resume_task, task["id"])
        self.assertEqual(self.sent.call_count, 1)

    async def test_reused_model_call_id_creates_distinct_operations(self):
        self.script(self.request("same"), self.request("same"), ModelResponse(message="done"))
        task = await self.submit("owner-a", "reuse", "reuse-chat")
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED")
        record = self.agent.workflow_store.lookup_task(task["id"])
        self.assertEqual(set(record.snapshot["remote_calls"]), {"1:same", "2:same"})
        self.assertEqual(len(self.agent.task_scheduler.list(owner_id=record.run_id, tenant_id=self.tenant)), 2)
        self.assertEqual(len({call.kwargs["message_id"] for call in self.sent.call_args_list}), 2)

    async def test_committed_admission_survives_parent_result_commit_failure(self):
        self.script(self.request(), ModelResponse(message="done"))
        original = self.agent._record_tool_outcome
        def fail(record, snapshot, call, *args, **kwargs):
            if call.name == "core_agent_send_message":
                raise CoreError("LEASE_LOST")
            return original(record, snapshot, call, *args, **kwargs)
        with patch.object(self.agent, "_record_tool_outcome", side_effect=fail):
            task = await self.submit("owner-a", "crash", "crash-chat")
        record = self.agent.workflow_store.lookup_task(task["id"])
        self.assertEqual(record.state, "MODEL_RESPONDED")
        self.assertIn("remote_admission", record.snapshot)
        await asyncio.to_thread(self.agent.resume_task, task["id"])
        self.assertEqual(self.sent.call_count, 1)
        self.assertEqual(len(self.agent.task_scheduler.list(owner_id=record.run_id, tenant_id=self.tenant)), 1)

    async def test_remote_wait_rejects_timeout_even_after_terminal_outcome(self):
        self.script(self.request(), ModelResponse(message="done"))
        task = await self.submit("owner-a", "wait-check", "wait-chat")
        record = self.agent.workflow_store.lookup_task(task["id"])
        handle = self.agent.task_scheduler.list(owner_id=record.run_id, tenant_id=self.tenant)[0]
        self.agent._run_scopes[record.run_id] = {"tenant_id": self.tenant}
        for timeout in (0, None, 1):
            arguments = {"task_id": handle.id, "timeout": timeout}
            with self.assertRaises(CoreError) as error:
                self.agent._prepare_tool_wait(record, copy.deepcopy(record.snapshot), ToolCall("wait", "core_task_wait", arguments), lease_token=None)
            self.assertEqual(error.exception.code, "TOOL_ARGUMENT_INVALID")
            with self.assertRaises(CoreError) as error:
                self.agent._task_wait(arguments, record.run_id)
            self.assertEqual(error.exception.code, "TOOL_ARGUMENT_INVALID")
        self.assertEqual(self.sent.call_count, 1)

    async def test_python_remote_call_lifts_to_same_admission_without_prefix_replay(self):
        prefix, remainder, stopped = [], [], []
        def execute(manager, **kwargs):
            prefix.append("prefix")
            result = ExecutionResult(-9, "prefix output", "", (), (), status="failed", cleanup="sandbox_terminated")
            session = SimpleNamespace(cancel=lambda _: stopped.append("stopped"), wait=lambda *_: result)
            kwargs["on_start"](session, SimpleNamespace(id="process"))
            def broker():
                try:
                    kwargs["dispatch"]("core_agent_send_message", {"agent_name": "delivery", "task": "Deliver parcel"}, "stable-request")
                    remainder.append("remainder")
                except PythonContinuationStopped:
                    pass
            thread = threading.Thread(target=copy_context().run, args=(broker,))
            thread.start()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            return result
        self.script(ModelResponse(tool_requests=(ToolRequest("python", "core_python_exec", {"code": "pass"}),)),
            ModelResponse(message="done"))
        with patch("core_agent.runtime.execute_python", side_effect=execute):
            task = await self.submit("owner-a", "python-call", "python-chat")
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED")
        record = self.agent.workflow_store.lookup_task(task["id"])
        self.assertEqual(record.snapshot["tool_calls"], 2)
        self.assertEqual((prefix, remainder, stopped), (["prefix"], [], ["stopped"]))
        self.assertEqual(self.sent.call_count, 1)
        self.assertEqual(len(self.agent.task_scheduler.list(owner_id=record.run_id, tenant_id=self.tenant)), 1)

    async def test_child_keeps_registry_without_registering_another_executor(self):
        executor = self.agent.task_scheduler._handlers["remote_a2a"]
        child = self.agent._child_agent(self.agent.agent_config.to_dict(), ["core_agent_send_message", "core_task_wait"])
        self.assertIs(child.remote_registry, self.registry)
        self.assertIs(child.task_scheduler._handlers["remote_a2a"], executor)

    async def test_unknown_persisted_remote_versions_fail_before_runtime_restore(self):
        self.script(self.request(), ModelResponse(message="done"))
        task = await self.submit("owner-a", "versions", "versions-chat")
        original = self.agent.workflow_store.lookup_task(task["id"])
        for field in ("remote_calls", "remote_admission"):
            record = copy.deepcopy(original)
            if field == "remote_calls":
                next(iter(record.snapshot[field].values()))["version"] = 2
            else:
                record.snapshot[field]["version"] = 2
            with patch.object(self.agent, "_bind_workspace", side_effect=AssertionError("restore before validation")):
                with self.assertRaises(CoreError) as error:
                    self.agent._load_workflow_runtime(record)
            self.assertEqual(error.exception.code, "CHECKPOINT_INVALID")
        self.assertEqual(self.sent.call_count, 1)

    async def test_remote_result_is_withheld_by_material_guard(self):
        from core_agent.guardrails import GuardrailClassifier
        class Detector:
            def count_tokens(self, text):
                return len(text)
            def generate(self, **kwargs):
                suspicious = "delivered" in kwargs["context"]
                return ModelResponse(message=json.dumps({"verdict": "suspicious" if suspicious else "clear"}), finish_reason="stop")
        self.agent.guardrail_classifier = GuardrailClassifier(Detector())
        model = self.script(self.request(), ModelResponse(message="unused"), ModelResponse(message="after review"))
        original = self.agent.task_scheduler.start_remote
        def settled(*args, **kwargs):
            handle = original(*args, **kwargs)
            self.agent.task_scheduler.wait(handle.id, timeout=3, owner_id=kwargs["owner_id"], tenant_id=kwargs["tenant_id"])
            return handle
        with patch.object(self.agent.task_scheduler, "start_remote", side_effect=settled):
            task = await self.submit("external-a", "guarded", "guarded-chat")
        self.assertNotIn("delivered", json.dumps(task))
        record = self.agent.workflow_store.lookup_task(task["id"])
        self.assertEqual(record.state, "WAITING_INPUT")
        waits = await self.http.get("/api/interactions", headers=self.headers("owner-a"), params={"task_id": task["id"]})
        self.assertEqual(waits.json()["interactions"][0]["kind"], "guardrail")
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(self.sent.call_count, 1)
