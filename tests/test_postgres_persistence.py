import asyncio
import hashlib
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
from a2a.auth.user import User
from a2a.server.context import ServerCallContext
from a2a.types import (
    Artifact,
    Task,
    TaskPushNotificationConfig,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from cryptography.fernet import Fernet

from tests.app_support import create_app
from core_agent.artifacts import PostgresArtifactStore
from core_agent.database import (
    PostgresAuditLog,
    PostgresCheckpointStore,
    PostgresDatabase,
    PostgresEventStore,
    PostgresTaskStore,
)
from core_agent.errors import CoreError
from core_agent.mcp import InMemoryMcpConnector
from core_agent.postgres_tasks import PostgresTaskScheduler
from core_agent.push import (
    DurablePushNotificationSender,
    PostgresPushNotificationConfigStore,
)
from core_agent.model import ModelResponse, ScriptedModel
from core_agent.lifecycle import PostgresRetentionManager
from core_agent.workflow import OutboxDispatcher, PostgresWorkflowStore, WorkflowRecord
from psycopg.types.json import Jsonb


class ProductionConfigurationTests(unittest.TestCase):
    def test_health_routes_precede_a2a_catch_all(self):
        model = type("Model", (), {"model": "test-model"})()
        with patch.dict(
            os.environ,
            {
                "CORE_AGENT_ENVIRONMENT": "development",
                "SESSION_STORAGE_TYPE": "in-memory",
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

    def test_builtin_allowlist_removes_disabled_tools_from_card_and_model(self):
        model = ScriptedModel([ModelResponse(message="ok")])
        model.model = "allowlist-model"
        with patch.dict(
            os.environ,
            {
                "CORE_AGENT_ENVIRONMENT": "development",
                "SESSION_STORAGE_TYPE": "in-memory",
                "CORE_AGENT_ALLOWED_BUILTIN_TOOLS": "core_task_list",
            },
            clear=True,
        ):
            app = create_app(model=model)
        try:
            result = app.state.core_agent.run({"prompt": "answer"})
            self.assertEqual(result.message, "ok")
            self.assertEqual(model.calls[0].tools, frozenset({"core_task_list"}))
            advertised = {
                skill.id for skill in app.state.a2a_request_handler._agent_card.skills
            }
            self.assertEqual(advertised, {"core_task_list"})
        finally:
            app.state.close()

        with patch.dict(
            os.environ,
            {
                "CORE_AGENT_ENVIRONMENT": "development",
                "SESSION_STORAGE_TYPE": "in-memory",
                "CORE_AGENT_ALLOWED_BUILTIN_TOOLS": "core_artifact_get",
            },
            clear=True,
        ):
            with self.assertRaises(CoreError) as caught:
                create_app(model=model)
        self.assertEqual(caught.exception.code, "CONFIG_INVALID")

    def test_default_prompt_and_builtin_descriptions_match_runtime_contract(self):
        model = ScriptedModel([ModelResponse(message="ok")])
        model.model = "prompt-contract-model"
        with patch.dict(
            os.environ,
            {
                "CORE_AGENT_ENVIRONMENT": "development",
                "SESSION_STORAGE_TYPE": "in-memory",
            },
            clear=True,
        ):
            app = create_app(model=model)
        try:
            prompt = "Actual request stays in the user context."
            app.state.core_agent.run({"prompt": prompt})
            call = model.calls[0]
            self.assertIn(prompt, call.context)
            self.assertNotIn(prompt, call.instructions)
            self.assertNotIn(
                "Complete the user's task using available tools.", call.instructions
            )
            self.assertIn("Delegate a coherent outcome", call.instructions)
            # Python is a plain built-in now: no HITL gate hides it.
            self.assertIn("PYTHON:", call.instructions)
            self.assertNotIn("core_artifact_put", call.tools)
            self.assertNotIn("core_artifact_get", call.tools)

            registry = app.state.core_agent.tool_runtime.registry
            delegate = registry.get("core_delegate").description
            self.assertIn("coherent outcome", delegate)
            self.assertIn("minimum sufficient capabilities", delegate)
            self.assertIn("child receives exactly that set", delegate)
            self.assertIn("independently chooses its method", delegate)
            self.assertIn("ordinary text result", delegate)
            self.assertIn(
                "Always set both budget.turns >= 1 and budget.tool_calls >= 1",
                delegate,
            )
            self.assertNotIn("exactly once", delegate)
            budget_schema = registry.get("core_delegate").input_schema["properties"][
                "budget"
            ]
            self.assertEqual(budget_schema["required"], ["turns", "tool_calls"])
            for field in budget_schema["required"]:
                self.assertEqual(budget_schema["properties"][field]["minimum"], 1)
            self.assertIn(
                "Always set both budget.turns >= 1 and budget.tool_calls >= 1",
                call.instructions,
            )
            for guidance in (delegate, call.instructions):
                self.assertIn(
                    "independent work can run in parallel with a material latency benefit",
                    guidance,
                )
                self.assertIn(
                    "large separable context should be isolated",
                    guidance,
                )
                self.assertIn(
                    "bounded independently verifiable deliverable",
                    guidance,
                )
                self.assertIn("coordination overhead", guidance)
                self.assertIn("immediate serial next steps", guidance)
                self.assertIn(
                    "generic second opinions without a concrete deliverable",
                    guidance,
                )
                self.assertNotIn("more appropriate tools", guidance)
                self.assertNotIn("minimum number of child agents", guidance)
            self.assertIn(
                "non-task, non-delegation, non-Python",
                registry.get("core_task_start").description,
            )
            self.assertIn(
                "timeout returns the current snapshot",
                registry.get("core_task_wait").description,
            )
            self.assertNotIn("core_artifact_put", registry.names())
            self.assertNotIn("core_artifact_get", registry.names())
            python = registry.get("core_python_exec").description
            self.assertIn("datetime.now().astimezone()", python)
            self.assertIn("Execution is restricted to the chat workspace", python)
            terminal = registry.get("core_terminal_exec").description
            self.assertIn(
                "Preinstalled CLI: GNU coreutils/findutils/gawk/sed/grep, rg, fd",
                terminal,
            )
            self.assertIn("Mike Farah yq", terminal)
            self.assertIn(
                "pdftotext/pdfinfo/pdftoppm/pdfimages, and qpdf", terminal
            )
            self.assertIn("This list is not exhaustive", terminal)
            self.assertIn(
                "install workspace-local tools when policy and network access allow",
                terminal,
            )
            self.assertNotIn("root", terminal)
            self.assertLessEqual(len(terminal), 700)
        finally:
            app.state.close()

    def test_without_terminal_mode_is_a_capability_ceiling(self):
        model = ScriptedModel([ModelResponse(message="ok")])
        model.model = "without-terminal-model"
        with patch.dict(
            os.environ,
            {
                "CORE_AGENT_ENVIRONMENT": "development",
                "SESSION_STORAGE_TYPE": "in-memory",
                "CORE_AGENT_RUNTIME_MODE": "without_terminal",
                "CORE_AGENT_ALLOWED_BUILTIN_TOOLS": (
                    "core_terminal_exec,core_python_exec,core_task_start,core_task_get,"
                    "core_task_list,core_task_wait,core_task_cancel,"
                    "core_delegate"
                ),
            },
            clear=True,
        ):
            app = create_app(model=model)
        try:
            result = app.state.core_agent.run({"prompt": "answer"})
            self.assertEqual(result.message, "ok")
            self.assertNotIn("core_terminal_exec", model.calls[0].tools)
            self.assertNotIn("core_task_start", model.calls[0].tools)
            # without_terminal removes terminal tools, not Python.
            self.assertIn("core_python_exec", model.calls[0].tools)
            self.assertIn("core_delegate", model.calls[0].tools)
            self.assertIn("core_task_wait", model.calls[0].tools)
            advertised = {
                skill.id for skill in app.state.a2a_request_handler._agent_card.skills
            }
            self.assertEqual(advertised, set(model.calls[0].tools))
            execution = app.state.core_agent.agent_config.to_dict()["execution"]
            self.assertEqual(execution["runtime_mode"], "without_terminal")
            # Python stays available, so the profile honestly reports local execution.
            self.assertEqual(execution["environment_profile"], "local-python")
        finally:
            app.state.close()

    def test_unknown_runtime_mode_is_rejected(self):
        model = ScriptedModel([ModelResponse(message="ok")])
        with patch.dict(
            os.environ,
            {
                "CORE_AGENT_ENVIRONMENT": "development",
                "SESSION_STORAGE_TYPE": "in-memory",
                "CORE_AGENT_RUNTIME_MODE": "maybe",
            },
            clear=True,
        ):
            with self.assertRaises(CoreError) as caught:
                create_app(model=model)
        self.assertEqual(caught.exception.code, "CONFIG_INVALID")

    def _assert_python_gate(self, runtime_mode, local_approval, expected):
        model = ScriptedModel([ModelResponse(message="ok")])
        model.model = "python-gate-model"
        with patch.dict(
            os.environ,
            {
                "CORE_AGENT_ENVIRONMENT": "development",
                "SESSION_STORAGE_TYPE": "in-memory",
                "CORE_AGENT_RUNTIME_MODE": runtime_mode,
                "CORE_AGENT_ALLOWED_BUILTIN_TOOLS": "core_python_exec",
            },
            clear=True,
        ):
            app = create_app(model=model)
        try:
            app.state.core_agent.run({"prompt": "answer"})
            self.assertEqual("core_python_exec" in model.calls[0].tools, expected)
            advertised = {
                skill.id for skill in app.state.a2a_request_handler._agent_card.skills
            }
            self.assertEqual("core_python_exec" in advertised, expected)
            if runtime_mode == "without_terminal" and expected:
                execution = app.state.core_agent.agent_config.to_dict()["execution"]
                self.assertEqual(execution["environment_profile"], "local-python")
        finally:
            app.state.close()


class NamedUser(User):
    def __init__(self, name="owner-1"):
        self.name = name

    @property
    def is_authenticated(self):
        return True

    @property
    def user_name(self):
        return self.name


@unittest.skipUnless(
    os.getenv("TEST_DATABASE_URL"),
    "set TEST_DATABASE_URL to run PostgreSQL restart tests",
)
class PostgresRestartTests(unittest.TestCase):
    def _database(self):
        return PostgresDatabase(os.environ["TEST_DATABASE_URL"], min_size=0, max_size=3)

    def _stop_workflow_recovery(self, app):
        agent = app.state.core_agent
        agent._recovery_stop.set()
        if agent._recovery_thread is not None:
            agent._recovery_thread.join(timeout=1)

    def test_pool_replaces_a_connection_the_server_closed_while_idle(self):
        """Managed PostgreSQL drops idle connections; the caller must not see it."""
        import psycopg

        database = PostgresDatabase(
            os.environ["TEST_DATABASE_URL"], min_size=1, max_size=1
        )
        try:
            with database.pool.connection() as connection:
                pid = connection.execute("SELECT pg_backend_pid() AS pid").fetchone()[
                    "pid"
                ]
            # Kill that pooled connection from outside, as an idle timeout would.
            with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as killer:
                killer.execute("SELECT pg_terminate_backend(%s)", (pid,))
            for _ in range(3):
                with database.pool.connection() as connection:
                    self.assertEqual(
                        connection.execute("SELECT 1 AS ok").fetchone()["ok"], 1
                    )
        finally:
            database.close()

    def _reset(self, database):
        with database.transaction() as connection:
            connection.execute(
                """TRUNCATE core_events, core_checkpoints,
                   core_audit_records, core_a2a_tasks, core_runs,
                   core_background_tasks, core_notifications, core_outbox,
                   core_push_notification_configs, core_push_deliveries,
                   core_artifacts, core_budget_ledgers CASCADE"""
            )

    def _wait_for_blocked_query(self, database, fragment):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with database.pool.connection() as connection:
                blocked = connection.execute(
                    """SELECT 1 FROM pg_stat_activity
                       WHERE pid <> pg_backend_pid() AND query LIKE %s
                         AND cardinality(pg_blocking_pids(pid)) > 0
                       LIMIT 1""",
                    (f"%{fragment}%",),
                ).fetchone()
            if blocked is not None:
                return
            time.sleep(0.01)
        self.fail(f"query did not block: {fragment}")

    def _state(self, database):
        return {
            "database": database,
            "events": PostgresEventStore(database),
            "checkpoints": PostgresCheckpointStore(database),
            "audit": PostgresAuditLog(database),
            "tasks": PostgresTaskStore(database),
            "workflow": PostgresWorkflowStore(database),
            "scheduler": PostgresTaskScheduler,
        }

    def test_mcp_cold_start_begins_after_durable_admission_commit(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        started = threading.Event()

        class StartingConnector(InMemoryMcpConnector):
            cold_start_timeout = 30.0

            def connect(self, declaration, *, cancel_event=None, deadline=None):
                started.set()
                cancel_event.wait(2)
                raise CoreError("TASK_CANCELLED")

        model = ScriptedModel([ModelResponse(message="must not run")])
        model.model = "postgres-cold-start-model"
        app = None
        failures = []
        with patch.dict(
            os.environ,
            {
                "CORE_AGENT_ENVIRONMENT": "development",
                "CORE_AGENT_MEMORY": "disabled",
                "DATABASE_AUTO_MIGRATE": "false",
                "MCP_URL": "http://127.0.0.1:1/mcp",
                "SESSION_STORAGE_TYPE": "postgres",
                "TASK_STORAGE_TYPE": "postgres",
            },
        ):
            try:
                app = create_app(
                    model=model,
                    mcp_connector=StartingConnector(),
                    database=database,
                )

                def run():
                    try:
                        app.state.core_agent.run(
                            {"prompt": "wait for MCP"},
                            task_id="postgres-cold-start",
                            tenant_id="tenant-1",
                            identity="owner-1",
                        )
                    except Exception as error:
                        failures.append(error)

                thread = threading.Thread(target=run)
                thread.start()
                self.assertTrue(started.wait(1))
                with database.pool.connection() as connection:
                    row = connection.execute(
                        "SELECT run_id, state, snapshot FROM core_runs WHERE task_id = %s",
                        ("postgres-cold-start",),
                    ).fetchone()
                    checkpoint = connection.execute(
                        "SELECT state FROM core_checkpoints WHERE run_id = %s",
                        (row["run_id"],),
                    ).fetchone()
                self.assertEqual(row["state"], "RUNNING")
                self.assertTrue(row["snapshot"]["initializing"])
                self.assertIn("platform_config", row["snapshot"]["admission"])
                self.assertEqual(len(row["snapshot"]["admission"]["mcp"]), 1)
                self.assertGreater(
                    row["snapshot"]["mcp_cold_start_expires_at"], time.time()
                )
                self.assertTrue(checkpoint["state"]["initializing"])

                app.state.core_agent.cancel_task("postgres-cold-start")
                thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assertEqual([error.code for error in failures], ["TASK_CANCELLED"])
                self.assertEqual(
                    app.state.core_agent.workflow_store.lookup_task(
                        "postgres-cold-start"
                    ).state,
                    "CANCELLED",
                )
                self.assertEqual(model.calls, ())
            finally:
                if app is not None:
                    app.state.close()
                else:
                    database.close()

    def test_mcp_zero_cold_start_timeout_resumes_automatically_after_restart(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        push_key = Fernet.generate_key()

        class ZeroTimeoutConnector(InMemoryMcpConnector):
            cold_start_timeout = 0.0

        environment = {
            "CORE_AGENT_ENVIRONMENT": "development",
            "CORE_AGENT_MEMORY": "disabled",
            "DATABASE_AUTO_MIGRATE": "false",
            "MCP_URL": "http://127.0.0.1:1/mcp",
            "PUSH_NOTIFICATION_ENCRYPTION_KEY": push_key.decode(),
            "SESSION_STORAGE_TYPE": "postgres",
            "TASK_STORAGE_TYPE": "postgres",
        }
        first = None
        first_model = ScriptedModel([])
        first_model.model = "postgres-zero-timeout-first"
        with patch.dict(os.environ, environment):
            try:
                first = create_app(
                    model=first_model,
                    mcp_connector=ZeroTimeoutConnector(),
                    database=database,
                )
                self._stop_workflow_recovery(first)
                record, _raw, _discovered, _effective = (
                    first.state.core_agent._new_workflow(
                        {"prompt": "one attempt"},
                        task_id="postgres-zero-timeout",
                        identity="owner-1",
                        session_id="postgres-zero-timeout-context",
                        tenant_id="tenant-1",
                        defer_initialization=True,
                    )
                )
                self.assertIsNone(record.snapshot["mcp_cold_start_expires_at"])
                context = ServerCallContext(user=NamedUser(), tenant="tenant-1")
                asyncio.run(
                    PostgresTaskStore(database).save(
                        Task(
                            id=record.task_id,
                            context_id=record.context_id,
                            status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                        ),
                        context,
                    )
                )
                push_config = TaskPushNotificationConfig(
                    task_id=record.task_id,
                    url="https://push.example/hook",
                )
                with patch(
                    "core_agent.push.socket.getaddrinfo",
                    return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
                ):
                    asyncio.run(
                        PostgresPushNotificationConfigStore(
                            database, push_key
                        ).set_info(record.task_id, push_config, context)
                    )
            finally:
                if first is not None:
                    first.state.close()
                else:
                    database.close()

        class CapturingConnector(InMemoryMcpConnector):
            cold_start_timeout = 300.0

            def __init__(self):
                super().__init__()
                self.deadlines = []

            def connect(self, declaration, *, cancel_event=None, deadline=None):
                self.deadlines.append(deadline)
                return super().connect(
                    declaration, cancel_event=cancel_event, deadline=deadline
                )

        connector = CapturingConnector()
        database = self._database()
        second = None
        model = ScriptedModel([ModelResponse(message="continued without retries")])
        model.model = "postgres-zero-timeout-second"
        with patch.dict(os.environ, environment):
            try:
                second = create_app(
                    model=model,
                    mcp_connector=connector,
                    database=database,
                )
                deadline = time.monotonic() + 2
                while True:
                    persisted = second.state.core_agent.workflow_store.lookup_task(
                        record.task_id
                    )
                    with database.pool.connection() as connection:
                        deliveries = connection.execute(
                            "SELECT state, attempts, payload FROM core_push_deliveries"
                        ).fetchall()
                    if (
                        persisted.state == "COMPLETED"
                        and deliveries
                        or time.monotonic() >= deadline
                    ):
                        break
                    time.sleep(0.01)
                self.assertEqual(persisted.state, "COMPLETED")
                self.assertEqual(
                    persisted.result["message"], "continued without retries"
                )
                self.assertEqual(connector.deadlines, [0.0])
                task = asyncio.run(
                    PostgresTaskStore(database).get(record.task_id, context)
                )
                self.assertEqual(task.status.state, TaskState.TASK_STATE_COMPLETED)
                self.assertEqual(
                    task.artifacts[0].parts[0].text, persisted.result["message"]
                )
                self.assertEqual(len(deliveries), 1)
                self.assertEqual(deliveries[0]["state"], "pending")
                self.assertEqual(deliveries[0]["attempts"], 0)
                self.assertIn(
                    persisted.result["message"], str(deliveries[0]["payload"])
                )
                self.assertEqual(
                    PostgresTaskStore(database).reconcile_from_workflows(
                        enqueue_notification=second.state.push_sender.enqueue_notification
                    ),
                    0,
                )
                with database.pool.connection() as connection:
                    delivery_count = connection.execute(
                        "SELECT count(*) AS count FROM core_push_deliveries"
                    ).fetchone()["count"]
                self.assertEqual(delivery_count, 1)
            finally:
                if second is not None:
                    second.state.close()
                else:
                    database.close()

    def test_mcp_cold_start_observes_cancel_from_another_postgres_worker(self):
        database_a = self._database()
        database_a.migrate()
        self._reset(database_a)
        started = threading.Event()

        class BlockingConnector(InMemoryMcpConnector):
            cold_start_timeout = 30.0

            def connect(self, declaration, *, cancel_event=None, deadline=None):
                started.set()
                if cancel_event.wait(3):
                    raise CoreError("TASK_CANCELLED")
                raise CoreError("MCP_CONNECTION_FAILED", retryable=True)

        environment = {
            "CORE_AGENT_ENVIRONMENT": "development",
            "CORE_AGENT_MEMORY": "disabled",
            "DATABASE_AUTO_MIGRATE": "false",
            "MCP_URL": "http://127.0.0.1:1/mcp",
            "SESSION_STORAGE_TYPE": "postgres",
            "TASK_STORAGE_TYPE": "postgres",
        }
        app_a = None
        app_b = None
        database_b = None
        failures = []
        model = ScriptedModel([ModelResponse(message="must not run")])
        model.model = "postgres-cross-worker-first"
        with patch.dict(os.environ, environment):
            try:
                app_a = create_app(
                    model=model,
                    mcp_connector=BlockingConnector(),
                    database=database_a,
                )

                def run():
                    try:
                        app_a.state.core_agent.run(
                            {"prompt": "wait for MCP"},
                            task_id="postgres-cross-worker-cancel",
                            tenant_id="tenant-1",
                            identity="owner-1",
                        )
                    except Exception as error:
                        failures.append(error)

                thread = threading.Thread(target=run)
                thread.start()
                self.assertTrue(started.wait(1))

                database_b = self._database()
                cancel_model = ScriptedModel([])
                cancel_model.model = "postgres-cross-worker-second"
                app_b = create_app(
                    model=cancel_model,
                    mcp_connector=InMemoryMcpConnector(),
                    database=database_b,
                )
                app_b.state.core_agent.cancel_task("postgres-cross-worker-cancel")
                thread.join(2)

                self.assertFalse(thread.is_alive())
                self.assertEqual([error.code for error in failures], ["TASK_CANCELLED"])
                self.assertEqual(model.calls, ())
                self.assertEqual(
                    app_b.state.core_agent.workflow_store.lookup_task(
                        "postgres-cross-worker-cancel"
                    ).state,
                    "CANCELLED",
                )
            finally:
                if app_b is not None:
                    app_b.state.close()
                elif database_b is not None:
                    database_b.close()
                if app_a is not None:
                    app_a.state.close()
                else:
                    database_a.close()

    def test_terminal_followup_disposition_rolls_back_with_terminal_transition(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        workflows = PostgresWorkflowStore(database)
        record = workflows.create(
            WorkflowRecord(
                "terminal-disposition-run",
                "terminal-disposition-task",
                "terminal-disposition-context",
                "tenant-1",
                "owner-1",
                None,
                "RUNNING",
                1,
                {"prompt": "start"},
                {"context": {}, "turns": 0},
            )
        )
        workflows.append_inbound(
            record.task_id,
            tenant_id=record.tenant_id,
            owner_id=record.owner_id,
            message_id="accepted-before-failure",
            context_id=record.context_id,
            content="correction",
            provenance={},
        )
        original_event = workflows._event

        def crash_on_terminal(connection, current, kind, data, now):
            if kind == "task.failed":
                raise RuntimeError("injected crash before terminal commit")
            return original_event(connection, current, kind, data, now)

        try:
            with (
                patch.object(workflows, "_event", side_effect=crash_on_terminal),
                self.assertRaisesRegex(RuntimeError, "injected crash"),
            ):
                workflows.consume_inbound(
                    record,
                    expected_version=record.version,
                    snapshot={"context": {"disposed": True}, "turns": 0},
                    sequences=(1,),
                    lease_token=None,
                    state="FAILED",
                    event_kind="task.failed",
                    inbound_event_kind="input.dispositioned",
                    error_code="MCP_PROTOCOL_ERROR",
                )

            persisted = workflows.get(
                record.run_id,
                tenant_id=record.tenant_id,
                owner_id=record.owner_id,
            )
            self.assertEqual(persisted.state, "RUNNING")
            self.assertEqual(
                [item["message_id"] for item in workflows.pending_inbound(persisted)],
                ["accepted-before-failure"],
            )
        finally:
            database.close()

    def test_initialized_mcp_catalog_and_reconnect_deadline_survive_restart(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        environment = {
            "CORE_AGENT_ENVIRONMENT": "development",
            "CORE_AGENT_MEMORY": "disabled",
            "DATABASE_AUTO_MIGRATE": "false",
            "MCP_URL": "http://127.0.0.1:1/mcp",
            "MCP_ALLOWED_TOOLS": "search",
            "SESSION_STORAGE_TYPE": "postgres",
            "TASK_STORAGE_TYPE": "postgres",
        }
        first = None
        first_model = ScriptedModel([])
        first_model.model = "postgres-mcp-catalog-first"
        with patch.dict(os.environ, environment):
            try:
                first = create_app(
                    model=first_model,
                    mcp_connector=InMemoryMcpConnector(
                        catalogs={"mcp": {"search": {"type": "object"}}}
                    ),
                    database=database,
                )
                self._stop_workflow_recovery(first)
                record, _raw, _discovered, _effective = (
                    first.state.core_agent._new_workflow(
                        {"prompt": "use the saved catalog"},
                        task_id="postgres-mcp-catalog-recovery",
                        identity="owner-1",
                        session_id="postgres-mcp-catalog-context",
                        tenant_id="tenant-1",
                    )
                )
                digest = record.snapshot["effective_config_digest"]
                snapshot = {
                    **record.snapshot,
                    "mcp_reconnect_started": True,
                    "mcp_reconnect_expires_at": time.time() - 1,
                }
                first.state.core_agent.workflow_store.transition(
                    record.run_id,
                    tenant_id=record.tenant_id,
                    owner_id=record.owner_id,
                    expected_version=record.version,
                    state=record.state,
                    snapshot=snapshot,
                    event_kind="test.process.stopped",
                )
            finally:
                if first is not None:
                    first.state.close()
                else:
                    database.close()

        class UnavailableConnector(InMemoryMcpConnector):
            cold_start_timeout = 300.0

            def __init__(self):
                super().__init__()
                self.deadlines = []

            def connect(self, declaration, *, cancel_event=None, deadline=None):
                self.deadlines.append(deadline)
                raise CoreError(
                    "MCP_CONNECTION_FAILED",
                    "server is still starting",
                    retryable=True,
                )

        connector = UnavailableConnector()
        model = ScriptedModel([ModelResponse(message="used persisted catalog")])
        model.model = "postgres-mcp-catalog-second"
        database = self._database()
        second = None
        with patch.dict(os.environ, environment):
            try:
                second = create_app(
                    model=model,
                    mcp_connector=connector,
                    database=database,
                )
                result = second.state.core_agent.resume_task(record.task_id)
                persisted = second.state.core_agent.workflow_store.lookup_task(
                    record.task_id
                )
                self.assertEqual(result.message, "used persisted catalog")
                self.assertEqual(len(connector.deadlines), 1)
                self.assertLessEqual(connector.deadlines[0], time.monotonic())
                # Persisted identities remain the ceiling; unavailable live tools
                # must be absent from the model catalog after failed reconnect.
                self.assertNotIn("mcp_search", model.calls[0].tools)
                self.assertEqual(persisted.snapshot["effective_config_digest"], digest)
                self.assertEqual(
                    persisted.snapshot["mcp_catalogs"],
                    {"mcp": {"search": {"type": "object"}}},
                )
                self.assertNotIn("mcp_reconnect_expires_at", persisted.snapshot)
            finally:
                if second is not None:
                    second.state.close()
                else:
                    database.close()

    def test_required_mcp_reconnect_failure_is_terminal_after_restart(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        environment = {
            "CORE_AGENT_ENVIRONMENT": "development",
            "CORE_AGENT_MEMORY": "disabled",
            "DATABASE_AUTO_MIGRATE": "false",
            "MCP_URL": "http://127.0.0.1:1/mcp",
            "MCP_ALLOWED_TOOLS": "search",
            "SESSION_STORAGE_TYPE": "postgres",
            "TASK_STORAGE_TYPE": "postgres",
        }
        first = None
        first_model = ScriptedModel([])
        first_model.model = "postgres-required-mcp-first"
        with patch.dict(os.environ, environment):
            try:
                first = create_app(
                    model=first_model,
                    mcp_connector=InMemoryMcpConnector(
                        catalogs={"mcp": {"search": {"type": "object"}}}
                    ),
                    database=database,
                )
                self._stop_workflow_recovery(first)
                declaration = {
                    **first.state.core_agent.platform_mcp[0],
                    "required": True,
                }
                first.state.core_agent.platform_mcp = (declaration,)
                record, _raw, _discovered, _effective = (
                    first.state.core_agent._new_workflow(
                        {"prompt": "required MCP"},
                        task_id="postgres-required-mcp-recovery",
                        identity="owner-1",
                        session_id="postgres-required-mcp-context",
                        tenant_id="tenant-1",
                    )
                )
            finally:
                if first is not None:
                    first.state.close()
                else:
                    database.close()

        class UnavailableConnector(InMemoryMcpConnector):
            cold_start_timeout = 0.0

            def connect(self, declaration, *, cancel_event=None, deadline=None):
                raise CoreError(
                    "MCP_CONNECTION_FAILED",
                    "required server unavailable",
                    retryable=True,
                )

        model = ScriptedModel([ModelResponse(message="must not run")])
        model.model = "postgres-required-mcp-second"
        database = self._database()
        second = None
        with patch.dict(os.environ, environment):
            try:
                second = create_app(
                    model=model,
                    mcp_connector=UnavailableConnector(),
                    database=database,
                )
                with self.assertRaises(CoreError) as caught:
                    second.state.core_agent.resume_task(record.task_id)
                terminal = second.state.core_agent.workflow_store.lookup_task(
                    record.task_id
                )
                self.assertEqual(caught.exception.code, "MCP_CONNECTION_FAILED")
                self.assertEqual(terminal.state, "FAILED")
                self.assertEqual(terminal.error_code, "MCP_CONNECTION_FAILED")
                self.assertEqual(model.calls, ())
            finally:
                if second is not None:
                    second.state.close()
                else:
                    database.close()

    def test_terminal_workflow_reconciles_same_a2a_task_and_artifact_after_crash(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        workflows = PostgresWorkflowStore(database)
        workflows.create(
            WorkflowRecord(
                "reconcile-run",
                "reconcile-task",
                "reconcile-context",
                "tenant-1",
                "owner-1",
                None,
                "RUNNING",
                1,
                {"prompt": "work"},
                {"turns": 1},
            )
        )
        context = ServerCallContext(user=NamedUser(), tenant="tenant-1")
        store = PostgresTaskStore(database)
        asyncio.run(
            store.save(
                Task(
                    id="reconcile-task",
                    context_id="reconcile-context",
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                ),
                context,
            )
        )
        with database.transaction() as connection:
            connection.execute(
                """UPDATE core_runs SET state = 'COMPLETED',
                       result = %s, version = version + 1
                   WHERE run_id = 'reconcile-run'""",
                (
                    Jsonb(
                        {
                            "message": "recovered result",
                            "usage": {"model_turns": 1, "tool_calls": 0},
                            "complete": False,
                            "completion_reason": "budget_exhausted",
                            "exhausted_dimension": "model_turns",
                            "shared_budget": {
                                "scope": "root",
                                "used": {"model_turns": 3, "tool_calls": 1},
                                "limits": {"model_turns": 3, "tool_calls": 2},
                            },
                        }
                    ),
                ),
            )

        def fail_enqueue(*_args, **_kwargs):
            raise RuntimeError("push enqueue failed")

        with self.assertRaisesRegex(RuntimeError, "push enqueue failed"):
            store.reconcile_from_workflows(enqueue_notification=fail_enqueue)
        unchanged = asyncio.run(store.get("reconcile-task", context))
        self.assertEqual(unchanged.status.state, TaskState.TASK_STATE_WORKING)
        self.assertEqual(list(unchanged.artifacts), [])
        self.assertEqual(store.reconcile_from_workflows(), 1)
        task = asyncio.run(store.get("reconcile-task", context))
        self.assertEqual(task.status.state, TaskState.TASK_STATE_COMPLETED)
        self.assertEqual(task.artifacts[0].parts[0].text, "recovered result")
        provenance = task.artifacts[0].metadata["provenance"]
        self.assertFalse(provenance["complete"])
        self.assertEqual(provenance["completion_reason"], "budget_exhausted")
        self.assertEqual(provenance["exhausted_dimension"], "model_turns")
        self.assertEqual(provenance["usage"], {"model_turns": 1, "tool_calls": 0})
        self.assertEqual(
            provenance["shared_budget"]["used"],
            {"model_turns": 3, "tool_calls": 1},
        )
        self.assertEqual(store.reconcile_from_workflows(), 0)
        database.close()

    def test_failed_workflow_reconciliation_preserves_safe_error_code(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        workflows = PostgresWorkflowStore(database)
        context = ServerCallContext(user=NamedUser(), tenant="tenant-1")
        store = PostgresTaskStore(database)
        try:
            workflows.create(
                WorkflowRecord(
                    "failed-reconcile-run",
                    "failed-reconcile-task",
                    "failed-reconcile-context",
                    "tenant-1",
                    "owner-1",
                    None,
                    "RUNNING",
                    1,
                    {"prompt": "work"},
                    {},
                )
            )
            asyncio.run(
                store.save(
                    Task(
                        id="failed-reconcile-task",
                        context_id="failed-reconcile-context",
                        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                    ),
                    context,
                )
            )
            with database.transaction() as connection:
                connection.execute(
                    """UPDATE core_runs SET state = 'ABORTED',
                              error_code = 'SIDE_EFFECT_UNKNOWN', version = version + 1
                       WHERE run_id = 'failed-reconcile-run'"""
                )

            self.assertEqual(store.reconcile_from_workflows(), 1)
            task = asyncio.run(store.get("failed-reconcile-task", context))

            self.assertEqual(task.status.state, TaskState.TASK_STATE_FAILED)
            self.assertEqual(task.status.message.parts[0].text, "SIDE_EFFECT_UNKNOWN")
        finally:
            database.close()

    def test_postgres_task_store_does_not_regress_a_terminal_task(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        context = ServerCallContext(user=NamedUser(), tenant="tenant-1")
        store = PostgresTaskStore(database)
        try:
            asyncio.run(
                store.save(
                    Task(
                        id="terminal-monotonic-task",
                        context_id="terminal-monotonic-context",
                        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
                    ),
                    context,
                )
            )
            asyncio.run(
                store.save(
                    Task(
                        id="terminal-monotonic-task",
                        context_id="terminal-monotonic-context",
                        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                    ),
                    context,
                )
            )
            persisted = asyncio.run(store.get("terminal-monotonic-task", context))
            self.assertEqual(
                persisted.status.state,
                TaskState.TASK_STATE_COMPLETED,
            )
        finally:
            database.close()

    def test_workflow_reconciliation_matches_task_owner_and_tenant(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        workflows = PostgresWorkflowStore(database)
        store = PostgresTaskStore(database)
        scopes = (
            ("scope-run-1", "owner-1", "tenant-1"),
            ("scope-run-2", "owner-2", "tenant-1"),
            ("scope-run-3", "owner-1", "tenant-2"),
        )
        try:
            for run_id, owner, tenant in scopes:
                workflows.create(
                    WorkflowRecord(
                        run_id,
                        "shared-task-id",
                        f"{run_id}-context",
                        tenant,
                        owner,
                        None,
                        "RUNNING",
                        1,
                        {"prompt": "work"},
                        {"turns": 1},
                    )
                )
                context = ServerCallContext(user=NamedUser(owner), tenant=tenant)
                asyncio.run(
                    store.save(
                        Task(
                            id="shared-task-id",
                            context_id=f"{run_id}-context",
                            status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                        ),
                        context,
                    )
                )
                with database.transaction() as connection:
                    connection.execute(
                        """UPDATE core_runs SET state = 'COMPLETED', result = %s,
                                  version = version + 1 WHERE run_id = %s""",
                        (
                            Jsonb(
                                {
                                    "message": f"result for {run_id}",
                                    "usage": {"model_turns": 1, "tool_calls": 0},
                                }
                            ),
                            run_id,
                        ),
                    )

            self.assertEqual(store.reconcile_from_workflows(), len(scopes))
            for run_id, owner, tenant in scopes:
                context = ServerCallContext(user=NamedUser(owner), tenant=tenant)
                task = asyncio.run(store.get("shared-task-id", context))
                self.assertEqual(
                    task.artifacts[0].parts[0].text, f"result for {run_id}"
                )
                self.assertEqual(
                    task.artifacts[0].metadata["provenance"]["run_id"], run_id
                )
        finally:
            database.close()

    def test_workflow_reconciliation_repairs_partial_final_artifact(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        workflows = PostgresWorkflowStore(database)
        message = "complete recovered result"
        digest = "sha256:" + hashlib.sha256(message.encode()).hexdigest()
        try:
            workflows.create(
                WorkflowRecord(
                    "partial-artifact-run",
                    "partial-artifact-task",
                    "partial-artifact-context",
                    "tenant-1",
                    "owner-1",
                    None,
                    "RUNNING",
                    1,
                    {"prompt": "work"},
                    {"turns": 1},
                )
            )
            context = ServerCallContext(user=NamedUser(), tenant="tenant-1")
            partial = Artifact(artifact_id=digest)
            partial.parts.add(text="complete rec", media_type="text/plain")
            store = PostgresTaskStore(database)
            asyncio.run(
                store.save(
                    Task(
                        id="partial-artifact-task",
                        context_id="partial-artifact-context",
                        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                        artifacts=[partial],
                    ),
                    context,
                )
            )
            with database.transaction() as connection:
                connection.execute(
                    """UPDATE core_runs SET state = 'COMPLETED', result = %s,
                              version = version + 1
                       WHERE run_id = 'partial-artifact-run'""",
                    (
                        Jsonb(
                            {
                                "message": message,
                                "usage": {"model_turns": 1, "tool_calls": 0},
                                "complete": True,
                                "completion_reason": "completed",
                            }
                        ),
                    ),
                )

            self.assertEqual(store.reconcile_from_workflows(), 1)
            task = asyncio.run(store.get("partial-artifact-task", context))
            self.assertEqual(len(task.artifacts), 1)
            self.assertEqual(task.artifacts[0].parts[0].text, message)
            self.assertEqual(
                task.artifacts[0].metadata["provenance"]["completion_reason"],
                "completed",
            )
        finally:
            database.close()

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

    def test_artifacts_are_durable_verified_and_tenant_scoped(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        with tempfile.TemporaryDirectory() as root:
            store = PostgresArtifactStore(database, root, max_bytes=1024)
            first = store.put(
                "tenant-1",
                b"durable result",
                media_type="text/plain",
                provenance={"run_id": "run-1"},
            )
            second = store.put(
                "tenant-2",
                b"durable result",
                media_type="text/plain",
                provenance={"run_id": "run-2"},
            )
            database.close()

            reopened = self._database()
            store = PostgresArtifactStore(reopened, root, max_bytes=1024)
            metadata, content = store.get("tenant-1", first.id)
            self.assertEqual(metadata.digest, first.digest)
            self.assertEqual(content, b"durable result")
            with self.assertRaises(CoreError) as caught:
                store.get("tenant-1", second.id)
            self.assertEqual(caught.exception.code, "NOT_FOUND")
            blob = store._blob(first.digest)
            store.delete("tenant-1", first.id)
            self.assertTrue(blob.exists())
            store.delete("tenant-2", second.id)
            self.assertFalse(blob.exists())
            reopened.close()

    def test_coordinated_retention_deletes_run_family_content_and_keeps_tombstone(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        workflows = PostgresWorkflowStore(database)
        root = workflows.create(
            WorkflowRecord(
                "retain-root",
                "retain-task",
                "retain-context",
                "tenant-1",
                "owner-1",
                None,
                "RUNNING",
                1,
                {"prompt": "private"},
                {"context": "private transcript"},
            ),
            audit=(("task.started", {"content": False}),),
        )
        workflows.create(
            WorkflowRecord(
                "retain-child",
                "retain-child-task",
                "retain-context",
                "tenant-1",
                "owner-1",
                root.run_id,
                "RUNNING",
                1,
                {"prompt": "child private"},
                {"context": "child transcript"},
            )
        )
        context = ServerCallContext(user=NamedUser(), tenant="tenant-1")
        asyncio.run(
            PostgresTaskStore(database).save(
                Task(
                    id="retain-task",
                    context_id="retain-context",
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                ),
                context,
            )
        )
        with tempfile.TemporaryDirectory() as durable:
            artifacts = PostgresArtifactStore(database, durable)
            artifact = artifacts.put(
                "tenant-1",
                b"private artifact",
                media_type="text/plain",
                provenance={"run_id": "retain-child"},
            )
            result = PostgresRetentionManager(database, artifacts).delete_run(
                "tenant-1",
                "retain-root",
                operator_principal_id="operator-1",
            )
            self.assertEqual(result, {"deleted": True, "runs": 2, "artifacts": 1})
            self.assertFalse(artifacts._blob(artifact.digest).exists())
            with self.assertRaises(CoreError):
                artifacts.get("tenant-1", artifact.id)
        with database.pool.connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) AS count FROM core_runs WHERE tenant_id = 'tenant-1'"
                ).fetchone()["count"],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) AS count FROM core_checkpoints WHERE tenant_id = 'tenant-1'"
                ).fetchone()["count"],
                0,
            )
            tombstones = connection.execute(
                """SELECT data FROM core_audit_records
                   WHERE tenant_id = 'tenant-1' AND kind = 'retention.deleted'"""
            ).fetchall()
        self.assertEqual(len(tombstones), 2)
        self.assertNotIn("private", repr(tombstones))
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
                {"prompt": "test"},
                {"turns": 0},
            ),
            audit=(("task.started", {"safe": True}),),
            budget_limits=(4, 2),
        )
        self.assertEqual(created.version, 1)
        # Runtime charges each accepted run's finalization turn up front.
        workflows.consume_budget(created, model_turns=1)
        workflows.consume_budget(created, model_turns=1)
        child = workflows.create(
            WorkflowRecord(
                "run-child",
                "task-child",
                "context-1",
                "tenant-1",
                "owner-1",
                created.run_id,
                "RUNNING",
                1,
                {"prompt": "child"},
                {"turns": 0},
            ),
            budget_limits=(100, 100),
        )
        self.assertEqual(
            child.snapshot["budget_root_id"], created.snapshot["budget_root_id"]
        )
        workflows.consume_budget(created, model_turns=1, tool_calls=1)
        workflows.consume_budget(child, model_turns=1, tool_calls=1)
        with self.assertRaises(CoreError) as caught:
            workflows.consume_budget(child, model_turns=1)
        self.assertEqual(caught.exception.code, "BUDGET_EXCEEDED")
        workflows.release_budget(child, model_turns=1)
        workflows.consume_budget(child, model_turns=1)
        completed_child = workflows.transition(
            child.run_id,
            tenant_id=child.tenant_id,
            owner_id=child.owner_id,
            expected_version=child.version,
            state="COMPLETED",
            snapshot=child.snapshot,
            event_kind="task.completed",
            result={"message": "done", "usage": {"model_turns": 1, "tool_calls": 0}},
            release_model_turns=1,
        )
        self.assertEqual(completed_child.state, "COMPLETED")
        workflows.consume_budget(created, model_turns=1)
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
            for task_id, kind, recoverable, cancel_requested in (
                ("read-task", "test-read", True, False),
                ("write-task", "test-read", False, False),
                ("cancel-task", "test-cancel", True, True),
                ("cancel-write-task", "test-read", False, True),
            ):
                connection.execute(
                    """INSERT INTO core_background_tasks
                       (id, owner_run_id, tenant_id, kind, state, required,
                        recoverable, contract, cancel_requested, created_at, updated_at)
                       VALUES (%s, 'run-1', 'tenant-1', %s, 'working',
                               true, %s, %s, %s, %s, %s)""",
                    (
                        task_id,
                        kind,
                        recoverable,
                        Jsonb({"value": task_id}),
                        cancel_requested,
                        now,
                        now,
                    ),
                )
        database.close()

        reopened = self._database()
        try:
            scheduler = PostgresTaskScheduler(reopened)
            cancel_reconciled = []
            scheduler.register(
                "test-read", lambda contract, cancel: {"value": contract["value"]}
            )
            scheduler.register(
                "test-cancel",
                lambda contract, cancel: cancel_reconciled.append(cancel.is_set()),
            )
            self.assertEqual(scheduler.recover(), 1)
            read = scheduler.wait(
                "read-task", owner_id="run-1", tenant_id="tenant-1", timeout=2
            )
            self.assertEqual(read.state, "completed")
            self.assertEqual(read.result, {"value": "read-task"})
            write = scheduler.get("write-task", owner_id="run-1", tenant_id="tenant-1")
            self.assertEqual(write.state, "failed")
            self.assertEqual(write.error.code, "SIDE_EFFECT_UNKNOWN")
            canceled = scheduler.wait(
                "cancel-task",
                owner_id="run-1",
                tenant_id="tenant-1",
                timeout=2,
            )
            self.assertEqual(canceled.state, "canceled")
            self.assertEqual(cancel_reconciled, [True])
            canceled_write = scheduler.get(
                "cancel-write-task", owner_id="run-1", tenant_id="tenant-1"
            )
            self.assertEqual(canceled_write.state, "failed")
            self.assertEqual(canceled_write.error.code, "SIDE_EFFECT_UNKNOWN")
            notifications = scheduler.mailbox("run-1", "tenant-1").poll()
            self.assertEqual(
                {item.task_id for item in notifications},
                {"read-task", "write-task", "cancel-task", "cancel-write-task"},
            )

            published = []
            dispatcher = OutboxDispatcher(
                reopened, lambda event: published.append(event["id"])
            )
            self.assertGreaterEqual(dispatcher.drain_once(), 4)
            self.assertEqual(len(published), len(set(published)))
            scheduler.close()
        finally:
            reopened.close()

    def test_workflow_recovery_skips_run_with_live_lease(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        workflows = PostgresWorkflowStore(database)
        record = workflows.create(
            WorkflowRecord(
                "leased-run",
                "leased-task",
                "leased-context",
                "tenant-1",
                "owner-1",
                None,
                "EXECUTING",
                1,
                {"prompt": "work"},
                {"turns": 1, "pending_call": {"id": "call-1"}},
            )
        )
        workflows.acquire_lease(
            record.run_id,
            tenant_id=record.tenant_id,
            owner_id=record.owner_id,
            worker_id="live-worker",
            ttl=30,
        )
        selected = record.run_id in {item.run_id for item in workflows.recoverable()}
        model = ScriptedModel([])
        model.model = "lease-recovery-model"
        app = None
        try:
            with patch.dict(
                os.environ,
                {
                    "CORE_AGENT_ENVIRONMENT": "development",
                    "SESSION_STORAGE_TYPE": "postgres",
                    "DATABASE_AUTO_MIGRATE": "false",
                    "CORE_AGENT_MEMORY": "disabled",
                    "ARTIFACT_STORAGE_ENABLED": "false",
                },
                clear=True,
            ):
                app = create_app(model=model, database=database)
            persisted = app.state.core_agent.workflow_store.get(
                record.run_id,
                tenant_id=record.tenant_id,
                owner_id=record.owner_id,
            )
            self.assertEqual((selected, persisted.state), (False, "EXECUTING"))
        finally:
            if app is not None:
                app.state.close()
            else:
                database.close()

    def test_cross_worker_cancel_waits_for_the_fenced_lease_owner(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        workflows = PostgresWorkflowStore(database)
        record = workflows.create(
            WorkflowRecord(
                "cancel-fenced-run",
                "cancel-fenced-task",
                "cancel-fenced-context",
                "tenant-1",
                "owner-1",
                None,
                "RUNNING",
                1,
                {"prompt": "work"},
                {"turns": 0},
            )
        )
        lease = workflows.acquire_lease(
            record.run_id,
            tenant_id=record.tenant_id,
            owner_id=record.owner_id,
            worker_id="active-worker",
            ttl=30,
        )
        model = ScriptedModel([])
        model.model = "cancel-fenced-model"
        app = None
        errors = []
        try:
            with patch.dict(
                os.environ,
                {
                    "CORE_AGENT_ENVIRONMENT": "development",
                    "SESSION_STORAGE_TYPE": "postgres",
                    "DATABASE_AUTO_MIGRATE": "false",
                    "CORE_AGENT_MEMORY": "disabled",
                    "ARTIFACT_STORAGE_ENABLED": "false",
                },
                clear=True,
            ):
                app = create_app(model=model, database=database)

            def cancel():
                try:
                    app.state.core_agent.cancel_task(record.task_id)
                except Exception as error:
                    errors.append(error)

            thread = threading.Thread(target=cancel)
            thread.start()
            time.sleep(0.1)
            self.assertTrue(thread.is_alive())
            self.assertEqual(
                workflows.lookup_task(record.task_id).state,
                "RUNNING",
            )
            workflows.release_lease(
                record.run_id,
                tenant_id=record.tenant_id,
                worker_id="active-worker",
                token=lease,
            )
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(
                workflows.lookup_task(record.task_id).state,
                "CANCELLED",
            )
        finally:
            if app is not None:
                app.state.close()
            else:
                database.close()

    def test_cancel_intent_atomically_blocks_a_later_completed_transition(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        workflows = PostgresWorkflowStore(database)
        try:
            record = workflows.create(
                WorkflowRecord(
                    "cancel-gate-run",
                    "cancel-gate-task",
                    "cancel-gate-context",
                    "tenant-1",
                    "owner-1",
                    None,
                    "RUNNING",
                    1,
                    {"prompt": "work"},
                    {"turns": 0},
                )
            )
            record = workflows.request_cancel(
                record.run_id,
                tenant_id=record.tenant_id,
                owner_id=record.owner_id,
            )
            lease = workflows.acquire_lease(
                record.run_id,
                tenant_id=record.tenant_id,
                owner_id=record.owner_id,
                worker_id="completion-worker",
                ttl=30,
            )
            with self.assertRaises(CoreError) as caught:
                workflows.transition(
                    record.run_id,
                    tenant_id=record.tenant_id,
                    owner_id=record.owner_id,
                    expected_version=record.version,
                    state="COMPLETED",
                    snapshot=record.snapshot,
                    event_kind="task.completed",
                    result={
                        "message": "too late",
                        "usage": {"model_turns": 0, "tool_calls": 0},
                    },
                    lease_token=lease,
                )
            self.assertEqual(caught.exception.code, "CANCEL_REQUESTED")
            self.assertEqual(workflows.lookup_task(record.task_id).state, "RUNNING")
        finally:
            database.close()

    def test_workflow_lease_acquire_and_recovery_use_database_clock(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        slow = PostgresWorkflowStore(database, clock=lambda: 0.0)
        fast = PostgresWorkflowStore(database, clock=lambda: 10**12)
        record = slow.create(
            WorkflowRecord(
                "skewed-workflow",
                "skewed-workflow-task",
                "skewed-workflow-context",
                "tenant-1",
                "owner-1",
                None,
                "RUNNING",
                1,
                {"prompt": "work"},
                {"turns": 0},
            )
        )
        try:
            slow.acquire_lease(
                record.run_id,
                tenant_id=record.tenant_id,
                owner_id=record.owner_id,
                worker_id="slow-worker",
                ttl=30,
            )
            self.assertNotIn(
                record.run_id, {candidate.run_id for candidate in fast.recoverable()}
            )
            with self.assertRaises(CoreError) as caught:
                fast.acquire_lease(
                    record.run_id,
                    tenant_id=record.tenant_id,
                    owner_id=record.owner_id,
                    worker_id="fast-worker",
                    ttl=30,
                )
            self.assertEqual(caught.exception.code, "LEASE_LOST")
        finally:
            database.close()

    def test_workflow_lease_renew_and_transition_use_database_clock(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        workflows = PostgresWorkflowStore(database, clock=lambda: 10**12)
        record = workflows.create(
            WorkflowRecord(
                "skewed-transition",
                "skewed-transition-task",
                "skewed-transition-context",
                "tenant-1",
                "owner-1",
                None,
                "RUNNING",
                1,
                {"prompt": "work"},
                {"turns": 0},
            )
        )
        token = "server-timed-token"
        with database.transaction() as connection:
            connection.execute(
                """UPDATE core_runs SET lease_owner = 'worker-1', lease_token = %s,
                          lease_expires_at = EXTRACT(EPOCH FROM clock_timestamp()) + 30
                       WHERE run_id = %s""",
                (token, record.run_id),
            )
        try:
            workflows.renew_lease(
                record.run_id,
                tenant_id=record.tenant_id,
                worker_id="worker-1",
                token=token,
                ttl=30,
            )
            transitioned = workflows.transition(
                record.run_id,
                tenant_id=record.tenant_id,
                owner_id=record.owner_id,
                expected_version=record.version,
                state="WAITING_TASK",
                snapshot={**record.snapshot, "waiting": "task-1"},
                event_kind="task.waiting",
                lease_token=token,
            )
            self.assertEqual(transitioned.state, "WAITING_TASK")
        finally:
            database.close()

    def test_workflow_renew_rechecks_database_clock_after_row_lock_wait(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        workflows = PostgresWorkflowStore(database)
        record = workflows.create(
            WorkflowRecord(
                "blocked-workflow-renew",
                "blocked-workflow-task",
                "blocked-workflow-context",
                "tenant-1",
                "owner-1",
                None,
                "RUNNING",
                1,
                {"prompt": "work"},
                {"turns": 0},
            )
        )
        token = workflows.acquire_lease(
            record.run_id,
            tenant_id=record.tenant_id,
            owner_id=record.owner_id,
            worker_id="worker-1",
            ttl=0.2,
        )
        outcome = []

        def renew():
            try:
                workflows.renew_lease(
                    record.run_id,
                    tenant_id=record.tenant_id,
                    worker_id="worker-1",
                    token=token,
                    ttl=30,
                )
            except CoreError as error:
                outcome.append(error.code)
            else:
                outcome.append("renewed")

        try:
            with database.pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        "SELECT 1 FROM core_runs WHERE run_id = %s FOR UPDATE",
                        (record.run_id,),
                    ).fetchone()
                    worker = threading.Thread(target=renew)
                    worker.start()
                    self._wait_for_blocked_query(
                        database, "SELECT lease_owner, lease_token, lease_expires_at"
                    )
                    connection.execute(
                        """SELECT pg_sleep(GREATEST(
                             lease_expires_at
                             - EXTRACT(EPOCH FROM clock_timestamp()) + 0.05, 0))
                           FROM core_runs WHERE run_id = %s""",
                        (record.run_id,),
                    ).fetchone()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(outcome, ["LEASE_LOST"])
        finally:
            database.close()

    def test_workflow_transition_rechecks_lease_after_budget_lock_wait(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        workflows = PostgresWorkflowStore(database)
        record = workflows.create(
            WorkflowRecord(
                "blocked-transition",
                "blocked-transition-task",
                "blocked-transition-context",
                "tenant-1",
                "owner-1",
                None,
                "RUNNING",
                1,
                {"prompt": "work"},
                {"turns": 0, "finalization_turn_reserved": True},
            ),
            budget_limits=(3, 1),
            reserve_model_turns=1,
        )
        token = workflows.acquire_lease(
            record.run_id,
            tenant_id=record.tenant_id,
            owner_id=record.owner_id,
            worker_id="worker-1",
            ttl=0.3,
        )
        outcome = []

        def transition():
            try:
                workflows.transition(
                    record.run_id,
                    tenant_id=record.tenant_id,
                    owner_id=record.owner_id,
                    expected_version=record.version,
                    state="RUNNING",
                    snapshot={**record.snapshot, "turns": 1},
                    event_kind="model.attempt.started",
                    lease_token=token,
                    consume_model_turns=1,
                )
            except CoreError as error:
                outcome.append(error.code)
            else:
                outcome.append("transitioned")

        try:
            with database.pool.connection() as connection:
                with connection.transaction():
                    expiry = connection.execute(
                        "SELECT lease_expires_at FROM core_runs WHERE run_id = %s",
                        (record.run_id,),
                    ).fetchone()["lease_expires_at"]
                    connection.execute(
                        """SELECT 1 FROM core_budget_ledgers
                           WHERE root_run_id = %s FOR UPDATE""",
                        (record.run_id,),
                    ).fetchone()
                    worker = threading.Thread(target=transition)
                    worker.start()
                    self._wait_for_blocked_query(
                        database, "SELECT 1 FROM core_budget_ledgers WHERE root_run_id"
                    )
                    connection.execute(
                        """SELECT pg_sleep(GREATEST(
                             %s - EXTRACT(EPOCH FROM clock_timestamp()) + 0.05, 0))""",
                        (expiry,),
                    ).fetchone()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(outcome, ["LEASE_LOST"])

            persisted = workflows.get(
                record.run_id,
                tenant_id=record.tenant_id,
                owner_id=record.owner_id,
            )
            with database.pool.connection() as connection:
                effects = connection.execute(
                    """SELECT
                         (SELECT used_model_turns FROM core_budget_ledgers
                          WHERE root_run_id = %s) AS used_model_turns,
                         (SELECT count(*) FROM core_events
                          WHERE run_id = %s AND kind = 'model.attempt.started')
                           AS events,
                         (SELECT count(*) FROM core_outbox
                          WHERE aggregate_id = %s
                            AND event_type = 'model.attempt.started') AS outbox""",
                    (record.run_id, record.run_id, record.run_id),
                ).fetchone()
            self.assertEqual(persisted.version, record.version)
            self.assertEqual(persisted.state, record.state)
            self.assertEqual(persisted.snapshot, record.snapshot)
            self.assertEqual(
                dict(effects), {"used_model_turns": 1, "events": 0, "outbox": 0}
            )
        finally:
            database.close()

    def test_postgres_scheduler_preserves_id_and_cancel_wins_over_worker_error(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        scheduler = PostgresTaskScheduler(database)
        started = threading.Event()
        cleaned = []

        def work(cancel_event):
            started.set()
            self.assertTrue(cancel_event.wait(2))
            raise RuntimeError("raised after cancellation")

        try:
            task = scheduler.start(
                work,
                owner_id="parent-run",
                accepts_cancel_event=True,
                kind="cancel-test",
                contract={},
                task_id="stable-child-id",
                tenant_id="tenant-1",
                on_cancel=lambda: cleaned.append("stable-child-id"),
            )
            self.assertEqual(task.id, "stable-child-id")
            self.assertTrue(started.wait(1))
            scheduler.cancel(task.id, owner_id="parent-run", tenant_id="tenant-1")
            terminal = scheduler.wait(
                task.id,
                owner_id="parent-run",
                tenant_id="tenant-1",
                timeout=2,
            )
            self.assertEqual(terminal.state, "canceled")
            self.assertIsNone(terminal.error)
            self.assertEqual(cleaned, ["stable-child-id"])
        finally:
            scheduler.close()
            database.close()

    def test_postgres_recovery_claim_is_renewed_and_exclusive(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        now = time.time()
        with database.transaction() as connection:
            connection.execute(
                """INSERT INTO core_background_tasks
                   (id, owner_run_id, tenant_id, kind, state, required,
                    recoverable, contract, created_at, updated_at)
                   VALUES ('claimed-task', 'parent-run', 'tenant-1', 'claimed',
                           'working', true, true, '{}'::jsonb, %s, %s)""",
                (now, now),
            )
        first = PostgresTaskScheduler(
            database, task_lease_ttl=0.2, task_lease_heartbeat_interval=0.05
        )
        second = PostgresTaskScheduler(
            database, task_lease_ttl=0.2, task_lease_heartbeat_interval=0.05
        )
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def recover(_contract, _cancel_event):
            calls.append("called")
            entered.set()
            release.wait(2)
            return "done"

        first.register("claimed", recover)
        second.register("claimed", recover)
        try:
            self.assertEqual(first.recover(), 1)
            self.assertTrue(entered.wait(1))
            time.sleep(0.35)
            self.assertEqual(second.recover(), 0)
            self.assertEqual(calls, ["called"])
            release.set()
            terminal = first.wait(
                "claimed-task",
                owner_id="parent-run",
                tenant_id="tenant-1",
                timeout=2,
            )
            self.assertEqual(terminal.state, "completed")
        finally:
            release.set()
            first.close()
            second.close()
            database.close()

    def test_scheduler_claim_and_recovery_use_database_clock(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        now = time.time()
        with database.transaction() as connection:
            for task_id in ("skewed-claim", "skewed-recovery"):
                connection.execute(
                    """INSERT INTO core_background_tasks
                       (id, owner_run_id, tenant_id, kind, state, required,
                        recoverable, contract, mutating, created_at, updated_at)
                       VALUES (%s, 'parent-run', 'tenant-1', 'skewed-clock',
                               'submitted', true, true, '{}'::jsonb, false, %s, %s)""",
                    (task_id, now, now),
                )
        slow = PostgresTaskScheduler(
            database,
            clock=lambda: 0.0,
            task_lease_ttl=30,
            task_lease_heartbeat_interval=10,
        )
        fast = PostgresTaskScheduler(
            database,
            clock=lambda: 10**12,
            task_lease_ttl=30,
            task_lease_heartbeat_interval=10,
        )
        calls = []
        fast.register("skewed-clock", lambda _contract, _cancel: calls.append("run"))
        try:
            token, claimed = slow._claim(
                "skewed-claim", "tenant-1", allow_working=False
            )
            self.assertIsNotNone(token)
            self.assertIsNotNone(claimed)
            stolen_token, stolen = fast._claim(
                "skewed-claim", "tenant-1", allow_working=True
            )
            self.assertIsNone(stolen_token)
            self.assertIsNone(stolen)

            slow._claim("skewed-recovery", "tenant-1", allow_working=False)
            self.assertEqual(fast.recover(), 0)
            self.assertEqual(calls, [])
        finally:
            slow.close()
            fast.close()
            database.close()

    def test_scheduler_renew_and_finish_use_database_clock(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        scheduler = PostgresTaskScheduler(
            database,
            clock=lambda: 10**12,
            task_lease_ttl=30,
            task_lease_heartbeat_interval=10,
        )
        now = time.time()
        with database.transaction() as connection:
            for task_id in ("skewed-renew", "skewed-finish"):
                connection.execute(
                    """INSERT INTO core_background_tasks
                       (id, owner_run_id, tenant_id, kind, state, required,
                        recoverable, contract, mutating, claim_owner, claim_token,
                        claim_expires_at, created_at, updated_at)
                       VALUES (%s, 'parent-run', 'tenant-1', 'skewed-clock',
                               'working', true, true, '{}'::jsonb, false, %s, %s,
                               EXTRACT(EPOCH FROM clock_timestamp()) + 30, %s, %s)""",
                    (task_id, scheduler._worker_id, f"{task_id}-token", now, now),
                )
        try:
            scheduler._renew_claim("skewed-renew", "tenant-1", "skewed-renew-token")
            self.assertTrue(
                scheduler._finish(
                    "skewed-finish",
                    "tenant-1",
                    "skewed-finish-token",
                    "completed",
                    "done",
                    None,
                )
            )
        finally:
            scheduler.close()
            database.close()

    def test_scheduler_renew_rechecks_database_clock_after_row_lock_wait(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        scheduler = PostgresTaskScheduler(
            database, task_lease_ttl=0.2, task_lease_heartbeat_interval=0.05
        )
        now = time.time()
        with database.transaction() as connection:
            connection.execute(
                """INSERT INTO core_background_tasks
                   (id, owner_run_id, tenant_id, kind, state, required,
                    recoverable, contract, mutating, created_at, updated_at)
                   VALUES ('blocked-renew', 'parent-run', 'tenant-1', 'blocked',
                           'submitted', true, true, '{}'::jsonb, false, %s, %s)""",
                (now, now),
            )
        token, _ = scheduler._claim("blocked-renew", "tenant-1", allow_working=False)
        outcome = []

        def renew():
            try:
                scheduler._renew_claim("blocked-renew", "tenant-1", token)
            except CoreError as error:
                outcome.append(error.code)
            else:
                outcome.append("renewed")

        try:
            with database.pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        """SELECT 1 FROM core_background_tasks
                           WHERE id = 'blocked-renew' FOR UPDATE"""
                    ).fetchone()
                    worker = threading.Thread(target=renew)
                    worker.start()
                    self._wait_for_blocked_query(
                        database,
                        "SELECT state, claim_owner, claim_token",
                    )
                    connection.execute(
                        """SELECT pg_sleep(GREATEST(
                             claim_expires_at
                             - EXTRACT(EPOCH FROM clock_timestamp()) + 0.05, 0))
                           FROM core_background_tasks WHERE id = 'blocked-renew'"""
                    ).fetchone()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(outcome, ["LEASE_LOST"])
        finally:
            scheduler.close()
            database.close()

    def test_scheduler_finish_rechecks_database_clock_after_row_lock_wait(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        scheduler = PostgresTaskScheduler(
            database, task_lease_ttl=0.2, task_lease_heartbeat_interval=0.05
        )
        now = time.time()
        with database.transaction() as connection:
            connection.execute(
                """INSERT INTO core_background_tasks
                   (id, owner_run_id, tenant_id, kind, state, required,
                    recoverable, contract, mutating, created_at, updated_at)
                   VALUES ('blocked-finish', 'parent-run', 'tenant-1', 'blocked',
                           'submitted', true, true, '{}'::jsonb, false, %s, %s)""",
                (now, now),
            )
        token, _ = scheduler._claim("blocked-finish", "tenant-1", allow_working=False)
        outcome = []

        def finish():
            outcome.append(
                scheduler._finish(
                    "blocked-finish",
                    "tenant-1",
                    token,
                    "completed",
                    "stale",
                    None,
                )
            )

        try:
            with database.pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        """SELECT 1 FROM core_background_tasks
                           WHERE id = 'blocked-finish' FOR UPDATE"""
                    ).fetchone()
                    worker = threading.Thread(target=finish)
                    worker.start()
                    self._wait_for_blocked_query(
                        database, "SELECT * FROM core_background_tasks"
                    )
                    connection.execute(
                        """SELECT pg_sleep(GREATEST(
                             claim_expires_at
                             - EXTRACT(EPOCH FROM clock_timestamp()) + 0.05, 0))
                           FROM core_background_tasks WHERE id = 'blocked-finish'"""
                    ).fetchone()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(outcome, [False])
        finally:
            scheduler.close()
            database.close()

    def test_cancel_between_recovery_read_and_claim_runs_reconciliation(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        now = time.time()
        with database.transaction() as connection:
            connection.execute(
                """INSERT INTO core_background_tasks
                   (id, owner_run_id, tenant_id, kind, state, required,
                    recoverable, contract, mutating, claim_expires_at,
                    created_at, updated_at)
                   VALUES ('cancel-race', 'parent-run', 'tenant-1', 'reconcile',
                           'working', true, true, '{}'::jsonb, false, %s, %s, %s)""",
                (now - 1, now, now),
            )
        canceler = PostgresTaskScheduler(database)

        class CancelBeforeClaimScheduler(PostgresTaskScheduler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.cancel_injected = False

            def _claim(self, task_id, tenant_id, *, allow_working):
                if task_id == "cancel-race" and not self.cancel_injected:
                    self.cancel_injected = True
                    canceler.cancel(task_id, owner_id="parent-run", tenant_id=tenant_id)
                return super()._claim(task_id, tenant_id, allow_working=allow_working)

        scheduler = CancelBeforeClaimScheduler(database)
        reconciled = []

        def reconcile(_contract, cancel_event):
            reconciled.append(cancel_event.is_set())
            raise CoreError("SIDE_EFFECT_UNKNOWN")

        scheduler.register("reconcile", reconcile)
        try:
            scheduler.recover()
            terminal = scheduler.wait(
                "cancel-race",
                owner_id="parent-run",
                tenant_id="tenant-1",
                timeout=2,
            )
            self.assertEqual(reconciled, [True])
            self.assertEqual(terminal.state, "failed")
            self.assertEqual(terminal.error.code, "SIDE_EFFECT_UNKNOWN")
        finally:
            canceler.close()
            scheduler.close()
            database.close()

    def test_cancel_after_recovery_claim_does_not_mask_reconciliation_failure(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        now = time.time()
        with database.transaction() as connection:
            connection.execute(
                """INSERT INTO core_background_tasks
                   (id, owner_run_id, tenant_id, kind, state, required,
                    recoverable, contract, mutating, claim_expires_at,
                    created_at, updated_at)
                   VALUES ('post-claim-cancel', 'parent-run', 'tenant-1',
                           'post-claim-reconcile', 'working', true, true,
                           '{}'::jsonb, false, %s, %s, %s)""",
                (now - 1, now, now),
            )
        scheduler = PostgresTaskScheduler(
            database, task_lease_ttl=1, task_lease_heartbeat_interval=0.02
        )
        canceler = PostgresTaskScheduler(database)
        entered = threading.Event()

        def reconcile(_contract, cancel_event):
            entered.set()
            self.assertTrue(cancel_event.wait(2))
            raise RuntimeError("reconciliation failed")

        scheduler.register("post-claim-reconcile", reconcile)
        try:
            self.assertEqual(scheduler.recover(), 1)
            self.assertTrue(entered.wait(1))
            canceler.cancel(
                "post-claim-cancel",
                owner_id="parent-run",
                tenant_id="tenant-1",
            )
            terminal = scheduler.wait(
                "post-claim-cancel",
                owner_id="parent-run",
                tenant_id="tenant-1",
                timeout=2,
            )
            self.assertEqual(terminal.state, "failed")
            self.assertEqual(terminal.error.code, "RECOVERY_REQUIRES_RECONCILIATION")
        finally:
            scheduler.close()
            canceler.close()
            database.close()

    def test_expired_scheduler_claim_must_be_reclaimed_before_renew_or_finish(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        first = PostgresTaskScheduler(
            database,
            task_lease_ttl=10,
            task_lease_heartbeat_interval=5,
        )
        second = PostgresTaskScheduler(
            database,
            task_lease_ttl=10,
            task_lease_heartbeat_interval=5,
        )
        now = time.time()
        with database.transaction() as connection:
            for task_id in ("expired-renew", "expired-finish"):
                connection.execute(
                    """INSERT INTO core_background_tasks
                       (id, owner_run_id, tenant_id, kind, state, required,
                        recoverable, contract, created_at, updated_at)
                       VALUES (%s, 'parent-run', 'tenant-1', 'lease-fence',
                               'submitted', true, true, '{}'::jsonb, %s, %s)""",
                    (task_id, now, now),
                )
        renew_token, _ = first._claim("expired-renew", "tenant-1", allow_working=False)
        finish_token, _ = first._claim(
            "expired-finish", "tenant-1", allow_working=False
        )
        with database.transaction() as connection:
            connection.execute(
                """UPDATE core_background_tasks SET claim_expires_at =
                           EXTRACT(EPOCH FROM statement_timestamp()) - 1
                   WHERE id IN ('expired-renew', 'expired-finish')"""
            )

        renew_error = None
        try:
            first._renew_claim("expired-renew", "tenant-1", renew_token)
        except CoreError as error:
            renew_error = error.code
        stale_finish = first._finish(
            "expired-finish",
            "tenant-1",
            finish_token,
            "completed",
            "stale result",
            None,
        )

        reclaimed = []
        finished = []
        for task_id in ("expired-renew", "expired-finish"):
            token, row = second._claim(task_id, "tenant-1", allow_working=True)
            reclaimed.append(row is not None)
            finished.append(
                row is not None
                and second._finish(
                    task_id,
                    "tenant-1",
                    token,
                    "completed",
                    "reclaimed result",
                    None,
                )
            )
        states = [
            second.get(task_id, owner_id="parent-run", tenant_id="tenant-1").state
            for task_id in ("expired-renew", "expired-finish")
        ]
        try:
            self.assertEqual(
                {
                    "renew_error": renew_error,
                    "stale_finish": stale_finish,
                    "reclaimed": reclaimed,
                    "finished": finished,
                    "states": states,
                },
                {
                    "renew_error": "LEASE_LOST",
                    "stale_finish": False,
                    "reclaimed": [True, True],
                    "finished": [True, True],
                    "states": ["completed", "completed"],
                },
            )
        finally:
            first.close()
            second.close()
            database.close()

    def test_distributed_cancel_preserves_late_result_and_notification(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        first = PostgresTaskScheduler(
            database, task_lease_ttl=0.2, task_lease_heartbeat_interval=0.02
        )
        second = PostgresTaskScheduler(
            database, task_lease_ttl=0.2, task_lease_heartbeat_interval=0.02
        )
        started = threading.Event()
        cancel_seen = threading.Event()
        release = threading.Event()
        cleaned = []

        def work(cancel_event):
            started.set()
            cancel_event.wait(2)
            cancel_seen.set()
            release.wait(2)
            return "late durable result"

        try:
            task = first.start(
                work,
                owner_id="parent-run",
                kind="distributed-cancel",
                contract={},
                accepts_cancel_event=True,
                tenant_id="tenant-1",
                on_cancel=lambda: cleaned.append("destroyed"),
                mutating=False,
            )
            self.assertTrue(started.wait(1))
            second.cancel(task.id, owner_id="parent-run", tenant_id="tenant-1")
            self.assertTrue(cancel_seen.wait(1))
            observer = PostgresTaskScheduler(database)
            self.assertEqual(
                observer.get(
                    task.id, owner_id="parent-run", tenant_id="tenant-1"
                ).state,
                "working",
            )
            release.set()
            terminal = first.wait(
                task.id,
                owner_id="parent-run",
                tenant_id="tenant-1",
                timeout=2,
            )
            self.assertEqual(terminal.state, "canceled")
            self.assertEqual(terminal.result, "late durable result")
            self.assertEqual(cleaned, ["destroyed"])
            notice = observer.mailbox("parent-run", "tenant-1").poll()[-1]
            self.assertEqual(notice.payload["result"], "late durable result")
            observer.close()
        finally:
            release.set()
            first.close()
            second.close()
            database.close()

    def test_committed_task_is_recovered_when_launch_fails_before_worker_start(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        workflows = PostgresWorkflowStore(database)
        scheduler = PostgresTaskScheduler(database)
        root = workflows.create(
            WorkflowRecord(
                "launch-root",
                "launch-root-task",
                "launch-context",
                "tenant-1",
                "owner-1",
                None,
                "RUNNING",
                1,
                {"prompt": "root"},
                {"turns": 0, "finalization_turn_reserved": True},
            ),
            budget_limits=(3, 1),
            reserve_model_turns=1,
        )
        child = WorkflowRecord(
            "launch-child",
            "launch-child-task",
            "launch-context",
            "tenant-1",
            "owner-1",
            root.run_id,
            "RUNNING",
            1,
            {"prompt": "child"},
            {"turns": 0, "finalization_turn_reserved": True},
        )

        def admit(connection):
            workflows.create(
                child,
                reserve_model_turns=1,
                connection=connection,
            )

        try:
            with patch.object(
                scheduler, "_launch", side_effect=RuntimeError("thread unavailable")
            ):
                with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
                    scheduler.start(
                        lambda: "unused",
                        owner_id=root.run_id,
                        task_id=child.task_id,
                        kind="recover-child",
                        contract={},
                        recoverable=True,
                        tenant_id="tenant-1",
                        admission=admit,
                    )
            with database.pool.connection() as connection:
                counts = connection.execute(
                    """SELECT
                         (SELECT count(*) FROM core_background_tasks
                          WHERE id = 'launch-child-task') AS tasks,
                         (SELECT count(*) FROM core_runs
                          WHERE run_id = 'launch-child') AS workflows,
                         (SELECT used_model_turns FROM core_budget_ledgers
                          WHERE root_run_id = 'launch-root') AS used"""
                ).fetchone()
            self.assertEqual(dict(counts), {"tasks": 1, "workflows": 1, "used": 2})

            scheduler.register("recover-child", lambda _contract, _cancel: "recovered")
            self.assertEqual(scheduler.recover(), 1)
            terminal = scheduler.wait(
                child.task_id,
                owner_id=root.run_id,
                tenant_id="tenant-1",
                timeout=2,
            )
            self.assertEqual(terminal.result, "recovered")
            with database.pool.connection() as connection:
                used = connection.execute(
                    """SELECT used_model_turns FROM core_budget_ledgers
                       WHERE root_run_id = 'launch-root'"""
                ).fetchone()["used_model_turns"]
            self.assertEqual(used, 2)
        finally:
            scheduler.close()
            database.close()

    def test_shared_budget_snapshot_locks_ledger_until_terminal_transition(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        workflows = PostgresWorkflowStore(database)
        record = workflows.create(
            WorkflowRecord(
                "snapshot-run",
                "snapshot-task",
                "snapshot-context",
                "tenant-1",
                "owner-1",
                None,
                "RUNNING",
                1,
                {"prompt": "work"},
                {"turns": 1, "finalization_turn_reserved": False},
            ),
            budget_limits=(2, 1),
            reserve_model_turns=1,
        )
        finished = threading.Event()
        outcome = []

        def complete():
            try:
                outcome.append(
                    workflows.transition(
                        record.run_id,
                        tenant_id=record.tenant_id,
                        owner_id=record.owner_id,
                        expected_version=record.version,
                        state="COMPLETED",
                        snapshot=record.snapshot,
                        event_kind="task.completed",
                        result={
                            "message": "done",
                            "usage": {"model_turns": 1, "tool_calls": 0},
                        },
                        include_shared_budget=True,
                    )
                )
            finally:
                finished.set()

        try:
            with database.pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        """SELECT 1 FROM core_budget_ledgers
                           WHERE root_run_id = %s FOR UPDATE""",
                        (record.run_id,),
                    ).fetchone()
                    worker = threading.Thread(target=complete)
                    worker.start()
                    self.assertFalse(finished.wait(0.15))
            self.assertTrue(finished.wait(2))
            worker.join(1)
            self.assertEqual(
                outcome[0].result["shared_budget"]["used"]["model_turns"], 1
            )
        finally:
            database.close()

    def test_model_attempt_charge_and_checkpoint_commit_together(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        workflows = PostgresWorkflowStore(database)
        record = workflows.create(
            WorkflowRecord(
                "attempt-run",
                "attempt-task",
                "attempt-context",
                "tenant-1",
                "owner-1",
                None,
                "RUNNING",
                1,
                {"prompt": "work"},
                {"turns": 0, "finalization_turn_reserved": True},
            ),
            budget_limits=(2, 1),
            reserve_model_turns=1,
        )
        token = workflows.acquire_lease(
            record.run_id,
            tenant_id=record.tenant_id,
            owner_id=record.owner_id,
            worker_id="attempt-worker",
            ttl=30,
        )
        attempt_snapshot = {
            **record.snapshot,
            "turns": 1,
            "model_attempt_in_flight": {"turn": 1, "finalizing": False},
        }
        try:
            started = workflows.transition(
                record.run_id,
                tenant_id=record.tenant_id,
                owner_id=record.owner_id,
                expected_version=record.version,
                state="RUNNING",
                snapshot=attempt_snapshot,
                event_kind="model.attempt.started",
                lease_token=token,
                consume_model_turns=1,
            )
            with database.pool.connection() as connection:
                ledger = connection.execute(
                    """SELECT used_model_turns FROM core_budget_ledgers
                       WHERE root_run_id = %s""",
                    (record.run_id,),
                ).fetchone()
            self.assertEqual(started.snapshot["turns"], 1)
            self.assertEqual(ledger["used_model_turns"], 2)

            rejected_snapshot = {
                **started.snapshot,
                "turns": 2,
                "model_attempt_in_flight": {"turn": 2, "finalizing": False},
            }
            with self.assertRaises(CoreError) as caught:
                workflows.transition(
                    started.run_id,
                    tenant_id=started.tenant_id,
                    owner_id=started.owner_id,
                    expected_version=started.version,
                    state="RUNNING",
                    snapshot=rejected_snapshot,
                    event_kind="model.attempt.started",
                    lease_token=token,
                    consume_model_turns=1,
                )
            self.assertEqual(caught.exception.code, "BUDGET_EXCEEDED")
            persisted = workflows.get(
                record.run_id,
                tenant_id=record.tenant_id,
                owner_id=record.owner_id,
            )
            self.assertEqual(persisted.version, started.version)
            self.assertEqual(persisted.snapshot["turns"], 1)
            with database.pool.connection() as connection:
                used = connection.execute(
                    """SELECT used_model_turns FROM core_budget_ledgers
                       WHERE root_run_id = %s""",
                    (record.run_id,),
                ).fetchone()["used_model_turns"]
            self.assertEqual(used, 2)
        finally:
            database.close()

    def test_tool_call_charge_and_pending_marker_commit_together(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        workflows = PostgresWorkflowStore(database)
        record = workflows.create(
            WorkflowRecord(
                "tool-charge-run",
                "tool-charge-task",
                "tool-charge-context",
                "tenant-1",
                "owner-1",
                None,
                "MODEL_RESPONDED",
                1,
                {"prompt": "work"},
                {"turns": 1, "tool_calls": 0, "pending_call": None},
            ),
            budget_limits=(2, 2),
        )
        token = workflows.acquire_lease(
            record.run_id,
            tenant_id=record.tenant_id,
            owner_id=record.owner_id,
            worker_id="tool-worker",
            ttl=30,
        )
        first_snapshot = {
            **record.snapshot,
            "tool_calls": 1,
            "pending_call": {"id": "call-1", "name": "read"},
        }
        try:
            started = workflows.transition(
                record.run_id,
                tenant_id=record.tenant_id,
                owner_id=record.owner_id,
                expected_version=record.version,
                state="EXECUTING",
                snapshot=first_snapshot,
                event_kind="tool.dispatch.started",
                lease_token=token,
                consume_tool_calls=1,
            )
            with database.pool.connection() as connection:
                used = connection.execute(
                    """SELECT used_tool_calls FROM core_budget_ledgers
                       WHERE root_run_id = %s""",
                    (record.run_id,),
                ).fetchone()["used_tool_calls"]
            self.assertEqual(used, 1)
            self.assertEqual(started.snapshot["pending_call"]["id"], "call-1")

            second_snapshot = {
                **started.snapshot,
                "tool_calls": 2,
                "pending_call": {"id": "call-2", "name": "read"},
            }
            with patch.object(
                workflows, "_event", side_effect=RuntimeError("injected failure")
            ):
                with self.assertRaisesRegex(RuntimeError, "injected failure"):
                    workflows.transition(
                        started.run_id,
                        tenant_id=started.tenant_id,
                        owner_id=started.owner_id,
                        expected_version=started.version,
                        state="EXECUTING",
                        snapshot=second_snapshot,
                        event_kind="tool.dispatch.started",
                        lease_token=token,
                        consume_tool_calls=1,
                    )
            persisted = workflows.get(
                record.run_id,
                tenant_id=record.tenant_id,
                owner_id=record.owner_id,
            )
            with database.pool.connection() as connection:
                used_after_rollback = connection.execute(
                    """SELECT used_tool_calls FROM core_budget_ledgers
                       WHERE root_run_id = %s""",
                    (record.run_id,),
                ).fetchone()["used_tool_calls"]
            self.assertEqual(persisted.version, started.version)
            self.assertEqual(persisted.snapshot["pending_call"]["id"], "call-1")
            self.assertEqual(used_after_rollback, 1)
        finally:
            database.close()

    def test_postgres_child_admission_rolls_back_scheduler_workflow_and_reserve(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        workflows = PostgresWorkflowStore(database)
        scheduler = PostgresTaskScheduler(database)
        root = workflows.create(
            WorkflowRecord(
                "admission-root",
                "admission-root-task",
                "admission-context",
                "tenant-1",
                "owner-1",
                None,
                "RUNNING",
                1,
                {"prompt": "root"},
                {"turns": 0, "finalization_turn_reserved": True},
            ),
            budget_limits=(2, 2),
            reserve_model_turns=1,
        )
        child = WorkflowRecord(
            "admission-child",
            "admission-child-task",
            "admission-context",
            "tenant-1",
            "owner-1",
            root.run_id,
            "RUNNING",
            1,
            {"prompt": "child"},
            {"turns": 0, "finalization_turn_reserved": True},
        )

        def reject(connection):
            workflows.create(
                child,
                reserve_model_turns=1,
                connection=connection,
            )
            raise CoreError("TOOL_START_FAILED", "reject admission")

        try:
            with self.assertRaises(CoreError) as caught:
                scheduler.start(
                    lambda: None,
                    owner_id=root.run_id,
                    task_id=child.task_id,
                    kind="subagent",
                    contract={"scope": {"task_id": child.task_id}},
                    recoverable=True,
                    tenant_id="tenant-1",
                    admission=reject,
                )
            self.assertEqual(caught.exception.code, "TOOL_START_FAILED")
            with database.pool.connection() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT count(*) AS count FROM core_background_tasks "
                        "WHERE id = %s",
                        (child.task_id,),
                    ).fetchone()["count"],
                    0,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT count(*) AS count FROM core_runs WHERE run_id = %s",
                        (child.run_id,),
                    ).fetchone()["count"],
                    0,
                )
                ledger = connection.execute(
                    "SELECT used_model_turns FROM core_budget_ledgers "
                    "WHERE root_run_id = %s",
                    (root.run_id,),
                ).fetchone()
            self.assertEqual(ledger["used_model_turns"], 1)
        finally:
            scheduler.close()
            database.close()

    def test_inbound_inbox_is_durable_idempotent_and_gates_completion(self):
        database = self._database()
        database.migrate()
        self._reset(database)
        workflows = PostgresWorkflowStore(database)
        record = workflows.create(
            WorkflowRecord(
                "inbound-run",
                "inbound-task",
                "inbound-context",
                "tenant-1",
                "owner-1",
                None,
                "RUNNING",
                1,
                {"prompt": "initial"},
                {"turns": 1},
            )
        )
        first, accepted = workflows.append_inbound(
            record.task_id,
            tenant_id=record.tenant_id,
            owner_id=record.owner_id,
            message_id="message-1",
            context_id=record.context_id,
            content="correction",
            provenance={"source": "a2a"},
        )
        duplicate, accepted_again = workflows.append_inbound(
            record.task_id,
            tenant_id=record.tenant_id,
            owner_id=record.owner_id,
            message_id="message-1",
            context_id=record.context_id,
            content="correction",
            provenance={"source": "a2a"},
        )
        self.assertTrue(accepted)
        self.assertFalse(accepted_again)
        self.assertEqual(first["sequence"], duplicate["sequence"])
        database.close()

        database = self._database()
        workflows = PostgresWorkflowStore(database)
        record = workflows.lookup_task("inbound-task")
        self.assertEqual(
            [item["content"] for item in workflows.pending_inbound(record)],
            ["correction"],
        )
        token = workflows.acquire_lease(
            record.run_id,
            tenant_id=record.tenant_id,
            owner_id=record.owner_id,
            worker_id="worker-1",
            ttl=30,
        )
        with self.assertRaises(CoreError) as pending:
            workflows.transition(
                record.run_id,
                tenant_id=record.tenant_id,
                owner_id=record.owner_id,
                expected_version=record.version,
                state="COMPLETED",
                snapshot=record.snapshot,
                event_kind="task.completed",
                lease_token=token,
            )
        self.assertEqual(pending.exception.code, "INBOUND_MESSAGE_PENDING")
        delivered = workflows.consume_inbound(
            record,
            expected_version=record.version,
            snapshot={**record.snapshot, "delivered": True},
            sequences=(first["sequence"],),
            lease_token=token,
        )
        self.assertEqual(workflows.pending_inbound(delivered), ())
        completed = workflows.transition(
            delivered.run_id,
            tenant_id=delivered.tenant_id,
            owner_id=delivered.owner_id,
            expected_version=delivered.version,
            state="COMPLETED",
            snapshot=delivered.snapshot,
            event_kind="task.completed",
            lease_token=token,
        )
        self.assertEqual(completed.state, "COMPLETED")
        database.close()

        reopened = self._database()
        try:
            persisted = PostgresWorkflowStore(reopened).lookup_task("inbound-task")
            self.assertEqual(persisted.state, "COMPLETED")
            with reopened.pool.connection() as connection:
                rows = connection.execute(
                    """SELECT message_id, sequence, consumed_at
                       FROM core_inbound_messages WHERE run_id = 'inbound-run'"""
                ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["message_id"], "message-1")
            self.assertIsNotNone(rows[0]["consumed_at"])
        finally:
            reopened.close()
