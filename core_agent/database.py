from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import time
from contextlib import contextmanager

from a2a.server.owner_resolver import resolve_user_scope
from a2a.server.tasks import TaskStore
from a2a.types import a2a_pb2
from a2a.utils.constants import DEFAULT_LIST_TASKS_PAGE_SIZE
from a2a.utils.errors import InvalidParamsError
from a2a.utils.task import decode_page_token, encode_page_token
from psycopg.rows import dict_row
from psycopg.sql import SQL, Identifier
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .audit import AuditRecord
from .durability import Event
from .errors import CoreError


SCHEMA_VERSION = 10
MIGRATIONS = {
    1: """
CREATE TABLE IF NOT EXISTS core_schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE core_tool_proposals (
    id text PRIMARY KEY,
    task_id text NOT NULL,
    context_id text NOT NULL,
    tenant_id text NOT NULL,
    caller_principal_id text NOT NULL,
    tool_call_id text NOT NULL,
    tool_name text NOT NULL,
    tool_version text NOT NULL,
    environment text NOT NULL,
    target_json text NOT NULL,
    arguments_json text NOT NULL,
    side_effect_class text NOT NULL,
    risk_level text NOT NULL,
    policy_version text NOT NULL,
    created_at double precision NOT NULL,
    action_digest text NOT NULL
);

CREATE TABLE core_approval_requests (
    id text PRIMARY KEY,
    task_id text NOT NULL,
    proposal_id text NOT NULL UNIQUE REFERENCES core_tool_proposals(id),
    action_digest text NOT NULL,
    state text NOT NULL,
    version integer NOT NULL,
    created_at double precision NOT NULL,
    expires_at double precision,
    required_operator_role text NOT NULL,
    policy_version text NOT NULL,
    decision text,
    operator_principal_id text,
    operator_session_id text
);

CREATE INDEX core_approval_pending_idx
    ON core_approval_requests (created_at) WHERE state = 'PENDING';

CREATE TABLE core_execution_records (
    id text PRIMARY KEY,
    task_id text NOT NULL,
    approval_id text NOT NULL UNIQUE REFERENCES core_approval_requests(id),
    proposal_id text NOT NULL REFERENCES core_tool_proposals(id),
    action_digest text NOT NULL,
    idempotency_key text NOT NULL UNIQUE,
    state text NOT NULL,
    attempt integer NOT NULL,
    reserved_at double precision NOT NULL
);

CREATE TABLE core_events (
    run_id text NOT NULL,
    revision bigint NOT NULL,
    kind text NOT NULL,
    data jsonb NOT NULL,
    published_at double precision NOT NULL,
    PRIMARY KEY (run_id, revision)
);

CREATE TABLE core_checkpoints (
    run_id text PRIMARY KEY,
    revision bigint NOT NULL,
    state jsonb NOT NULL,
    saved_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE core_audit_records (
    run_id text NOT NULL,
    sequence bigint NOT NULL,
    kind text NOT NULL,
    data jsonb NOT NULL,
    written_at double precision NOT NULL,
    PRIMARY KEY (run_id, sequence)
);

CREATE TABLE core_a2a_tasks (
    task_id text NOT NULL,
    owner text NOT NULL,
    tenant text NOT NULL,
    context_id text NOT NULL,
    state integer NOT NULL,
    status_timestamp double precision,
    payload bytea NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, owner, tenant)
);

CREATE INDEX core_a2a_tasks_list_idx
    ON core_a2a_tasks (owner, tenant, status_timestamp DESC, task_id DESC);

CREATE OR REPLACE FUNCTION core_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'row is immutable';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER core_tool_proposals_immutable
BEFORE UPDATE OR DELETE ON core_tool_proposals
FOR EACH ROW EXECUTE FUNCTION core_reject_mutation();

CREATE TRIGGER core_audit_records_immutable
BEFORE UPDATE OR DELETE ON core_audit_records
FOR EACH ROW EXECUTE FUNCTION core_reject_mutation();
""",
    2: """
ALTER TABLE core_events
    ADD COLUMN tenant_id text NOT NULL DEFAULT 'default';
ALTER TABLE core_checkpoints
    ADD COLUMN tenant_id text NOT NULL DEFAULT 'default';
ALTER TABLE core_audit_records
    ADD COLUMN tenant_id text NOT NULL DEFAULT 'default';
ALTER TABLE core_execution_records
    ADD COLUMN started_at double precision,
    ADD COLUMN finished_at double precision,
    ADD COLUMN outcome jsonb,
    ADD COLUMN error_code text;
ALTER TABLE core_a2a_tasks
    ADD COLUMN protocol_version text NOT NULL DEFAULT '1.0',
    ADD COLUMN extension_version text NOT NULL DEFAULT 'v1';

CREATE INDEX core_events_tenant_run_idx
    ON core_events (tenant_id, run_id, revision);
CREATE INDEX core_checkpoints_tenant_run_idx
    ON core_checkpoints (tenant_id, run_id);
CREATE INDEX core_audit_tenant_run_idx
    ON core_audit_records (tenant_id, run_id, sequence);

CREATE TABLE core_runs (
    run_id text PRIMARY KEY,
    task_id text NOT NULL,
    context_id text NOT NULL,
    tenant_id text NOT NULL,
    owner_id text NOT NULL,
    parent_run_id text,
    state text NOT NULL,
    version bigint NOT NULL DEFAULT 1,
    request jsonb NOT NULL,
    snapshot jsonb NOT NULL,
    pending_approval_id text,
    lease_owner text,
    lease_token text,
    lease_expires_at double precision,
    result jsonb,
    error_code text,
    created_at double precision NOT NULL,
    updated_at double precision NOT NULL,
    UNIQUE (task_id, tenant_id, owner_id)
);

CREATE INDEX core_runs_recovery_idx
    ON core_runs (state, updated_at)
    WHERE state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED', 'REJECTED', 'ABORTED');

CREATE TABLE core_background_tasks (
    id text PRIMARY KEY,
    owner_run_id text NOT NULL,
    tenant_id text NOT NULL,
    kind text NOT NULL,
    state text NOT NULL,
    required boolean NOT NULL,
    recoverable boolean NOT NULL,
    revision bigint NOT NULL DEFAULT 0,
    contract jsonb NOT NULL,
    result jsonb,
    error_code text,
    cancel_requested boolean NOT NULL DEFAULT false,
    created_at double precision NOT NULL,
    updated_at double precision NOT NULL
);

CREATE INDEX core_background_owner_idx
    ON core_background_tasks (tenant_id, owner_run_id, created_at);
CREATE INDEX core_background_recovery_idx
    ON core_background_tasks (state, updated_at)
    WHERE state IN ('submitted', 'working');

CREATE TABLE core_notifications (
    id text PRIMARY KEY,
    owner_run_id text NOT NULL,
    tenant_id text NOT NULL,
    task_id text NOT NULL,
    kind text NOT NULL,
    revision bigint NOT NULL,
    payload jsonb NOT NULL,
    acknowledged_at double precision,
    created_at double precision NOT NULL,
    UNIQUE (tenant_id, owner_run_id, task_id, kind, revision)
);

CREATE INDEX core_notifications_pending_idx
    ON core_notifications (tenant_id, owner_run_id, created_at)
    WHERE acknowledged_at IS NULL;

CREATE TABLE core_outbox (
    id text PRIMARY KEY,
    tenant_id text NOT NULL,
    aggregate_type text NOT NULL,
    aggregate_id text NOT NULL,
    event_type text NOT NULL,
    sequence bigint NOT NULL,
    payload jsonb NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    available_at double precision NOT NULL,
    locked_by text,
    locked_until double precision,
    published_at double precision,
    last_error_code text,
    created_at double precision NOT NULL,
    UNIQUE (tenant_id, aggregate_type, aggregate_id, event_type, sequence)
);

CREATE INDEX core_outbox_pending_idx
    ON core_outbox (available_at, created_at)
    WHERE published_at IS NULL;
""",
    3: """
CREATE TABLE core_push_notification_configs (
    task_id text NOT NULL,
    config_id text NOT NULL,
    owner text NOT NULL,
    tenant_id text NOT NULL,
    encrypted_payload bytea NOT NULL,
    updated_at double precision NOT NULL,
    PRIMARY KEY (task_id, config_id, owner, tenant_id)
);

CREATE INDEX core_push_configs_dispatch_idx
    ON core_push_notification_configs (task_id, config_id);

CREATE TABLE core_push_deliveries (
    id text PRIMARY KEY,
    task_id text NOT NULL,
    config_id text NOT NULL,
    event_key text NOT NULL,
    payload jsonb NOT NULL,
    state text NOT NULL,
    attempts integer NOT NULL,
    available_at double precision NOT NULL,
    locked_until double precision,
    last_error_code text,
    delivered_at double precision,
    created_at double precision NOT NULL,
    updated_at double precision NOT NULL,
    UNIQUE (task_id, config_id, event_key)
);

CREATE INDEX core_push_deliveries_pending_idx
    ON core_push_deliveries (available_at, created_at)
    WHERE state != 'delivered';
""",
    4: """
CREATE TABLE core_artifacts (
    id text NOT NULL,
    tenant_id text NOT NULL,
    media_type text NOT NULL,
    digest text NOT NULL,
    size bigint NOT NULL,
    provenance jsonb NOT NULL,
    state text NOT NULL,
    created_at double precision NOT NULL,
    deleted_at double precision,
    PRIMARY KEY (id, tenant_id)
);

CREATE INDEX core_artifacts_digest_idx
    ON core_artifacts (digest) WHERE state = 'active';
CREATE INDEX core_artifacts_run_idx
    ON core_artifacts (tenant_id, ((provenance->>'run_id')))
    WHERE state = 'active';
""",
    5: """
UPDATE core_a2a_tasks SET tenant = 'default' WHERE tenant = '';
""",
    6: """
CREATE TABLE core_budget_ledgers (
    root_run_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    max_model_turns integer NOT NULL,
    max_tool_calls integer NOT NULL,
    used_model_turns integer NOT NULL DEFAULT 0,
    used_tool_calls integer NOT NULL DEFAULT 0,
    updated_at double precision NOT NULL
);
""",
    7: """
CREATE TABLE core_inbound_messages (
    run_id text NOT NULL REFERENCES core_runs(run_id) ON DELETE CASCADE,
    sequence bigint NOT NULL,
    message_id text NOT NULL,
    context_id text NOT NULL,
    role text NOT NULL,
    content text NOT NULL,
    provenance jsonb NOT NULL,
    received_at double precision NOT NULL,
    consumed_at double precision,
    PRIMARY KEY (run_id, sequence),
    UNIQUE (run_id, message_id)
);

CREATE INDEX core_inbound_pending_idx
    ON core_inbound_messages (run_id, sequence)
    WHERE consumed_at IS NULL;
""",
    8: """
DROP TRIGGER IF EXISTS core_tool_proposals_immutable ON core_tool_proposals;
DROP TABLE IF EXISTS core_execution_records;
DROP TABLE IF EXISTS core_approval_requests;
DROP TABLE IF EXISTS core_tool_proposals;
""",
    9: """
CREATE TABLE core_memory_documents (
    app_name text NOT NULL,
    user_id text NOT NULL,
    memory_id text NOT NULL,
    namespace text NOT NULL,
    path text NOT NULL,
    content text NOT NULL,
    revision integer NOT NULL,
    embedding real[],
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (app_name, user_id, memory_id)
);

CREATE INDEX core_memory_namespace_idx
    ON core_memory_documents (app_name, user_id, namespace);

CREATE TABLE core_memory_document_versions (
    app_name text NOT NULL,
    user_id text NOT NULL,
    memory_id text NOT NULL,
    namespace text NOT NULL,
    path text NOT NULL,
    content text NOT NULL,
    revision integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (app_name, user_id, memory_id, revision)
);

CREATE TABLE core_memory_revisions (
    app_name text NOT NULL,
    user_id text NOT NULL,
    repository_revision integer NOT NULL,
    resolutions jsonb NOT NULL,
    published_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (app_name, user_id, repository_revision)
);
""",
    10: """
ALTER TABLE core_memory_documents ADD COLUMN entities jsonb;
""",
}


