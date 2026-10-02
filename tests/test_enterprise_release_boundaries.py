"""Combined enterprise boundaries through existing authenticated/runtime fixtures."""
import asyncio
import base64
import io
import json
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
from a2a.types import TaskPushNotificationConfig

from core_agent.config import AgentConfig
from core_agent.database import PostgresDatabase
from core_agent.errors import CoreError
from core_agent.model import ModelCall, ModelResponse, ScriptedModel, ToolRequest
from core_agent.remote_agents import RemoteAgentCard, RemoteAgentConnection
from core_agent.workspace import WorkspaceBinding
from core_agent.workflow import SuspendedRun, TERMINAL_STATES
from tests import test_auth as auth_fixtures
from tests import test_cron as cron_fixtures
from tests import test_runtime_observability as context_fixtures
from tests import test_remote_operations as remote_fixtures
from tests.app_support import create_app


class SemanticModel(ScriptedModel):
    """Script the provider response while exercising the actual semantic pipeline."""

    def __init__(self, responses, *, omit_pending=False):
        super().__init__(responses)
        self.model = "auth-test-model"
        self.summaries = []
        self.omit_pending = omit_pending

    def generate(self, **call):
        if not call["instructions"].startswith("SEMANTIC CONTEXT SUMMARY"):
            return super().generate(**call)
        self.summaries.append(call)
        self._calls.append(ModelCall(call["context"], frozenset(call["tools"]),
            call["instructions"], tuple(call.get("messages", ()))))
        response = context_fixtures.SemanticRuntimeTests.semantic_answer(call, "Publishing is planned only")
        value = json.loads(response.message)
        latest = "C" if "Correction: C" in call["context"] else "B"
        value["Decisions"] = [{**value["Pending"][0], "text": "Use " + latest}]
        if self.omit_pending:
            value["Pending"] = []
        return ModelResponse(message=json.dumps(value), finish_reason="stop")


