from __future__ import annotations

import dataclasses
import threading
import time
import uuid
import copy
from contextlib import nullcontext

from psycopg.types.json import Jsonb

from .errors import CoreError, ExecutionNotStarted
from .observability import TraceContext
from .tasks import (
    BackgroundTask, Notification, RemoteTaskClaim, REMOTE_KIND, _remote_progress,
    _remote_contract, _remote_checkpoint, _initial_remote_checkpoint, _advance_remote_checkpoint,
    _remote_expired, _remote_due, _remote_text, remote_timeout_result, _remote_file_binding, remote_result_projection,
    _remote_cancelled_outcome, _remote_reply_status,
    _pending_remote_input, _queue_remote_reply, _new_remote_input, _remote_result_batches,
    _remote_context_key, _remote_context_matches, _remote_context_unknown, _remote_context_checkpoint,
)
from .workflow import SuspendedRun
from .response_files import with_prefetched_artifacts
from .peer_conversations import merge_observation, OBSERVATION_TTL


TERMINAL = {"completed", "failed", "canceled"}
DEFAULT_TASK_LEASE_TTL = 600.0


def _value(value):
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, dict):
        return {str(key): _value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class PostgresMailbox:
    def __init__(self, scheduler, owner_id, tenant_id):
        self.scheduler = scheduler
        self.owner_id = owner_id
        self.tenant_id = tenant_id

    def poll(self):
        with self.scheduler.database.pool.connection() as connection:
            rows = connection.execute(
                """SELECT id, owner_run_id AS owner_id, task_id, kind,
                          revision, payload
                   FROM core_notifications
                   WHERE tenant_id = %s AND owner_run_id = %s
                     AND acknowledged_at IS NULL ORDER BY created_at""",
                (self.tenant_id, self.owner_id),
            ).fetchall()
        return tuple(Notification(**row) for row in rows)

    def ack(self, notification_id):
        with self.scheduler.database.transaction() as connection:
            updated = connection.execute(
                """UPDATE core_notifications SET acknowledged_at = %s
                   WHERE id = %s AND tenant_id = %s AND owner_run_id = %s
                     AND acknowledged_at IS NULL""",
                (time.time(), notification_id, self.tenant_id, self.owner_id),
            )
            if updated.rowcount != 1:
                raise CoreError("TASK_NOT_FOUND")


class PostgresTaskScheduler:
    """Durable task registry/mailbox with in-process execution workers."""

    def __init__(
        self,
        database,
        telemetry=None,
        clock=time.time,
        task_lease_ttl=DEFAULT_TASK_LEASE_TTL,
        task_lease_heartbeat_interval=None,
    ):
        self.database = database
        self.telemetry = telemetry
        self.clock = clock
        self.task_lease_ttl = float(task_lease_ttl)
        self.task_lease_heartbeat_interval = float(
            task_lease_heartbeat_interval
            if task_lease_heartbeat_interval is not None
            else self.task_lease_ttl / 3
        )
        if (
            self.task_lease_ttl <= 0
            or self.task_lease_heartbeat_interval <= 0
            or self.task_lease_heartbeat_interval >= self.task_lease_ttl
        ):
            raise CoreError("CONFIG_INVALID", "invalid task lease timing")
        self._worker_id = str(uuid.uuid4())
        self._handlers = {}
        self._workflow_outcome = None
        self._cancel_events = {}
        self._cancel_callbacks = {}
        self._conditions = {}
        self._threads = set()
        self._remote_claims = set()
        self._lock = threading.Lock()
        self._closed = False
        self.active_compute_waiters = 0

    def register(self, kind, handler):
        if kind in self._handlers:
            raise CoreError("CONFIG_CONFLICT", f"duplicate task handler: {kind}")
        self._handlers[kind] = handler

    def start_remote(self, contract, *, owner_id, tenant_id, task_id, required=True, admission=None, trace_context=None):
        _remote_contract(contract, tenant_id, owner_id)
        _remote_text(task_id)
        if REMOTE_KIND not in self._handlers:
            raise CoreError("INVALID_TASK_STATE")
        return self.start(None, owner_id=owner_id, tenant_id=tenant_id, task_id=task_id, required=required,
                          kind=REMOTE_KIND, contract=copy.deepcopy(contract), recoverable=True, mutating=True,
                          admission=admission, trace_context=trace_context)

    @staticmethod
    def _remote_now(connection):
        return connection.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision AS now").fetchone()["now"]

    def is_remote(self, task_id, *, owner_id, tenant_id):
        with self.database.pool.connection() as connection:
            row = connection.execute("SELECT kind FROM core_background_tasks WHERE id = %s AND tenant_id = %s AND owner_run_id = %s",
                                     (task_id, tenant_id, owner_id)).fetchone()
        if row is None:
            raise CoreError("TASK_NOT_FOUND")
        return row["kind"] == REMOTE_KIND

    def remote_progress(self, *, owner_id, tenant_id):
        values = []
        with self.database.transaction() as connection:
            rows = connection.execute("""SELECT * FROM core_background_tasks
                WHERE owner_run_id = %s AND tenant_id = %s AND kind = %s AND state = 'working' ORDER BY id""",
                (owner_id, tenant_id, REMOTE_KIND)).fetchall()
            now = self._remote_now(connection)
            for row in rows:
                _remote_contract(row["contract"], tenant_id, owner_id)
                _remote_checkpoint(row["checkpoint"])
                if _remote_expired(row["checkpoint"], now):
                    continue
                progress = _remote_progress(row["result"], row["contract"], row["checkpoint"], None)
                if progress is not None:
                    values.append({"task_id": row["id"], "revision": row["revision"],
                                   "agent_name": progress["agent_name"], "remote_state": progress["remote_state"]})
        return tuple(values)

    def remote_input(self, task_id, *, owner_id, tenant_id):
        with self.database.transaction() as connection:
            row = connection.execute("""SELECT * FROM core_background_tasks
                WHERE id=%s AND tenant_id=%s AND owner_run_id=%s AND kind=%s""",
                (task_id, tenant_id, owner_id, REMOTE_KIND)).fetchone()
            if row is None:
                raise CoreError("TASK_NOT_FOUND")
            return _pending_remote_input(row["contract"], row["checkpoint"], row["state"],
                                         row["cancel_requested"], self._remote_now(connection))

    def remote_reply_status(self, task_id, input_id, *, owner_id, tenant_id):
        with self.database.transaction() as connection:
            row = connection.execute("""SELECT * FROM core_background_tasks
                WHERE id=%s AND tenant_id=%s AND owner_run_id=%s AND kind=%s""",
                (task_id, tenant_id, owner_id, REMOTE_KIND)).fetchone()
            if row is None:
                raise CoreError("TASK_NOT_FOUND")
            _remote_checkpoint(row["checkpoint"], row["contract"])
            return _remote_reply_status(row["checkpoint"], input_id, row["state"], row["cancel_requested"], self._remote_now(connection))

    def queue_remote_reply(self, task_id, reply, *, owner_id, tenant_id, admission=None):
        def commit_reply():
            with self.database.transaction() as connection:
                row = connection.execute("""SELECT * FROM core_background_tasks
                    WHERE id=%s AND tenant_id=%s AND owner_run_id=%s AND kind=%s FOR UPDATE""",
                    (task_id, tenant_id, owner_id, REMOTE_KIND)).fetchone()
                if row is None:
                    raise CoreError("TASK_NOT_FOUND")
                now = self._remote_now(connection)
                checkpoint, changed = _queue_remote_reply(row["contract"], row["checkpoint"], row["state"],
                                                         row["cancel_requested"], now, reply)
                if admission is not None:
                    admission(connection)
                    now = self._remote_now(connection)
                    checkpoint, changed = _queue_remote_reply(row["contract"], row["checkpoint"], row["state"],
                                                             row["cancel_requested"], now, reply)
                if changed:
                    result = {key: value for key, value in (row["result"] or {}).items() if key not in {"input_id", "question"}}
                    row = connection.execute("""UPDATE core_background_tasks SET checkpoint=%s, result=%s,
                        revision=revision+1, updated_at=%s WHERE id=%s AND tenant_id=%s RETURNING *""",
                        (Jsonb(checkpoint), Jsonb(result), now, task_id, tenant_id)).fetchone()
            return row, changed

        row, changed = with_prefetched_artifacts(
            getattr(self.database, "response_files_service", None), commit_reply)
        if changed:
            self._notify_remote(task_id)
        return self._task(row)

    def observe_remote(self, task_id, *, owner_id, tenant_id, visible):
        if type(visible) is not bool:
            raise CoreError("REQUEST_INVALID")
        with self.database.transaction() as connection:
            row = connection.execute("""SELECT * FROM core_background_tasks
                WHERE id=%s AND tenant_id=%s AND owner_run_id=%s AND kind=%s FOR UPDATE""",
                (task_id, tenant_id, owner_id, REMOTE_KIND)).fetchone()
            if row is None:
                raise CoreError("TASK_NOT_FOUND")
            now = self._remote_now(connection)
            _remote_contract(row["contract"], tenant_id, owner_id)
            _remote_checkpoint(row["checkpoint"])
            if visible and row["state"] not in TERMINAL and not _remote_expired(row["checkpoint"], now):
                expires = now + OBSERVATION_TTL
                checkpoint = copy.deepcopy(row["checkpoint"])
                if checkpoint["remote_task_id"] is not None:
                    checkpoint["next_poll_at"] = min(checkpoint["next_poll_at"] or now, now + 15, checkpoint["deadline"])
                connection.execute("UPDATE core_background_tasks SET remote_observed_until=%s, checkpoint=%s WHERE id=%s AND tenant_id=%s",
                                   (expires, Jsonb(checkpoint), task_id, tenant_id))
                return expires
            return row.get("remote_observed_until")

    def _remote_claim_locked(self, connection, claim, *, terminal=False):
        if self._closed or not isinstance(claim, RemoteTaskClaim) or not isinstance(claim.token, str) or not claim.token:
            raise CoreError("LEASE_LOST")
        row = connection.execute("""SELECT * FROM core_background_tasks WHERE id = %s AND tenant_id = %s
                                    AND owner_run_id = %s AND kind = %s FOR UPDATE""",
                                 (claim.task_id, claim.tenant_id, claim.owner_id, REMOTE_KIND)).fetchone()
        if row is None:
            raise CoreError("TASK_NOT_FOUND")
        _remote_contract(row["contract"], claim.tenant_id, claim.owner_id)
        _remote_checkpoint(row["checkpoint"], row["contract"])
        now = self._remote_now(connection)
        if terminal and row["state"] in TERMINAL:
            return row, now
        if (row["state"] != "working" or row["claim_owner"] != self._worker_id or row["claim_token"] != claim.token
                or (row["claim_expires_at"] or 0) <= now):
            raise CoreError("LEASE_LOST")
        return row, now

    def _remote_finish_locked(self, connection, row, checkpoint, outcome, now):
        checkpoint = {**checkpoint, "next_poll_at": None}
        outcome = (outcome[0], _remote_result_batches(outcome[1], checkpoint), outcome[2])
        connection.execute("UPDATE core_background_tasks SET checkpoint = %s WHERE id = %s AND tenant_id = %s",
                           (Jsonb(checkpoint), row["id"], row["tenant_id"]))
        return self._finish_locked(connection, {**row, "checkpoint": checkpoint}, *outcome, now)

    def _remote_context_locked(self, connection, contract):
        key = _remote_context_key(contract)
        if key is None:
            return None
        where = "tenant_id=%s AND owner_id=%s AND context_id=%s AND peer_id=%s AND generation=%s AND binding=%s"
        found = connection.execute("SELECT * FROM core_remote_context_bindings WHERE " + where + " FOR UPDATE", key).fetchone()
        if found is not None:
            return found
        context_id, active, blocked = None, None, False
        # Foreign jobs are read without row locks: all writers lock their own job before its binding.
        candidates = connection.execute("""SELECT * FROM core_background_tasks
            WHERE tenant_id=%s AND kind=%s AND contract->>'peer_id'=%s
              AND contract->>'url'=%s AND contract->>'binding'=%s
              AND COALESCE(contract->'conversation'->>'owner_id',contract->'caller_scope'->>'owner_id')=%s
              AND COALESCE(contract->'conversation'->>'context_id',contract->'caller_scope'->>'context_id')=%s
              AND COALESCE(contract->'conversation'->>'generation',contract->>'peer_revision')=%s
              AND checkpoint->>'send_started'='true'
            ORDER BY created_at DESC,id DESC LIMIT 101""",
            (contract["tenant_id"], REMOTE_KIND, contract["peer_id"], contract["url"], contract["binding"], key[1], key[2], str(key[4]))).fetchall()
        for previous in candidates[:100]:
            if not _remote_context_matches(contract, previous["contract"], previous["checkpoint"]):
                continue
            if previous["state"] not in TERMINAL:
                active = previous["id"]
            else:
                blocked = _remote_context_unknown(previous["checkpoint"],
                    (previous["state"], previous["result"], previous["error_code"]))
            context_id = previous["checkpoint"]["remote_context_id"]
            if context_id is None and active is None and not blocked:
                continue
            break
        else:
            if len(candidates) > 100:
                raise CoreError("CHECKPOINT_INVALID")
        connection.execute("""INSERT INTO core_remote_context_bindings
            (tenant_id,owner_id,context_id,peer_id,generation,binding,remote_context_id,active_operation_id,blocked)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""", (*key, context_id, active, blocked))
        return connection.execute("SELECT * FROM core_remote_context_bindings WHERE " + where + " FOR UPDATE", key).fetchone()

    @staticmethod
    def _save_remote_context(connection, contract, binding):
        connection.execute("""UPDATE core_remote_context_bindings
            SET remote_context_id=%s,active_operation_id=%s,blocked=%s
            WHERE tenant_id=%s AND owner_id=%s AND context_id=%s AND peer_id=%s AND generation=%s AND binding=%s""",
            (binding["remote_context_id"], binding["active_operation_id"], binding["blocked"], *_remote_context_key(contract)))

    def _reserve_remote_context(self, connection, row, now):
        binding = self._remote_context_locked(connection, row["contract"])
        if binding is None or row["cancel_requested"] and not row["checkpoint"]["send_started"]:
            return True
        active = binding["active_operation_id"]
        if active not in (None, row["id"]):
            previous = connection.execute("SELECT * FROM core_background_tasks WHERE tenant_id=%s AND id=%s",
                (row["tenant_id"], active)).fetchone()
            if previous is None or not _remote_context_matches(row["contract"], previous["contract"], previous["checkpoint"]):
                binding["blocked"] = True
            else:
                previous_context = previous["checkpoint"]["remote_context_id"]
                if previous_context is not None:
                    if binding["remote_context_id"] not in (None, previous_context):
                        binding["blocked"] = True
                    else:
                        binding["remote_context_id"] = previous_context
                if previous["state"] not in TERMINAL and not binding["blocked"]:
                    self._save_remote_context(connection, row["contract"], binding)
                    return False
                if previous["state"] in TERMINAL:
                    binding["blocked"] |= _remote_context_unknown(previous["checkpoint"],
                        (previous["state"], previous["result"], previous["error_code"]))
                    binding["active_operation_id"] = None
        if binding["blocked"]:
            self._save_remote_context(connection, row["contract"], binding)
            self._remote_finish_locked(connection, row, row["checkpoint"],
                ("failed", {"agent_name": row["contract"]["peer_name"], "reason": "remote_context_unresolved",
                            "message": "Исход предыдущего поручения неизвестен. Перед новой отправкой в этот разговор требуется сверка с контрагентом.",
                            "remote_outcome": "not_dispatched"}, "REMOTE_CONTEXT_UNRESOLVED"), now)
            return False
        binding["active_operation_id"] = row["id"]
        self._save_remote_context(connection, row["contract"], binding)
        return True

    def _notify_remote(self, task_id):
        with self._condition(task_id):
            self._condition(task_id).notify_all()

    def read_remote_claim(self, claim):
        expired = False
        with self.database.transaction() as connection:
            row, now = self._remote_claim_locked(connection, claim)
            if _remote_expired(row["checkpoint"], now):
                self._remote_finish_locked(connection, row, row["checkpoint"],
                    ("failed", remote_timeout_result(row["contract"]), "REMOTE_OPERATION_TIMEOUT"), now)
                expired = True
            else:
                result = {"contract": copy.deepcopy(row["contract"]), "checkpoint": copy.deepcopy(row["checkpoint"]),
                          "cancel_requested": row["cancel_requested"], "now": now, "revision": row["revision"],
                          "observed_until": row.get("remote_observed_until")}
        if expired:
            self._notify_remote(claim.task_id)
            raise CoreError("LEASE_LOST")
        return result

    def commit_remote_claim(self, claim, *, expected_revision, checkpoint, outcome=None, progress=None,
                            prepared_file_batch=None, conversation_observation=None):
        if prepared_file_batch is not None:
            return self._commit_remote_files(claim, expected_revision, checkpoint, outcome, progress, prepared_file_batch, conversation_observation)
        if isinstance(outcome, tuple) and len(outcome) > 1 and isinstance(outcome[1], dict) and {"file_batch_id", "file_batch_ids"} & outcome[1].keys():
            raise CoreError("CHECKPOINT_INVALID")
        with self.database.transaction() as connection:
            row, now = self._remote_claim_locked(connection, claim, terminal=True)
            if row["state"] in TERMINAL:
                return self._task(row)
            if _remote_expired(row["checkpoint"], now):
                outcome = ("failed", remote_timeout_result(row["contract"]), "REMOTE_OPERATION_TIMEOUT")
                checkpoint = row["checkpoint"]
            else:
                if type(expected_revision) is not int or row["revision"] != expected_revision:
                    raise CoreError("SESSION_CONFLICT")
                if row["contract"]["version"] == 3 and row["cancel_requested"]:
                    conversation_observation, progress = None, None
                    reply = checkpoint.get("reply")
                    pending = row["checkpoint"]["input_request"]
                    checkpoint = {**checkpoint, "input_request": None if reply and reply["status"] == "confirmed"
                                  and (pending is None or reply["input_id"] == pending["input_id"]) else pending}
                outcome = _remote_cancelled_outcome(row["contract"], outcome, row["cancel_requested"])
                context_binding = self._remote_context_locked(connection, row["contract"])
                checkpoint = _remote_context_checkpoint(context_binding, row["id"], row["checkpoint"], checkpoint)
                checkpoint = _advance_remote_checkpoint(row["checkpoint"], checkpoint, row["contract"], now, row["cancel_requested"], outcome)
                progress = _remote_progress(progress, row["contract"], checkpoint, outcome)
                if context_binding is not None and checkpoint["remote_context_id"] is not None:
                    context_binding["remote_context_id"] = checkpoint["remote_context_id"]
                    self._save_remote_context(connection, row["contract"], context_binding)
            new_input = outcome is None and _new_remote_input(row["checkpoint"], checkpoint)
            if not _remote_expired(row["checkpoint"], now):
                conversation = merge_observation(row.get("remote_conversation"), conversation_observation, now)
                connection.execute("UPDATE core_background_tasks SET remote_conversation=%s WHERE id=%s AND tenant_id=%s",
                                   (Jsonb(conversation), claim.task_id, claim.tenant_id))
                if outcome is None and (row.get("remote_observed_until") or 0) > now and checkpoint["remote_task_id"]:
                    checkpoint["next_poll_at"] = min(checkpoint["next_poll_at"] or now, now + 15, checkpoint["deadline"])
            if outcome is not None:
                row = self._remote_finish_locked(connection, row, checkpoint, outcome, now)
            else:
                row = connection.execute("""UPDATE core_background_tasks SET checkpoint = %s, result = %s, revision = revision + 1, updated_at = %s
                                            WHERE id = %s AND tenant_id = %s RETURNING *""",
                                         (Jsonb(checkpoint), Jsonb(_remote_result_batches(progress if progress is not None else row["result"], checkpoint)) if progress is not None or row["result"] is not None else None,
                                          now, claim.task_id, claim.tenant_id)).fetchone()
                if new_input:
                    self._deliver_notification(connection, row, "task.input-required", row["revision"],
                        {"task_id": row["id"], "input_id": checkpoint["input_request"]["input_id"]}, now)
        if outcome is not None or new_input:
            self._notify_remote(claim.task_id)
        return self._task(row)

    def _commit_remote_files(self, claim, expected_revision, checkpoint, outcome, progress, batch, conversation_observation):
        service = getattr(self, "chat_file_service", None)
        if service is None or getattr(service.store, "database", None) is not self.database:
            raise CoreError("FILE_ADMISSION_TRANSACTION_REQUIRED")
        if not isinstance(claim, RemoteTaskClaim):
            raise CoreError("LEASE_LOST")
        new_input = False
        with self.database.transaction() as connection:
            initial = connection.execute("""SELECT contract FROM core_background_tasks
                WHERE id=%s AND tenant_id=%s AND owner_run_id=%s AND kind=%s""",
                (claim.task_id, claim.tenant_id, claim.owner_id, REMOTE_KIND)).fetchone()
            if initial is None:
                raise CoreError("TASK_NOT_FOUND")
            contract = initial["contract"]
            _remote_contract(contract, claim.tenant_id, claim.owner_id)
            binding = _remote_file_binding(contract, batch, outcome, claim.task_id, checkpoint)
            scope = contract["caller_scope"]
            with service.caller_scope(binding, task_id=scope["task_id"], run_id=scope["run_id"], connection=connection):
                row, now = self._remote_claim_locked(connection, claim, terminal=True)
                if row["state"] in TERMINAL:
                    return self._task(row)
                if row["contract"] != contract or type(expected_revision) is not int or row["revision"] != expected_revision:
                    raise CoreError("SESSION_CONFLICT")
                if _remote_expired(row["checkpoint"], now):
                    row = self._remote_finish_locked(connection, row, row["checkpoint"],
                        ("failed", remote_timeout_result(contract), "REMOTE_OPERATION_TIMEOUT"), now)
                else:
                    if row["cancel_requested"] or row["checkpoint"]["cancel_started"]:
                        raise CoreError("CANCEL_REQUESTED")
                    context_binding = self._remote_context_locked(connection, contract)
                    checkpoint = _remote_context_checkpoint(context_binding, row["id"], row["checkpoint"], checkpoint)
                    updated = _advance_remote_checkpoint(row["checkpoint"], checkpoint, contract, now, False, outcome,
                        file_batch_id=batch["batch_id"] if checkpoint["version"] == 3 else None)
                    _remote_progress(progress, contract, updated, outcome)
                    if not updated["send_started"]:
                        raise CoreError("CHECKPOINT_INVALID")
                    service.bind(batch["batch_id"], binding, task_id=scope["task_id"], run_id=scope["run_id"],
                        actor_id=batch["actor_id"], message_id=batch["message_id"], request_digest=batch["request_digest"],
                        lease_token=batch["lease_token"], connection=connection)
                    with service.caller_scope(binding, task_id=scope["task_id"], run_id=scope["run_id"], connection=connection):
                        fresh, now = self._remote_claim_locked(connection, claim)
                        if fresh["contract"] != contract or fresh["revision"] != expected_revision:
                            raise CoreError("SESSION_CONFLICT")
                        if _remote_expired(updated, now):
                            raise CoreError("LEASE_LOST")
                        if fresh["cancel_requested"]:
                            raise CoreError("CANCEL_REQUESTED")
                        if context_binding is not None and updated["remote_context_id"] is not None:
                            context_binding["remote_context_id"] = updated["remote_context_id"]
                            self._save_remote_context(connection, contract, context_binding)
                        conversation = merge_observation(fresh.get("remote_conversation"), conversation_observation, now)
                        connection.execute("UPDATE core_background_tasks SET remote_conversation=%s WHERE id=%s AND tenant_id=%s",
                                           (Jsonb(conversation), claim.task_id, claim.tenant_id))
                        if outcome is None:
                            new_input = _new_remote_input(fresh["checkpoint"], updated)
                            row = connection.execute("""UPDATE core_background_tasks
                                SET checkpoint=%s, result=%s, revision=revision+1, updated_at=%s
                                WHERE id=%s AND tenant_id=%s RETURNING *""",
                                (Jsonb(updated), Jsonb(_remote_result_batches(progress, updated)),
                                 now, claim.task_id, claim.tenant_id)).fetchone()
                            if new_input:
                                self._deliver_notification(connection, row, "task.input-required", row["revision"],
                                    {"task_id": row["id"], "input_id": updated["input_request"]["input_id"]}, now)
                        else:
                            row = self._remote_finish_locked(connection, fresh, updated,
                                ("completed", {**outcome[1], **({} if updated["version"] == 3 else {"file_batch_id": batch["batch_id"]})}, None), now)
        if row["state"] in TERMINAL or new_input:
            self._notify_remote(claim.task_id)
        return self._task(row)

    def expire_remote(self, limit=100, *, tenant_id=None):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise CoreError("CONFIG_INVALID")
        identities = []
        with self.database.transaction() as connection:
            tenant_filter = " AND tenant_id = %s" if tenant_id is not None else ""
            values = [REMOTE_KIND, tenant_id] if tenant_id is not None else [REMOTE_KIND]
            rows = connection.execute("""SELECT * FROM core_background_tasks WHERE kind = %s
                AND state NOT IN ('completed','failed','canceled')
                AND (checkpoint->>'deadline')::double precision <= EXTRACT(EPOCH FROM clock_timestamp())""" + tenant_filter
                + " ORDER BY (checkpoint->>'deadline')::double precision, id LIMIT %s FOR UPDATE SKIP LOCKED",
                (*values, limit)).fetchall()
            for row in rows:
                _remote_contract(row["contract"], row["tenant_id"], row["owner_run_id"])
                _remote_checkpoint(row["checkpoint"])
                now = self._remote_now(connection)
                if _remote_expired(row["checkpoint"], now):
                    self._remote_finish_locked(connection, row, row["checkpoint"],
                        ("failed", remote_timeout_result(row["contract"]), "REMOTE_OPERATION_TIMEOUT"), now)
                    identities.append(row["id"])
        for identity in identities:
            self._notify_remote(identity)
        return len(identities)

    def mailbox(self, owner_id, tenant_id="default"):
        return PostgresMailbox(self, owner_id, tenant_id)

    @staticmethod
    def _task(row):
        error = CoreError(row["error_code"]) if row.get("error_code") else None
        return BackgroundTask(
            row["id"],
            row["owner_run_id"],
            row["required"],
            row["state"],
            row["result"],
            error,
            row["revision"],
        )

    def _condition(self, task_id):
        with self._lock:
            return self._conditions.setdefault(task_id, threading.Condition())

    def _cancel_event(self, task_id):
        with self._lock:
            return self._cancel_events.setdefault(task_id, threading.Event())

    def _signal_cancel(self, task_id, tenant_id):
        self._cancel_event(task_id).set()
        with self._lock:
            callback = self._cancel_callbacks.pop((tenant_id, task_id), None)
        if callback is not None:
            try:
                callback()
            except ExecutionNotStarted as error:
                # The durable cancel is already committed; a callback cannot undo it.
                raise CoreError(error.code, diagnostics=error.diagnostics) from error

    def start(
        self,
        function,
        *,
        owner_id,
        task_id=None,
        required=False,
        accepts_cancel_event=False,
        trace_context=None,
        kind,
        contract,
        recoverable=False,
        tenant_id="default",
        continue_trace=False,
        admission=None,
        on_cancel=None,
        mutating=True,
    ):
        if self._closed:
            raise CoreError("INVALID_TASK_STATE")
        if not kind or not isinstance(contract, dict):
            raise CoreError("CONFIG_INVALID", "durable task contract is required")
        linked_context = trace_context
        if self.telemetry and linked_context is None:
            with self.telemetry.span("core_agent.task.submit") as submission:
                linked_context = submission.context
        now = self.clock()
        task_id = task_id or str(uuid.uuid4())
        if not isinstance(task_id, str) or not task_id:
            raise CoreError("CONFIG_INVALID", "task_id must be a non-empty string")
        task = BackgroundTask(task_id, owner_id, required)
        stored_contract = dict(contract)
        if continue_trace and linked_context is not None:
            stored_contract["_trace_parent"] = {
                "trace_id": linked_context.trace_id,
                "span_id": linked_context.span_id,
                "trace_flags": linked_context.trace_flags,
            }
        def commit_admission():
            with self.database.transaction() as connection:
                connection.execute(
                    """INSERT INTO core_background_tasks
                       (id, owner_run_id, tenant_id, kind, state, required,
                        recoverable, contract, mutating, created_at, updated_at, checkpoint)
                       VALUES (%s, %s, %s, %s, 'submitted', %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        task.id,
                        owner_id,
                        tenant_id,
                        kind,
                        required,
                        recoverable,
                        Jsonb(_value(stored_contract)),
                        bool(mutating),
                        now,
                        now,
                        Jsonb(_initial_remote_checkpoint(contract)) if kind == REMOTE_KIND else None,
                    ),
                )
                if admission is not None:
                    admission(connection)
                initial_claim = self._claim(
                    task.id, tenant_id, allow_working=False, connection=connection
                )
            return initial_claim

        initial_claim = with_prefetched_artifacts(
            getattr(self.database, "response_files_service", None), commit_admission)
        if on_cancel is not None:
            with self._lock:
                self._cancel_callbacks[(tenant_id, task.id)] = on_cancel
        try:
            self._launch(
                task.id,
                tenant_id,
                function,
                accepts_cancel_event=accepts_cancel_event,
                trace_context=linked_context,
                continue_trace=continue_trace,
                initial_claim=initial_claim,
            )
        except BaseException:
            with self._lock:
                self._cancel_callbacks.pop((tenant_id, task.id), None)
            self._release_claim(task.id, tenant_id, initial_claim[0], state="submitted")
            raise
        return task

    def _claim(self, task_id, tenant_id, *, allow_working, connection=None):
        token = str(uuid.uuid4())
        with (
            self.database.transaction() if connection is None else nullcontext(connection)
        ) as connection:
            row = connection.execute(
                """SELECT * FROM core_background_tasks
                   WHERE id = %s AND tenant_id = %s FOR UPDATE""",
                (task_id, tenant_id),
            ).fetchone()
            now = connection.execute(
                """SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision
                          AS now"""
            ).fetchone()["now"]
            if row is None or not (
                row["state"] == "submitted"
                or (
                    allow_working
                    and row["state"] == "working"
                    and (
                        row["claim_expires_at"] is None
                        or row["claim_expires_at"] <= now
                    )
                )
            ):
                return None, None
            if row["kind"] == REMOTE_KIND:
                _remote_contract(row["contract"], tenant_id, row["owner_run_id"])
                _remote_checkpoint(row["checkpoint"])
                if not _remote_due(row["checkpoint"], now, row["cancel_requested"]):
                    return None, None
                if not self._reserve_remote_context(connection, row, now):
                    return None, None
            if allow_working and not row["cancel_requested"] and row["kind"] != REMOTE_KIND:
                waiting = connection.execute(
                    """SELECT 1 FROM core_runs
                       WHERE task_id = %s AND tenant_id = %s
                         AND state NOT IN
                             ('COMPLETED', 'FAILED', 'CANCELLED', 'REJECTED', 'ABORTED')
                         AND NOT cancel_requested
                         AND snapshot->>'wait_id' IS NOT NULL
                         AND COALESCE(snapshot->>'wait_ready', 'false') <> 'true'
                       LIMIT 1""",
                    (task_id, tenant_id),
                ).fetchone()
                if waiting is not None:
                    return None, None
            claimed_from_state = row["state"]
            claimed = connection.execute(
                """UPDATE core_background_tasks SET state = 'working',
                       claim_owner = %s, claim_token = %s,
                       claim_expires_at = %s, updated_at = %s
                   WHERE id = %s AND tenant_id = %s RETURNING *""",
                (
                    self._worker_id,
                    token,
                    now + self.task_lease_ttl,
                    now,
                    task_id,
                    tenant_id,
                ),
            ).fetchone()
            claimed = {**claimed, "claimed_from_state": claimed_from_state}
        return token, claimed

    def _renew_claim(self, task_id, tenant_id, token):
        with self.database.transaction() as connection:
            row = connection.execute(
                """SELECT state, claim_owner, claim_token, claim_expires_at,
                          cancel_requested
                   FROM core_background_tasks
                   WHERE id = %s AND tenant_id = %s FOR UPDATE""",
                (task_id, tenant_id),
            ).fetchone()
            now = connection.execute(
                """SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision
                          AS now"""
            ).fetchone()["now"]
            if (
                row is None
                or row["state"] != "working"
                or row["claim_owner"] != self._worker_id
                or row["claim_token"] != token
                or (row["claim_expires_at"] or 0) <= now
            ):
                raise CoreError("LEASE_LOST")
            connection.execute(
                """UPDATE core_background_tasks SET claim_expires_at = %s,
                       updated_at = %s WHERE id = %s AND tenant_id = %s""",
                (now + self.task_lease_ttl, now, task_id, tenant_id),
            )
        if row["cancel_requested"]:
            self._signal_cancel(task_id, tenant_id)

    def _release_claim(self, task_id, tenant_id, token, *, state="submitted"):
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE core_background_tasks SET state = %s,
                       claim_owner = NULL, claim_token = NULL,
                       claim_expires_at = NULL,
                       updated_at = EXTRACT(EPOCH FROM clock_timestamp())
                   WHERE id = %s AND tenant_id = %s AND state = 'working'
                     AND claim_owner = %s AND claim_token = %s""",
                (state, task_id, tenant_id, self._worker_id, token),
            )

    def _launch(
        self,
        task_id,
        tenant_id,
        function,
        *,
        accepts_cancel_event,
        trace_context=None,
        continue_trace=False,
        allow_working=False,
        reconcile_cancel=False,
        initial_claim=None,
    ):
        token, row = initial_claim if initial_claim is not None else self._claim(
            task_id, tenant_id, allow_working=allow_working
        )
        if row is None:
            return False
        cancel_event = self._cancel_event(task_id)
        if row["cancel_requested"]:
            self._signal_cancel(task_id, tenant_id)
        reconcile_cancel = bool(
            reconcile_cancel
            or (allow_working and row["claimed_from_state"] == "working")
        )

        def run():
            heartbeat_stop = threading.Event()
            heartbeat_failures = []
            suspended = False

            def heartbeat():
                while not heartbeat_stop.wait(self.task_lease_heartbeat_interval):
                    try:
                        self._renew_claim(task_id, tenant_id, token)
                    except Exception as error:
                        heartbeat_failures.append(error)
                        return

            heartbeat_thread = threading.Thread(
                target=heartbeat,
                daemon=True,
                name=f"core-task-lease-{task_id[:8]}",
            )
            heartbeat_thread.start()
            span = None
            if self.telemetry:
                span = (
                    self.telemetry.span("core_agent.task.execute", parent=trace_context)
                    if continue_trace
                    else self.telemetry.start_background_span(
                        "core_agent.task.execute", trace_context
                    )
                )
            context = span if span else _NullContext()
            try:
                with context:
                    try:
                        self._renew_claim(task_id, tenant_id, token)
                    except CoreError as error:
                        if error.code != "LEASE_LOST":
                            raise
                        return
                    if row.get("kind") == REMOTE_KIND:
                        suspended = True
                        claim = RemoteTaskClaim(task_id, tenant_id, row["owner_run_id"], token)
                        try:
                            self.read_remote_claim(claim)
                            self._handlers[REMOTE_KIND](claim, cancel_event)
                        except CoreError as error:
                            if error.code not in {"LEASE_LOST", "WORKER_STOPPED"}:
                                raise
                        return
                    outcome = self._canonical_outcome(task_id, tenant_id, row)
                    if outcome is not None and not isinstance(outcome, SuspendedRun):
                        self._finish(task_id, tenant_id, token, *outcome, workflow_outcome=outcome)
                        return
                    if cancel_event.is_set() and not reconcile_cancel and outcome is None:
                        self._finish(
                            task_id, tenant_id, token, "canceled", None, None
                        )
                        return
                    try:
                        result = (
                            function(cancel_event)
                            if accepts_cancel_event
                            else function()
                        )
                        if isinstance(result, SuspendedRun):
                            suspended = True
                            return
                        state = "canceled" if cancel_event.is_set() else "completed"
                        error_code = None
                    except Exception as error:
                        result = None
                        error_code = getattr(error, "code", type(error).__name__)
                        canceled = (
                            cancel_event.is_set()
                            and not reconcile_cancel
                            and error_code
                            not in {
                                "SIDE_EFFECT_UNKNOWN",
                                "RECOVERY_REQUIRES_RECONCILIATION",
                            }
                        )
                        state = "canceled" if canceled else "failed"
                        if canceled:
                            error_code = None
                        if reconcile_cancel and error_code != "SIDE_EFFECT_UNKNOWN":
                            error_code = "RECOVERY_REQUIRES_RECONCILIATION"
                    if not heartbeat_failures:
                        self._finish(
                            task_id, tenant_id, token, state, result, error_code
                        )
            finally:
                heartbeat_stop.set()
                heartbeat_thread.join(timeout=1)
                if suspended and not heartbeat_failures:
                    self._release_claim(task_id, tenant_id, token, state="working")
                with self._lock:
                    self._remote_claims.discard((task_id, tenant_id, token))
                    self._threads.discard(threading.current_thread())

        thread = threading.Thread(
            target=run, daemon=True, name=f"core-task-{task_id[:8]}"
        )
        with self._lock:
            self._threads.add(thread)
            if row.get("kind") == REMOTE_KIND:
                self._remote_claims.add((task_id, tenant_id, token))
        try:
            thread.start()
        except BaseException:
            with self._lock:
                self._threads.discard(thread)
                self._remote_claims.discard((task_id, tenant_id, token))
            self._release_claim(
                task_id, tenant_id, token, state=row["claimed_from_state"]
            )
            raise
        return True

    def _canonical_outcome(self, task_id, tenant_id, row=None):
        if self._workflow_outcome is None:
            return None
        if row is None:
            with self.database.pool.connection() as connection:
                row = connection.execute(
                    "SELECT kind, contract FROM core_background_tasks WHERE id = %s AND tenant_id = %s",
                    (task_id, tenant_id),
                ).fetchone()
        if row is None:
            return None
        return self._workflow_outcome(task_id, tenant_id, row["kind"], row["contract"])

    def _finish(self, task_id, tenant_id, token, state, result, error_code, *, workflow_outcome=None):
        # Workflow reads precede the scheduler row lock and do not borrow a
        # second pool connection inside its transaction. Terminal evidence is
        # immutable, so a cancel arriving before the row lock cannot replace it.
        if workflow_outcome is None:
            workflow_outcome = self._canonical_outcome(task_id, tenant_id)
        if isinstance(workflow_outcome, SuspendedRun):
            self._release_claim(task_id, tenant_id, token, state="working")
            return False
        if workflow_outcome is not None:
            state, result, error_code = workflow_outcome
        with self.database.transaction() as connection:
            row = connection.execute(
                """SELECT * FROM core_background_tasks
                   WHERE id = %s AND tenant_id = %s FOR UPDATE""",
                (task_id, tenant_id),
            ).fetchone()
            now = connection.execute(
                """SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision
                          AS now"""
            ).fetchone()["now"]
            if (
                row is None
                or row["state"] in TERMINAL
                or row["claim_owner"] != self._worker_id
                or row["claim_token"] != token
                or (row["claim_expires_at"] or 0) <= now
            ):
                return False
            if workflow_outcome is None and row["cancel_requested"] and error_code not in {
                "SIDE_EFFECT_UNKNOWN",
                "RECOVERY_REQUIRES_RECONCILIATION",
            }:
                state = "canceled"
                error_code = None
            self._finish_locked(connection, row, state, result, error_code, now)
        with self._lock:
            self._cancel_callbacks.pop((tenant_id, task_id), None)
        with self._condition(task_id):
            self._condition(task_id).notify_all()
        return True

    def _finish_locked(self, connection, row, state, result, error_code, now):
        task_id, tenant_id = row["id"], row["tenant_id"]
        if row["kind"] == REMOTE_KIND:
            binding = self._remote_context_locked(connection, row["contract"])
            if binding is not None and binding["active_operation_id"] == task_id:
                binding["blocked"] |= _remote_context_unknown(row["checkpoint"], (state, result, error_code))
                binding["active_operation_id"] = None
                self._save_remote_context(connection, row["contract"], binding)
        revision = row["revision"] + 1
        public_result = remote_result_projection(result) if row["kind"] == REMOTE_KIND else result
        payload = {"result": _value(public_result), "error_code": error_code}
        connection.execute(
            """UPDATE core_background_tasks SET state = %s, result = %s,
                   error_code = %s, revision = %s, updated_at = %s,
                   claim_owner = NULL, claim_token = NULL,
                   claim_expires_at = NULL
               WHERE id = %s AND tenant_id = %s""",
            (
                state,
                Jsonb(_value(result)) if result is not None else None,
                error_code,
                revision,
                now,
                task_id,
                tenant_id,
            ),
        )
        self._deliver_notification(connection, row, f"task.{state}", revision, payload, now)
        return {**row, "state": state, "result": _value(result), "error_code": error_code, "revision": revision,
                "claim_owner": None, "claim_token": None, "claim_expires_at": None}

    def _deliver_notification(self, connection, row, kind, revision, payload, now):
        notification_id = str(uuid.uuid4())
        connection.execute(
            """INSERT INTO core_notifications
               (id, owner_run_id, tenant_id, task_id, kind, revision,
                payload, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (tenant_id, owner_run_id, task_id, kind, revision)
               DO NOTHING""",
            (
                notification_id,
                row["owner_run_id"],
                row["tenant_id"],
                row["id"],
                kind,
                revision,
                Jsonb(payload),
                now,
            ),
        )
        connection.execute(
            """INSERT INTO core_outbox
               (id, tenant_id, aggregate_type, aggregate_id, event_type,
                sequence, payload, available_at, created_at)
               VALUES (%s, %s, 'background_task', %s, %s, %s, %s, %s, %s)
               ON CONFLICT (tenant_id, aggregate_type, aggregate_id,
                            event_type, sequence) DO NOTHING""",
            (
                notification_id,
                row["tenant_id"],
                row["id"],
                kind,
                revision,
                Jsonb(payload),
                now,
                now,
            ),
        )

    def get(self, task_id, *, owner_id=None, tenant_id="default"):
        with self.database.pool.connection() as connection:
            sql = "SELECT * FROM core_background_tasks WHERE id = %s AND tenant_id = %s"
            values = [task_id, tenant_id]
            if owner_id is not None:
                sql += " AND owner_run_id = %s"
                values.append(owner_id)
            row = connection.execute(sql, values).fetchone()
        if row is None or row.get("kind") == REMOTE_KIND and owner_id is None:
            raise CoreError("TASK_NOT_FOUND")
        return self._task(row)

    def list(self, *, owner_id, tenant_id="default"):
        with self.database.pool.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM core_background_tasks
                   WHERE tenant_id = %s AND owner_run_id = %s ORDER BY created_at""",
                (tenant_id, owner_id),
            ).fetchall()
        return tuple(self._task(row) for row in rows)

    def count(self, *, owner_id, kind=None, active_only=False, tenant_id="default"):
        sql = """SELECT count(*) AS count FROM core_background_tasks
                 WHERE tenant_id = %s AND owner_run_id = %s"""
        values = [tenant_id, owner_id]
        if kind is not None:
            sql += " AND kind = %s"
            values.append(kind)
        if active_only:
            sql += " AND state NOT IN ('completed','failed','canceled')"
        with self.database.pool.connection() as connection:
            return connection.execute(sql, values).fetchone()["count"]

    def wait(self, task_id, timeout=None, *, owner_id=None, tenant_id="default"):
        end = None if timeout is None else time.monotonic() + timeout
        condition = self._condition(task_id)
        with condition:
            while True:
                task = self.get(task_id, owner_id=owner_id, tenant_id=tenant_id)
                if task.state in TERMINAL:
                    return task
                remaining = None if end is None else end - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError(task_id)
                condition.wait(remaining)

    def cancel(self, task_id, *, owner_id=None, tenant_id="default"):
        try:
            self.get(task_id, owner_id=owner_id, tenant_id=tenant_id)
        except CoreError as error:
            if error.code == "TASK_NOT_FOUND":
                raise ExecutionNotStarted(error.code) from error
            raise
        with self.database.transaction() as connection:
            remote = connection.execute("""SELECT * FROM core_background_tasks WHERE id = %s AND tenant_id = %s
                                           AND kind = %s FOR UPDATE""", (task_id, tenant_id, REMOTE_KIND)).fetchone()
            if remote is not None:
                if owner_id is not None and remote["owner_run_id"] != owner_id:
                    raise ExecutionNotStarted("TASK_NOT_FOUND")
                _remote_checkpoint(remote["checkpoint"])
                if remote["state"] in TERMINAL:
                    return self._task(remote)
                now = self._remote_now(connection)
                if _remote_expired(remote["checkpoint"], now):
                    remote = self._remote_finish_locked(connection, remote, remote["checkpoint"],
                        ("failed", remote_timeout_result(remote["contract"]), "REMOTE_OPERATION_TIMEOUT"), now)
                else:
                    remote = connection.execute("""UPDATE core_background_tasks SET cancel_requested = true, updated_at = %s
                                                   WHERE id = %s AND tenant_id = %s RETURNING *""", (now, task_id, tenant_id)).fetchone()
            else:
                # Preserve the local-task cancellation contract below.
                pass
        if remote is not None:
            if remote["state"] not in TERMINAL:
                self._signal_cancel(task_id, tenant_id)
            self._notify_remote(task_id)
            return self._task(remote)
        with self.database.transaction() as connection:
            sql = """UPDATE core_background_tasks SET cancel_requested = true,
                         updated_at = %s
                     WHERE id = %s AND tenant_id = %s
                       AND state NOT IN ('completed','failed','canceled')"""
            values = [self.clock(), task_id, tenant_id]
            if owner_id is not None:
                sql += " AND owner_run_id = %s"
                values.append(owner_id)
            updated = connection.execute(sql, values)
            if updated.rowcount != 1:
                raise ExecutionNotStarted("TASK_NOT_CANCELABLE")
        self._signal_cancel(task_id, tenant_id)
        return self.get(task_id, owner_id=owner_id, tenant_id=tenant_id)

    def assert_can_complete_parent(self, owner_id, tenant_id="default"):
        with self.database.pool.connection() as connection:
            row = connection.execute(
                """SELECT 1 FROM core_background_tasks
                   WHERE tenant_id = %s AND owner_run_id = %s AND required
                     AND state NOT IN ('completed','failed','canceled') LIMIT 1""",
                (tenant_id, owner_id),
            ).fetchone()
        if row:
            raise CoreError("REQUIRED_TASK_PENDING")

    def recover(self, *, ready=None, tenant_id=None):
        if self._closed:
            return 0
        with self.database.pool.connection() as connection:
            tenant_filter = " AND task.tenant_id = %s" if tenant_id is not None else ""
            values = [tenant_id] if tenant_id is not None else []
            rows = connection.execute(
                """SELECT * FROM core_background_tasks AS task
                   WHERE (state = 'submitted'
                      OR (state = 'working'
                          AND (claim_expires_at IS NULL OR claim_expires_at <=
                               EXTRACT(EPOCH FROM clock_timestamp()))))
                     AND (kind <> 'remote_a2a' OR cancel_requested
                          OR checkpoint->>'next_poll_at' IS NULL
                          OR (checkpoint->>'next_poll_at')::double precision <= EXTRACT(EPOCH FROM clock_timestamp())
                          OR (checkpoint->>'deadline')::double precision <= EXTRACT(EPOCH FROM clock_timestamp()))
                     AND (cancel_requested OR NOT EXISTS (
                         SELECT 1 FROM core_runs AS run
                         WHERE run.task_id = task.id AND run.tenant_id = task.tenant_id
                           AND run.state NOT IN
                               ('COMPLETED', 'FAILED', 'CANCELLED', 'REJECTED', 'ABORTED')
                           AND NOT run.cancel_requested
                           AND run.snapshot->>'wait_id' IS NOT NULL
                           AND COALESCE(run.snapshot->>'wait_ready', 'false') <> 'true'))
                   """ + tenant_filter + " ORDER BY created_at", values,
            ).fetchall()
        recovered = 0
        for row in rows:
            if not row["cancel_requested"] and ready is not None and not ready(
                row["id"], row["tenant_id"]
            ):
                continue
            handler = self._handlers.get(row["kind"])
            if not row["recoverable"] or handler is None:
                token, claimed = self._claim(
                    row["id"], row["tenant_id"], allow_working=True
                )
                if claimed is None:
                    continue
                claimed_from_state = claimed["claimed_from_state"]
                if claimed["cancel_requested"] and claimed_from_state == "submitted":
                    state, error_code = "canceled", None
                elif claimed_from_state == "working" and claimed["mutating"]:
                    state, error_code = "failed", "SIDE_EFFECT_UNKNOWN"
                else:
                    state, error_code = (
                        "failed",
                        "RECOVERY_REQUIRES_RECONCILIATION",
                    )
                self._finish(
                    row["id"],
                    row["tenant_id"],
                    token,
                    state,
                    None,
                    error_code,
                )
                continue

            def execute(cancel_event, current=row, callback=handler):
                return callback(current["contract"], cancel_event)

            trace_parent = row["contract"].get("_trace_parent")
            trace_context = (
                TraceContext(**trace_parent) if trace_parent is not None else None
            )

            launched = self._launch(
                row["id"],
                row["tenant_id"],
                execute,
                accepts_cancel_event=True,
                trace_context=trace_context,
                continue_trace=trace_context is not None,
                allow_working=True,
            )
            if launched and not row["cancel_requested"]:
                recovered += 1
        return recovered

    def close(self):
        self._closed = True
        with self._lock:
            threads = tuple(self._threads)
            remote_claims = tuple(self._remote_claims)
        for task_id, tenant_id, token in remote_claims:
            self._release_claim(task_id, tenant_id, token, state="working")
        for thread in threads:
            thread.join(timeout=5)


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False