class PostgresDatabase:
    """One bounded PostgreSQL pool shared by all production state adapters."""

    def __init__(
        self,
        url: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
        timeout: float = 10,
    ):
        if not url:
            raise CoreError(
                "DATABASE_URL_REQUIRED",
                "set SESSION_DATABASE_URL, SESSION_POSTGRES_HOST or DATABASE_URL",
            )
        if min_size < 0 or max_size < 1 or min_size > max_size or timeout <= 0:
            raise CoreError("CONFIG_INVALID", "invalid database pool configuration")
        self._closed = False
        self.pool = ConnectionPool(
            conninfo=url,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            open=False,
            name="core-agent",
            kwargs={"autocommit": True, "row_factory": dict_row},
            # Managed PostgreSQL drops idle connections without telling the client;
            # without this the first request after a pause fails on a dead socket.
            check=ConnectionPool.check_connection,
        )
        try:
            self.pool.open(wait=True, timeout=timeout)
            self.check()
        except Exception:
            self.pool.close()
            raise CoreError(
                "DATABASE_UNAVAILABLE",
                "cannot reach the configured PostgreSQL host",
            ) from None

    @classmethod
    def from_environment(cls, url=None):
        def integer(name, default):
            try:
                return int(os.getenv(name, default))
            except ValueError:
                raise CoreError("CONFIG_INVALID", f"{name} must be an integer") from None

        try:
            timeout = float(os.getenv("DATABASE_CONNECT_TIMEOUT_SECONDS", "10"))
        except ValueError:
            raise CoreError(
                "CONFIG_INVALID", "DATABASE_CONNECT_TIMEOUT_SECONDS must be numeric"
            ) from None
        return cls(
            os.getenv("DATABASE_URL", "") if url is None else url,
            min_size=integer("DATABASE_POOL_MIN", "1"),
            max_size=integer("DATABASE_POOL_MAX", "10"),
            timeout=timeout,
        )

    @contextmanager
    def transaction(self):
        with self.pool.connection() as connection:
            with connection.transaction():
                yield connection

    def check(self):
        with self.pool.connection() as connection:
            connection.execute("SELECT 1").fetchone()

    def schema_version(self):
        with self.pool.connection() as connection:
            row = connection.execute("SELECT to_regclass('core_schema_migrations') AS name").fetchone()
            if not row["name"]:
                return 0
            row = connection.execute(
                "SELECT COALESCE(max(version), 0) AS version FROM core_schema_migrations"
            ).fetchone()
            return row["version"]

    def migrate(self):
        with self.transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(913728451)")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS core_schema_migrations (
                    version integer PRIMARY KEY,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )"""
            )
            applied = {
                row["version"]
                for row in connection.execute(
                    "SELECT version FROM core_schema_migrations"
                ).fetchall()
            }
            unknown = applied - MIGRATIONS.keys()
            if unknown:
                raise CoreError(
                    "DATABASE_SCHEMA_UNSUPPORTED",
                    "the database carries migrations this build does not know",
                )
            for version, sql in MIGRATIONS.items():
                if version not in applied:
                    connection.execute(sql)
                    connection.execute(
                        "INSERT INTO core_schema_migrations (version) VALUES (%s)",
                        (version,),
                    )
        self.verify_schema()

    def verify_schema(self):
        try:
            version = self.schema_version()
        except Exception:
            raise CoreError(
                "DATABASE_SCHEMA_UNAVAILABLE",
                "run the migration job before starting the agent",
            ) from None
        if version != SCHEMA_VERSION:
            raise CoreError(
                "DATABASE_SCHEMA_MISMATCH",
                f"expected schema {SCHEMA_VERSION}, got {version}",
            )

    def grant_application_role(self, role):
        if not role:
            raise CoreError("CONFIG_INVALID", "DATABASE_APP_ROLE is required")
        grants = {
            "core_schema_migrations": "SELECT",
            "core_events": "SELECT, INSERT, DELETE",
            "core_checkpoints": "SELECT, INSERT, UPDATE, DELETE",
            "core_audit_records": "SELECT, INSERT",
            "core_a2a_tasks": "SELECT, INSERT, UPDATE, DELETE",
            "core_runs": "SELECT, INSERT, UPDATE, DELETE",
            "core_background_tasks": "SELECT, INSERT, UPDATE, DELETE",
            "core_notifications": "SELECT, INSERT, UPDATE, DELETE",
            "core_outbox": "SELECT, INSERT, UPDATE, DELETE",
            "core_push_notification_configs": "SELECT, INSERT, UPDATE, DELETE",
            "core_push_deliveries": "SELECT, INSERT, UPDATE",
            "core_artifacts": "SELECT, INSERT, UPDATE, DELETE",
            "core_budget_ledgers": "SELECT, INSERT, UPDATE, DELETE",
            "core_inbound_messages": "SELECT, INSERT, UPDATE, DELETE",
            "core_memory_documents": "SELECT, INSERT, UPDATE, DELETE",
            "core_memory_document_versions": "SELECT, INSERT, DELETE",
            "core_memory_revisions": "SELECT, INSERT, DELETE",
        }
        with self.transaction() as connection:
            connection.execute(
                SQL("GRANT USAGE ON SCHEMA public TO {}").format(Identifier(role))
            )
            for table, privileges in grants.items():
                connection.execute(
                    SQL("GRANT {} ON {} TO {}").format(
                        SQL(privileges), Identifier(table), Identifier(role)
                    )
                )

    def close(self):
        if not self._closed:
            self.pool.close()
            self._closed = True


class PostgresEventStore:
    def __init__(self, database):
        self.database = database

    def append(self, run_id, kind, data, *, tenant_id="default"):
        published_at = time.time()
        with self.database.transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (run_id,))
            row = connection.execute(
                """SELECT COALESCE(max(revision), 0) + 1 AS revision
                   FROM core_events WHERE run_id = %s AND tenant_id = %s""",
                (run_id, tenant_id),
            ).fetchone()
            revision = row["revision"]
            connection.execute(
                """INSERT INTO core_events
                   (run_id, revision, kind, data, published_at, tenant_id)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (run_id, revision, kind, Jsonb(dict(data)), published_at, tenant_id),
            )
        return Event(run_id, revision, kind, dict(data), published_at)

    def events(self, run_id, *, tenant_id="default"):
        with self.database.pool.connection() as connection:
            rows = connection.execute(
                """SELECT run_id, revision, kind, data, published_at
                   FROM core_events WHERE run_id = %s AND tenant_id = %s
                   ORDER BY revision""",
                (run_id, tenant_id),
            ).fetchall()
        return tuple(Event(**row) for row in rows)

    def revision(self, run_id, *, tenant_id="default"):
        with self.database.pool.connection() as connection:
            return connection.execute(
                """SELECT COALESCE(max(revision), 0) AS revision
                   FROM core_events WHERE run_id = %s AND tenant_id = %s""",
                (run_id, tenant_id),
            ).fetchone()["revision"]

    def count(self, run_id, *, kind=None, tenant_id="default"):
        sql = "SELECT count(*) AS count FROM core_events WHERE run_id = %s AND tenant_id = %s"
        values = [run_id, tenant_id]
        if kind is not None:
            sql += " AND kind = %s"
            values.append(kind)
        with self.database.pool.connection() as connection:
            return connection.execute(sql, values).fetchone()["count"]


