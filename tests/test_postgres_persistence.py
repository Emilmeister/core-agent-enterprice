import asyncio
import os
import time
import unittest
from unittest.mock import patch

import httpx
from a2a.auth.user import User
from a2a.server.context import ServerCallContext
from a2a.types import (
    Task,
    TaskPushNotificationConfig,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from cryptography.fernet import Fernet

from core_agent.app import _agent, create_app
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
from core_agent.push import (
    DurablePushNotificationSender,
    PostgresPushNotificationConfigStore,
)
from core_agent.tools import ToolCall
from core_agent.approvals import ApproveAllControlPlane
from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
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
                   core_background_tasks, core_notifications, core_outbox,
                   core_push_notification_configs, core_push_deliveries CASCADE"""
            )

    def _state(self, database):
        return {
            "database": database,
            "approvals": PostgresApprovalManager(database),
            "events": PostgresEventStore(database),
            "checkpoints": PostgresCheckpointStore(database),
            "audit": PostgresAuditLog(database),
            "tasks": PostgresTaskStore(database),
            "workflow": PostgresWorkflowStore(database),
            "scheduler": PostgresTaskScheduler,
        }

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

    def test_stock_production_entrypoint_builds_private_operator_plane(self):
        database = self._database()
        database.migrate()
        model = ScriptedModel([ModelResponse(message="ok")])
        model.model = "production-model"
        with patch.dict(
            os.environ,
            {
                "CORE_AGENT_ENVIRONMENT": "production",
                "CORE_AGENT_STATE_BACKEND": "postgres",
                "DATABASE_AUTO_MIGRATE": "false",
                "OPERATOR_JWT_HS256_SECRET": "x" * 32,
                "OPERATOR_JWT_ISSUER": "operator-issuer",
                "OPERATOR_JWT_AUDIENCE": "operator-api",
                "LOCAL_APPROVAL_EXTENSION_URI": "https://agent.example/extensions/local-approval/v1",
                "PUSH_NOTIFICATION_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            },
            clear=True,
        ):
            app = create_app(model=model, database=database)
        try:
            paths = {getattr(route, "path", "") for route in app.routes}
            self.assertIn("/internal/approvals", paths)
            self.assertFalse(app.state.operator_control_plane.automatic)
        finally:
            app.state.close()

    def test_push_delivery_is_encrypted_deduplicated_and_retried_after_restart(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        key = Fernet.generate_key()
        store = PostgresPushNotificationConfigStore(database, key)
        context = ServerCallContext(user=NamedUser(), tenant="tenant-1")
        config = TaskPushNotificationConfig(
            task_id="task-push",
            url="https://push.example/hook",
            token="push-secret",
        )
        public_dns = [(2, 1, 6, "", ("93.184.216.34", 443))]
        with patch("core_agent.push.socket.getaddrinfo", return_value=public_dns):
            asyncio.run(store.set_info("task-push", config, context))
        self.assertTrue(config.id)
        with database.pool.connection() as connection:
            encrypted = bytes(
                connection.execute(
                    "SELECT encrypted_payload FROM core_push_notification_configs"
                ).fetchone()["encrypted_payload"]
            )
        self.assertNotIn(b"push-secret", encrypted)
        self.assertNotIn(b"push.example", encrypted)

        responses = [503, 204]
        deliveries = []

        def webhook(request):
            deliveries.append(request)
            return httpx.Response(responses.pop(0), request=request)

        event = TaskStatusUpdateEvent(
            task_id="task-push",
            context_id="context-push",
            status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(webhook))
        first = DurablePushNotificationSender(
            database, store, client=client, retry_seconds=0
        )
        with patch("core_agent.push.socket.getaddrinfo", return_value=public_dns):
            asyncio.run(first.send_notification("task-push", event))
            restarted = DurablePushNotificationSender(
                database, store, client=client, retry_seconds=0
            )
            asyncio.run(restarted.dispatch_pending())
            asyncio.run(restarted.send_notification("task-push", event))
        asyncio.run(client.aclose())

        with database.pool.connection() as connection:
            row = connection.execute(
                """SELECT count(*) AS count, min(state) AS state,
                          min(attempts) AS attempts
                   FROM core_push_deliveries"""
            ).fetchone()
        self.assertEqual(row, {"count": 1, "state": "delivered", "attempts": 2})
        self.assertEqual(len(deliveries), 2)
        self.assertEqual(
            deliveries[0].headers["X-Core-Delivery-Id"],
            deliveries[1].headers["X-Core-Delivery-Id"],
        )
        other = ServerCallContext(user=NamedUser(), tenant="tenant-2")
        self.assertEqual(asyncio.run(store.get_info("task-push", other)), [])
        database.close()

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

    def test_agent_approval_continues_after_complete_process_state_loss(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        first_model = ScriptedModel(
            [
                ModelResponse(
                    tool_requests=(
                        ToolRequest(
                            "call-restart",
                            "core.terminal.exec",
                            {"argv": ["python", "-c", "print('once')"]},
                        ),
                    )
                )
            ]
        )
        first_model.model = "restart-model"
        environment = {
            "CORE_AGENT_TRUST_TERMINAL": "0",
            "CORE_AGENT_APPROVAL_MODE": "on_risk",
            "LOCAL_WORKSPACE_ROOT": "/tmp/core-agent-restart-test",
        }
        with patch.dict(os.environ, environment):
            first, first_telemetry = _agent(first_model, state=self._state(database))
            pending = first.run(
                {"prompt": "run once", "mcp": [], "skills": []},
                task_id="restart-task",
                identity="owner-1",
                session_id="context-1",
                tenant_id="tenant-1",
            )
        approval_id = pending.request.id
        run_id = pending.run_id
        first.close()
        first_telemetry.shutdown()
        database.close()

        reopened = self._database()
        second_model = ScriptedModel([ModelResponse(message="continued")])
        second_model.model = "restart-model"
        with patch.dict(os.environ, environment):
            second, second_telemetry = _agent(
                second_model, state=self._state(reopened)
            )
            reserved = second.reserve_local_approval(
                "restart-task", approval_id, ApproveAllControlPlane()
            )
            result = second.dispatch_reserved_approval(
                "restart-task", approval_id, reserved.execution_id
            )
        try:
            self.assertEqual(result.message, "continued")
            self.assertEqual(len(second_model.calls), 1)
            self.assertEqual(
                PostgresApprovalManager(reopened).execution_for(approval_id).state,
                "SUCCEEDED",
            )
            record = PostgresWorkflowStore(reopened).get(
                run_id, tenant_id="tenant-1", owner_id="owner-1"
            )
            self.assertEqual(record.state, "COMPLETED")
        finally:
            second.close()
            second_telemetry.shutdown()
            reopened.close()

    def test_dispatched_side_effect_is_aborted_not_retried_after_restart(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_requests=(
                        ToolRequest(
                            "unknown-call",
                            "core.terminal.exec",
                            {"argv": ["python", "-c", "print('must-not-repeat')"]},
                        ),
                    )
                )
            ]
        )
        model.model = "restart-model"
        environment = {
            "CORE_AGENT_TRUST_TERMINAL": "0",
            "CORE_AGENT_APPROVAL_MODE": "on_risk",
            "LOCAL_WORKSPACE_ROOT": "/tmp/core-agent-unknown-test",
        }
        with patch.dict(os.environ, environment):
            agent, telemetry = _agent(model, state=self._state(database))
            pending = agent.run(
                {"prompt": "unknown outcome", "mcp": [], "skills": []},
                task_id="unknown-task",
                identity="owner-1",
                session_id="context-1",
                tenant_id="tenant-1",
            )
            reserved = agent.reserve_local_approval(
                "unknown-task", pending.request.id, ApproveAllControlPlane()
            )
        call = ToolCall(
            "unknown-call",
            "core.terminal.exec",
            {"argv": ["python", "-c", "print('must-not-repeat')"]},
        )
        PostgresApprovalManager(database).authorize_dispatch(pending.request.id, call)
        workflows = PostgresWorkflowStore(database)
        current = workflows.lookup_task("unknown-task")
        workflows.transition(
            current.run_id,
            tenant_id=current.tenant_id,
            owner_id=current.owner_id,
            expected_version=current.version,
            state="EXECUTING",
            snapshot={**current.snapshot, "execution_id": reserved.execution_id},
            event_kind="tool.intent",
            pending_approval_id=pending.request.id,
        )
        agent.close()
        telemetry.shutdown()
        database.close()

        reopened = self._database()
        empty_model = ScriptedModel([])
        empty_model.model = "restart-model"
        with patch.dict(os.environ, environment):
            recovered, recovered_telemetry = _agent(
                empty_model, state=self._state(reopened)
            )
        try:
            record = PostgresWorkflowStore(reopened).lookup_task("unknown-task")
            self.assertEqual(record.state, "ABORTED")
            self.assertEqual(record.error_code, "SIDE_EFFECT_UNKNOWN")
            self.assertEqual(empty_model.calls, ())
        finally:
            recovered.close()
            recovered_telemetry.shutdown()
            reopened.close()
