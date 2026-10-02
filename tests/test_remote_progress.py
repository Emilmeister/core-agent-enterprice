"""Safe remote status projection from committed task state, without waking a run."""
import asyncio
import os
import unittest
import uuid

from a2a.server.context import ServerCallContext
from a2a.types import a2a_pb2
from google.protobuf.json_format import MessageToDict

from core_agent.a2a_sdk import ScopedMemoryTaskStore, build_starlette_app
from core_agent.a2a import AgentCard
from core_agent.auth import OWNER_SCOPE, Principal, ScopeUser
from core_agent.database import PostgresDatabase, PostgresTaskStore
from core_agent.postgres_tasks import PostgresTaskScheduler
from core_agent.tasks import REMOTE_TASK_PENDING, TaskScheduler
from core_agent.workflow import InMemoryWorkflowStore, PostgresWorkflowStore, WorkflowRecord


KEY = "core_agent_remote_progress"


class RemoteProgressContract:
    use_postgres = False

    async def asyncSetUp(self):
        self.tenant, self.owner = str(uuid.uuid4()), "external-service"
        self.root, self.run, self.remote = (str(uuid.uuid4()) for _ in range(3))
        if self.use_postgres:
            self.database = PostgresDatabase(os.environ["TEST_DATABASE_URL"], min_size=0, max_size=5)
            self.database.migrate()
            self.addCleanup(self.database.close)
            self.addCleanup(self.cleanup_remote_tasks)
            self.workflows = PostgresWorkflowStore(self.database)
            self.scheduler = PostgresTaskScheduler(self.database)
            self.store = PostgresTaskStore(self.database)
        else:
            self.workflows = InMemoryWorkflowStore()
            self.scheduler = TaskScheduler()
            self.store = ScopedMemoryTaskStore(workflow_store=self.workflows, task_scheduler=self.scheduler)
        self.addCleanup(self.scheduler.close)
        self.contract = {"version": 1, "tenant_id": self.tenant, "owner_id": self.run,
            "peer_id": "private-peer-id", "peer_revision": 1, "peer_name": "delivery",
            "url": "https://peer.example/a2a", "binding": "HTTP+JSON", "message_id": "private-message-id",
            "task": "private-task-text", "timeout_seconds": 86400, "poll_interval_seconds": 300}
        record = self.workflows.create(WorkflowRecord(self.run, self.root, "chat", self.tenant, self.owner,
            None, "RUNNING", 1, {"prompt": "wait"}, {"turns": 2, "tool_calls": 1,
                "remote_calls": {"1:send": {"version": 1, "contract": self.contract}}}))
        token = self.workflows.acquire_lease(self.run, tenant_id=self.tenant, owner_id=self.owner, worker_id="test", ttl=60)
        self.wait = self.workflows.enter_wait(record, kind="task", source_id="wait", subject={"task_id": self.remote},
            continuation={"version": 1, "phase": "tool_wait", "call_id": "wait"}, deadline=None,
            snapshot=record.snapshot, lease_token=token)
        self.record = self.workflows.get(self.run, tenant_id=self.tenant, owner_id=self.owner)
        self.context = ServerCallContext(user=ScopeUser(self.owner), tenant=self.tenant)
        self.owner_context = ServerCallContext(user=ScopeUser(OWNER_SCOPE), tenant=self.tenant,
            state={"principal": Principal("alice", self.tenant, True, False)})
        self.task = a2a_pb2.Task(id=self.root, context_id="chat")
        self.task.status.state = a2a_pb2.TASK_STATE_WORKING
        self.task.metadata["core_agent_workflow_version"] = self.record.version
        await self.store.save(self.task, self.context)
        self.progress = {"agent_name": "delivery", "remote_state": "TASK_STATE_WORKING"}
        self.worker_errors = []
        def handler(claim, cancel):
            try:
                current = self.scheduler.read_remote_claim(claim)
                if not current["checkpoint"]["send_started"]:
                    self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"],
                        checkpoint={**current["checkpoint"], "send_started": True})
                    current = self.scheduler.read_remote_claim(claim)
                self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"],
                    checkpoint={**current["checkpoint"], "remote_task_id": "secret-remote-id"}, progress=self.progress)
            except BaseException as error:
                self.worker_errors.append(error)
            return REMOTE_TASK_PENDING
        self.scheduler.register("remote_a2a", handler)
        self.scheduler.start_remote(self.contract, owner_id=self.run, tenant_id=self.tenant, task_id=self.remote)
        self.settle()

    def cleanup_remote_tasks(self):
        # Recovery and projection reconciliation scan all tenants.
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM core_notifications WHERE tenant_id=%s", (self.tenant,))
            connection.execute("DELETE FROM core_runs WHERE tenant_id=%s", (self.tenant,))
            connection.execute("DELETE FROM core_background_tasks WHERE tenant_id=%s", (self.tenant,))
            connection.execute("DELETE FROM core_a2a_tasks WHERE tenant=%s", (self.tenant,))

    def settle(self):
        with self.scheduler._lock:
            threads = tuple(self.scheduler._threads)
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        if self.worker_errors:
            raise self.worker_errors[0]

    def update(self, state):
        self.progress = {"agent_name": "delivery", "remote_state": state}
        self.scheduler.recover()
        self.settle()

    def metadata(self, task):
        return MessageToDict(task.metadata).get(KEY, [])

    async def test_get_list_owner_scope_and_unchanged_wait_budget(self):
        task = await self.store.get(self.root, self.context)
        entries = self.metadata(task)
        self.assertEqual(entries, [{"task_id": self.remote, "revision": 2,
            "agent_name": "delivery", "remote_state": "TASK_STATE_WORKING"}])
        self.assertEqual(task.status.state, a2a_pb2.TASK_STATE_WORKING)
        self.assertEqual(self.metadata(await self.store.get(self.root, self.owner_context)), entries)
        listed = await self.store.list(a2a_pb2.ListTasksRequest(), self.context)
        self.assertEqual(self.metadata(listed.tasks[0]), entries)
        for tenant, owner in (("other", self.owner), (self.tenant, "external-other")):
            foreign = ServerCallContext(user=ScopeUser(owner), tenant=tenant)
            self.assertIsNone(await self.store.get(self.root, foreign))
            self.assertEqual(len((await self.store.list(a2a_pb2.ListTasksRequest(), foreign)).tasks), 0)
        current = self.workflows.get(self.run, tenant_id=self.tenant, owner_id=self.owner)
        self.assertEqual((current.version, current.snapshot), (self.record.version, self.record.snapshot))
        self.assertIsNone(self.workflows.get_wait(self.wait.wait_id, tenant_id=self.tenant, owner_id=self.owner).outcome)
        self.assertEqual(self.scheduler.mailbox(self.run, self.tenant).poll(), ())
        public = str(MessageToDict(task))
        for private in ("secret-remote-id", "private-peer-id", "private-task-text", self.wait.wait_id):
            self.assertNotIn(private, public)

    async def test_identical_poll_keeps_display_revision_and_timestamp(self):
        first = await self.store.get(self.root, self.context)
        self.update("TASK_STATE_WORKING")
        again = await self.store.get(self.root, self.context)
        self.assertEqual(again.SerializeToString(deterministic=True), first.SerializeToString(deterministic=True))
        self.update("TASK_STATE_INPUT_REQUIRED")
        changed = await self.store.get(self.root, self.context)
        self.assertEqual(self.metadata(changed)[0]["remote_state"], "TASK_STATE_INPUT_REQUIRED")
        self.assertEqual(self.metadata(changed)[0]["revision"], 4)

    async def test_stale_sdk_save_cannot_erase_or_resurrect_progress(self):
        stale = await self.store.get(self.root, self.context)
        self.update("TASK_STATE_AUTH_REQUIRED")
        changed = await self.store.get(self.root, self.context)
        await self.store.save(stale, self.context)
        self.assertEqual(self.metadata(await self.store.get(self.root, self.context)), self.metadata(changed))
        current = self.workflows.get(self.run, tenant_id=self.tenant, owner_id=self.owner)
        self.workflows.transition(self.run, tenant_id=self.tenant, owner_id=self.owner,
            expected_version=current.version, state="COMPLETED", snapshot=current.snapshot,
            event_kind="task.completed", result={"message": "done"})
        if self.use_postgres:
            self.store.reconcile_from_workflows()
        terminal = await self.store.get(self.root, self.context)
        self.assertEqual(terminal.status.state, a2a_pb2.TASK_STATE_COMPLETED)
        self.assertEqual(self.metadata(terminal), [])
        self.update("TASK_STATE_WORKING")
        await self.store.save(stale, self.context)
        after = await self.store.get(self.root, self.context)
        self.assertEqual(after.SerializeToString(deterministic=True), terminal.SerializeToString(deterministic=True))

    async def test_subscription_sees_persisted_progress_while_live_queue_is_idle(self):
        app = build_starlette_app(agent_card=AgentCard.minimal("progress"), base_url="https://agent.example",
            handler=lambda *_: None, cancel_handler=lambda *_: None, followup_handler=lambda *_: None,
            resume_handler=lambda *_: self.fail("Progress must not resume the workflow"), task_store=self.store)
        handler = app.state.a2a_request_handler
        idle = asyncio.Event()
        class Active:
            async def subscribe(inner, *, include_initial_task):
                await idle.wait()
                yield self.task
        handler._active_task_registry._active_tasks[self.root] = Active()
        stream = handler.on_subscribe_to_task(a2a_pb2.SubscribeToTaskRequest(id=self.root), self.context)
        first = await anext(stream)
        self.assertEqual(self.metadata(first)[0]["remote_state"], "TASK_STATE_WORKING")
        self.update("TASK_STATE_INPUT_REQUIRED")
        event = await asyncio.wait_for(anext(stream), 1)
        self.assertEqual(self.metadata(event)[0]["remote_state"], "TASK_STATE_INPUT_REQUIRED")
        pending = asyncio.create_task(anext(stream))
        self.update("TASK_STATE_INPUT_REQUIRED")
        await asyncio.sleep(.3)
        self.assertFalse(pending.done(), "An unchanged poll must not publish another progress event")
        current = self.workflows.get(self.run, tenant_id=self.tenant, owner_id=self.owner)
        self.workflows.transition(self.run, tenant_id=self.tenant, owner_id=self.owner,
            expected_version=current.version, state="COMPLETED", snapshot=current.snapshot,
            event_kind="task.completed", result={"message": "done"})
        if self.use_postgres:
            self.store.reconcile_from_workflows()
        terminal = await asyncio.wait_for(pending, 1)
        self.assertEqual(terminal.status.state, a2a_pb2.TASK_STATE_COMPLETED)
        with self.assertRaises(StopAsyncIteration):
            await anext(stream)

    async def test_new_store_instance_reads_latest_committed_progress(self):
        await self.store.get(self.root, self.context)
        self.update("TASK_STATE_AUTH_REQUIRED")
        if self.use_postgres:
            restarted = PostgresTaskStore(self.database)
        else:
            restarted = ScopedMemoryTaskStore(workflow_store=self.workflows, task_scheduler=self.scheduler)
            restarted._impl = self.store._impl
            restarted._store = self.store._store
            restarted._owners = dict(self.store._owners)
        task = await restarted.get(self.root, self.context)
        self.assertEqual(self.metadata(task)[0]["remote_state"], "TASK_STATE_AUTH_REQUIRED")

    async def test_late_live_task_cannot_erase_new_progress_or_follow_terminal(self):
        stale = await self.store.get(self.root, self.context)
        self.update("TASK_STATE_INPUT_REQUIRED")
        app = build_starlette_app(agent_card=AgentCard.minimal("progress"), base_url="https://agent.example",
            handler=lambda *_: None, cancel_handler=lambda *_: None, followup_handler=lambda *_: None,
            resume_handler=lambda *_: self.fail("Subscription must remain passive"), task_store=self.store)
        handler = app.state.a2a_request_handler
        frames = asyncio.Queue()
        class Active:
            async def subscribe(inner, *, include_initial_task):
                while True:
                    yield await frames.get()
        handler._active_task_registry._active_tasks[self.root] = Active()
        stream = handler.on_subscribe_to_task(a2a_pb2.SubscribeToTaskRequest(id=self.root), self.context)
        initial = await anext(stream)
        await frames.put(stale)
        corrected = await asyncio.wait_for(anext(stream), 1)
        self.assertEqual(self.metadata(corrected), self.metadata(initial))
        current = self.workflows.get(self.run, tenant_id=self.tenant, owner_id=self.owner)
        self.workflows.transition(self.run, tenant_id=self.tenant, owner_id=self.owner,
            expected_version=current.version, state="COMPLETED", snapshot=current.snapshot,
            event_kind="task.completed", result={"message": "done"})
        if self.use_postgres:
            self.store.reconcile_from_workflows()
        await frames.put(stale)
        terminal = await asyncio.wait_for(anext(stream), 1)
        self.assertEqual(terminal.status.state, a2a_pb2.TASK_STATE_COMPLETED)
        self.assertEqual(self.metadata(terminal), [])
        with self.assertRaises(StopAsyncIteration):
            await anext(stream)

    async def test_remote_timeout_removes_progress_without_changing_root_wait(self):
        stale = await self.store.get(self.root, self.context)
        if self.use_postgres:
            with self.database.transaction() as connection:
                connection.execute("""UPDATE core_background_tasks SET checkpoint =
                    jsonb_set(checkpoint, '{deadline}', to_jsonb(extract(epoch FROM now()) - 1))
                    WHERE id = %s AND tenant_id = %s""", (self.remote, self.tenant))
        else:
            now = self.scheduler.clock()
            self.scheduler.clock = lambda: now + 86401
        self.assertEqual(self.metadata(await self.store.get(self.root, self.context)), [])
        self.assertEqual(self.scheduler.mailbox(self.run, self.tenant).poll(), ())
        self.assertEqual(self.scheduler.expire_remote(), 1)
        await self.store.save(stale, self.context)
        task = await self.store.get(self.root, self.context)
        self.assertEqual(self.metadata(task), [])
        self.assertEqual(task.status.state, a2a_pb2.TASK_STATE_WORKING)
        current = self.workflows.get(self.run, tenant_id=self.tenant, owner_id=self.owner)
        self.assertEqual((current.version, current.snapshot), (self.record.version, self.record.snapshot))
        self.assertEqual(self.scheduler.expire_remote(), 0)


