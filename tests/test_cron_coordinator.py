"""Coordinator uses canonical admission, one main-loop job and a real PG leader session."""
import asyncio
import json
import time
import unittest
from datetime import UTC, datetime, timedelta
from contextlib import contextmanager
from unittest.mock import patch

from core_agent.cron import CronStore
from core_agent.errors import CoreError
from tests.test_auth import AuthAppTestCase, TEST_DATABASE_URL
from tests import test_admission as admission_tests


class CronCoordinatorTests(AuthAppTestCase):
    context = admission_tests.AuthAdmissionTests.context

    async def asyncSetUp(self):
        await super().asyncSetUp()
        from core_agent.cron_service import CronCoordinator
        self.agent = self.app.state.core_agent
        self.admission = self.agent.tool_runtime.environment_manager.validate_workspace_scope.__self__
        self.now = datetime.now(UTC)
        self.store = CronStore(self.admission, clock=lambda: self.now)
        self.tenant = self.context("owner-a").tenant
        self.admitted = []
        self.logs = []
        def committed(admission, tenant):
            self.assertEqual(tenant, self.tenant)
            self.assertIsNotNone(self.agent.workflow_store.lookup_task(admission.task.id))
            self.admitted.append(admission)
        self.coordinator = CronCoordinator(self.store, self.tenant, on_admitted=committed,
                                           log=lambda event, **data: self.logs.append((event, data)))
        self.coordinator.bind_loop(asyncio.get_running_loop())
        self.addAsyncCleanup(self.coordinator.aclose)

    async def create(self, request="create", **values):
        return await self.store.create(self.context("owner-a"), {
            "request_id": request, "prompt": "Scheduled report", "expression": "* * * * *", **values})

    def due_at(self, row, when):
        self.admission._cron_schedules[(self.tenant, row["id"])]["next_due_at"] = when

    async def tick(self):
        await asyncio.to_thread(self.coordinator.tick)
        await asyncio.sleep(0)
        task = self.coordinator._task
        if task:
            await task

    async def test_startup_skips_past_due_then_healthy_tick_admits_once(self):
        row = await self.create()
        self.due_at(row, self.now - timedelta(seconds=1))
        await self.tick()
        events = self.store.events(self.tenant, context_id=row["context_id"])
        self.assertEqual(events[-1]["reason"], "service_unavailable")
        self.assertFalse(self.admitted)
        self.now = datetime.fromisoformat(self.store.get(self.tenant, row["id"])["next_due_at"])
        await self.tick()
        await self.tick()
        self.assertEqual(len(self.admitted), 1)
        self.assertFalse(self.model.calls)
        self.assertEqual([event["kind"] for event in self.store.events(self.tenant, context_id=row["context_id"])],
                         ["created", "skipped", "started"])

    async def test_gap_reestablishes_cutoff_without_catchup_task(self):
        row = await self.create()
        await self.tick()
        self.now += timedelta(minutes=10)
        self.coordinator._last_completed = time.monotonic() - 61
        await self.tick()
        self.assertFalse(self.admitted)
        event = self.store.events(self.tenant, context_id=row["context_id"])[-1]
        self.assertEqual(event["reason"], "service_unavailable")
        self.assertEqual(event["through"], self.now)
        self.assertGreater(datetime.fromisoformat(self.store.get(self.tenant, row["id"])["next_due_at"]), self.now)

    async def test_internal_context_preserves_external_owner_without_operator_authority(self):
        initial = await self.submit("external-a", "initial", "external-chat")
        original = self.agent.workflow_store.lookup_task(initial["id"])
        row = await self.create(context_id="external-chat")
        await self.tick()
        self.now = datetime.fromisoformat(row["next_due_at"])
        contexts = []
        occur = self.store.occur_memory
        async def record(context, *args, **kwargs):
            contexts.append(context)
            return await occur(context, *args, **kwargs)
        with patch.object(self.store, "occur_memory", side_effect=record):
            await self.tick()
        self.assertEqual(len(contexts), 1)
        principal = contexts[0].state["principal"]
        self.assertFalse(principal.is_owner)
        self.assertFalse(principal.is_external)
        self.assertEqual(principal.owner_id, original.owner_id)
        self.assertEqual(contexts[0].user.user_name, original.owner_id)
        self.assertEqual(set(contexts[0].state), {"principal"})
        self.assertEqual(self.agent.workflow_store.lookup_task(self.admitted[0].task.id).owner_id, original.owner_id)

    async def test_cross_thread_ticks_schedule_one_main_loop_job(self):
        await self.create()
        entered, release = asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()
        occur = self.store.occur_memory
        async def wait(context, *args, **kwargs):
            self.assertIs(asyncio.get_running_loop(), loop)
            entered.set()
            await release.wait()
            return await occur(context, *args, **kwargs)
        row = self.store.list(self.tenant)[0]
        self.due_at(row, self.now - timedelta(seconds=1))
        with patch.object(self.store, "occur_memory", side_effect=wait) as calls:
            await asyncio.to_thread(self.coordinator.tick)
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.gather(*(asyncio.to_thread(self.coordinator.tick) for _ in range(12)))
            self.assertEqual(calls.call_count, 1)
            release.set()
            await self.coordinator._task

    async def test_close_cancels_before_admission_and_late_ticks_do_nothing(self):
        row = await self.create()
        self.due_at(row, self.now - timedelta(seconds=1))
        entered = asyncio.Event()
        async def wait(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(self.store, "occur_memory", side_effect=wait):
            await asyncio.to_thread(self.coordinator.tick)
            await asyncio.wait_for(entered.wait(), 2)
            await self.coordinator.aclose()
            await asyncio.to_thread(self.coordinator.tick)
        self.assertFalse(self.admitted)
        self.assertEqual(len(self.store.events(self.tenant)), 1)

    async def test_failed_transaction_has_no_handoff_and_safe_logging(self):
        row = await self.create()
        await self.tick()
        self.now = datetime.fromisoformat(row["next_due_at"])
        with patch.object(self.store, "_event", side_effect=RuntimeError("PRIVATE PROMPT TOKEN")):
            await self.tick()
        self.assertFalse(self.admitted)
        self.assertIsNone(self.store.get(self.tenant, row["id"])["active_task_id"])
        self.assertNotIn("PRIVATE", repr(self.logs))
        await self.tick()
        self.assertEqual(len(self.admitted), 1)

    async def test_handoff_failure_keeps_committed_root_without_resending(self):
        row = await self.create()
        await self.tick()
        self.now = datetime.fromisoformat(row["next_due_at"])
        def failure(*args):
            raise RuntimeError("private callback")
        self.coordinator.on_admitted = failure
        await self.tick()
        root = self.store.get(self.tenant, row["id"])["active_task_id"]
        self.assertIsNotNone(root)
        await self.tick()
        events = self.store.events(self.tenant, context_id=row["context_id"])
        self.assertEqual(sum(event["kind"] == "started" for event in events), 1)
        self.assertNotIn("private callback", repr(self.logs))

    async def test_busy_next_tick_is_notice_without_new_task(self):
        row = await self.create()
        await self.tick()
        self.now = datetime.fromisoformat(row["next_due_at"])
        await self.tick()
        self.now += timedelta(minutes=1)
        await self.tick()
        self.assertEqual(len(self.admitted), 1)
        self.assertEqual(self.store.events(self.tenant, context_id=row["context_id"])[-1]["reason"], "context_busy")

    async def test_bound_100_and_rotation_past_corrupt_rows(self):
        rows = [await self.create(str(index)) for index in range(101)]
        rows.sort(key=lambda row: row["id"])
        for row in rows:
            self.due_at(row, self.now - timedelta(seconds=1))
        occur = self.store.occur_memory
        attempted = []
        async def corrupted(context, schedule_id, *args, **kwargs):
            attempted.append(schedule_id)
            if schedule_id != rows[-1]["id"]:
                raise CoreError("CRON_INVALID")
            return await occur(context, schedule_id, *args, **kwargs)
        with patch.object(self.store, "occur_memory", side_effect=corrupted):
            await self.tick()
            self.assertEqual(len(attempted), 100)
            await self.tick()
        self.assertEqual(attempted[-1], rows[-1]["id"])
        self.assertEqual(self.store.events(self.tenant, context_id=rows[-1]["context_id"])[-1]["kind"], "skipped")


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL not set")
class PostgresCronLeaderTests(AuthAppTestCase):
    use_postgres = True
    context = admission_tests.AuthAdmissionTests.context

    async def asyncSetUp(self):
        await super().asyncSetUp()
        agent = self.app.state.core_agent
        # The explicit test coordinators must own the company leader session.
        agent._recovery_stop.set()
        if agent._recovery_thread is not None:
            await asyncio.to_thread(agent._recovery_thread.join, 1)
            self.assertFalse(agent._recovery_thread.is_alive())
        await agent.cron_coordinator.aclose()

    async def test_single_session_leader_takeover_and_same_connection_transaction(self):
        from core_agent.cron_service import CronCoordinator
        agent = self.app.state.core_agent
        admission = agent.tool_runtime.environment_manager.validate_workspace_scope.__self__
        store = CronStore(admission)
        tenant = self.context("owner-a").tenant
        row = await store.create(self.context("owner-a"), {
            "request_id": "leader", "prompt": "Scheduled", "expression": "* * * * *"})
        first, second = CronCoordinator(store, tenant), CronCoordinator(store, tenant)
        self.addAsyncCleanup(first.aclose)
        self.addAsyncCleanup(second.aclose)
        await asyncio.to_thread(first.tick)
        await asyncio.to_thread(second.tick)
        self.assertTrue(first._leader)
        self.assertFalse(second._leader)
        with store.database.transaction() as connection:
            connection.execute("UPDATE core_cron_schedules SET next_due_at=clock_timestamp() WHERE tenant_id=%s AND id=%s",
                               (tenant, row["id"]))
        occur = store.occur
        def checked(*args, **kwargs):
            self.assertIs(kwargs["connection"], first._connection)
            self.assertEqual(kwargs["connection"].info.transaction_status.name, "INTRANS")
            return occur(*args, **kwargs)
        with patch.object(store, "occur", side_effect=checked):
            await asyncio.to_thread(first.tick)
        await first.aclose()
        await asyncio.to_thread(second.tick)
        self.assertTrue(second._leader)
        events = store.events(tenant, context_id=row["context_id"])
        self.assertEqual(sum(event["kind"] == "started" for event in events), 1)

    async def test_dedicated_leader_does_not_borrow_pool_or_stack_session_locks(self):
        from core_agent.cron_service import CronCoordinator
        agent = self.app.state.core_agent
        admission = agent.tool_runtime.environment_manager.validate_workspace_scope.__self__
        store = CronStore(admission)
        tenant = self.context("owner-a").tenant
        row = await store.create(self.context("owner-a"), {
            "request_id": "dedicated", "prompt": "Scheduled", "expression": "* * * * *"})
        first, second = CronCoordinator(store, tenant), CronCoordinator(store, tenant)
        self.addAsyncCleanup(first.aclose)
        self.addAsyncCleanup(second.aclose)
        await asyncio.to_thread(first.tick)
        await asyncio.to_thread(first.tick)
        with store.database.transaction() as connection:
            connection.execute("UPDATE core_cron_schedules SET next_due_at=clock_timestamp() WHERE tenant_id=%s AND id=%s",
                               (tenant, row["id"]))
        store.database.pool.resize(1, 1)
        with store.database.pool.connection():
            await asyncio.to_thread(first.tick)
        self.assertIsNotNone(store.get(tenant, row["id"])["active_task_id"])
        key = json.dumps(["core-agent-cron-leader", tenant], separators=(",", ":"))
        first._connection.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (key,))
        await asyncio.to_thread(second.tick)
        self.assertTrue(second._leader)
        await first.aclose()

    async def test_unknown_commit_does_not_handoff_or_repeat_durable_occurrence(self):
        import psycopg
        from core_agent.cron_service import CronCoordinator
        agent = self.app.state.core_agent
        admission = agent.tool_runtime.environment_manager.validate_workspace_scope.__self__
        store = CronStore(admission)
        tenant = self.context("owner-a").tenant
        row = await store.create(self.context("owner-a"), {
            "request_id": "unknown-commit", "prompt": "Scheduled", "expression": "* * * * *"})
        handed = []
        coordinator = CronCoordinator(store, tenant, on_admitted=lambda *args: handed.append(args))
        self.addAsyncCleanup(coordinator.aclose)
        await asyncio.to_thread(coordinator.tick)
        with store.database.transaction() as connection:
            connection.execute("UPDATE core_cron_schedules SET next_due_at=clock_timestamp() WHERE tenant_id=%s AND id=%s",
                               (tenant, row["id"]))
        real = coordinator._connection
        class LostCommitReply:
            def __getattr__(self, name):
                return getattr(real, name)

            @contextmanager
            def transaction(self):
                with real.transaction():
                    yield
                raise psycopg.OperationalError("Simulated lost COMMIT reply after real commit")
        coordinator._connection = LostCommitReply()
        await asyncio.to_thread(coordinator.tick)
        self.assertFalse(handed)
        self.assertIsNone(coordinator._connection)
        task_id = store.get(tenant, row["id"])["active_task_id"]
        self.assertIsNotNone(task_id)
        await asyncio.to_thread(coordinator.tick)
        self.assertEqual(store.get(tenant, row["id"])["active_task_id"], task_id)
        self.assertEqual(sum(event["kind"] == "started" for event in store.events(tenant)), 1)
        self.assertFalse(handed)
