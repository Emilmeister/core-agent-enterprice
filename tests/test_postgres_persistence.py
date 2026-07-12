import asyncio
import os
import time
import unittest
from unittest.mock import patch

import httpx
from a2a.auth.user import User
from a2a.server.context import ServerCallContext
from a2a.types import Task, TaskState, TaskStatus

from core_agent.app import create_app
from core_agent.database import (
    PostgresAuditLog,
    PostgresCheckpointStore,
    PostgresDatabase,
    PostgresEventStore,
    PostgresTaskStore,
)
from core_agent.errors import CoreError
from core_agent.postgres_approvals import PostgresApprovalManager
from core_agent.postgres_tasks import PostgresTaskScheduler
from core_agent.tools import ToolCall
from core_agent.workflow import OutboxDispatcher, PostgresWorkflowStore, WorkflowRecord
from psycopg.types.json import Jsonb


class ProductionConfigurationTests(unittest.TestCase):
    def test_health_routes_precede_a2a_catch_all(self):
        model = type("Model", (), {"model": "test-model"})()
        with patch.dict(
            os.environ,
            {
                "CORE_AGENT_STATE_BACKEND": "test",
                "LOCAL_APPROVAL_DB_PATH": ":memory:",
            },
            clear=True,
        ):
            app = create_app(model=model)

        async def request(path):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                return await client.get(path)

        try:
            self.assertEqual(asyncio.run(request("/health/live")).status_code, 200)
            self.assertEqual(asyncio.run(request("/health/ready")).status_code, 200)
        finally:
            app.state.close()

    def test_production_rejects_test_state_backend(self):
        model = type("Model", (), {"model": "test-model"})()
        with patch.dict(
            os.environ,
            {
                "CORE_AGENT_ENVIRONMENT": "production",
                "CORE_AGENT_STATE_BACKEND": "test",
            },
            clear=True,
        ):
            with self.assertRaises(CoreError) as caught:
                create_app(model=model, control_plane=object())
        self.assertEqual(caught.exception.code, "PRODUCTION_DATABASE_REQUIRED")

    def test_postgres_backend_requires_database_url(self):
        model = type("Model", (), {"model": "test-model"})()
        with patch.dict(
            os.environ,
            {
                "CORE_AGENT_ENVIRONMENT": "production",
                "CORE_AGENT_STATE_BACKEND": "postgres",
            },
            clear=True,
        ):
            with self.assertRaises(CoreError) as caught:
                create_app(model=model, control_plane=object())
        self.assertEqual(caught.exception.code, "DATABASE_URL_REQUIRED")

    def test_production_rejects_in_process_migration(self):
        class Database:
            def close(self):
                pass

        model = type("Model", (), {"model": "test-model"})()
        with patch.dict(
            os.environ,
            {
                "CORE_AGENT_ENVIRONMENT": "production",
                "CORE_AGENT_STATE_BACKEND": "postgres",
                "DATABASE_AUTO_MIGRATE": "true",
            },
            clear=True,
        ):
            with self.assertRaises(CoreError) as caught:
                create_app(model=model, control_plane=object(), database=Database())
        self.assertEqual(caught.exception.code, "PRODUCTION_AUTO_MIGRATE_FORBIDDEN")


class NamedUser(User):
    @property
    def is_authenticated(self):
        return True

    @property
    def user_name(self):
        return "owner-1"