class PostgresCheckpointStore:
    def __init__(self, database):
        self.database = database

    def save(self, run_id, revision, state, *, tenant_id="default"):
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO core_checkpoints (run_id, revision, state, tenant_id)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (run_id) DO UPDATE SET
                     revision = EXCLUDED.revision,
                     state = EXCLUDED.state,
                     tenant_id = EXCLUDED.tenant_id,
                     saved_at = now()
                   WHERE core_checkpoints.revision <= EXCLUDED.revision
                     AND core_checkpoints.tenant_id = EXCLUDED.tenant_id""",
                (run_id, revision, Jsonb(dict(state)), tenant_id),
            )

    def load(self, run_id, *, tenant_id="default"):
        with self.database.pool.connection() as connection:
            row = connection.execute(
                """SELECT revision, state FROM core_checkpoints
                   WHERE run_id = %s AND tenant_id = %s""",
                (run_id, tenant_id),
            ).fetchone()
        return (row["revision"], row["state"]) if row else None


class PostgresAuditLog:
    def __init__(self, database):
        self.database = database

    def append(self, run_id, kind, data, *, tenant_id="default"):
        written_at = time.time()
        with self.database.transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (run_id,))
            sequence = connection.execute(
                """SELECT COALESCE(max(sequence), 0) + 1 AS sequence
                   FROM core_audit_records WHERE run_id = %s AND tenant_id = %s""",
                (run_id, tenant_id),
            ).fetchone()["sequence"]
            connection.execute(
                """INSERT INTO core_audit_records
                   (run_id, sequence, kind, data, written_at, tenant_id)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (run_id, sequence, kind, Jsonb(dict(data)), written_at, tenant_id),
            )
        return AuditRecord(run_id, sequence, kind, dict(data), written_at)

    def records(self, run_id, *, tenant_id="default"):
        with self.database.pool.connection() as connection:
            rows = connection.execute(
                """SELECT run_id, sequence, kind, data, written_at
                   FROM core_audit_records
                   WHERE run_id = %s AND tenant_id = %s ORDER BY sequence""",
                (run_id, tenant_id),
            ).fetchall()
        return tuple(AuditRecord(**row) for row in rows)

    def replace(self, run_id, sequence, data):
        raise CoreError("AUDIT_IMMUTABLE")