class MemoryRemoteProgressTests(RemoteProgressContract, unittest.IsolatedAsyncioTestCase):
    pass


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "set TEST_DATABASE_URL for PostgreSQL remote projection tests")
class PostgresRemoteProgressTests(RemoteProgressContract, unittest.IsolatedAsyncioTestCase):
    use_postgres = True

    async def test_progress_and_push_rollback_together_and_unchanged_progress_deduplicates(self):
        def unavailable(*args, **kwargs):
            raise RuntimeError("push enqueue unavailable")
        self.store.enqueue_notification = unavailable
        with self.assertRaisesRegex(RuntimeError, "push enqueue unavailable"):
            await self.store.get(self.root, self.context)
        with self.database.pool.connection() as connection:
            row = connection.execute("SELECT payload FROM core_a2a_tasks WHERE task_id = %s", (self.root,)).fetchone()
            self.assertEqual(self.metadata(a2a_pb2.Task.FromString(bytes(row["payload"]))), [])
        published = []
        self.store.enqueue_notification = lambda task_id, event, **kwargs: published.append(event.SerializeToString())
        await self.store.get(self.root, self.context)
        self.update("TASK_STATE_WORKING")
        self.store.reconcile_from_workflows()
        await self.store.list(a2a_pb2.ListTasksRequest(), self.context)
        self.assertEqual(len(published), 1)
