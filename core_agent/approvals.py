from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass

from .errors import CoreError


def _json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(scope):
    return "sha256:" + hashlib.sha256(_json(scope).encode()).hexdigest()


def _semantic_arguments(value, key=""):
    normalized = key.lower().replace("-", "_")
    if any(
        marker in normalized
        for marker in ("authorization", "password", "private_key", "secret", "token")
    ):
        if (
            isinstance(value, dict)
            and set(value) == {"secretRef"}
            and isinstance(value["secretRef"], str)
            and value["secretRef"]
        ):
            return value
        raise CoreError("SECRET_REFERENCE_REQUIRED")
    if isinstance(value, dict):
        return {
            item_key: _semantic_arguments(item, item_key)
            for item_key, item in value.items()
            if not item_key.startswith("__runtime_")
        }
    if isinstance(value, list):
        return [_semantic_arguments(item) for item in value]
    if isinstance(value, str) and re.search(
        r"(?i)(authorization\s*:\s*bearer|(?:token|password|secret)\s*=)", value
    ):
        raise CoreError("SECRET_REFERENCE_REQUIRED")
    return value


def _target(arguments, tool_name):
    for key in ("target", "repo", "url", "path", "cwd"):
        if key in arguments:
            return {"type": key, "id": arguments[key]}
    return {"type": "tool", "id": tool_name}


@dataclass(frozen=True)
class ToolProposal:
    id: str
    task_id: str
    context_id: str
    tenant_id: str
    caller_principal_id: str
    tool_call_id: str
    tool_name: str
    tool_version: str
    environment: str
    target_json: str
    arguments_json: str
    side_effect_class: str
    risk_level: str
    policy_version: str
    created_at: float
    action_digest: str

    @property
    def target(self):
        return json.loads(self.target_json)

    @property
    def arguments(self):
        return json.loads(self.arguments_json)

    def scope(self, *, tool_name=None, arguments=None):
        return {
            "schemaVersion": "1",
            "taskId": self.task_id,
            "tenantId": self.tenant_id,
            "callerPrincipalId": self.caller_principal_id,
            "tool": {"name": tool_name or self.tool_name, "version": self.tool_version},
            "environment": self.environment,
            "target": self.target,
            "arguments": (
                self.arguments if arguments is None else _semantic_arguments(arguments)
            ),
            "sideEffectClass": self.side_effect_class,
            "policyVersion": self.policy_version,
        }

    def recompute_digest(self, *, tool_name=None, arguments=None):
        return _digest(self.scope(tool_name=tool_name, arguments=arguments))


@dataclass(frozen=True)
class ApprovalRequest:
    id: str
    task_id: str
    proposal_id: str
    action_digest: str
    state: str
    version: int
    created_at: float
    expires_at: float | None
    required_operator_role: str
    policy_version: str
    proposal: ToolProposal
    decision: str | None = None
    operator_principal_id: str | None = None
    operator_session_id: str | None = None

    @property
    def tool_call_id(self):
        return self.proposal.tool_call_id

    @property
    def tool_name(self):
        return self.proposal.tool_name

    @property
    def argument_digest(self):
        return self.action_digest

    @property
    def risks(self):
        return tuple(self.proposal.side_effect_class.split(","))

    @property
    def arguments(self):
        return self.proposal.arguments

    @property
    def identity(self):
        return self.proposal.caller_principal_id

    @property
    def session_id(self):
        return self.proposal.context_id

    @property
    def status(self):
        return self.state.lower()


@dataclass(frozen=True)
class ExecutionRecord:
    id: str
    task_id: str
    approval_id: str
    proposal_id: str
    action_digest: str
    idempotency_key: str
    state: str
    attempt: int
    reserved_at: float


