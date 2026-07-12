from __future__ import annotations

import time
import uuid
import dataclasses

from psycopg.types.json import Jsonb

from .approvals import (
    ApprovalRequest,
    ExecutionRecord,
    ToolProposal,
    _json,
    _semantic_arguments,
    _target,
)
from .errors import CoreError


def _value(value):
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, dict):
        return {str(key): _value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class PostgresApprovalManager:
    """Transactional, single-use local approval store for production."""

    def __init__(self, database, *, ttl_seconds=7200, clock=time.time):
        self.database = database
        self.ttl_seconds = ttl_seconds
        self.clock = clock

    @staticmethod
    def _proposal(row):
        return ToolProposal(**dict(row))

    @staticmethod
    def _execution(row):
        return ExecutionRecord(
            **{
                key: row[key]
                for key in ExecutionRecord.__dataclass_fields__
            }
        )

    def _get(self, connection, approval_id, *, lock=False):
        suffix = " FOR UPDATE" if lock else ""
        row = connection.execute(
            "SELECT * FROM core_approval_requests WHERE id = %s" + suffix,
            (approval_id,),
        ).fetchone()
        if row is None:
            raise CoreError("APPROVAL_NOT_FOUND")
        proposal_row = connection.execute(
            "SELECT * FROM core_tool_proposals WHERE id = %s",
            (row["proposal_id"],),
        ).fetchone()
        values = dict(row)
        values["proposal"] = self._proposal(proposal_row)
        return ApprovalRequest(**values)

    def request(
        self,
        call,
        *,
        risks,
        task_id,
        context_id,
        tenant_id,
        caller_principal_id,
        environment="local-container",
        policy_version="core-policy-v1",
        ttl_seconds=None,
        connection=None,
    ):
        created_at = self.clock()
        proposal = ToolProposal(
            str(uuid.uuid4()),
            task_id,
            context_id,
            tenant_id,
            caller_principal_id,
            call.id,
            call.name,
            "1",
            environment,
            _json(_target(call.arguments, call.name)),
            _json(_semantic_arguments(call.arguments)),
            ",".join(sorted(risks)) if risks else "protected_action",
            "high" if risks else "medium",
            policy_version,
            created_at,
            "",
        )
        proposal = ToolProposal(
            **{**proposal.__dict__, "action_digest": proposal.recompute_digest()}
        )
        approval_id = str(uuid.uuid4())
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        expires_at = created_at + ttl if ttl is not None else None
        def insert(db):
            db.execute(
                "INSERT INTO core_tool_proposals VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                tuple(proposal.__dict__.values()),
            )
            db.execute(
                "INSERT INTO core_approval_requests VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    approval_id,
                    task_id,
                    proposal.id,
                    proposal.action_digest,
                    "PENDING",
                    1,
                    created_at,
                    expires_at,
                    "agent_operator",
                    policy_version,
                    None,
                    None,
                    None,
                ),
            )
            return self._get(db, approval_id)

        if connection is not None:
            return insert(connection)
        with self.database.transaction() as db:
            return insert(db)

    def get(self, approval_id):
        with self.database.pool.connection() as connection:
            return self._get(connection, approval_id)

    def list_pending(self):
        with self.database.pool.connection() as connection:
            ids = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM core_approval_requests WHERE state = 'PENDING' ORDER BY created_at"
                ).fetchall()
            ]
            return tuple(self._get(connection, approval_id) for approval_id in ids)

    def approve_once(
        self,
        approval_id,
        *,
        action_digest,
        expected_version,
        operator_principal_id,
        operator_session_id,
        idempotency_key,
    ):
        expired = False
        with self.database.transaction() as connection:
            approval = self._get(connection, approval_id, lock=True)
            row = connection.execute(
                "SELECT * FROM core_execution_records WHERE approval_id = %s",
                (approval_id,),
            ).fetchone()
            if row:
                execution = self._execution(row)
                if execution.idempotency_key == idempotency_key:
                    return execution
                raise CoreError("APPROVAL_ALREADY_RESOLVED")
            if approval.state != "PENDING":
                raise CoreError("APPROVAL_ALREADY_RESOLVED")
            if approval.expires_at is not None and approval.expires_at <= self.clock():
                connection.execute(
                    "UPDATE core_approval_requests SET state = 'EXPIRED', version = version + 1 WHERE id = %s",
                    (approval_id,),
                )
                expired = True
            elif (
                approval.version != expected_version
                or approval.action_digest != action_digest
                or approval.proposal.recompute_digest() != approval.action_digest
            ):
                raise CoreError("APPROVAL_ARGUMENTS_CHANGED")
            else:
                execution = ExecutionRecord(
                    str(uuid.uuid4()),
                    approval.task_id,
                    approval.id,
                    approval.proposal_id,
                    approval.action_digest,
                    idempotency_key,
                    "RESERVED",
                    0,
                    self.clock(),
                )
                connection.execute(
                    "INSERT INTO core_execution_records VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    tuple(execution.__dict__.values()),
                )
                connection.execute(
                    """UPDATE core_approval_requests
                       SET state = 'CONSUMED', version = version + 1,
                           decision = 'APPROVE_ONCE', operator_principal_id = %s,
                           operator_session_id = %s
                       WHERE id = %s AND state = 'PENDING'""",
                    (operator_principal_id, operator_session_id, approval_id),
                )
        if expired:
            raise CoreError("APPROVAL_EXPIRED")
        return execution

    def approve_once_in_transaction(
        self,
        connection,
        approval_id,
        *,
        action_digest,
        expected_version,
        operator_principal_id,
        operator_session_id,
        idempotency_key,
    ):
        approval = self._get(connection, approval_id, lock=True)
        row = connection.execute(
            "SELECT * FROM core_execution_records WHERE approval_id = %s",
            (approval_id,),
        ).fetchone()
        if row:
            execution = self._execution(row)
            if execution.idempotency_key == idempotency_key:
                return execution
            raise CoreError("APPROVAL_ALREADY_RESOLVED")
        if approval.state != "PENDING":
            raise CoreError("APPROVAL_ALREADY_RESOLVED")
        if approval.expires_at is not None and approval.expires_at <= self.clock():
            connection.execute(
                """UPDATE core_approval_requests SET state = 'EXPIRED',
                   version = version + 1 WHERE id = %s""",
                (approval_id,),
            )
            raise CoreError("APPROVAL_EXPIRED")
        if (
            approval.version != expected_version
            or approval.action_digest != action_digest
            or approval.proposal.recompute_digest() != approval.action_digest
        ):
            raise CoreError("APPROVAL_ARGUMENTS_CHANGED")
        execution = ExecutionRecord(
            str(uuid.uuid4()),
            approval.task_id,
            approval.id,
            approval.proposal_id,
            approval.action_digest,
            idempotency_key,
            "RESERVED",
            0,
            self.clock(),
        )
        connection.execute(
            """INSERT INTO core_execution_records
               (id, task_id, approval_id, proposal_id, action_digest,
                idempotency_key, state, attempt, reserved_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            tuple(execution.__dict__.values()),
        )
        connection.execute(
            """UPDATE core_approval_requests
               SET state = 'CONSUMED', version = version + 1,
                   decision = 'APPROVE_ONCE', operator_principal_id = %s,
                   operator_session_id = %s
               WHERE id = %s AND state = 'PENDING'""",
            (operator_principal_id, operator_session_id, approval_id),
        )
        return execution

    def deny(
        self,
        approval_id,
        *,
        action_digest,
        expected_version,
        operator_principal_id,
        operator_session_id,
    ):
        with self.database.transaction() as connection:
            approval = self._get(connection, approval_id, lock=True)
            if approval.state != "PENDING":
                raise CoreError("APPROVAL_ALREADY_RESOLVED")
            if approval.version != expected_version or approval.action_digest != action_digest:
                raise CoreError("APPROVAL_ARGUMENTS_CHANGED")
            connection.execute(
                """UPDATE core_approval_requests
                   SET state = 'DENIED', version = version + 1, decision = 'DENY',
                       operator_principal_id = %s, operator_session_id = %s
                   WHERE id = %s AND state = 'PENDING'""",
                (operator_principal_id, operator_session_id, approval_id),
            )
        return self.get(approval_id)

    def cancel(self, approval_id):
        with self.database.transaction() as connection:
            approval = self._get(connection, approval_id, lock=True)
            if approval.state == "CANCELED":
                return approval
            if approval.state != "PENDING":
                raise CoreError("TASK_NOT_CANCELABLE")
            connection.execute(
                "UPDATE core_approval_requests SET state = 'CANCELED', version = version + 1 WHERE id = %s",
                (approval_id,),
            )
        return self.get(approval_id)

    def authorize_dispatch(self, approval_id, call):
        with self.database.transaction() as connection:
            approval = self._get(connection, approval_id, lock=True)
            row = connection.execute(
                "SELECT * FROM core_execution_records WHERE approval_id = %s FOR UPDATE",
                (approval_id,),
            ).fetchone()
            if row is None or approval.state != "CONSUMED":
                raise CoreError("APPROVAL_REQUIRED")
            execution = self._execution(row)
            if (
                execution.state != "RESERVED"
                or call.id != approval.proposal.tool_call_id
                or call.name != approval.proposal.tool_name
                or approval.proposal.recompute_digest(
                    tool_name=call.name, arguments=call.arguments
                )
                != approval.action_digest
            ):
                raise CoreError("APPROVAL_ARGUMENTS_CHANGED")
            updated = connection.execute(
                """UPDATE core_execution_records
                   SET state = 'DISPATCHED', attempt = attempt + 1, started_at = %s
                   WHERE id = %s AND state = 'RESERVED'""",
                (self.clock(), execution.id),
            )
            if updated.rowcount != 1:
                raise CoreError("EXECUTION_ALREADY_DISPATCHED")
        return ExecutionRecord(
            **{**execution.__dict__, "state": "DISPATCHED", "attempt": execution.attempt + 1}
        )

    def finish_execution(self, execution_id, state, outcome=None, error_code=None):
        with self.database.transaction() as connection:
            updated = connection.execute(
                """UPDATE core_execution_records SET state = %s,
                       finished_at = %s, outcome = %s, error_code = %s
                   WHERE id = %s AND state = 'DISPATCHED'""",
                (
                    state,
                    self.clock(),
                    Jsonb(_value(outcome)) if outcome is not None else None,
                    error_code,
                    execution_id,
                ),
            )
            if updated.rowcount != 1:
                raise CoreError("INVALID_TASK_STATE")

    def execution_for(self, approval_id):
        with self.database.pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM core_execution_records WHERE approval_id = %s",
                (approval_id,),
            ).fetchone()
        return self._execution(row) if row else None

    def execution_outcome(self, approval_id):
        with self.database.pool.connection() as connection:
            row = connection.execute(
                """SELECT state, outcome, error_code FROM core_execution_records
                   WHERE approval_id = %s""",
                (approval_id,),
            ).fetchone()
        return dict(row) if row else None

    def close(self):
        # The application owns and closes the shared pool after CoreAgent.close().
        return None
