from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass

from psycopg.types.json import Jsonb

from .errors import CoreError


TERMINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED", "REJECTED", "ABORTED"}


@dataclass(frozen=True)
class WorkflowRecord:
    run_id: str
    task_id: str
    context_id: str
    tenant_id: str
    owner_id: str
    parent_run_id: str | None
    state: str
    version: int
    request: dict
    snapshot: dict
    pending_approval_id: str | None = None
    result: dict | None = None
    error_code: str | None = None


class InMemoryWorkflowStore:
    """Test adapter with the same optimistic state contract as PostgreSQL."""

    atomic = False

    def __init__(self, clock=time.time):
        self.clock = clock
        self._records = {}
        self._leases = {}
        self._lock = threading.RLock()
        self._budgets = {}

    def create(self, record, *, budget_limits=(100, 200), **_metadata):
        with self._lock:
            if record.run_id in self._records:
                raise CoreError("SESSION_CONFLICT")
            if record.parent_run_id:
                parent = self._records.get(record.parent_run_id)
                if parent is None:
                    raise CoreError("TASK_NOT_FOUND")
                root_run_id = parent.snapshot.get("budget_root_id", parent.run_id)
            else:
                root_run_id = record.run_id
                self._budgets[root_run_id] = [*budget_limits, 0, 0]
            record = WorkflowRecord(
                **{
                    **record.__dict__,
                    "snapshot": {**record.snapshot, "budget_root_id": root_run_id},
                }
            )
            self._records[record.run_id] = record
            return record

    def consume_budget(self, record, *, model_turns=0, tool_calls=0):
        with self._lock:
            root = record.snapshot["budget_root_id"]
            budget = self._budgets[root]
            if budget[2] + model_turns > budget[0] or budget[3] + tool_calls > budget[1]:
                raise CoreError("BUDGET_EXCEEDED")
            budget[2] += model_turns
            budget[3] += tool_calls

    def get(self, run_id, *, tenant_id, owner_id=None, **_options):
        with self._lock:
            record = self._records.get(run_id)
            if (
                record is None
                or record.tenant_id != tenant_id
                or (owner_id is not None and record.owner_id != owner_id)
            ):
                raise CoreError("TASK_NOT_FOUND")
            return record

    def by_task(self, task_id, *, tenant_id, owner_id):
        with self._lock:
            for record in self._records.values():
                if (
                    record.task_id == task_id
                    and record.tenant_id == tenant_id
                    and record.owner_id == owner_id
                ):
                    return record
        raise CoreError("TASK_NOT_FOUND")

    def lookup_task(self, task_id):
        with self._lock:
            records = [
                record for record in self._records.values() if record.task_id == task_id
            ]
            if len(records) != 1:
                raise CoreError("TASK_NOT_FOUND")
            return records[0]

    def transition(
        self,
        run_id,
        *,
        tenant_id,
        owner_id,
        expected_version,
        state,
        snapshot,
        pending_approval_id=None,
        result=None,
        error_code=None,
        lease_token=None,
        **_metadata,
    ):
        with self._lock:
            current = self.get(run_id, tenant_id=tenant_id, owner_id=owner_id)
            if current.version != expected_version:
                raise CoreError("SESSION_CONFLICT")
            if current.state in TERMINAL_STATES and state != current.state:
                raise CoreError("INVALID_TASK_STATE")
            if lease_token is not None:
                lease = self._leases.get(run_id)
                if not lease or lease[1] != lease_token or lease[2] <= self.clock():
                    raise CoreError("LEASE_LOST")
            record = WorkflowRecord(
                **{
                    **current.__dict__,
                    "state": state,
                    "version": current.version + 1,
                    "snapshot": dict(snapshot),
                    "pending_approval_id": pending_approval_id,
                    "result": result,
                    "error_code": error_code,
                }
            )
            self._records[run_id] = record
            return record

    def enter_approval(
        self,
        record,
        approval_manager,
        call,
        *,
        risks,
        snapshot,
        environment,
        policy_version,
        lease_token,
    ):
        approval = approval_manager.request(
            call,
            risks=risks,
            task_id=record.task_id,
            context_id=record.context_id,
            tenant_id=record.tenant_id,
            caller_principal_id=record.owner_id,
            environment=environment,
            policy_version=policy_version,
        )
        next_record = self.transition(
            record.run_id,
            tenant_id=record.tenant_id,
            owner_id=record.owner_id,
            expected_version=record.version,
            state="WAITING_LOCAL_APPROVAL",
            snapshot=snapshot,
            event_kind="approval.required",
            event_data={"approval_id": approval.id},
            pending_approval_id=approval.id,
            lease_token=lease_token,
        )
        return next_record, approval

    def reserve_approval(
        self,
        record,
        approval_manager,
        approval,
        *,
        operator_principal_id,
        operator_session_id,
        snapshot,
    ):
        execution = approval_manager.approve_once(
            approval.id,
            action_digest=approval.action_digest,
            expected_version=approval.version,
            operator_principal_id=operator_principal_id,
            operator_session_id=operator_session_id,
            idempotency_key=f"{approval.task_id}:{approval.id}:{approval.action_digest}",
        )
        snapshot = dict(snapshot)
        snapshot["execution_id"] = execution.id
        next_record = self.transition(
            record.run_id,
            tenant_id=record.tenant_id,
            owner_id=record.owner_id,
            expected_version=record.version,
            state="APPROVED_RESERVED",
            snapshot=snapshot,
            event_kind="execution.reserved",
            event_data={"approval_id": approval.id, "execution_id": execution.id},
            pending_approval_id=approval.id,
        )
        return next_record, execution

    def acquire_lease(self, run_id, *, tenant_id, owner_id, worker_id, ttl):
        with self._lock:
            self.get(run_id, tenant_id=tenant_id, owner_id=owner_id)
            current = self._leases.get(run_id)
            now = self.clock()
            if current and current[2] > now and current[0] != worker_id:
                raise CoreError("LEASE_LOST")
            token = str(uuid.uuid4())
            self._leases[run_id] = (worker_id, token, now + ttl)
            return token

    def renew_lease(self, run_id, *, tenant_id, worker_id, token, ttl):
        with self._lock:
            current = self._leases.get(run_id)
            if (
                not current
                or current[0] != worker_id
                or current[1] != token
                or current[2] <= self.clock()
            ):
                raise CoreError("LEASE_LOST")
            self._leases[run_id] = (worker_id, token, self.clock() + ttl)

    def release_lease(self, run_id, *, tenant_id, worker_id, token):
        with self._lock:
            current = self._leases.get(run_id)
            if not current or current[:2] != (worker_id, token):
                raise CoreError("LEASE_LOST")
            del self._leases[run_id]

    def recoverable(self, *, limit=100):
        with self._lock:
            return tuple(
                record
                for record in self._records.values()
                if record.state not in TERMINAL_STATES
            )[:limit]


