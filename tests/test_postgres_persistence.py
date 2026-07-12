import asyncio
import os
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
from core_agent.tools import ToolCall


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

    def test_all_production_state_survives_pool_restart_and_is_scoped(self):
        database = self._database()
        database.migrate()
        with database.transaction() as connection:
            connection.execute(
                """TRUNCATE core_execution_records, core_approval_requests,
                   core_tool_proposals, core_events, core_checkpoints,
                   core_audit_records, core_a2a_tasks CASCADE"""
            )
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
