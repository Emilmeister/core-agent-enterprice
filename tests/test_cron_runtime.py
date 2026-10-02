"""Cron creation uses the real authenticated tool gates and a fenced store mutation."""
import asyncio
import threading
from contextvars import copy_context
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core_agent.errors import CoreError
from core_agent.interactions import tool_origin
from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
from tests.test_auth import AuthAppTestCase, TEST_DATABASE_URL


class CronRuntimeTests(AuthAppTestCase):
    automatic_tools = False

    async def asyncSetUp(self):
        with patch("core_agent.runtime.CoreAgent.recover_workflows", return_value=()):
            await super().asyncSetUp()
        self.agent = self.app.state.core_agent
        self.tenant = self.app.state.authenticator.settings.tenant
        self.store = self.agent.cron_store

    def policy(self, mode, name="core_cron_create"):
        previous = self.agent.interaction_store.get_policy(self.tenant, name, tool_origin(name))
        self.agent.interaction_store.update_policy(self.tenant, name, previous.origin, mode=mode,
            guardrails_exempt=False, expected_revision=previous.revision, actor_id="owner-test")

    def script(self, *calls):
        model = ScriptedModel([*calls, ModelResponse(message="done")])
        self.agent.model = model
        return model

    @staticmethod
    def call(call_id="cron", **args):
        return ModelResponse(tool_requests=(ToolRequest(call_id, "core_cron_create", {
            "prompt": "Daily report", "expression": "0 9 * * *", **args}),))

    async def test_default_approval_then_single_creation_in_external_owned_chat(self):
        model = self.script(self.call())
        task = await self.submit("external-a", "initial", "cron-chat")
        record = self.agent.workflow_store.lookup_task(task["id"])
        self.assertEqual(record.state, "WAITING_INPUT")
        self.assertEqual(self.store.list(self.tenant), [])
        wait = self.agent.workflow_store.get_wait(record.snapshot["wait_id"], tenant_id=self.tenant)
        self.assertEqual(wait.subject["tool_name"], "core_cron_create")
        self.assertEqual(wait.subject["resolved_parameters"], {"timezone": "Europe/Moscow"})
        self.assertEqual(wait.subject["arguments"], {"prompt": "Daily report", "expression": "0 9 * * *"})
        self.agent.workflow_store.resolve_wait(wait.wait_id, tenant_id=self.tenant,
            outcome={"reason": "allowed"}, actor_id="owner-test")
        await asyncio.to_thread(self.agent.resume_task, task["id"])
        await asyncio.to_thread(self.agent.resume_task, task["id"])
        rows = self.store.list(self.tenant)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["context_id"], "cron-chat")
        self.assertEqual(rows[0]["timezone"], "Europe/Moscow")
        event = self.store.events(self.tenant, schedule_id=rows[0]["id"])[0]
        self.assertEqual((event["source"], event["owner_id"]), ("tool", record.owner_id))
        self.assertIn(rows[0]["id"], str(model.calls[-1].messages))
        self.assertEqual(self.agent.workflow_store.lookup_task(task["id"]).snapshot["tool_calls"], 1)

    async def test_resolved_timezone_binds_digest_and_stale_approval(self):
        from core_agent.owner_api import interaction_digest
        model = self.script(self.call(timezone="Europe/Berlin"))
        task = await self.submit("owner-a", "timezone", "timezone-chat")
        record = self.agent.workflow_store.lookup_task(task["id"])
        wait = self.agent.workflow_store.get_wait(record.snapshot["wait_id"], tenant_id=self.tenant)
        self.assertEqual(wait.subject["resolved_parameters"], {"timezone": "Europe/Berlin"})
        self.assertEqual(wait.subject["arguments"]["timezone"], "Europe/Berlin")
        changed = replace(wait, subject={**wait.subject, "resolved_parameters": {"timezone": "UTC"}})
        self.assertNotEqual(interaction_digest(wait), interaction_digest(changed))
        self.agent.workflow_store.resolve_wait(wait.wait_id, tenant_id=self.tenant,
            outcome={"reason": "allowed"}, actor_id="owner-test")
        subject = self.agent._approval_subject
        def changed_subject(*args):
            return {**subject(*args), "resolved_parameters": {"timezone": "UTC"}}
        with patch.object(self.agent, "_approval_subject", side_effect=changed_subject):
            await asyncio.to_thread(self.agent.resume_task, task["id"])
        self.assertEqual(self.store.list(self.tenant), [])
        self.assertIn("TOOL_APPROVAL_STALE", str(model.calls[-1].messages))

    async def test_deny_catalog_and_stale_dispatch(self):
        self.policy("deny")
        model = self.script(self.call())
        task = await self.submit("owner-a", "denied", "deny-chat")
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertTrue(all("core_cron_create" not in call.tools for call in model.calls))
        self.assertIn("POLICY_DENIED", str(model.calls[-1].messages))
        self.assertEqual(self.store.list(self.tenant), [])

    async def test_allowed_reused_call_ids_are_distinct_attempts_and_exact_schema(self):
        self.policy("allow")
        model = self.script(self.call("same"), self.call("same"), self.call("bad", context_id="foreign"))
        await self.submit("owner-a", "allowed", "same-chat")
        self.assertEqual(len(self.store.list(self.tenant)), 2)
        self.assertIn("TOOL_ARGUMENT_INVALID", str(model.calls[-1].messages))
        definition = self.agent.tool_runtime.registry.get("core_cron_create")
        self.assertTrue(definition.mutating)
        self.assertEqual(definition.risk_tags, frozenset({"external_write"}))
        self.assertEqual(set(definition.input_schema["properties"]), {"prompt", "expression", "timezone"})

    async def test_reject_and_timeout_do_not_create(self):
        for reason, expected in (("rejected", "OWNER_APPROVAL_REJECTED"), ("timeout", "OWNER_APPROVAL_TIMEOUT")):
            model = self.script(self.call())
            task = await self.submit("owner-a", reason, reason)
            record = self.agent.workflow_store.lookup_task(task["id"])
            wait = self.agent.workflow_store.get_wait(record.snapshot["wait_id"], tenant_id=self.tenant)
            self.agent.workflow_store.resolve_wait(wait.wait_id, tenant_id=self.tenant,
                outcome={"reason": reason}, actor_id="owner-test")
            await asyncio.to_thread(self.agent.resume_task, task["id"])
            self.assertIn(expected, str(model.calls[-1].messages))
        self.assertEqual(self.store.list(self.tenant), [])


    async def test_fenced_store_replays_exact_attempt_and_rejects_wrong_lease_or_call(self):
        self.policy("allow")
        self.script(self.call())
        create = self.store.create_from_tool
        def checked(record, token, call_id, arguments):
            for bad_token, bad_call, bad_args in (("expired", call_id, arguments),
                    (token, "other-call", arguments), (token, call_id, {**arguments, "prompt": "Changed"})):
                with self.assertRaises(CoreError):
                    create(record, bad_token, bad_call, bad_args)
                self.assertEqual(self.store.list(self.tenant), [])
            first = create(record, token, call_id, arguments)
            self.assertEqual(create(record, token, call_id, arguments), first)
            return first
        with patch.object(self.store, "create_from_tool", side_effect=checked):
            await self.submit("owner-a", "fenced", "fenced-chat")
        self.assertEqual(len(self.store.list(self.tenant)), 1)
        self.assertEqual(len(self.store.events(self.tenant, kind="created")), 1)

    async def test_store_event_failure_rolls_back_creation_and_does_not_blind_retry(self):
        self.policy("allow")
        model = self.script(self.call())
        with patch.object(self.store, "_event", side_effect=RuntimeError("lost storage write")):
            task = await self.submit("owner-a", "rollback", "rollback-chat")
        self.assertEqual(self.store.list(self.tenant), [])
        self.assertEqual(self.store.events(self.tenant), [])
        record = self.agent.workflow_store.lookup_task(task["id"])
        self.assertEqual(record.error_code, "SIDE_EFFECT_UNKNOWN")
        self.assertEqual(len(model.calls), 1)

    async def test_invalid_calendar_is_recoverable_and_unconfigured_store_is_hidden(self):
        self.policy("allow")
        model = self.script(self.call(expression="0 0 31 2 *"))
        await self.submit("owner-a", "invalid-calendar", "invalid-calendar-chat")
        self.assertIn("CRON_INVALID", str(model.calls[-1].messages))
        self.assertEqual(self.store.list(self.tenant), [])
        self.agent.cron_store = None
        model = self.script(self.call())
        await self.submit("owner-a", "no-store", "no-store-chat")
        self.assertTrue(all("core_cron_create" not in call.tools for call in model.calls))
        self.assertIn("CAPABILITY_DISABLED", str(model.calls[-1].messages))

    async def test_child_registry_keeps_store_but_effective_capability_can_be_narrowed(self):
        from core_agent.config import AgentConfig, compile_effective_config
        raw = self.agent.agent_config.to_dict()
        raw["tools"]["builtins"] = {"default": "deny", "allow": ["core_cron_create"], "deny": []}
        child = self.agent._child_agent(raw, ["core_cron_create"])
        self.assertIs(child.cron_store, self.store)
        self.assertIn("core_cron_create", compile_effective_config(self.agent.platform_config, AgentConfig.from_dict(raw), [], {}).model_tool_catalog)
        raw["tools"]["builtins"]["allow"] = []
        self.assertNotIn("core_cron_create", compile_effective_config(self.agent.platform_config, AgentConfig.from_dict(raw), [], {}).model_tool_catalog)


    async def test_python_nested_approval_lifts_without_replaying_prefix(self):
        from core_agent.execution import ExecutionResult
        from core_agent.python_exec import PythonContinuationStopped
        self.policy("allow", "core_python_exec")
        prefix, remainder = [], []
        def execute(manager, **kwargs):
            prefix.append("once")
            result = ExecutionResult(-9, "prefix output", "", (), (), status="failed", cleanup="sandbox_terminated")
            kwargs["on_start"](SimpleNamespace(cancel=lambda _: None, wait=lambda *_: result), SimpleNamespace(id="python-process"))
            def broker():
                try:
                    kwargs["dispatch"]("core_cron_create", {"prompt": "Daily report", "expression": "0 9 * * *"}, "stable-nested")
                    remainder.append("executed")
                except PythonContinuationStopped:
                    pass
            thread = threading.Thread(target=copy_context().run, args=(broker,))
            thread.start()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            return result
        self.script(ModelResponse(tool_requests=(ToolRequest("python", "core_python_exec", {"code": "pass"}),)))
        with patch("core_agent.runtime.execute_python", side_effect=execute):
            task = await self.submit("owner-a", "nested", "nested-chat")
            record = self.agent.workflow_store.lookup_task(task["id"])
            self.assertEqual(record.state, "WAITING_INPUT")
            self.assertEqual(self.store.list(self.tenant), [])
            wait = self.agent.workflow_store.get_wait(record.snapshot["wait_id"], tenant_id=self.tenant)
            self.assertEqual(wait.subject["tool_name"], "core_cron_create")
            self.assertEqual(wait.subject["resolved_parameters"], {"timezone": "Europe/Moscow"})
            self.assertEqual(wait.subject["arguments"], {"prompt": "Daily report", "expression": "0 9 * * *"})
            self.agent.workflow_store.resolve_wait(wait.wait_id, tenant_id=self.tenant,
                outcome={"reason": "allowed"}, actor_id="owner-test")
            await asyncio.to_thread(self.agent.resume_task, task["id"])
        self.assertEqual((prefix, remainder), (["once"], []))
        self.assertEqual(len(self.store.list(self.tenant)), 1)
        self.assertEqual(self.agent.workflow_store.lookup_task(task["id"]).snapshot["tool_calls"], 2)

    async def test_cancel_between_dispatch_and_store_commit_never_creates(self):
        self.policy("allow")
        self.script(self.call())
        create = self.store.create_from_tool
        checked = []
        def cancel(record, token, call_id, arguments):
            self.agent.workflow_store.request_cancel(record.run_id, tenant_id=record.tenant_id, owner_id=record.owner_id)
            with self.assertRaises(CoreError) as error:
                create(record, token, call_id, arguments)
            self.assertEqual(error.exception.code, "TASK_CANCELLED")
            checked.append(True)
            raise error.exception
        with patch.object(self.store, "create_from_tool", side_effect=cancel):
            await self.submit("owner-a", "cancel", "cancel-chat")
        self.assertEqual(checked, [True])
        self.assertEqual(self.store.list(self.tenant), [])

    async def test_expired_lease_prevents_creation(self):
        self.policy("allow")
        self.script(self.call())
        create = self.store.create_from_tool
        checked = []
        def block(record, token, call_id, arguments):
            workflow = self.agent.workflow_store
            if self.store.database:
                with self.store.database.transaction() as connection:
                    connection.execute("UPDATE core_runs SET lease_expires_at=0 WHERE run_id=%s", (record.run_id,))
            else:
                worker, lease, _expiry = workflow._leases[record.run_id]
                workflow._leases[record.run_id] = (worker, lease, 0)
            with self.assertRaises(CoreError) as error:
                create(record, token, call_id, arguments)
            self.assertEqual(error.exception.code, "LEASE_LOST")
            checked.append(True)
            raise error.exception
        with patch.object(self.store, "create_from_tool", side_effect=block):
            await self.submit("owner-a", "expired", "expired-chat")
        self.assertEqual(checked, [True])
        self.assertEqual(self.store.list(self.tenant), [])


    async def test_terminal_intent_fence_prevents_creation(self):
        self.policy("allow")
        self.script(self.call())
        create = self.store.create_from_tool
        checked = []
        def block(record, token, call_id, arguments):
            self.agent.workflow_store.begin_terminal(record, {"id": "close", "state": "COMPLETED"}, lease_token=token)
            with self.assertRaises(CoreError) as error:
                create(record, token, call_id, arguments)
            self.assertEqual(error.exception.code, "TASK_CANCELLED")
            checked.append(True)
            raise error.exception
        with patch.object(self.store, "create_from_tool", side_effect=block):
            await self.submit("owner-a", "terminal", "terminal-chat")
        self.assertEqual(checked, [True])
        self.assertEqual(self.store.list(self.tenant), [])

    async def test_python_allowed_nested_call_uses_exact_nested_dispatch(self):
        from core_agent.execution import ExecutionResult
        self.policy("allow", "core_python_exec")
        self.policy("allow")
        outputs = []
        def execute(manager, **kwargs):
            result = ExecutionResult(0, "done", "", (), (), status="completed", cleanup="sandbox_terminated")
            kwargs["on_start"](SimpleNamespace(cancel=lambda _: None, wait=lambda *_: result), SimpleNamespace(id="python-process"))
            def broker():
                outputs.append(kwargs["dispatch"]("core_cron_create", {"prompt": "Nested report", "expression": "0 9 * * *"}, "nested-allowed"))
            thread = threading.Thread(target=copy_context().run, args=(broker,))
            thread.start()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            return result
        self.script(ModelResponse(tool_requests=(ToolRequest("python", "core_python_exec", {"code": "pass"}),)))
        with patch("core_agent.runtime.execute_python", side_effect=execute):
            task = await self.submit("owner-a", "nested-allowed", "nested-allowed-chat")
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(len(outputs), 1)
        self.assertEqual(len(self.store.list(self.tenant)), 1)
        self.assertEqual(self.agent.workflow_store.lookup_task(task["id"]).snapshot["tool_calls"], 2)

    async def test_delegated_child_creates_only_in_parent_canonical_chat(self):
        self.policy("allow", "core_delegate")
        self.policy("allow")
        self.script(ModelResponse(tool_requests=(ToolRequest("delegate", "core_delegate", {
            "instruction": "Create the report schedule", "tools": ["core_cron_create"], "skills": [],
            "budget": {"turns": 3, "tool_calls": 1}}),)), self.call(), ModelResponse(message="child done"))
        task = await self.submit("external-a", "delegation", "delegated-chat")
        # Joined child admission is durable; finish its worker before the parent's recovery boundary.
        def settle():
            self.agent.recover_durable_tasks()
            if hasattr(self.agent.task_scheduler, "_threads"):
                with self.agent.task_scheduler._lock:
                    threads = tuple(self.agent.task_scheduler._threads)
                for thread in threads:
                    thread.join(3)
            with patch.object(self.agent, "_launch_recovery", return_value=False):
                self.agent._recover_workflows_once()
            self.agent.resume_task(task["id"])
        await asyncio.to_thread(settle)
        rows = self.store.list(self.tenant)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["context_id"], "delegated-chat")
        event = self.store.events(self.tenant, schedule_id=rows[0]["id"])[0]
        self.assertEqual(event["owner_id"], self.agent.workflow_store.lookup_task(task["id"]).owner_id)


    async def test_both_runtime_modes_keep_cron_inside_effective_intersection(self):
        from core_agent.config import AgentConfig
        self.policy("allow")
        for mode in ("without_terminal", "with_terminal"):
            raw = self.agent.agent_config.to_dict()
            raw["execution"]["runtime_mode"] = mode
            self.agent.agent_config = AgentConfig.from_dict(raw)
            model = self.script(self.call())
            await self.submit("owner-a", mode, mode)
            self.assertIn("core_cron_create", model.calls[0].tools)
        self.assertEqual(len(self.store.list(self.tenant)), 2)

    async def test_parent_cancel_fences_child_creation_before_propagation(self):
        self.policy("allow", "core_delegate")
        self.policy("allow")
        self.script(ModelResponse(tool_requests=(ToolRequest("delegate", "core_delegate", {
            "instruction": "Create the report schedule", "tools": ["core_cron_create"], "skills": [],
            "budget": {"turns": 3, "tool_calls": 1}}),)), self.call())
        create = self.store.create_from_tool
        checked = []
        def cancel_parent(record, token, call_id, arguments):
            self.assertIsNotNone(record.parent_run_id)
            self.agent.workflow_store.request_cancel(record.parent_run_id, tenant_id=record.tenant_id, owner_id=record.owner_id)
            with self.assertRaises(CoreError) as error:
                create(record, token, call_id, arguments)
            self.assertEqual(error.exception.code, "TASK_CANCELLED")
            checked.append(True)
            raise error.exception
        with patch.object(self.store, "create_from_tool", side_effect=cancel_parent):
            await self.submit("external-a", "parent-cancel", "parent-cancel-chat")
            def settle():
                if hasattr(self.agent.task_scheduler, "_threads"):
                    with self.agent.task_scheduler._lock:
                        threads = tuple(self.agent.task_scheduler._threads)
                    for thread in threads:
                        thread.join(3)
            await asyncio.to_thread(settle)
        self.assertEqual(checked, [True])
        self.assertEqual(self.store.list(self.tenant), [])


    async def test_parent_cancel_serializes_behind_child_creation_commit(self):
        self.policy("allow", "core_delegate")
        self.policy("allow")
        self.script(ModelResponse(tool_requests=(ToolRequest("delegate", "core_delegate", {
            "instruction": "Create the report schedule", "tools": ["core_cron_create"], "skills": [],
            "budget": {"turns": 3, "tool_calls": 1}}),)), self.call())
        create, save = self.store.create_from_tool, self.store._save
        completed = []
        def checked(record, token, call_id, arguments):
            entered, cancelled = threading.Event(), threading.Event()
            failures = []
            def cancel():
                entered.set()
                try:
                    self.agent.workflow_store.request_cancel(record.parent_run_id,
                        tenant_id=record.tenant_id, owner_id=record.owner_id)
                except Exception as error:
                    failures.append(error)
                finally:
                    cancelled.set()
            worker = threading.Thread(target=cancel)
            def captured(*args, **kwargs):
                worker.start()
                self.assertTrue(entered.wait(2))
                self.assertFalse(cancelled.wait(.05), "Cancel passed the held ancestry fence")
                return save(*args, **kwargs)
            with patch.object(self.store, "_save", side_effect=captured):
                result = create(record, token, call_id, arguments)
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertFalse(failures)
            completed.append(result)
            return result
        with patch.object(self.store, "create_from_tool", side_effect=checked):
            await self.submit("external-a", "concurrent-cancel", "concurrent-cancel-chat")
            if hasattr(self.agent.task_scheduler, "_threads"):
                with self.agent.task_scheduler._lock:
                    workers = tuple(self.agent.task_scheduler._threads)
                for worker in workers:
                    await asyncio.to_thread(worker.join, 3)
        self.assertEqual(len(completed), 1)
        self.assertEqual(len(self.store.list(self.tenant)), 1)


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL not set")
class PostgresCronRuntimeTests(CronRuntimeTests):
    use_postgres = True

    async def test_single_pool_slot_request_lock_precedes_borrowed_workflow_fence(self):
        self.policy("allow")
        self.script(self.call())
        self.store.database.pool.resize(1, 1)
        create = self.store.create_from_tool
        def checked(record, token, call_id, arguments):
            locks, connections = [], []
            request_lock = self.store._request_lock
            get = self.agent.workflow_store.get
            def request(*args):
                locks.append("request")
                connections.append(args[-1])
                return request_lock(*args)
            def read(*args, **kwargs):
                self.assertTrue(locks)
                self.assertEqual(locks[0], "request")
                self.assertIsNotNone(kwargs.get("connection"))
                self.assertIs(kwargs["connection"], connections[0])
                return get(*args, **kwargs)
            with patch.object(self.store, "_request_lock", side_effect=request), patch.object(
                    self.agent.workflow_store, "get", side_effect=read):
                return create(record, token, call_id, arguments)
        with patch.object(self.store, "create_from_tool", side_effect=checked):
            task = await self.submit("owner-a", "single-pool", "single-pool-chat")
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(len(self.store.list(self.tenant)), 1)