@unittest.skipUnless(
    os.getenv("TEST_DATABASE_URL"),
    "set TEST_DATABASE_URL to run PostgreSQL restart tests",
)
class PostgresRestartTests(unittest.TestCase):
    def _database(self):
        return PostgresDatabase(os.environ["TEST_DATABASE_URL"], min_size=0, max_size=3)

    def _reset(self, database):
        with database.transaction() as connection:
            connection.execute(
                """TRUNCATE core_execution_records, core_approval_requests,
                   core_tool_proposals, core_events, core_checkpoints,
                   core_audit_records, core_a2a_tasks, core_runs,
                   core_background_tasks, core_notifications, core_outbox CASCADE"""
            )

    def test_all_production_state_survives_pool_restart_and_is_scoped(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        approvals = PostgresApprovalManager(database)
        approval = approvals.request(
            ToolCall("call-1", "core.terminal.exec", {"argv": ["true"]}),
            risks={"local_execution"},
            task_id="task-1",
            context_id="context-1",
            tenant_id="tenant-1",
            caller_principal_id="owner-1",
        )
        PostgresEventStore(database).append("run-1", "task.started", {"ok": True})
        PostgresCheckpointStore(database).save("run-1", 1, {"state": "working"})
        PostgresAuditLog(database).append("run-1", "task.started", {"ok": True})
        context = ServerCallContext(user=NamedUser(), tenant="tenant-1")
        task_store = PostgresTaskStore(database)
        task = Task(
            id="task-1",
            context_id="context-1",
            status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
        )
        asyncio.run(task_store.save(task, context))
        database.close()

        reopened = self._database()
        try:
            reopened.verify_schema()
            self.assertEqual(PostgresApprovalManager(reopened).get(approval.id).state, "PENDING")
            self.assertEqual(PostgresEventStore(reopened).revision("run-1"), 1)
            self.assertEqual(
                PostgresCheckpointStore(reopened).load("run-1"),
                (1, {"state": "working"}),
            )
            self.assertEqual(len(PostgresAuditLog(reopened).records("run-1")), 1)
            reopened_tasks = PostgresTaskStore(reopened)
            self.assertEqual(asyncio.run(reopened_tasks.get("task-1", context)).id, "task-1")
            other_tenant = ServerCallContext(user=NamedUser(), tenant="tenant-2")
            self.assertIsNone(asyncio.run(reopened_tasks.get("task-1", other_tenant)))
        finally:
            reopened.close()

    def test_workflow_lease_outbox_and_background_recovery(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        workflows = PostgresWorkflowStore(database)
        created = workflows.create(
            WorkflowRecord(
                "run-1",
                "task-1",
                "context-1",
                "tenant-1",
                "owner-1",
                None,
                "RUNNING",
                1,
                {"prompt": "test", "mcp": [], "skills": []},
                {"turns": 0},
            ),
            audit=(("task.started", {"safe": True}),),
        )
        self.assertEqual(created.version, 1)
        token = workflows.acquire_lease(
            "run-1",
            tenant_id="tenant-1",
            owner_id="owner-1",
            worker_id="worker-1",
            ttl=30,
        )
        with self.assertRaises(CoreError) as caught:
            workflows.acquire_lease(
                "run-1",
                tenant_id="tenant-1",
                owner_id="owner-1",
                worker_id="worker-2",
                ttl=30,
            )
        self.assertEqual(caught.exception.code, "LEASE_LOST")
        transitioned = workflows.transition(
            "run-1",
            tenant_id="tenant-1",
            owner_id="owner-1",
            expected_version=1,
            state="WAITING_TASK",
            snapshot={"turns": 1, "waiting": "background-read"},
            event_kind="task.waiting",
            audit=(("task.waiting", {"kind": "background"}),),
            lease_token=token,
        )
        self.assertEqual(transitioned.version, 2)
        with self.assertRaises(CoreError) as caught:
            workflows.get("run-1", tenant_id="tenant-2", owner_id="owner-1")
        self.assertEqual(caught.exception.code, "TASK_NOT_FOUND")

        now = time.time()
        with database.transaction() as connection:
            for task_id, recoverable in (("read-task", True), ("write-task", False)):
                connection.execute(
                    """INSERT INTO core_background_tasks
                       (id, owner_run_id, tenant_id, kind, state, required,
                        recoverable, contract, created_at, updated_at)
                       VALUES (%s, 'run-1', 'tenant-1', 'test-read', 'working',
                               true, %s, %s, %s, %s)""",
                    (task_id, recoverable, Jsonb({"value": task_id}), now, now),
                )
        database.close()

        reopened = self._database()
        try:
            scheduler = PostgresTaskScheduler(reopened)
            scheduler.register(
                "test-read", lambda contract, cancel: {"value": contract["value"]}
            )
            self.assertEqual(scheduler.recover(), 1)
            read = scheduler.wait(
                "read-task", owner_id="run-1", tenant_id="tenant-1", timeout=2
            )
            self.assertEqual(read.state, "completed")
            self.assertEqual(read.result, {"value": "read-task"})
            write = scheduler.get(
                "write-task", owner_id="run-1", tenant_id="tenant-1"
            )
            self.assertEqual(write.state, "failed")
            self.assertEqual(write.error.code, "RECOVERY_REQUIRES_RECONCILIATION")
            notifications = scheduler.mailbox("run-1", "tenant-1").poll()
            self.assertEqual({item.task_id for item in notifications}, {"read-task", "write-task"})

            published = []
            dispatcher = OutboxDispatcher(reopened, lambda event: published.append(event["id"]))
            self.assertGreaterEqual(dispatcher.drain_once(), 3)
            self.assertEqual(len(published), len(set(published)))
            scheduler.close()
        finally:
            reopened.close()