class EnterpriseReleaseBoundaryTests(auth_fixtures.AuthAppTestCase):
    context = cron_fixtures.CronStoreTests.context
    durable_blobs = True

    async def asyncSetUp(self):
        self.tasks = set()
        self.push_payloads = []
        self.push_client = httpx.AsyncClient(transport=httpx.MockTransport(self.webhook))
        self.addAsyncCleanup(self.push_client.aclose)
        compose = auth_fixtures.create_app

        def composed(**kwargs):
            return compose(**kwargs, push_client=self.push_client)

        with patch.object(auth_fixtures, "create_app", side_effect=composed), \
                patch.object(auth_fixtures, "PostgresDatabase", side_effect=lambda url:
                    PostgresDatabase(url, min_size=1, max_size=2, timeout=3)), \
                patch("core_agent.runtime.CoreAgent.recover_workflows") as recovery:
            await super().asyncSetUp()
        self.reconcile = recovery.call_args.kwargs["on_settled"]
        self.bind_services()

    def bind_services(self):
        self.agent = self.app.state.core_agent
        self.store = self.agent.cron_store
        self.admission = self.store.admission
        self.tenant = self.app.state.authenticator.settings.tenant

    def webhook(self, request):
        self.push_payloads.append(json.loads(request.content))
        return httpx.Response(204, request=request)

    async def asyncTearDown(self):
        for task_id in self.tasks:
            try:
                record = self.agent.workflow_store.lookup_task(task_id)
            except CoreError as error:
                if error.code != "TASK_NOT_FOUND":
                    raise
                continue
            if record.state not in TERMINAL_STATES:
                await asyncio.to_thread(self.agent.cancel_task, task_id)

    async def schedule(self, **values):
        return await self.store.create(self.context("owner-a"), {
            "request_id": uuid.uuid4().hex, "prompt": "Report the confirmed decision", "expression": "0 18 * * *",
            **values})

    async def run_schedule(self, row):
        admitted = await self.store.run_now(self.context("owner-a"), row["id"], {
            "request_id": uuid.uuid4().hex, "expected_revision": row["revision"]})
        self.tasks.add(admitted.task.id)
        self.release_admission(admitted)
        return admitted

    def release_admission(self, admitted):
        # Production cron_handoff releases this exact reservation before recovery.
        if admitted is not None and admitted.run_id:
            self.agent.workflow_store.release_lease(admitted.run_id, tenant_id=self.tenant,
                worker_id=self.agent._worker_id, token=admitted.lease_token)

    async def automatic(self, row):
        if self.use_postgres:
            def execute():
                with self.store.database.transaction() as connection:
                    return self.store.occur(self.context("owner-a"), row["id"], row["revision"], connection=connection)
            admitted = await asyncio.to_thread(execute)
        else:
            admitted = await self.store.occur_memory(self.context("owner-a"), row["id"], row["revision"])
        self.release_admission(admitted)
        return admitted

    def policy(self, name, mode, *, guardrails_exempt=False):
        previous = self.agent.interaction_store.get_policy(self.tenant, name, "builtin:" + name)
        self.agent.interaction_store.update_policy(self.tenant, name, previous.origin, mode=mode,
            guardrails_exempt=guardrails_exempt, expected_revision=previous.revision, actor_id="release-proof-owner")

    def compactions(self):
        raw = self.agent.agent_config.to_dict()
        raw["context"]["compaction_interval"] = 1
        self.agent.agent_config = AgentConfig.from_dict(raw)

    async def pending(self, task_id):
        response = await self.http.get("/api/interactions", headers=self.headers("owner-a"), params={"task_id": task_id})
        self.assertEqual(response.status_code, 200, response.text)
        pending = [entry for entry in response.json()["interactions"] if not entry.get("outcome")]
        self.assertEqual(len(pending), 1, response.json())
        return pending[0]

    async def test_cron_same_chat_imports_latest_decision_after_two_semantic_compactions(self):
        row = await self.schedule()
        model = SemanticModel([
            ModelResponse(message="Correction: B. Publishing is planned only.",
                tool_requests=(ToolRequest("first-read", "core_task_list", {}),)),
            ModelResponse(message="Correction: C. Publishing is still planned, not done.",
                tool_requests=(ToolRequest("second-read", "core_task_list", {}),)),
            ModelResponse(message="Confirmed C; publishing remains planned"),
            ModelResponse(message="The next cron report preserves C"),
        ])
        self.agent.model = model
        self.compactions()
        response = await self.http.post("/a2a/owner/message:send", headers=self.headers("owner-a"),
            json={"message": {"messageId": uuid.uuid4().hex, "contextId": row["context_id"],
                "role": "ROLE_USER", "parts": [{"text": "Correction: C. Publishing is planned only."}]}})
        self.assertEqual(response.status_code, 200, response.text)
        first = response.json()["task"]
        self.tasks.add(first["id"])
        previous = self.agent.workflow_store.lookup_task(first["id"])
        self.assertEqual(previous.state, "COMPLETED", previous.error_code)
        self.assertGreaterEqual(len(model.summaries), 2)
        summary = next(item for item in previous.snapshot["context"]["active"] if item["kind"] == "summary")
        self.assertIn("Use C", summary["content"])
        self.assertNotIn("Publishing", json.dumps(json.loads(summary["content"])["Completed"]))
        admitted = await self.run_schedule(row)
        await asyncio.to_thread(self.agent.resume_task, admitted.task.id)
        current = self.agent.workflow_store.lookup_task(admitted.task.id)
        self.assertEqual(current.state, "COMPLETED", current.error_code)
        self.assertEqual(current.context_id, previous.context_id)
        self.assertEqual(current.snapshot["previous_root_run_id"], previous.run_id)
        self.assertIn("Use C", model.calls[-1].context)
        self.assertIn("planned", model.calls[-1].context)
        self.assertIn("Confirmed C", model.calls[-1].context)
        self.assertEqual(self.store.events(self.tenant, schedule_id=row["id"])[-1]["run_id"], current.run_id)

    async def test_manual_and_automatic_occurrence_compete_for_one_chat_root(self):
        row = await self.schedule()
        cron_fixtures.CronStoreTests.due_at(self, row, datetime.now(UTC))
        manual, automatic = await asyncio.gather(self.run_schedule(row), self.automatic(row))
        if automatic is not None:
            self.tasks.add(automatic.task.id)
        admitted = [entry for entry in (manual, automatic) if entry is not None and entry.run_id]
        self.assertEqual(len(admitted), 1)
        winner = self.agent.workflow_store.lookup_task(admitted[0].task.id)
        self.assertEqual(winner.context_id, row["context_id"])
        self.assertEqual(winner.state, "RUNNING")
        events = self.store.events(self.tenant, schedule_id=row["id"])
        started = [entry for entry in events if entry["kind"] == "started" and entry["run_id"]]
        self.assertEqual([(entry["task_id"], entry["run_id"]) for entry in started], [(winner.task_id, winner.run_id)])
        if automatic is None:
            self.assertEqual(started[0]["source"], "manual")
            self.assertEqual([(entry["source"], entry["reason"]) for entry in events if entry["kind"] == "skipped"],
                [("automatic", "context_busy")])
        else:
            self.assertEqual(started[0]["source"], "automatic")
            self.assertIsNone(manual.run_id)
            self.assertIn("CONTEXT_BUSY", str(manual.task))
        self.assertIsNone(await self.automatic(row))
        self.assertEqual(sum(entry["kind"] == "started" and bool(entry["run_id"])
            for entry in self.store.events(self.tenant, schedule_id=row["id"])), 1)
        self.assertFalse(self.agent.model.calls)

    async def test_omitted_summary_pending_state_cannot_reopen_timer_or_recharge_work(self):
        # Keep this summary/wait proof independent of the detector's one-slot
        # contention policy, covered separately by tests.test_guardrails.
        self.policy("core_terminal_exec", "allow", guardrails_exempt=True)
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        model = SemanticModel([
            ModelResponse(tool_requests=(ToolRequest("background", "core_task_start", {
                "tool": "core_terminal_exec", "arguments": {"argv": ["sleep", "60"]}, "required": True,
            }),)),
            ModelResponse(tool_requests=(ToolRequest("timer", "core_wait_until", {"until": future}),)),
            ModelResponse(message="after the real timer"),
        ], omit_pending=True)
        self.agent.model = model
        self.compactions()
        task = await self.submit("owner-a", uuid.uuid4().hex, uuid.uuid4().hex)
        self.tasks.add(task["id"])
        record = self.agent.workflow_store.lookup_task(task["id"])
        wait_id = record.snapshot.get("wait_id")
        current_wait = self.agent.workflow_store.get_wait(wait_id, tenant_id=self.tenant) if wait_id else None
        self.assertEqual(record.state, "WAITING_TASK", (record.error_code,
            current_wait.kind if current_wait else None, current_wait.subject if current_wait else None))
        summary = next(item for item in record.snapshot["context"]["active"] if item["kind"] == "summary")
        self.assertEqual(json.loads(summary["content"])["Pending"], [])
        owned = self.agent.task_scheduler.list(owner_id=record.run_id, tenant_id=self.tenant)
        self.assertEqual(len(owned), 1)
        self.assertEqual(owned[0].state, "working")
        self.assertIn(owned[0].id, model.summaries[0]["context"])
        wait = self.agent.workflow_store.get_wait(record.snapshot["wait_id"], tenant_id=self.tenant)
        self.assertEqual(wait.kind, "timer")
        before = (record.snapshot["turns"], record.snapshot["tool_calls"], len(model.calls))
        for _ in range(2):
            result = await asyncio.to_thread(self.agent.resume_task, record.task_id)
            self.assertIsInstance(result, SuspendedRun)
            self.assertEqual(result.wait_id, wait.wait_id)
        current = self.agent.workflow_store.lookup_task(record.task_id)
        saved_wait = self.agent.workflow_store.get_wait(wait.wait_id, tenant_id=self.tenant)
        self.assertEqual((current.snapshot["turns"], current.snapshot["tool_calls"], len(model.calls)), before)
        self.assertEqual((saved_wait.deadline, saved_wait.generation, saved_wait.outcome),
            (wait.deadline, wait.generation, None))
        self.assertEqual(current.snapshot["wait_id"], wait.wait_id)
        self.assertIsNone(current.result)
        still_owned = self.agent.task_scheduler.get(owned[0].id, owner_id=record.run_id, tenant_id=self.tenant)
        self.assertEqual(still_owned.state, "working")
        self.assertIsNone(still_owned.result)

    async def test_followup_survives_remote_owner_and_auth_wait_before_next_model_call(self):
        self.policy("core_task_start", "deny")
        for binding in ("HTTP+JSON", "JSONRPC"):
            with self.subTest(binding=binding):
                self.compactions()
                self.scheduler = self.agent.task_scheduler
                peer_name = "peer-" + uuid.uuid4().hex
                self.app.state.remote_registry_store.create(self.tenant, {
                    "name": peer_name, "url": "https://peer.example/a2a", "description": "Peer owner wait",
                    "enabled": True, "header_name": "Authorization"}, actor_id="alice")
                wire, operation = [], {}

                def response(request, timeout):
                    payload = json.loads(request.data) if request.data else {}
                    method = payload.get("method") or ("GetTask" if request.method == "GET" else "SendMessage")
                    wire.append(method)
                    self.assertEqual(method, "SendMessage" if len(wire) == 1 else "GetTask")
                    state = ("INPUT_REQUIRED", "AUTH_REQUIRED", "COMPLETED")[len(wire) - 1]
                    task = {"id": "peer-task", "contextId": "peer-chat", "status": {"state": "TASK_STATE_" + state}}
                    if state == "COMPLETED":
                        task["artifacts"] = [{"artifactId": "peer-answer", "parts": [{"text": "peer completed"}]}]
                    result = {"task": task} if method == "SendMessage" else task
                    if binding == "JSONRPC":
                        result = {"jsonrpc": "2.0", "id": payload["id"], "result": result}
                    return io.BytesIO(json.dumps(result).encode())

                original_start = self.scheduler.start_remote
                def started(*args, **kwargs):
                    task = original_start(*args, **kwargs)
                    operation["id"] = task.id
                    model._responses[0] = ModelResponse(tool_requests=(ToolRequest("wait", "core_task_wait", {
                        "task_id": task.id}),))
                    model._responses[1] = stale_calls(task.id)
                    remote_fixtures.RemoteOperationTests.settle(self)
                    return self.scheduler.get(task.id, owner_id=kwargs["owner_id"], tenant_id=kwargs["tenant_id"])

                def stale_calls(task_id):
                    return ModelResponse(tool_requests=(
                        ToolRequest("closed-wait", "core_task_wait", {"task_id": task_id}),
                        ToolRequest("denied-action", "core_task_start", {
                            "tool": "core_terminal_exec", "arguments": {"argv": ["sleep", "60"]}}),
                    ))

                model = SemanticModel([
                    ModelResponse(tool_requests=(ToolRequest("send", "core_agent_send_message", {
                        "agent_name": peer_name, "task": "Ask peer owner. core_task_start is prohibited by owner policy.",
                        "files": []}),)),
                    ModelResponse(message="wait placeholder"), ModelResponse(message="stale call placeholder"),
                    ModelResponse(message="clarification applied"),
                ], omit_pending=True)
                self.agent.model = model
                def discovery(peer, **kwargs):
                    return RemoteAgentConnection(RemoteAgentCard(
                        peer["name"], peer["description"], peer["url"], False, (), binding))
                with patch("core_agent.remote_agents.connect_peer", side_effect=discovery), \
                        patch("core_agent.remote_agents._OPENER.open", side_effect=response), \
                        patch.object(self.scheduler, "start_remote", side_effect=started):
                    task = await self.submit("external-a", uuid.uuid4().hex, uuid.uuid4().hex)
                    self.tasks.add(task["id"])
                    record = self.agent.workflow_store.lookup_task(task["id"])
                    self.assertEqual(record.state, "WAITING_TASK", record.error_code)
                    wait_id = record.snapshot["wait_id"]
                    original_wait = self.agent.workflow_store.get_wait(wait_id, tenant_id=self.tenant)
                    followup = {"message": {"messageId": uuid.uuid4().hex, "taskId": record.task_id,
                        "role": "ROLE_USER", "parts": [{"text": "clarify route 29"}]}}
                    for _ in range(2):
                        accepted = await self.http.post("/a2a/external/message:send",
                            headers=self.headers("external-a"), json=followup)
                        self.assertEqual(accepted.status_code, 200, accepted.text)
                        self.assertEqual(accepted.json()["task"]["id"], record.task_id)
                    self.assertEqual(len(self.agent.workflow_store.pending_inbound(record)), 1)
                    busy = await self.submit("external-a", uuid.uuid4().hex, record.context_id)
                    self.assertIn("CONTEXT_BUSY", json.dumps(busy))
                    self.assertEqual(sum(not call.instructions.startswith("SEMANTIC CONTEXT SUMMARY") for call in model.calls), 2)
                    self.assertTrue(any("prohibited by owner policy" in call["context"] for call in model.summaries))
                    self.assertTrue(all("core_task_start" not in call.tools for call in model.calls))
                    self.assertEqual(wire, ["SendMessage"])
                    if self.use_postgres:
                        model = SemanticModel([stale_calls(operation["id"]),
                            ModelResponse(message="clarification applied")], omit_pending=True)
                        await self.restart(model)
                        self.scheduler = self.agent.task_scheduler
                    for expected in ("AUTH_REQUIRED", "COMPLETED"):
                        if self.use_postgres:
                            with self.store.database.transaction() as connection:
                                connection.execute("UPDATE core_background_tasks SET checkpoint = jsonb_set(checkpoint, "
                                    "'{next_poll_at}', '1'::jsonb) WHERE id=%s AND tenant_id=%s", (operation["id"], self.tenant))
                        else:
                            with self.scheduler._lock:
                                self.scheduler._remote[operation["id"]]["checkpoint"]["next_poll_at"] = 1
                        await asyncio.to_thread(self.agent.recover_durable_tasks)
                        await asyncio.to_thread(remote_fixtures.RemoteOperationTests.settle, self)
                        handle = self.scheduler.get(operation["id"], owner_id=record.run_id, tenant_id=self.tenant)
                        self.assertEqual(handle.result["remote_state"], "TASK_STATE_" + expected)
                        if expected == "AUTH_REQUIRED":
                            self.assertEqual(handle.state, "working")
                            waiting = await asyncio.to_thread(self.agent.resume_task, record.task_id)
                            self.assertIsInstance(waiting, SuspendedRun)
                            self.assertEqual(waiting.wait_id, wait_id)
                            self.assertEqual(len(self.agent.workflow_store.pending_inbound(record)), 1)
                        else:
                            self.assertEqual(handle.state, "completed")
                    with patch.object(self.agent, "_launch_recovery", return_value=False):
                        await asyncio.to_thread(self.agent._recover_workflows_once)
                    result = await asyncio.to_thread(self.agent.resume_task, record.task_id)
                    self.assertEqual(result.message, "clarification applied")
                    self.assertIn("clarify route 29", model.calls[-1].context)
                    self.assertIn("peer completed", model.calls[-1].context)
                    self.assertEqual(wire, ["SendMessage", "GetTask", "GetTask"])
                    finished = self.agent.workflow_store.lookup_task(record.task_id)
                    self.assertEqual(finished.state, "COMPLETED", finished.error_code)
                    self.assertEqual(self.agent.workflow_store.pending_inbound(finished), ())
                    closed = self.agent.workflow_store.get_wait(wait_id, tenant_id=self.tenant)
                    self.assertIsNotNone(closed.applied_at)
                    self.assertEqual((closed.wait_id, closed.generation, closed.subject, closed.deadline),
                        (original_wait.wait_id, original_wait.generation, original_wait.subject, original_wait.deadline))
                    self.assertEqual(closed.outcome["result"]["state"], "completed")
                    self.assertNotIn("wait_id", finished.snapshot)
                    events = self.agent.event_store.events(record.run_id,
                        **({"tenant_id": self.tenant} if self.use_postgres else {}))
                    self.assertEqual(sum(event.kind == "wait.entered" for event in events), 1)
                    self.assertEqual([handle.id for handle in self.scheduler.list(owner_id=record.run_id,
                        tenant_id=self.tenant)], [operation["id"]])
                    ordinary = [call for call in model.calls if not call.instructions.startswith("SEMANTIC CONTEXT SUMMARY")]
                    self.assertTrue(all("core_task_start" not in call.tools for call in ordinary))
                    stale_input = ordinary[0] if self.use_postgres else ordinary[2]
                    self.assertTrue(any(call.instructions.startswith("SEMANTIC CONTEXT SUMMARY")
                        and "TASK_STATE_COMPLETED" in call.context
                        for call in model.calls[:model.calls.index(stale_input)]),
                        "Closed-wait facts must be omitted by committed compaction before the stale call")
                    outcomes = [json.loads(item["content"]) for item in finished.snapshot["context"]["transcript"]
                        if item["kind"] == "tool_result"]
                    repeated = next(outcome for outcome in outcomes if outcome["tool_call_id"] == "closed-wait")
                    self.assertEqual(repeated["status"], "succeeded")
                    self.assertEqual((repeated["output"]["task_id"], repeated["output"]["state"]),
                        (operation["id"], "completed"))
                    denied = next(outcome for outcome in outcomes if outcome["tool_call_id"] == "denied-action")
                    self.assertEqual((denied["status"], denied["error_code"]), ("failed", "POLICY_DENIED"))
                    self.assertTrue(any("TASK_STATE_COMPLETED" in call["context"] for call in model.summaries))
                    summary = next(item for item in finished.snapshot["context"]["active"] if item["kind"] == "summary")
                    value = json.loads(summary["content"])
                    self.assertEqual((value["Constraints"], value["Pending"]), ([], []))
                    self.assertNotIn("prohibited", summary["content"])
                    self.assertNotIn("TASK_STATE_COMPLETED", summary["content"])


