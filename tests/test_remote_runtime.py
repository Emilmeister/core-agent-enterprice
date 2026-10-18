"""Trusted remote dispatch through the authenticated composition root."""
import asyncio
import copy
import json
import threading
from dataclasses import replace
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
        self.assertEqual(set(wire), {"task", "message_id", "headers", "timeout", "files", "attachment_limit_bytes"})
        self.assertEqual(wire["files"], ())
        self.assertEqual(wire["attachment_limit_bytes"], 25_000_000)
        self.assertEqual(wire["headers"], {})
        self.assertNotEqual(wire["message_id"], "message-one")
        self.assertNotIn("external-a", json.dumps(record.snapshot))

    async def test_new_root_reuses_direct_message_context_after_metadata_change(self):
        self.sent.return_value = replace(self.sent.return_value, context_id="remote-conversation")
        self.script(self.request(), ModelResponse(message="done"))
        first = await self.submit("owner-a", "context-first", "same-local-chat")
        self.assertEqual(first["status"]["state"], "TASK_STATE_COMPLETED")
        first_wire = self.sent.call_args.kwargs
        self.assertNotIn("context_id", first_wire)
        self.peer = self.registry.update(self.tenant, self.peer["id"], {
            "url": self.peer["url"], "description": "Updated delivery description", "enabled": True,
            "header_name": self.peer["header_name"]}, expected_revision=self.peer["revision"], actor_id="alice")
        self.script(self.request(), ModelResponse(message="done again"))
        second = await self.submit("owner-b", "context-second", "same-local-chat")
        self.assertEqual(second["status"]["state"], "TASK_STATE_COMPLETED")
        second_wire = self.sent.call_args.kwargs
        self.assertEqual(second_wire.get("context_id"), "remote-conversation")
        self.assertNotIn("task_id", second_wire)
        self.assertNotEqual(first_wire["message_id"], second_wire["message_id"])
        record = self.agent.workflow_store.lookup_task(second["id"])
        entry = next(iter(record.snapshot["remote_calls"].values()))
        self.assertEqual(entry["contract"]["peer_revision"], 2)
        self.assertEqual(entry["contract"]["conversation"], {"version": 1,
            "owner_id": record.owner_id, "context_id": record.context_id, "generation": 1})
        self.script(self.request(), ModelResponse(message="separate"))
        await self.submit("owner-a", "context-third", "another-local-chat")
        self.assertNotIn("context_id", self.sent.call_args.kwargs)

    async def test_actual_delegated_child_reuses_parent_peer_context_only_in_same_chat(self):
        self.sent.return_value = replace(self.sent.return_value, context_id="remote-parent-conversation")
        self.agent.model = ScriptedModel([
            self.request("parent-send"),
            ModelResponse(tool_requests=(ToolRequest("delegate", "core_delegate", {
                "instruction": "Ask delivery for an update", "tools": ["core_agent_send_message"], "skills": [],
                "budget": {"turns": 3, "tool_calls": 1}}),)),
            self.request("child-send"), ModelResponse(message="child done"), ModelResponse(message="parent done"),
        ])
        start_remote = self.agent.task_scheduler.start_remote

        def settled_remote(*args, **kwargs):
            handle = start_remote(*args, **kwargs)
            self.agent.task_scheduler.wait(handle.id, timeout=3, owner_id=kwargs["owner_id"], tenant_id=kwargs["tenant_id"])
            return handle

        with patch.object(self.agent.task_scheduler, "start_remote", side_effect=settled_remote):
            task = await self.submit("owner-a", "parent-and-child", "delegated-peer-chat")

            def settle():
                with self.agent.task_scheduler._lock:
                    workers = tuple(self.agent.task_scheduler._threads)
                for worker in workers:
                    worker.join(3)
                    self.assertFalse(worker.is_alive())
                with patch.object(self.agent, "_launch_recovery", return_value=False):
                    self.agent._recover_workflows_once()
                self.agent.resume_task(task["id"])

            await asyncio.to_thread(settle)
        root = self.agent.workflow_store.lookup_task(task["id"])
        self.assertEqual(root.state, "COMPLETED", root.error_code)
        children = [self.agent.workflow_store.lookup_task(handle.id)
                    for handle in self.agent.task_scheduler.list(owner_id=root.run_id, tenant_id=self.tenant)
                    if not self.agent.task_scheduler.is_remote(handle.id, owner_id=root.run_id, tenant_id=self.tenant)]
        self.assertEqual(len(children), 1)
        child = children[0]
        self.assertEqual((child.parent_run_id, child.context_id, child.owner_id, child.state),
                         (root.run_id, root.context_id, root.owner_id, "COMPLETED"))
        parent_contract = next(iter(root.snapshot["remote_calls"].values()))["contract"]
        child_contract = next(iter(child.snapshot["remote_calls"].values()))["contract"]
        self.assertEqual(parent_contract["conversation"], child_contract["conversation"])
        self.assertEqual(child_contract["caller_scope"]["run_id"], child.run_id)
        self.assertEqual(child_contract["caller_scope"]["task_id"], child.task_id)
        self.assertEqual(self.sent.call_count, 2)
        parent_wire, child_wire = [call.kwargs for call in self.sent.call_args_list]
        self.assertNotIn("context_id", parent_wire)
        self.assertEqual(child_wire["context_id"], "remote-parent-conversation")
        self.assertNotIn("task_id", child_wire)
        self.assertNotEqual(parent_wire["message_id"], child_wire["message_id"])
        self.script(self.request(), ModelResponse(message="separate chat done"))
        separate = await self.submit("owner-a", "separate-peer-chat", "another-peer-chat")
        self.assertEqual(separate["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(self.sent.call_count, 3)
        self.assertNotIn("context_id", self.sent.call_args.kwargs)

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

    async def test_delete_during_discovery_blocks_unpinned_old_identity(self):
        model = self.script(self.request(), ModelResponse(message="unavailable"))
        removed = []
        def discovery(peer, **kwargs):
            if model.calls and not removed:
                removed.append(self.registry.delete(self.tenant, peer["id"], expected_revision=1, actor_id="alice"))
                self.registry.create(self.tenant, {"name": peer["name"], "url": "https://replacement.example/a2a",
                    "description": "Replacement", "enabled": True, "header_name": "Authorization"}, actor_id="alice")
            return RemoteAgentConnection(RemoteAgentCard(peer["name"], peer["description"], peer["url"], False, (), "HTTP+JSON"))
        with patch("core_agent.remote_agents.connect_peer", side_effect=discovery):
            task = await self.submit("owner-a", "deleted-discovery", "deleted-discovery-chat")
        self.assertTrue(removed)
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertIn("TOOL_UNAVAILABLE", str(model.calls[-1].messages))
        self.assertEqual(self.sent.call_count, 0)
        record = self.agent.workflow_store.lookup_task(task["id"])
        self.assertFalse(record.snapshot.get("remote_calls"))

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
        self.registry.delete(self.tenant, self.peer["id"], expected_revision=2, actor_id="alice")
        self.registry.create(self.tenant, {"name": self.peer["name"], "url": "https://replacement.example/a2a",
            "description": "Replacement", "enabled": True, "header_name": "Authorization"}, actor_id="alice")
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
        pinned = next(iter(record.snapshot["remote_calls"].values()))
        sources = pinned["request_provenance"]["sources"]
        self.assertTrue(sources)
        self.assertTrue(any(source.get("materials") for source in sources.values()))
        self.assertNotIn("python_execution", record.snapshot)

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

    async def test_public_question_owner_answer_replies_to_original_operation(self):
        from core_agent.owner_api import interaction_digest
        question = "Which city should receive the parcel?"
        self.sent.side_effect = [replace(RemoteEvent("task", "TASK_STATE_INPUT_REQUIRED", question, False,
            ({"text": question},), "peer-task", "peer-context"),
            public_messages=({"text": question},), input_request={"input_id": "a" * 64, "question": question}),
            replace(RemoteEvent("task", "TASK_STATE_COMPLETED", "Delivered to Kazan", True,
                ({"text": "Delivered to Kazan"},), "peer-task", "peer-context"),
                public_messages=({"text": "Delivered to Kazan"},))]
        model = self.script(self.request(),
            ModelResponse(tool_requests=(ToolRequest("owner", "core_ask_owner", {"question": question}),)),
            ModelResponse(message="reply placeholder"), ModelResponse(message="wait placeholder"), ModelResponse(message="done"))
        original = model.generate
        original_start = self.agent.task_scheduler.start_remote
        def settle_start(*args, **kwargs):
            task = original_start(*args, **kwargs)
            with self.agent.task_scheduler._lock:
                workers = tuple(self.agent.task_scheduler._threads)
            for worker in workers:
                worker.join(3)
            return self.agent.task_scheduler.get(task.id, owner_id=kwargs["owner_id"], tenant_id=kwargs["tenant_id"])
        self.agent.task_scheduler.start_remote = settle_start
        handles = []
        def next_call(**kwargs):
            if len(model.calls) == 3:
                self.agent.task_scheduler.recover()
            with self.agent.task_scheduler._lock:
                workers = tuple(self.agent.task_scheduler._threads)
            for worker in workers:
                worker.join(3)
            if len(model.calls) == 1:
                handles.extend(self.agent.task_scheduler._remote)
            elif len(model.calls) == 2:
                model._responses[0] = ModelResponse(tool_requests=(ToolRequest("reply", "core_agent_send_message",
                    {"agent_name": "delivery", "task": "Kazan", "task_id": handles[0], "input_id": "a" * 64}),))
            elif len(model.calls) == 3:
                model._responses[0] = ModelResponse(tool_requests=(ToolRequest("wait-final", "core_task_wait", {"task_id": handles[0]}),))
            return original(**kwargs)
        model.generate = next_call
        task = await self.submit("external-a", "public-question", "public-question-chat")
        record = self.agent.workflow_store.lookup_task(task["id"])
        self.assertEqual(record.state, "WAITING_INPUT", record.error_code)
        self.assertIn(question, model.calls[1].context)
        self.assertEqual(self.sent.call_count, 1)
        operation = self.agent.task_scheduler.get(handles[0], owner_id=record.run_id, tenant_id=self.tenant)
        deadline = self.agent.task_scheduler._remote[operation.id]["checkpoint"]["deadline"]
        self.assertEqual(operation.state, "working", str(operation.error))
        wait = self.agent.workflow_store.get_wait(record.snapshot["wait_id"], tenant_id=self.tenant)
        self.assertEqual(wait.kind, "owner_question")
        policy = self.agent.interaction_store.get_policy(self.tenant, "core_agent_send_message", "builtin:core_agent_send_message")
        self.agent.interaction_store.update_policy(self.tenant, "core_agent_send_message", policy.origin,
            mode="require_hitl", guardrails_exempt=False, expected_revision=policy.revision, actor_id="alice")
        response = await self.http.post(f"/api/questions/{wait.wait_id}/answer", headers=self.headers("owner-a"),
            json={"answer": "Kazan", "subject_digest": interaction_digest(wait)})
        self.assertEqual(response.status_code, 200, response.text)
        self.registry.delete(self.tenant, self.peer["id"], expected_revision=1, actor_id="alice")
        await asyncio.to_thread(self.agent.resume_task, task["id"])
        approval_record = self.agent.workflow_store.lookup_task(task["id"])
        approval = self.agent.workflow_store.get_wait(approval_record.snapshot["wait_id"], tenant_id=self.tenant)
        self.assertEqual(approval.kind, "tool_approval")
        self.assertEqual(approval.subject["remote_binding"]["task"], "Kazan")
        self.assertEqual(approval.subject["remote_binding"]["operation_id"], handles[0])
        self.assertEqual(approval.subject["remote_binding"]["peer_revision"], 1)
        self.assertEqual(self.sent.call_count, 1)
        allowed = await self.http.post(f"/api/hitl/{approval.wait_id}/decision", headers=self.headers("owner-a"),
            json={"decision": "allow", "subject_digest": interaction_digest(approval)})
        self.assertEqual(allowed.status_code, 200, allowed.text)
        await asyncio.to_thread(self.agent.resume_task, task["id"])
        final = self.agent.workflow_store.lookup_task(task["id"])
        self.assertEqual(final.state, "COMPLETED", final.error_code)
        self.assertEqual(len(self.agent.task_scheduler.list(owner_id=record.run_id, tenant_id=self.tenant)), 1)
        self.assertEqual(self.sent.call_count, 2)
        self.assertEqual(self.sent.call_args.kwargs["task"], "Kazan")
        self.assertEqual(self.sent.call_args.kwargs["task_id"], "peer-task")
        self.assertEqual(self.sent.call_args.kwargs["context_id"], "peer-context")
        self.assertEqual(self.agent.task_scheduler._remote[operation.id]["checkpoint"]["deadline"], deadline)
        self.assertEqual(final.snapshot["remote_inputs_seen"], [[handles[0], "a" * 64]])
        self.assertIn("Delivered to Kazan", model.calls[-1].context)
        self.assertNotIn("core_ask_owner", json.dumps(task))
        replay = copy.deepcopy(final.snapshot)
        replay["tool_calls"] = replay["remote_admission"]["attempt"]
        receipt = self.agent._start_remote_operation((final, replay, ToolCall("reply", "core_agent_send_message",
            {"agent_name": "delivery", "task": "Kazan", "task_id": handles[0], "input_id": "a" * 64}), None))
        self.assertEqual(receipt["task_id"], handles[0])
        self.assertEqual(receipt["input_id"], "a" * 64)
        self.assertEqual(receipt["reply_delivery"], "confirmed")
        self.assertEqual(self.sent.call_count, 2)

    async def test_late_public_question_wakes_wait_once_without_replaying_snapshot(self):
        now = [1_800_000_000.0]
        self.agent.task_scheduler.clock = lambda: now[0]
        self.sent.return_value = RemoteEvent("task", "TASK_STATE_WORKING", "", False, (), "remote-task", "remote-chat")
        model = self.script(self.request(), ModelResponse(message="wait placeholder"), ModelResponse(message="wait again"))
        original = model.generate
        handles = []
        def next_call(**kwargs):
            if len(model.calls) == 1:
                with self.agent.task_scheduler._lock:
                    workers = tuple(self.agent.task_scheduler._threads)
                for worker in workers:
                    worker.join(3)
                handles.extend(self.agent.task_scheduler._remote)
                model._responses[0] = ModelResponse(tool_requests=(ToolRequest("wait", "core_task_wait", {"task_id": handles[0]}),))
            return original(**kwargs)
        model.generate = next_call
        task = await self.submit("owner-a", "late-question", "late-question-chat")
        record = self.agent.workflow_store.lookup_task(task["id"])
        self.assertEqual(record.state, "WAITING_TASK")
        first_wait = record.snapshot["wait_id"]
        question = replace(RemoteEvent("task", "TASK_STATE_INPUT_REQUIRED", "Which order?", False, (), "remote-task", "remote-chat"),
            input_request={"input_id": "b" * 64, "question": "Which order?"})
        with patch.object(RemoteAgentConnection, "get_task", return_value=question), patch.object(
                self.agent, "_launch_recovery", return_value=False):
            now[0] += 11
            self.agent.task_scheduler.recover()
            with self.agent.task_scheduler._lock:
                workers = tuple(self.agent.task_scheduler._threads)
            for worker in workers:
                worker.join(3)
            await asyncio.to_thread(self.agent._recover_workflows_once)
            wait = self.agent.workflow_store.get_wait(first_wait, tenant_id=self.tenant)
            self.assertEqual(wait.outcome["result"]["question"], "Which order?")
            self.assertEqual(wait.outcome["result"]["input_id"], "b" * 64)
            self.assertEqual(wait.outcome["result"]["state"], "working")
            model._responses[0] = ModelResponse(tool_requests=(ToolRequest("wait-again", "core_task_wait", {"task_id": handles[0]}),))
            await asyncio.to_thread(self.agent.resume_task, task["id"])
            current = self.agent.workflow_store.lookup_task(task["id"])
            self.assertEqual(current.state, "WAITING_TASK")
            self.assertEqual(current.snapshot["remote_inputs_seen"], [[handles[0], "b" * 64]])
            self.assertIn("Which order?", model.calls[-1].context)
            now[0] += 11
            self.agent.task_scheduler.recover()
            with self.agent.task_scheduler._lock:
                workers = tuple(self.agent.task_scheduler._threads)
            for worker in workers:
                worker.join(3)
            await asyncio.to_thread(self.agent._recover_workflows_once)
            repeated = self.agent.workflow_store.get_wait(current.snapshot["wait_id"], tenant_id=self.tenant)
            self.assertIsNone(repeated.outcome)
            self.assertEqual(len(model.calls), 3)
        await asyncio.to_thread(self.agent.cancel_task, task["id"])

    async def test_public_question_nested_python_wait_stops_remainder(self):
        now = [1_800_000_000.0]
        self.agent.task_scheduler.clock = lambda: now[0]
        self.sent.return_value = RemoteEvent("task", "TASK_STATE_WORKING", "", False, (), "remote-task", "remote-chat")
        prefix, remainder, stopped = [], [], []
        def execute(manager, **kwargs):
            prefix.append("prefix")
            result = ExecutionResult(-9, "prefix output", "", (), (), status="failed", cleanup="sandbox_terminated")
            session = SimpleNamespace(cancel=lambda _: stopped.append("stopped"), wait=lambda *_: result)
            kwargs["on_start"](session, SimpleNamespace(id="process"))
            with self.agent.task_scheduler._lock:
                handle = next(iter(self.agent.task_scheduler._remote))
            now[0] += 11
            self.agent.task_scheduler.recover()
            with self.agent.task_scheduler._lock:
                workers = tuple(self.agent.task_scheduler._threads)
            for worker in workers:
                worker.join(3)
            def nested():
                try:
                    kwargs["dispatch"]("core_task_wait", {"task_id": handle}, "stable-question-wait")
                    remainder.append("ran")
                except PythonContinuationStopped:
                    pass
            thread = threading.Thread(target=copy_context().run, args=(nested,))
            thread.start()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            return result
        model = self.script(self.request(),
            ModelResponse(tool_requests=(ToolRequest("python", "core_python_exec", {"code": "pass"}),)),
            ModelResponse(tool_requests=(ToolRequest("owner", "core_ask_owner", {"question": "Which order?"}),)))
        event = replace(RemoteEvent("task", "TASK_STATE_INPUT_REQUIRED", "Which order?", False, (), "remote-task", "remote-chat"),
            input_request={"input_id": "c" * 64, "question": "Which order?"})
        with patch("core_agent.runtime.execute_python", side_effect=execute), patch.object(
                RemoteAgentConnection, "get_task", return_value=event):
            task = await self.submit("owner-a", "python-question", "python-question-chat")
        record = self.agent.workflow_store.lookup_task(task["id"])
        self.assertEqual(record.state, "WAITING_INPUT", record.error_code)
        self.assertEqual((prefix, remainder, stopped), (["prefix"], [], ["stopped"]))
        self.assertIn("PYTHON_CONTINUATION_INTERRUPTED", model.calls[-1].context)
        self.assertIn("Which order?", model.calls[-1].context)
        self.assertEqual(record.snapshot["remote_inputs_seen"], [[next(iter(self.agent.task_scheduler._remote)), "c" * 64]])
        await asyncio.to_thread(self.agent.cancel_task, task["id"])

    async def test_nested_task_get_question_is_not_delivered_again_by_mailbox(self):
        now = [1_800_000_000.0]
        self.agent.task_scheduler.clock = lambda: now[0]
        self.sent.return_value = RemoteEvent("task", "TASK_STATE_WORKING", "", False, (), "remote-task", "remote-chat")
        def execute(manager, **kwargs):
            with self.agent.task_scheduler._lock:
                handle = next(iter(self.agent.task_scheduler._remote))
            now[0] += 11
            self.agent.task_scheduler.recover()
            with self.agent.task_scheduler._lock:
                workers = tuple(self.agent.task_scheduler._threads)
            for worker in workers:
                worker.join(3)
            answer = kwargs["dispatch"]("core_task_get", {"task_id": handle}, "read-public-question")
            self.assertEqual(answer["result"]["question"], "Which order?")
            return ExecutionResult(0, json.dumps(answer), "", (), ())
        model = self.script(self.request(),
            ModelResponse(tool_requests=(ToolRequest("python", "core_python_exec", {"code": "pass"}),)),
            ModelResponse(tool_requests=(ToolRequest("owner", "core_ask_owner", {"question": "Which order?"}),)))
        event = replace(RemoteEvent("task", "TASK_STATE_INPUT_REQUIRED", "Which order?", False, (), "remote-task", "remote-chat"),
            input_request={"input_id": "d" * 64, "question": "Which order?"})
        with patch("core_agent.runtime.execute_python", side_effect=execute), patch.object(
                RemoteAgentConnection, "get_task", return_value=event):
            task = await self.submit("owner-a", "python-get-question", "python-get-question-chat")
        record = self.agent.workflow_store.lookup_task(task["id"])
        self.assertEqual(record.state, "WAITING_INPUT", record.error_code)
        notifications = [json.loads(item["content"]) for item in record.snapshot["context"]["transcript"]
            if item["kind"] == "task_notification"]
        self.assertFalse(any(item["task_notification"].get("public_input") for item in notifications))
        self.assertEqual(record.snapshot["remote_inputs_seen"], [[next(iter(self.agent.task_scheduler._remote)), "d" * 64]])
        self.assertIn("Which order?", model.calls[-1].context)
        await asyncio.to_thread(self.agent.cancel_task, task["id"])

    async def test_rejected_public_question_is_not_repeated_by_wait_or_mailbox(self):
        from core_agent.guardrails import GuardrailClassifier
        from core_agent.owner_api import interaction_digest
        class Detector:
            def count_tokens(self, text):
                return len(text)
            def generate(self, **kwargs):
                return ModelResponse(message=json.dumps({"verdict": "suspicious" if "question-canary" in kwargs["context"] else "clear"}), finish_reason="stop")
        self.agent.guardrail_classifier = GuardrailClassifier(Detector())
        self.sent.return_value = replace(RemoteEvent("task", "TASK_STATE_INPUT_REQUIRED", "question-canary", False,
            (), "remote-task", "remote-chat"), input_request={"input_id": "e" * 64, "question": "question-canary"})
        original_start = self.agent.task_scheduler.start_remote
        def settled(*args, **kwargs):
            task = original_start(*args, **kwargs)
            with self.agent.task_scheduler._lock:
                workers = tuple(self.agent.task_scheduler._threads)
            for worker in workers:
                worker.join(3)
            return self.agent.task_scheduler.get(task.id, owner_id=kwargs["owner_id"], tenant_id=kwargs["tenant_id"])
        self.agent.task_scheduler.start_remote = settled
        model = self.script(self.request(), ModelResponse(message="wait placeholder"))
        generate = model.generate
        def next_call(**kwargs):
            if len(model.calls) == 1:
                handle = next(iter(self.agent.task_scheduler._remote))
                model._responses[0] = ModelResponse(tool_requests=(ToolRequest("wait", "core_task_wait", {"task_id": handle}),))
            return generate(**kwargs)
        model.generate = next_call
        task = await self.submit("owner-a", "rejected-question", "rejected-question-chat")
        record = self.agent.workflow_store.lookup_task(task["id"])
        self.assertEqual(record.state, "WAITING_INPUT", record.error_code)
        self.assertEqual(len(model.calls), 1)
        wait = self.agent.workflow_store.get_wait(record.snapshot["wait_id"], tenant_id=self.tenant)
        self.assertEqual(wait.kind, "guardrail")
        response = await self.http.post(f"/api/guardrails/{wait.wait_id}/decision", headers=self.headers("owner-a"),
            json={"decision": "reject", "subject_digest": interaction_digest(wait)})
        self.assertEqual(response.status_code, 200, response.text)
        await asyncio.to_thread(self.agent.resume_task, task["id"])
        current = self.agent.workflow_store.lookup_task(task["id"])
        self.assertEqual(current.state, "WAITING_TASK", current.error_code)
        self.assertNotIn("question-canary", model.calls[-1].context)
        self.assertIn("MATERIAL_REJECTED", model.calls[-1].context)
        self.assertEqual(current.snapshot["remote_inputs_seen"], [[next(iter(self.agent.task_scheduler._remote)), "e" * 64]])
        self.assertEqual(len(model.calls), 2)
        await asyncio.to_thread(self.agent.cancel_task, task["id"])