class PostgresWorkflowStore:
    """Atomic source of truth for run state, events, checkpoints and outbox."""

    atomic = True

    def __init__(self, database, clock=time.time):
        self.database = database
        self.clock = clock

    @staticmethod
    def _record(row):
        return WorkflowRecord(
            **{
                key: row[key]
                for key in WorkflowRecord.__dataclass_fields__
            }
        )

    @staticmethod
    def _event(connection, record, kind, data, now):
        revision = connection.execute(
            """SELECT COALESCE(max(revision), 0) + 1 AS revision
               FROM core_events WHERE run_id = %s AND tenant_id = %s""",
            (record.run_id, record.tenant_id),
        ).fetchone()["revision"]
        connection.execute(
            """INSERT INTO core_events
               (run_id, revision, kind, data, published_at, tenant_id)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                record.run_id,
                revision,
                kind,
                Jsonb(dict(data)),
                now,
                record.tenant_id,
            ),
        )
        return revision

    @staticmethod
    def _audit(connection, record, entries, now):
        sequence = connection.execute(
            """SELECT COALESCE(max(sequence), 0) AS sequence
               FROM core_audit_records WHERE run_id = %s AND tenant_id = %s""",
            (record.run_id, record.tenant_id),
        ).fetchone()["sequence"]
        for kind, data in entries:
            sequence += 1
            connection.execute(
                """INSERT INTO core_audit_records
                   (run_id, sequence, kind, data, written_at, tenant_id)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (
                    record.run_id,
                    sequence,
                    kind,
                    Jsonb(dict(data)),
                    now,
                    record.tenant_id,
                ),
            )

    @staticmethod
    def _outbox(connection, record, event_type, sequence, payload, now):
        connection.execute(
            """INSERT INTO core_outbox
               (id, tenant_id, aggregate_type, aggregate_id, event_type,
                sequence, payload, available_at, created_at)
               VALUES (%s, %s, 'run', %s, %s, %s, %s, %s, %s)
               ON CONFLICT (tenant_id, aggregate_type, aggregate_id,
                            event_type, sequence) DO NOTHING""",
            (
                str(uuid.uuid4()),
                record.tenant_id,
                record.run_id,
                event_type,
                sequence,
                Jsonb(dict(payload)),
                now,
                now,
            ),
        )

    def create(
        self,
        record,
        *,
        audit=(),
        outbox_payload=None,
        budget_limits=(100, 200),
    ):
        now = self.clock()
        if record.version != 1:
            raise CoreError("INVALID_TASK_STATE")
        with self.database.transaction() as connection:
            if record.parent_run_id:
                parent = connection.execute(
                    """SELECT snapshot FROM core_runs
                       WHERE run_id = %s AND tenant_id = %s FOR SHARE""",
                    (record.parent_run_id, record.tenant_id),
                ).fetchone()
                if parent is None:
                    raise CoreError("TASK_NOT_FOUND")
                root_run_id = parent["snapshot"].get(
                    "budget_root_id", record.parent_run_id
                )
            else:
                root_run_id = record.run_id
                connection.execute(
                    """INSERT INTO core_budget_ledgers
                       (root_run_id, tenant_id, max_model_turns, max_tool_calls,
                        updated_at) VALUES (%s, %s, %s, %s, %s)""",
                    (
                        root_run_id,
                        record.tenant_id,
                        budget_limits[0],
                        budget_limits[1],
                        now,
                    ),
                )
            record = WorkflowRecord(
                **{
                    **record.__dict__,
                    "snapshot": {**record.snapshot, "budget_root_id": root_run_id},
                }
            )
            connection.execute(
                """INSERT INTO core_runs
                   (run_id, task_id, context_id, tenant_id, owner_id,
                    parent_run_id, state, version, request, snapshot,
                    pending_approval_id, result, error_code, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, %s,
                           %s, %s, %s, %s, %s)""",
                (
                    record.run_id,
                    record.task_id,
                    record.context_id,
                    record.tenant_id,
                    record.owner_id,
                    record.parent_run_id,
                    record.state,
                    Jsonb(record.request),
                    Jsonb(record.snapshot),
                    record.pending_approval_id,
                    Jsonb(record.result) if record.result is not None else None,
                    record.error_code,
                    now,
                    now,
                ),
            )
            revision = self._event(
                connection, record, "task.created", {"state": record.state}, now
            )
            connection.execute(
                """INSERT INTO core_checkpoints
                   (run_id, revision, state, tenant_id)
                   VALUES (%s, %s, %s, %s)""",
                (
                    record.run_id,
                    revision,
                    Jsonb({**record.snapshot, "state": record.state}),
                    record.tenant_id,
                ),
            )
            self._audit(connection, record, audit, now)
            self._outbox(
                connection,
                record,
                "task.created",
                revision,
                outbox_payload or {"task_id": record.task_id, "state": record.state},
                now,
            )
        return self.get(record.run_id, tenant_id=record.tenant_id, owner_id=record.owner_id)

    def consume_budget(self, record, *, model_turns=0, tool_calls=0):
        now = self.clock()
        root_run_id = record.snapshot["budget_root_id"]
        with self.database.transaction() as connection:
            updated = connection.execute(
                """UPDATE core_budget_ledgers SET
                     used_model_turns = used_model_turns + %s,
                     used_tool_calls = used_tool_calls + %s,
                     updated_at = %s
                   WHERE root_run_id = %s AND tenant_id = %s
                     AND used_model_turns + %s <= max_model_turns
                     AND used_tool_calls + %s <= max_tool_calls""",
                (
                    model_turns,
                    tool_calls,
                    now,
                    root_run_id,
                    record.tenant_id,
                    model_turns,
                    tool_calls,
                ),
            )
        if updated.rowcount != 1:
            raise CoreError("BUDGET_EXCEEDED")

    def get(self, run_id, *, tenant_id, owner_id=None, connection=None, lock=False):
        def query(db):
            sql = "SELECT * FROM core_runs WHERE run_id = %s AND tenant_id = %s"
            values = [run_id, tenant_id]
            if owner_id is not None:
                sql += " AND owner_id = %s"
                values.append(owner_id)
            if lock:
                sql += " FOR UPDATE"
            row = db.execute(sql, values).fetchone()
            if row is None:
                raise CoreError("TASK_NOT_FOUND")
            return self._record(row)

        if connection is not None:
            return query(connection)
        with self.database.pool.connection() as db:
            return query(db)

    def by_task(self, task_id, *, tenant_id, owner_id):
        with self.database.pool.connection() as connection:
            row = connection.execute(
                """SELECT * FROM core_runs
                   WHERE task_id = %s AND tenant_id = %s AND owner_id = %s""",
                (task_id, tenant_id, owner_id),
            ).fetchone()
        if row is None:
            raise CoreError("TASK_NOT_FOUND")
        return self._record(row)

    def lookup_task(self, task_id):
        with self.database.pool.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM core_runs WHERE task_id = %s LIMIT 2", (task_id,)
            ).fetchall()
        if len(rows) != 1:
            raise CoreError("TASK_NOT_FOUND")
        return self._record(rows[0])

    def _transition_locked(
        self,
        connection,
        current,
        *,
        expected_version,
        state,
        snapshot,
        event_kind,
        event_data=None,
        audit=(),
        pending_approval_id=None,
        result=None,
        error_code=None,
        outbox_payload=None,
        lease_token=None,
    ):
        now = self.clock()
        if current.version != expected_version:
            raise CoreError("SESSION_CONFLICT")
        if current.state in TERMINAL_STATES and state != current.state:
            raise CoreError("INVALID_TASK_STATE")
        if lease_token is not None:
            lease = connection.execute(
                """SELECT lease_token, lease_expires_at FROM core_runs
                   WHERE run_id = %s""",
                (current.run_id,),
            ).fetchone()
            if lease["lease_token"] != lease_token or (
                lease["lease_expires_at"] or 0
            ) <= now:
                raise CoreError("LEASE_LOST")
        updated = connection.execute(
            """UPDATE core_runs SET state = %s, version = version + 1,
                   snapshot = %s, pending_approval_id = %s, result = %s,
                   error_code = %s, updated_at = %s
               WHERE run_id = %s AND tenant_id = %s AND owner_id = %s
                 AND version = %s""",
            (
                state,
                Jsonb(dict(snapshot)),
                pending_approval_id,
                Jsonb(result) if result is not None else None,
                error_code,
                now,
                current.run_id,
                current.tenant_id,
                current.owner_id,
                expected_version,
            ),
        )
        if updated.rowcount != 1:
            raise CoreError("SESSION_CONFLICT")
        next_record = WorkflowRecord(
            **{
                **current.__dict__,
                "state": state,
                "version": current.version + 1,
                "snapshot": dict(snapshot),
                "pending_approval_id": pending_approval_id,
                "result": result,
                "error_code": error_code,
            }
        )
        revision = self._event(
            connection,
            next_record,
            event_kind,
            event_data or {"state": state},
            now,
        )
        connection.execute(
            """INSERT INTO core_checkpoints
               (run_id, revision, state, tenant_id)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (run_id) DO UPDATE SET revision = EXCLUDED.revision,
                 state = EXCLUDED.state, tenant_id = EXCLUDED.tenant_id,
                 saved_at = now()""",
            (
                current.run_id,
                revision,
                Jsonb({**dict(snapshot), "state": state}),
                current.tenant_id,
            ),
        )
        self._audit(connection, next_record, audit, now)
        self._outbox(
            connection,
            next_record,
            event_kind,
            revision,
            outbox_payload or {"task_id": current.task_id, "state": state},
            now,
        )
        return next_record

    def enter_approval(
        self,
        record,
        approval_manager,
        call,
        *,
        risks,
        snapshot,
        environment,
        policy_version,
        lease_token,
    ):
        with self.database.transaction() as connection:
            current = self.get(
                record.run_id,
                tenant_id=record.tenant_id,
                owner_id=record.owner_id,
                connection=connection,
                lock=True,
            )
            approval = approval_manager.request(
                call,
                risks=risks,
                task_id=record.task_id,
                context_id=record.context_id,
                tenant_id=record.tenant_id,
                caller_principal_id=record.owner_id,
                environment=environment,
                policy_version=policy_version,
                connection=connection,
            )
            next_record = self._transition_locked(
                connection,
                current,
                expected_version=record.version,
                state="WAITING_LOCAL_APPROVAL",
                snapshot=snapshot,
                event_kind="approval.required",
                event_data={"approval_id": approval.id},
                audit=(
                    (
                        "tool.proposed",
                        {
                            "task_id": approval.task_id,
                            "proposal_id": approval.proposal_id,
                            "approval_id": approval.id,
                            "tool_call_id": approval.tool_call_id,
                            "action_digest": approval.action_digest,
                        },
                    ),
                    (
                        "policy.evaluated",
                        {
                            "proposal_id": approval.proposal_id,
                            "decision": "REQUIRE_LOCAL_APPROVAL",
                            "policy_version": approval.policy_version,
                        },
                    ),
                    (
                        "approval.requested",
                        {
                            "approval_id": approval.id,
                            "proposal_id": approval.proposal_id,
                            "action_digest": approval.action_digest,
                        },
                    ),
                ),
                pending_approval_id=approval.id,
                lease_token=lease_token,
            )
        return next_record, approval

    def reserve_approval(
        self,
        record,
        approval_manager,
        approval,
        *,
        operator_principal_id,
        operator_session_id,
        snapshot,
    ):
        with self.database.transaction() as connection:
            current = self.get(
                record.run_id,
                tenant_id=record.tenant_id,
                owner_id=record.owner_id,
                connection=connection,
                lock=True,
            )
            execution = approval_manager.approve_once_in_transaction(
                connection,
                approval.id,
                action_digest=approval.action_digest,
                expected_version=approval.version,
                operator_principal_id=operator_principal_id,
                operator_session_id=operator_session_id,
                idempotency_key=(
                    f"{approval.task_id}:{approval.id}:{approval.action_digest}"
                ),
            )
            snapshot = dict(snapshot)
            snapshot["execution_id"] = execution.id
            next_record = self._transition_locked(
                connection,
                current,
                expected_version=record.version,
                state="APPROVED_RESERVED",
                snapshot=snapshot,
                event_kind="execution.reserved",
                event_data={
                    "approval_id": approval.id,
                    "execution_id": execution.id,
                },
                audit=(
                    (
                        "operator.approved",
                        {
                            "approval_id": approval.id,
                            "proposal_id": approval.proposal_id,
                            "execution_id": execution.id,
                            "action_digest": execution.action_digest,
                            "actor_type": "local_operator",
                            "actor_principal_id": operator_principal_id,
                        },
                    ),
                ),
                pending_approval_id=approval.id,
            )
        return next_record, execution

    def transition(
        self,
        run_id,
        *,
        tenant_id,
        owner_id,
        expected_version,
        state,
        snapshot,
        event_kind,
        event_data=None,
        audit=(),
        pending_approval_id=None,
        result=None,
        error_code=None,
        outbox_payload=None,
        lease_token=None,
    ):
        with self.database.transaction() as connection:
            current = self.get(
                run_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                connection=connection,
                lock=True,
            )
            return self._transition_locked(
                connection,
                current,
                expected_version=expected_version,
                state=state,
                snapshot=snapshot,
                event_kind=event_kind,
                event_data=event_data,
                audit=audit,
                pending_approval_id=pending_approval_id,
                result=result,
                error_code=error_code,
                outbox_payload=outbox_payload,
                lease_token=lease_token,
            )
    def acquire_lease(self, run_id, *, tenant_id, owner_id, worker_id, ttl):
        now = self.clock()
        token = str(uuid.uuid4())
        with self.database.transaction() as connection:
            row = connection.execute(
                """UPDATE core_runs SET lease_owner = %s, lease_token = %s,
                       lease_expires_at = %s, updated_at = %s
                   WHERE run_id = %s AND tenant_id = %s AND owner_id = %s
                     AND state NOT IN ('COMPLETED','FAILED','CANCELLED','REJECTED','ABORTED')
                     AND (lease_expires_at IS NULL OR lease_expires_at <= %s
                          OR lease_owner = %s)
                   RETURNING run_id""",
                (
                    worker_id,
                    token,
                    now + ttl,
                    now,
                    run_id,
                    tenant_id,
                    owner_id,
                    now,
                    worker_id,
                ),
            ).fetchone()
            if row is None:
                raise CoreError("LEASE_LOST")
        return token

    def renew_lease(self, run_id, *, tenant_id, worker_id, token, ttl):
        now = self.clock()
        with self.database.transaction() as connection:
            updated = connection.execute(
                """UPDATE core_runs SET lease_expires_at = %s, updated_at = %s
                   WHERE run_id = %s AND tenant_id = %s AND lease_owner = %s
                     AND lease_token = %s AND lease_expires_at > %s""",
                (now + ttl, now, run_id, tenant_id, worker_id, token, now),
            )
            if updated.rowcount != 1:
                raise CoreError("LEASE_LOST")

    def release_lease(self, run_id, *, tenant_id, worker_id, token):
        with self.database.transaction() as connection:
            updated = connection.execute(
                """UPDATE core_runs SET lease_owner = NULL, lease_token = NULL,
                       lease_expires_at = NULL
                   WHERE run_id = %s AND tenant_id = %s AND lease_owner = %s
                     AND lease_token = %s""",
                (run_id, tenant_id, worker_id, token),
            )
            if updated.rowcount != 1:
                raise CoreError("LEASE_LOST")

    def recoverable(self, *, limit=100):
        with self.database.pool.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM core_runs
                   WHERE state NOT IN ('COMPLETED','FAILED','CANCELLED','REJECTED','ABORTED')
                   ORDER BY updated_at LIMIT %s""",
                (limit,),
            ).fetchall()
        return tuple(self._record(row) for row in rows)


class OutboxDispatcher:
    """At-least-once dispatcher; publishers must deduplicate by outbox event ID."""

    def __init__(self, database, publisher, *, poll_seconds=0.5, clock=time.time):
        self.database = database
        self.publisher = publisher
        self.poll_seconds = poll_seconds
        self.clock = clock
        self.worker_id = str(uuid.uuid4())
        self._stop = threading.Event()
        self._thread = None

    def _claim(self, limit):
        now = self.clock()
        with self.database.transaction() as connection:
            rows = connection.execute(
                """SELECT * FROM core_outbox
                   WHERE published_at IS NULL AND available_at <= %s
                     AND (locked_until IS NULL OR locked_until <= %s)
                   ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT %s""",
                (now, now, limit),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """UPDATE core_outbox SET locked_by = %s, locked_until = %s,
                           attempts = attempts + 1 WHERE id = %s""",
                    (self.worker_id, now + 30, row["id"]),
                )
        return rows

    def drain_once(self, *, limit=50):
        delivered = 0
        for event in self._claim(limit):
            try:
                self.publisher(dict(event))
            except Exception as error:
                with self.database.transaction() as connection:
                    connection.execute(
                        """UPDATE core_outbox SET locked_by = NULL,
                               locked_until = NULL, available_at = %s,
                               last_error_code = %s WHERE id = %s AND locked_by = %s""",
                        (
                            self.clock() + min(60, 2 ** min(event["attempts"], 6)),
                            type(error).__name__,
                            event["id"],
                            self.worker_id,
                        ),
                    )
                continue
            with self.database.transaction() as connection:
                connection.execute(
                    """UPDATE core_outbox SET published_at = %s, locked_by = NULL,
                           locked_until = NULL, last_error_code = NULL
                       WHERE id = %s AND locked_by = %s""",
                    (self.clock(), event["id"], self.worker_id),
                )
            delivered += 1
        return delivered

    def start(self):
        if self._thread is not None:
            return

        def run():
            while not self._stop.is_set():
                self.drain_once()
                self._stop.wait(self.poll_seconds)

        self._thread = threading.Thread(target=run, daemon=True, name="core-outbox")
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