class ApprovalManager:
    """SQLite-backed local approval and single-use execution reservation store."""

    def __init__(self, path=":memory:", *, ttl_seconds=7200, clock=time.time):
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._lock = threading.RLock()
        self._closed = False
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        schema_version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if schema_version not in {0, 1}:
            raise CoreError("APPROVAL_SCHEMA_UNSUPPORTED")
        self._db.executescript(
            """
            PRAGMA foreign_keys = ON;
            PRAGMA busy_timeout = 5000;
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS tool_proposals (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                context_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                caller_principal_id TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                tool_version TEXT NOT NULL,
                environment TEXT NOT NULL,
                target_json TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                side_effect_class TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                created_at REAL NOT NULL,
                action_digest TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS approval_requests (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL UNIQUE REFERENCES tool_proposals(id),
                action_digest TEXT NOT NULL,
                state TEXT NOT NULL,
                version INTEGER NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL,
                required_operator_role TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                decision TEXT,
                operator_principal_id TEXT,
                operator_session_id TEXT
            );
            CREATE TABLE IF NOT EXISTS execution_records (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                approval_id TEXT NOT NULL UNIQUE REFERENCES approval_requests(id),
                proposal_id TEXT NOT NULL REFERENCES tool_proposals(id),
                action_digest TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                reserved_at REAL NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS tool_proposals_immutable
            BEFORE UPDATE ON tool_proposals
            BEGIN
                SELECT RAISE(ABORT, 'tool proposal is immutable');
            END;
            PRAGMA user_version = 1;
            """
        )

    def _transaction(self):
        class Transaction:
            def __init__(inner, manager):
                inner.manager = manager

            def __enter__(inner):
                inner.manager._lock.acquire()
                try:
                    inner.manager._db.execute("BEGIN IMMEDIATE")
                except Exception:
                    inner.manager._lock.release()
                    raise
                return inner.manager._db

            def __exit__(inner, exc_type, _exc, _tb):
                try:
                    inner.manager._db.execute("ROLLBACK" if exc_type else "COMMIT")
                finally:
                    inner.manager._lock.release()

        return Transaction(self)

    @staticmethod
    def _proposal(row):
        return ToolProposal(**dict(row))

    def _get(self, db, approval_id):
        row = db.execute(
            "SELECT * FROM approval_requests WHERE id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            raise CoreError("APPROVAL_NOT_FOUND")
        proposal_row = db.execute(
            "SELECT * FROM tool_proposals WHERE id = ?", (row["proposal_id"],)
        ).fetchone()
        values = dict(row)
        values["proposal"] = self._proposal(proposal_row)
        return ApprovalRequest(**values)

    @staticmethod
    def _execution(row):
        return ExecutionRecord(**dict(row))

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
    ):
        created_at = self.clock()
        proposal_id = str(uuid.uuid4())
        target = _target(call.arguments, call.name)
        side_effect_class = ",".join(sorted(risks)) if risks else "protected_action"
        proposal = ToolProposal(
            proposal_id,
            task_id,
            context_id,
            tenant_id,
            caller_principal_id,
            call.id,
            call.name,
            "1",
            environment,
            _json(target),
            _json(_semantic_arguments(call.arguments)),
            side_effect_class,
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
        with self._transaction() as db:
            db.execute(
                "INSERT INTO tool_proposals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(proposal.__dict__.values()),
            )
            db.execute(
                "INSERT INTO approval_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
        return self.get(approval_id)

    def get(self, approval_id):
        with self._lock:
            return self._get(self._db, approval_id)

    def list_pending(self):
        with self._lock:
            ids = [
                row["id"]
                for row in self._db.execute(
                    "SELECT id FROM approval_requests WHERE state = 'PENDING' ORDER BY created_at"
                )
            ]
            return tuple(self._get(self._db, approval_id) for approval_id in ids)

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
        with self._transaction() as db:
            approval = self._get(db, approval_id)
            existing = db.execute(
                "SELECT * FROM execution_records WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if existing is not None:
                execution = self._execution(existing)
                if execution.idempotency_key == idempotency_key:
                    return execution
                raise CoreError("APPROVAL_ALREADY_RESOLVED")
            if approval.state != "PENDING":
                raise CoreError("APPROVAL_ALREADY_RESOLVED")
            if approval.expires_at is not None and approval.expires_at <= self.clock():
                db.execute(
                    "UPDATE approval_requests SET state = 'EXPIRED', version = version + 1 WHERE id = ?",
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
                db.execute(
                    "INSERT INTO execution_records VALUES (?,?,?,?,?,?,?,?,?)",
                    tuple(execution.__dict__.values()),
                )
                db.execute(
                    """
                    UPDATE approval_requests
                    SET state = 'CONSUMED', version = version + 1, decision = 'APPROVE_ONCE',
                        operator_principal_id = ?, operator_session_id = ?
                    WHERE id = ? AND state = 'PENDING'
                    """,
                    (operator_principal_id, operator_session_id, approval_id),
                )
        if expired:
            raise CoreError("APPROVAL_EXPIRED")
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
        with self._transaction() as db:
            approval = self._get(db, approval_id)
            if approval.state != "PENDING":
                raise CoreError("APPROVAL_ALREADY_RESOLVED")
            if (
                approval.version != expected_version
                or approval.action_digest != action_digest
            ):
                raise CoreError("APPROVAL_ARGUMENTS_CHANGED")
            db.execute(
                """
                UPDATE approval_requests
                SET state = 'DENIED', version = version + 1, decision = 'DENY',
                    operator_principal_id = ?, operator_session_id = ?
                WHERE id = ? AND state = 'PENDING'
                """,
                (operator_principal_id, operator_session_id, approval_id),
            )
        return self.get(approval_id)

    def cancel(self, approval_id):
        with self._transaction() as db:
            approval = self._get(db, approval_id)
            if approval.state == "CANCELED":
                return approval
            if approval.state != "PENDING":
                raise CoreError("TASK_NOT_CANCELABLE")
            db.execute(
                "UPDATE approval_requests SET state = 'CANCELED', version = version + 1 WHERE id = ?",
                (approval_id,),
            )
        return self.get(approval_id)

    def authorize_dispatch(self, approval_id, call):
        with self._transaction() as db:
            approval = self._get(db, approval_id)
            execution_row = db.execute(
                "SELECT * FROM execution_records WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if execution_row is None or approval.state != "CONSUMED":
                raise CoreError("APPROVAL_REQUIRED")
            execution = self._execution(execution_row)
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
            updated = db.execute(
                "UPDATE execution_records SET state = 'DISPATCHED', attempt = attempt + 1 WHERE id = ? AND state = 'RESERVED'",
                (execution.id,),
            )
            if updated.rowcount != 1:
                raise CoreError("EXECUTION_ALREADY_DISPATCHED")
            return ExecutionRecord(
                **{**execution.__dict__, "state": "DISPATCHED", "attempt": 1}
            )

    def finish_execution(self, execution_id, state, outcome=None, error_code=None):
        with self._transaction() as db:
            updated = db.execute(
                "UPDATE execution_records SET state = ? WHERE id = ? AND state = 'DISPATCHED'",
                (state, execution_id),
            )
            if updated.rowcount != 1:
                raise CoreError("INVALID_TASK_STATE")

    def execution_for(self, approval_id):
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM execution_records WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            return self._execution(row) if row else None

    def close(self):
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True


@dataclass(frozen=True)
class ApproveAllControlPlane:
    operator_principal_id: str = "local-operator-stub"
    operator_session_id: str = "approve-all-development-stub"
    automatic: bool = True

    def approve(self, manager, approval):
        return manager.approve_once(
            approval.id,
            action_digest=approval.action_digest,
            expected_version=approval.version,
            operator_principal_id=self.operator_principal_id,
            operator_session_id=self.operator_session_id,
            idempotency_key=(
                f"{approval.task_id}:{approval.id}:{approval.action_digest}"
            ),
        )