@unittest.skipUnless(auth_fixtures.TEST_DATABASE_URL, "TEST_DATABASE_URL is required")
class PostgresEnterpriseReleaseBoundaryTests(EnterpriseReleaseBoundaryTests):
    use_postgres = True
    push_encryption_key = base64.urlsafe_b64encode(b"release-proof-key-material-32byt"[:32]).decode()

    async def restart(self, model):
        self.app.state.close()
        await self.http.aclose()
        with patch("core_agent.runtime.CoreAgent.recover_workflows") as recovery:
            self.app = create_app(model=model, database=PostgresDatabase(auth_fixtures.TEST_DATABASE_URL,
                min_size=1, max_size=1, timeout=3), base_url="https://agent.example.test",
                auth_transport=httpx.MockTransport(self.introspect), push_client=self.push_client)
        self.addCleanup(self.app.state.close)
        self.reconcile = recovery.call_args.kwargs["on_settled"]
        self.bind_services()
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://agent.example.test")
        self.addAsyncCleanup(self.http.aclose)

    async def test_disable_and_delete_preserve_active_hitl_files_and_task_after_app_restart(self):
        for action in ("disable", "delete"):
            with self.subTest(action=action):
                self.policy("core_task_list", "require_hitl")
                row = await self.schedule()
                self.agent.model = ScriptedModel([ModelResponse(tool_requests=(ToolRequest("owned-read", "core_task_list", {}),))])
                admitted = await self.run_schedule(row)
                await asyncio.to_thread(self.agent.resume_task, admitted.task.id)
                record = self.agent.workflow_store.lookup_task(admitted.task.id)
                approval = await self.pending(record.task_id)
                wait = self.agent.workflow_store.get_wait(approval["wait_id"], tenant_id=self.tenant)
                binding = WorkspaceBinding(self.tenant, record.owner_id, record.context_id)
                workspaces = self.agent.tool_runtime.environment_manager.backend.chats
                (workspaces.workspace(binding) / "retained.txt").write_bytes(b"active cron retained file")
                path = "/api/schedules/" + row["id"]
                if action == "disable":
                    values = {key: row[key] for key in ("prompt", "expression", "timezone", "enabled")}
                    response = await self.http.put(path, headers=self.headers("owner-b"),
                        json={**values, "enabled": False, "expected_revision": row["revision"]})
                else:
                    response = await self.http.request("DELETE", path, headers=self.headers("owner-b"),
                        json={"expected_revision": row["revision"]})
                self.assertEqual(response.status_code, 200, response.text)
                next_model = ScriptedModel([ModelResponse(message="original cron completed")])
                next_model.model = "auth-test-model"
                await self.restart(next_model)
                saved = self.agent.workflow_store.lookup_task(record.task_id)
                restored_wait = self.agent.workflow_store.get_wait(wait.wait_id, tenant_id=self.tenant)
                self.assertEqual((saved.run_id, saved.snapshot["wait_id"], restored_wait.deadline, restored_wait.subject),
                    (record.run_id, wait.wait_id, wait.deadline, wait.subject))
                self.assertIsNone(restored_wait.outcome)
                self.assertIsNone(await self.automatic(row))
                self.assertFalse(next_model.calls)
                file_response = await self.http.get(f"/api/chats/{record.context_id}/files/content",
                    headers=self.headers("owner-b"), params={"path": "retained.txt"})
                self.assertEqual(file_response.status_code, 200, file_response.text)
                self.assertEqual(file_response.content, b"active cron retained file")
                approval = await self.pending(record.task_id)
                allowed = await self.http.post(f"/api/hitl/{wait.wait_id}/decision", headers=self.headers("owner-b"),
                    json={"decision": "allow", "subject_digest": approval["subject_digest"]})
                self.assertEqual(allowed.status_code, 200, allowed.text)
                result = await asyncio.to_thread(self.agent.resume_task, record.task_id)
                self.assertEqual(result.message, "original cron completed")
                completed = self.agent.workflow_store.lookup_task(record.task_id)
                self.assertEqual(completed.state, "COMPLETED", completed.error_code)
                self.assertEqual(completed.snapshot["tool_calls"], 1)
                self.assertIsNotNone(self.agent.workflow_store.get_wait(wait.wait_id, tenant_id=self.tenant).applied_at)

    async def test_owner_question_answer_remain_private_across_restart_and_all_public_projections(self):
        question, answer = "private-owner-question-release", "private-owner-answer-release-" + "canary" * 2000
        self.policy("core_ask_owner", "allow")
        self.agent.model = ScriptedModel([ModelResponse(message="private-owner-intermediate-release",
            reasoning="private-owner-reasoning-release", tool_requests=(ToolRequest("private-question", "core_ask_owner", {"question": question}),))])
        task = await self.submit("external-a", uuid.uuid4().hex, uuid.uuid4().hex)
        self.tasks.add(task["id"])
        record = self.agent.workflow_store.lookup_task(task["id"])
        waiting = await self.pending(record.task_id)
        self.assertEqual(waiting["kind"], "owner_question")
        public_bytes = b"approved public response file"
        binding = WorkspaceBinding(self.tenant, record.owner_id, record.context_id)
        workspaces = self.agent.tool_runtime.environment_manager.backend.chats
        (workspaces.workspace(binding) / "public.txt").write_bytes(public_bytes)
        dns = [(2, 1, 6, "", ("93.184.216.34", 443))]
        with patch("core_agent.push.socket.getaddrinfo", return_value=dns):
            await self.app.state.push_sender.config_store.set_info(record.task_id,
                TaskPushNotificationConfig(task_id=record.task_id, url="https://push.example/hook"), self.context("external-a"))
        model = ScriptedModel([
            ModelResponse(tool_requests=(ToolRequest("public-file", "core_response_files", {"paths": ["public.txt"]}),)),
            ModelResponse(message="public final release proof"),
        ])
        model.model = "auth-test-model"
        await self.restart(model)
        private = (question, "private-owner-answer-release", "private-owner-intermediate-release", "private-owner-reasoning-release")
        for route in ("/a2a/external/tasks/" + record.task_id, "/a2a/external/tasks"):
            response = await self.http.get(route, headers=self.headers("external-a"),
                params={"includeArtifacts": "true"} if route.endswith("/tasks") else {})
            self.assertEqual(response.status_code, 200, response.text)
            for marker in private:
                self.assertNotIn(marker, response.text)
        for route in (f"/api/chats/{record.context_id}/history", f"/api/chats/{record.context_id}/files",
                      f"/api/chats/{record.context_id}/tasks/{record.task_id}/files/unknown-file"):
            response = await self.http.get(route, headers=self.headers("external-a"))
            self.assertEqual(response.status_code, 403, response.text)
        waiting = await self.pending(record.task_id)
        self.assertEqual(waiting["subject"]["question"], question)
        answered = await self.http.post(f"/api/questions/{waiting['wait_id']}/answer", headers=self.headers("owner-b"),
            json={"answer": answer, "subject_digest": waiting["subject_digest"]})
        self.assertEqual(answered.status_code, 200, answered.text)
        result = await asyncio.to_thread(self.agent.resume_task, record.task_id)
        self.assertEqual(result.message, "public final release proof")
        self.assertIn(answer, json.dumps(model.calls[-1].messages))
        self.reconcile(record.task_id)
        for route in ("/a2a/external/tasks/" + record.task_id, "/a2a/external/tasks",
                      "/a2a/external/tasks/" + record.task_id + ":subscribe"):
            response = await self.http.get(route, headers=self.headers("external-a"),
                params={"includeArtifacts": "true"} if route.endswith("/tasks") else {})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("public final release proof", response.text)
            self.assertIn(base64.b64encode(public_bytes).decode(), response.text)
            for marker in private:
                self.assertNotIn(marker, response.text)
        with patch("core_agent.push.socket.getaddrinfo", return_value=dns):
            await self.app.state.push_sender.dispatch_pending()
        self.assertTrue(self.push_payloads)
        self.assertIn("public final release proof", json.dumps(self.push_payloads))
        for marker in private:
            self.assertNotIn(marker, json.dumps(self.push_payloads))
        completed = self.agent.workflow_store.lookup_task(record.task_id)
        self.assertEqual(completed.state, "COMPLETED", completed.error_code)
        self.assertEqual([ref["name"] for ref in completed.result["outgoing_files"]], ["public.txt"])
        receipt = completed.result["outgoing_files"][0]
        path = f"/api/chats/{record.context_id}/tasks/{record.task_id}/files/{receipt['file_id']}"
        owned = await self.http.get(path, headers=self.headers("owner-b"))
        self.assertEqual(owned.status_code, 200, owned.text)
        self.assertEqual(owned.content, public_bytes)
        denied = await self.http.get(path, headers=self.headers("external-a"))
        self.assertEqual(denied.status_code, 403, denied.text)