class PostgresTaskStore(TaskStore):
    """Durable A2A task store scoped by authenticated owner and tenant."""

    def __init__(self, database, owner_resolver=resolve_user_scope):
        self.database = database
        self.owner_resolver = owner_resolver

    def _scope(self, context):
        owner = self.owner_resolver(context)
        return (
            owner if context.user.is_authenticated and owner else "anonymous"
        ), context.tenant or "default"

    def reconcile_from_workflows(self):
        terminal = {
            "COMPLETED": a2a_pb2.TASK_STATE_COMPLETED,
            "FAILED": a2a_pb2.TASK_STATE_FAILED,
            "ABORTED": a2a_pb2.TASK_STATE_FAILED,
            "CANCELLED": a2a_pb2.TASK_STATE_CANCELED,
            "REJECTED": a2a_pb2.TASK_STATE_REJECTED,
        }
        reconciled = 0
        with self.database.transaction() as connection:
            rows = connection.execute(
                """SELECT task.payload, task.owner, task.tenant,
                          run.state AS run_state, run.result, run.error_code
                   FROM core_a2a_tasks task
                   JOIN core_runs run ON run.task_id = task.task_id
                   WHERE task.state NOT IN (%s, %s, %s, %s)
                     AND run.state IN ('COMPLETED','FAILED','ABORTED','CANCELLED','REJECTED')
                   FOR UPDATE OF task""",
                (
                    int(a2a_pb2.TASK_STATE_COMPLETED),
                    int(a2a_pb2.TASK_STATE_FAILED),
                    int(a2a_pb2.TASK_STATE_CANCELED),
                    int(a2a_pb2.TASK_STATE_REJECTED),
                ),
            ).fetchall()
            for row in rows:
                task = a2a_pb2.Task.FromString(bytes(row["payload"]))
                task.status.state = terminal[row["run_state"]]
                task.status.timestamp.GetCurrentTime()
                result = row["result"] or {}
                message = result.get("message")
                if row["run_state"] == "COMPLETED" and message and not task.artifacts:
                    encoded = message.encode()
                    digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
                    artifact = task.artifacts.add()
                    artifact.artifact_id = digest
                    part = artifact.parts.add()
                    part.text = message
                    part.media_type = "text/plain"
                    artifact.metadata.update(
                        {"digest": digest, "size": len(encoded), "recovered": True}
                    )
                connection.execute(
                    """UPDATE core_a2a_tasks SET state = %s,
                           status_timestamp = %s, payload = %s, updated_at = now()
                       WHERE task_id = %s AND owner = %s AND tenant = %s""",
                    (
                        int(task.status.state),
                        task.status.timestamp.ToMilliseconds() / 1000,
                        task.SerializeToString(),
                        task.id,
                        row["owner"],
                        row["tenant"],
                    ),
                )
                reconciled += 1
        return reconciled

    def _save(self, task, context):
        owner, tenant = self._scope(context)
        timestamp = None
        if task.HasField("status") and task.status.HasField("timestamp"):
            timestamp = task.status.timestamp.ToMilliseconds() / 1000
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO core_a2a_tasks
                   (task_id, owner, tenant, context_id, state, status_timestamp, payload)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (task_id, owner, tenant) DO UPDATE SET
                     context_id = EXCLUDED.context_id,
                     state = EXCLUDED.state,
                     status_timestamp = EXCLUDED.status_timestamp,
                     payload = EXCLUDED.payload,
                     updated_at = now()""",
                (
                    task.id,
                    owner,
                    tenant,
                    task.context_id,
                    int(task.status.state),
                    timestamp,
                    task.SerializeToString(),
                ),
            )

    def _get(self, task_id, context):
        owner, tenant = self._scope(context)
        with self.database.pool.connection() as connection:
            row = connection.execute(
                "SELECT payload FROM core_a2a_tasks WHERE task_id = %s AND owner = %s AND tenant = %s",
                (task_id, owner, tenant),
            ).fetchone()
        return a2a_pb2.Task.FromString(bytes(row["payload"])) if row else None

    def _list(self, params, context):
        owner, tenant = self._scope(context)
        sql = "SELECT payload FROM core_a2a_tasks WHERE owner = %s AND tenant = %s"
        values = [owner, tenant]
        if params.context_id:
            sql += " AND context_id = %s"
            values.append(params.context_id)
        if params.status:
            sql += " AND state = %s"
            values.append(int(params.status))
        if params.HasField("status_timestamp_after"):
            sql += " AND status_timestamp >= %s"
            values.append(params.status_timestamp_after.ToMilliseconds() / 1000)
        sql += " ORDER BY status_timestamp DESC NULLS LAST, task_id DESC"
        with self.database.pool.connection() as connection:
            rows = connection.execute(sql, values).fetchall()
        tasks = [a2a_pb2.Task.FromString(bytes(row["payload"])) for row in rows]
        total_size = len(tasks)
        start_idx = 0
        if params.page_token:
            task_id = decode_page_token(params.page_token)
            for index, task in enumerate(tasks):
                if task.id == task_id:
                    start_idx = index
                    break
            else:
                raise InvalidParamsError(f"Invalid page token: {params.page_token}")
        page_size = params.page_size or DEFAULT_LIST_TASKS_PAGE_SIZE
        end_idx = start_idx + page_size
        next_token = encode_page_token(tasks[end_idx].id) if end_idx < total_size else None
        return a2a_pb2.ListTasksResponse(
            next_page_token=next_token,
            tasks=tasks[start_idx:end_idx],
            total_size=total_size,
            page_size=page_size,
        )

    def _delete(self, task_id, context):
        owner, tenant = self._scope(context)
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM core_a2a_tasks WHERE task_id = %s AND owner = %s AND tenant = %s",
                (task_id, owner, tenant),
            )

    async def save(self, task, context):
        await asyncio.to_thread(self._save, task, context)

    async def get(self, task_id, context):
        return await asyncio.to_thread(self._get, task_id, context)

    async def list(self, params, context):
        return await asyncio.to_thread(self._list, params, context)

    async def delete(self, task_id, context):
        await asyncio.to_thread(self._delete, task_id, context)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Manage core-agent PostgreSQL schema")
    parser.add_argument("command", nargs="?", default="migrate", choices=("migrate", "check"))
    args = parser.parse_args(argv)
    database = PostgresDatabase.from_environment(
        os.getenv("DATABASE_MIGRATION_URL") or os.getenv("DATABASE_URL", "")
    )
    try:
        if args.command == "migrate":
            database.migrate()
            if os.getenv("DATABASE_APP_ROLE"):
                database.grant_application_role(os.environ["DATABASE_APP_ROLE"])
        else:
            database.verify_schema()
    finally:
        database.close()


if __name__ == "__main__":
    main()
