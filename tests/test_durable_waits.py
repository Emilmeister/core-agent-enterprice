import asyncio
import json
import os
import tempfile
import threading
import uuid
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from tests.app_support import create_app
from core_agent.errors import CoreError
from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
from core_agent.tools import ToolCall
from tests.test_auth import AuthAppTestCase

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


class DurableTimerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict(os.environ, {
            "CORE_AGENT_ENVIRONMENT": "test", "CORE_AGENT_MEMORY": "disabled",
            "LOCAL_WORKSPACE_ROOT": self.temp.name + "/scratch",
            "CHAT_WORKSPACE_ROOT": self.temp.name + "/chats",
            "CORE_AGENT_ALLOWED_BUILTIN_TOOLS": "core_wait_until,core_task_wait",
            "LOG_LEVEL": "ERROR",
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.now = [1800000000.0]

    def app(self, *responses):
        model = ScriptedModel(responses)
        model.model = "wait-test"
        with patch("core_agent.runtime.CoreAgent.recover_workflows", return_value=()):
            app = create_app(model=model)
        self.addCleanup(app.state.close)
        agent = app.state.core_agent
        agent.workflow_store.clock = lambda: self.now[0]
        return agent, model

    def until(self, offset=60):
        return datetime.fromtimestamp(self.now[0] + offset, timezone.utc).isoformat()

    def timer(self, until):
        return ModelResponse(tool_requests=(ToolRequest("timer-call", "core_wait_until", {"until": until}),))

    def test_timer_suspends_and_recovers_same_charged_call(self):
        from core_agent.workflow import SuspendedRun

        agent, model = self.app(self.timer(self.until()), ModelResponse(message="awake"))
        sleeping = agent.run({"prompt": "wait"}, task_id="task", identity="owner", session_id="chat", tenant_id="company")
        self.assertIsInstance(sleeping, SuspendedRun)
        self.assertEqual(len(model.calls), 1)
        record = agent.workflow_store.lookup_task("task")
        self.assertEqual(record.state, "WAITING_TASK")
        self.assertEqual(record.snapshot["tool_calls"], 1)
        self.assertNotIn(record.run_id, agent.workflow_store._leases)
        agent._runtime_cache.clear()
        again = agent.resume_task("task")
        self.assertEqual(again.wait_id, sleeping.wait_id)
        self.assertEqual(len(model.calls), 1)
        self.now[0] += 61
        agent.workflow_store.expire_waits()
        result = agent.resume_task("task")
        self.assertEqual(result.message, "awake")
        self.assertEqual(result.usage.tool_calls, 1)
        self.assertIn('"reason": "time"', model.calls[-1].context)
        wait = agent.workflow_store.get_wait(sleeping.wait_id, tenant_id="company")
        self.assertIsNotNone(wait.applied_at)

    def test_followup_wakes_only_current_timer_and_is_delivered_before_model(self):
        agent, model = self.app(self.timer(self.until()), self.timer(self.until(120)), ModelResponse(message="awake"))
        first = agent.run({"prompt": "wait"}, task_id="task", identity="owner", session_id="chat", tenant_id="company")
        agent.enqueue_message({"prompt": "new order number 42"}, task_id="task", message_id="message", identity="owner", session_id="chat", tenant_id="company")
        second = agent.resume_task("task")
        self.assertNotEqual(first.wait_id, second.wait_id)
        self.assertIn("new order number 42", model.calls[1].context)
        self.assertIn('"reason": "message"', model.calls[1].context)
        agent.enqueue_message({"prompt": "new order number 42"}, task_id="task", message_id="message", identity="owner", session_id="chat", tenant_id="company")
        self.assertEqual(agent.resume_task("task").wait_id, second.wait_id)
        self.assertEqual(len(model.calls), 2)

    def test_past_timer_and_invalid_time_return_results_without_suspending(self):
        for value, expected in ((self.until(-1), '"reason": "time"'), ("2026-10-01T15:00:00", "TOOL_ARGUMENT_INVALID")):
            with self.subTest(until=value):
                agent, model = self.app(self.timer(value), ModelResponse(message="continued"))
                self.assertEqual(agent.run({"prompt": "wait"}).message, "continued")
                self.assertIn(expected, model.calls[-1].context)

    def test_local_task_wait_keeps_followup_until_completion_and_timeout_does_not_cancel(self):
        for timeout in (None, 30):
            with self.subTest(timeout=timeout):
                arguments = {"task_id": "child"}
                if timeout is not None:
                    arguments["timeout"] = timeout
                agent, model = self.app(ModelResponse(tool_requests=(ToolRequest("wait", "core_task_wait", arguments),)), ModelResponse(message="continued"))
                record, *_ = agent._new_workflow({"prompt": "wait"}, task_id="task", identity="owner", session_id="chat", tenant_id="company")
                release = threading.Event()
                self.addCleanup(release.set)
                task = agent.task_scheduler.start(lambda: release.wait(5), task_id="child", owner_id=record.run_id, tenant_id="company")
                sleeping = agent.resume_task("task")
                agent.enqueue_message({"prompt": "new detail"}, task_id="task", message_id="detail", identity="owner", session_id="chat", tenant_id="company")
                self.assertEqual(agent.resume_task("task").wait_id, sleeping.wait_id)
                if timeout is None:
                    release.set()
                    agent.task_scheduler.wait(task.id, timeout=5, owner_id=record.run_id, tenant_id="company")
                    with patch.object(agent, "_launch_recovery", return_value=False):
                        agent._recover_workflows_once()
                else:
                    self.now[0] += 31
                    agent.workflow_store.expire_waits()
                    self.assertFalse(task.cancel_event.is_set())
                    self.assertEqual(task.state, "working")
                result = agent.resume_task("task")
                self.assertEqual(result.message, "continued")
                self.assertEqual(result.usage.tool_calls, 1)
                self.assertIn("new detail", model.calls[-1].context)
                release.set()
                agent.task_scheduler.wait(task.id, timeout=5, owner_id=record.run_id, tenant_id="company")

    def test_joined_child_timer_suspends_both_runs_and_recovers_same_child(self):
        from core_agent.workflow import SuspendedRun

        delegate = ToolRequest("delegate", "core_delegate", {"instruction": "wait and answer", "tools": ["core_wait_until"], "skills": [], "budget": {"turns": 3, "tool_calls": 1}})
        with patch.dict(os.environ, {"CORE_AGENT_ALLOWED_BUILTIN_TOOLS": "core_delegate,core_wait_until"}):
            agent, model = self.app(ModelResponse(tool_requests=(delegate,)), self.timer(self.until()), ModelResponse(message="child done"), ModelResponse(message="parent done"))
        sleeping = agent.run({"prompt": "delegate"}, task_id="task", identity="owner", session_id="chat", tenant_id="company")
        self.assertIsInstance(sleeping, SuspendedRun)
        parent = agent.workflow_store.lookup_task("task")
        wait = agent.workflow_store.get_wait(sleeping.wait_id, tenant_id="company")
        child_id = wait.subject["task_id"]
        with agent.task_scheduler._lock:
            workers = tuple(agent.task_scheduler._threads)
        for worker in workers:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        child = agent.workflow_store.lookup_task(child_id)
        self.assertEqual(child.parent_run_id, parent.run_id)
        self.assertEqual(child.state, "WAITING_TASK")
        self.assertEqual(agent.recover_durable_tasks(), 0)
        self.now[0] += 61
        agent.workflow_store.expire_waits()
        self.assertEqual(agent.recover_durable_tasks(), 1)
        task = agent.task_scheduler.wait(child_id, timeout=5, owner_id=parent.run_id, tenant_id="company")
        self.assertEqual(task.state, "completed", task.error)
        with patch.object(agent, "_launch_recovery", return_value=False):
            agent._recover_workflows_once()
        result = agent.resume_task("task")
        self.assertEqual(result.message, "parent done")
        self.assertEqual(result.usage.tool_calls, 1)
        self.assertEqual(agent.task_scheduler.count(owner_id=parent.run_id, tenant_id="company"), 1)
        self.assertEqual(len(model.calls), 4)

    def test_stale_joined_admission_does_not_create_a_second_child(self):
        with patch.dict(os.environ, {"CORE_AGENT_ALLOWED_BUILTIN_TOOLS": "core_delegate"}):
            agent, model = self.app(ModelResponse(message="child"), ModelResponse(message="duplicate"))
        record, *_ = agent._new_workflow({"prompt": "parent"}, task_id="task", identity="owner", session_id="chat", tenant_id="company")
        token = agent.workflow_store.acquire_lease(record.run_id, tenant_id="company", owner_id="owner", worker_id=agent._worker_id, ttl=60)
        call = ToolCall("joined", "core_delegate", {"instruction": "child", "tools": [], "skills": [], "budget": {"turns": 2, "tool_calls": 1}})
        snapshot = {**record.snapshot, "pending_call": {"id": call.id, "name": call.name, "arguments": call.arguments}}
        run_context = agent._run_contexts[record.run_id]
        run_scope = agent._run_scopes[record.run_id]
        agent._delegate(call.arguments, record.run_id, wait_context=(record, snapshot, call, token))
        # A stale replica still holds its original runtime context and lease token.
        agent._run_contexts[record.run_id] = run_context
        agent._run_scopes[record.run_id] = run_scope
        with self.assertRaises(CoreError):
            agent._delegate(call.arguments, record.run_id, wait_context=(record, snapshot, call, token))
        tasks = agent.task_scheduler.list(owner_id=record.run_id, tenant_id="company")
        self.assertEqual(len(tasks), 1)
        agent.task_scheduler.wait(tasks[0].id, timeout=5, owner_id=record.run_id, tenant_id="company")
        self.assertEqual(len(model.calls), 1)

    def test_failure_after_joined_admission_preserves_suspension_for_recovery(self):
        from core_agent.workflow import SuspendedRun

        delegate = ToolRequest("delegate", "core_delegate", {"instruction": "child", "tools": [], "skills": [], "budget": {"turns": 2, "tool_calls": 1}})
        with patch.dict(os.environ, {"CORE_AGENT_ALLOWED_BUILTIN_TOOLS": "core_delegate"}):
            agent, _ = self.app(ModelResponse(tool_requests=(delegate,)), ModelResponse(message="child"))
        start = agent.task_scheduler.start

        def admitted_then_failed(*args, **kwargs):
            start(*args, **kwargs)
            raise RuntimeError("worker startup failure after commit")

        with patch.object(agent.task_scheduler, "start", side_effect=admitted_then_failed):
            result = agent.run({"prompt": "delegate"}, task_id="task", identity="owner", session_id="chat", tenant_id="company")
        self.assertIsInstance(result, SuspendedRun)
        record = agent.workflow_store.lookup_task("task")
        self.assertEqual(record.state, "WAITING_TASK")
        self.assertEqual(record.snapshot["wait_id"], result.wait_id)
        self.assertEqual(agent.task_scheduler.count(owner_id=record.run_id, tenant_id="company"), 1)

    def test_pending_wait_scan_reaches_completed_task_after_a_full_unresolved_batch(self):
        from core_agent.tasks import BackgroundTask
        from core_agent.workflow import WorkflowRecord

        agent, _ = self.app()
        for index in range(101):
            record = agent.workflow_store.create(WorkflowRecord(f"run-{index}", f"task-{index}", f"chat-{index}", "company", "owner", None, "RUNNING", 1, {"prompt": "wait"}, {}))
            token = agent.workflow_store.acquire_lease(record.run_id, tenant_id="company", owner_id="owner", worker_id="fixture", ttl=60)
            task = BackgroundTask(f"child-{index}", record.run_id, False, state="completed" if index == 100 else "working")
            agent.task_scheduler._tasks[task.id] = task
            wait = agent.workflow_store.enter_wait(record, kind="task", source_id="wait", subject={"task_id": task.id}, continuation={"version": 1, "phase": "tool_wait", "call_id": "wait"}, deadline=None, snapshot=record.snapshot, lease_token=token)
            self.now[0] += 1
        with patch.object(agent, "_launch_recovery", return_value=False):
            agent._recover_workflows_once()
            agent._recover_workflows_once()
        self.assertEqual(agent.workflow_store.get_wait(wait.wait_id, tenant_id="company").outcome["reason"], "task")

    def test_parent_cancel_closes_child_and_grandchild_waits_without_more_model_calls(self):
        def delegate(call_id, tools):
            return ModelResponse(tool_requests=(ToolRequest(call_id, "core_delegate", {
                "instruction": call_id, "tools": tools, "skills": [],
                "budget": {"turns": 4, "tool_calls": 2},
            }),))

        with patch.dict(os.environ, {"CORE_AGENT_ALLOWED_BUILTIN_TOOLS": "core_delegate,core_wait_until"}):
            agent, model = self.app(delegate("child", ["core_delegate", "core_wait_until"]), delegate("grandchild", ["core_wait_until"]), self.timer(self.until()))
        agent.run({"prompt": "delegate"}, task_id="task", identity="owner", session_id="chat", tenant_id="company")
        # Each admission starts the next worker before returning; join each layer.
        while True:
            with agent.task_scheduler._lock:
                workers = tuple(agent.task_scheduler._threads)
            if not workers:
                break
            for worker in workers:
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
        waits = agent.workflow_store.pending_waits()
        self.assertEqual(len(waits), 3)
        agent.enqueue_message({"prompt": "new detail"}, task_id="task", message_id="detail", identity="owner", session_id="chat", tenant_id="company")
        agent.cancel_task("task")
        agent.recover_durable_tasks()
        for record in tuple(agent.workflow_store._records.values()):
            self.assertEqual(record.state, "CANCELLED")
            self.assertFalse(agent.workflow_store.pending_inbound(record))
        for wait in waits:
            self.assertIsNotNone(agent.workflow_store.get_wait(wait.wait_id, tenant_id="company").applied_at)
        self.assertEqual(len(model.calls), 3)


class A2ADurableTimerTests(AuthAppTestCase):
    async def asyncSetUp(self):
        def configured_app(**kwargs):
            with patch.dict(os.environ, {"CORE_AGENT_ALLOWED_BUILTIN_TOOLS": "core_wait_until", "LOG_LEVEL": "ERROR"}):
                return create_app(**kwargs)

        with patch("tests.test_auth.create_app", configured_app), patch("core_agent.runtime.CoreAgent.recover_workflows", return_value=()):
            await super().asyncSetUp()
        self.now = [1800000000.0]
        self.agent = self.app.state.core_agent
        self.agent.workflow_store.clock = lambda: self.now[0]
        self.agent.model = ScriptedModel([
            ModelResponse(tool_requests=(ToolRequest("timer", "core_wait_until", {"until": "2027-01-15T08:01:00Z"}),)),
            ModelResponse(message="awake"),
        ])

    async def test_a2a_timer_returns_working_and_recovery_publishes_final_artifact(self):
        task = await asyncio.wait_for(self.submit("external-a", "timer-message", "timer-chat"), 5)
        self.assertEqual(task["status"]["state"], "TASK_STATE_WORKING")
        self.assertFalse(task.get("artifacts"))
        self.now[0] += 61
        self.agent.workflow_store.expire_waits()
        await asyncio.to_thread(self.agent.resume_task, task["id"])
        response = await self.http.get("/a2a/external/tasks/" + task["id"], headers=self.headers("external-a"))
        self.assertEqual(response.status_code, 200, response.text)
        completed = response.json()
        self.assertEqual(completed["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(completed["artifacts"][0]["parts"][0]["text"], "awake")
        denied = await self.http.get("/a2a/external/tasks/" + task["id"], headers=self.headers("external-b"))
        self.assertEqual(denied.status_code, 404)

    async def test_stream_finishes_execution_without_completing_sleeping_task(self):
        response = await asyncio.wait_for(self.http.post(
            "/a2a/external/message:stream", headers=self.headers("external-a"),
            json={"message": {"messageId": "stream-timer", "role": "ROLE_USER", "parts": [{"text": "wait"}]}},
        ), 5)
        self.assertEqual(response.status_code, 200, response.text)
        frames = [json.loads(line.removeprefix("data:")) for line in response.text.splitlines() if line.startswith("data:")]
        self.assertTrue(frames)
        self.assertNotIn("TASK_STATE_COMPLETED", response.text)
        self.assertNotIn("TASK_STATE_FAILED", response.text)
        self.assertNotIn("SuspendedRun", response.text)
        task_id = next(iter(self.agent.workflow_store._records.values())).task_id
        task = await self.http.get("/a2a/external/tasks/" + task_id, headers=self.headers("external-a"))
        self.assertEqual(task.json()["status"]["state"], "TASK_STATE_WORKING")

    async def test_followup_wakes_timer_and_cancel_closes_wait_without_model_call(self):
        task = await self.submit("external-a", "timer-message", "timer-chat")
        response = await self.http.post(
            "/a2a/external/message:send", headers=self.headers("external-a"),
            json={"message": {"messageId": "wake-message", "taskId": task["id"], "role": "ROLE_USER", "parts": [{"text": "order 42"}]}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        record = self.agent.workflow_store.lookup_task(task["id"])
        wait_id = record.snapshot["wait_id"]
        self.assertTrue(record.snapshot["wait_ready"])
        result = await self.http.post("/a2a/external/tasks/" + task["id"] + ":cancel", headers=self.headers("external-a"), json={})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["status"]["state"], "TASK_STATE_CANCELED")
        record = self.agent.workflow_store.lookup_task(task["id"])
        self.assertEqual(record.state, "CANCELLED")
        self.assertFalse(self.agent.workflow_store.pending_inbound(record))
        self.assertIsNotNone(self.agent.workflow_store.get_wait(wait_id, tenant_id=record.tenant_id).applied_at)
        self.assertEqual(len(self.agent.model.calls), 1)


@unittest.skipUnless(TEST_DATABASE_URL, "set TEST_DATABASE_URL for durable runtime restart tests")
class PostgresDurableWaitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict(os.environ, {
            "CORE_AGENT_ENVIRONMENT": "test", "CORE_AGENT_MEMORY": "disabled",
            "LOCAL_WORKSPACE_ROOT": self.temp.name + "/scratch",
            "CHAT_WORKSPACE_ROOT": self.temp.name + "/chats",
            "DURABLE_STORAGE_ROOT": self.temp.name + "/durable",
            "CORE_AGENT_ALLOWED_BUILTIN_TOOLS": "core_delegate,core_wait_until",
            "LOG_LEVEL": "ERROR",
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.tenant = "wait-" + uuid.uuid4().hex
        self.task_id = uuid.uuid4().hex

    def app(self, *responses):
        from core_agent.database import PostgresDatabase

        model = ScriptedModel(responses)
        model.model = "postgres-wait-test"
        database = PostgresDatabase(TEST_DATABASE_URL, min_size=0, max_size=8)
        with patch("core_agent.runtime.CoreAgent.recover_workflows", return_value=()):
            app = create_app(model=model, database=database)
        self.addCleanup(app.state.close)
        return app, app.state.core_agent, model

    def test_joined_timer_child_survives_new_pools_and_applies_once(self):
        import time

        until = datetime.fromtimestamp(time.time() + 3600, timezone.utc).isoformat()
        delegate = ToolRequest("delegate", "core_delegate", {"instruction": "wait", "tools": ["core_wait_until"], "skills": [], "budget": {"turns": 3, "tool_calls": 1}})
        first, agent, _ = self.app(
            ModelResponse(tool_requests=(delegate,)),
            ModelResponse(tool_requests=(ToolRequest("timer", "core_wait_until", {"until": until}),)),
        )
        sleeping = agent.run({"prompt": "delegate"}, task_id=self.task_id, identity="owner", session_id="chat", tenant_id=self.tenant)
        parent = agent.workflow_store.lookup_task(self.task_id)
        parent_wait = agent.workflow_store.get_wait(sleeping.wait_id, tenant_id=self.tenant)
        child_id = parent_wait.subject["task_id"]
        with agent.task_scheduler._lock:
            workers = tuple(agent.task_scheduler._threads)
        for worker in workers:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        child = agent.workflow_store.lookup_task(child_id)
        timer = agent.workflow_store.get_wait(child.snapshot["wait_id"], tenant_id=self.tenant)
        self.assertEqual(child.state, "WAITING_TASK")
        with first.state.database.pool.connection() as connection:
            claim = connection.execute("SELECT claim_token FROM core_background_tasks WHERE id = %s", (child_id,)).fetchone()
        self.assertIsNone(claim["claim_token"])
        first.state.close()

        _, restarted, model = self.app(ModelResponse(message="child done"), ModelResponse(message="parent done"))
        self.assertEqual(restarted.resume_task(self.task_id).wait_id, sleeping.wait_id)
        restored_timer = restarted.workflow_store.get_wait(timer.wait_id, tenant_id=self.tenant)
        self.assertEqual(restored_timer.deadline, timer.deadline)
        self.assertEqual(len(model.calls), 0)
        restarted.enqueue_message({"prompt": "wake now"}, task_id=child_id, message_id="wake", identity="owner", session_id="chat", tenant_id=self.tenant)
        self.assertEqual(restarted.recover_durable_tasks(), 1)
        task = restarted.task_scheduler.wait(child_id, timeout=5, owner_id=parent.run_id, tenant_id=self.tenant)
        self.assertEqual(task.state, "completed", task.error)
        with patch.object(restarted, "_launch_recovery", return_value=False):
            restarted._recover_workflows_once()
        result = restarted.resume_task(self.task_id)
        self.assertEqual(result.message, "parent done")
        self.assertEqual(result.usage.tool_calls, 1)
        self.assertEqual(restarted.resume_task(self.task_id), result)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(restarted.task_scheduler.count(owner_id=parent.run_id, tenant_id=self.tenant), 1)
        self.assertIsNotNone(restarted.workflow_store.get_wait(timer.wait_id, tenant_id=self.tenant).applied_at)
        self.assertIsNotNone(restarted.workflow_store.get_wait(parent_wait.wait_id, tenant_id=self.tenant).applied_at)


if __name__ == "__main__":
    unittest.main()
