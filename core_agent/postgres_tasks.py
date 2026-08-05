from __future__ import annotations

import dataclasses
import threading
import time
import uuid

from psycopg.types.json import Jsonb

from .errors import CoreError
from .observability import TraceContext
from .tasks import BackgroundTask, Notification


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
        self._cancel_events = {}
        self._cancel_callbacks = {}
        self._conditions = {}
        self._threads = set()
        self._lock = threading.Lock()
        self._closed = False
        self.active_compute_waiters = 0

    def register(self, kind, handler):
        if kind in self._handlers:
            raise CoreError("CONFIG_CONFLICT", f"duplicate task handler: {kind}")
        self._handlers[kind] = handler

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
            callback()

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
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO core_background_tasks
                   (id, owner_run_id, tenant_id, kind, state, required,
                    recoverable, contract, mutating, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, 'submitted', %s, %s, %s, %s, %s, %s)""",
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
                ),
            )
            if admission is not None:
                admission(connection)
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
            )
        except BaseException:
            with self._lock:
                self._cancel_callbacks.pop((tenant_id, task.id), None)
            raise
        return task

    def _claim(self, task_id, tenant_id, *, allow_working):
        token = str(uuid.uuid4())
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

    def _release_claim(self, task_id, tenant_id, token):
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE core_background_tasks SET state = 'submitted',
                       claim_owner = NULL, claim_token = NULL,
                       claim_expires_at = NULL,
                       updated_at = EXTRACT(EPOCH FROM clock_timestamp())
                   WHERE id = %s AND tenant_id = %s AND state = 'working'
                     AND claim_owner = %s AND claim_token = %s""",
                (task_id, tenant_id, self._worker_id, token),
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
    ):
        token, row = self._claim(task_id, tenant_id, allow_working=allow_working)
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
                    if cancel_event.is_set() and not reconcile_cancel:
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
                with self._lock:
                    self._threads.discard(threading.current_thread())

        thread = threading.Thread(
            target=run, daemon=True, name=f"core-task-{task_id[:8]}"
        )
        with self._lock:
            self._threads.add(thread)
        try:
            thread.start()
        except BaseException:
            with self._lock:
                self._threads.discard(thread)
            self._release_claim(task_id, tenant_id, token)
            raise
        return True

    def _finish(self, task_id, tenant_id, token, state, result, error_code):
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
            if row["cancel_requested"] and error_code not in {
                "SIDE_EFFECT_UNKNOWN",
                "RECOVERY_REQUIRES_RECONCILIATION",
            }:
                state = "canceled"
                error_code = None
            revision = row["revision"] + 1
            payload = {"result": _value(result), "error_code": error_code}
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
            notification_id = str(uuid.uuid4())
            kind = f"task.{state}"
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
                    tenant_id,
                    task_id,
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
                    tenant_id,
                    task_id,
                    kind,
                    revision,
                    Jsonb(payload),
                    now,
                    now,
                ),
            )
        with self._lock:
            self._cancel_callbacks.pop((tenant_id, task_id), None)
        with self._condition(task_id):
            self._condition(task_id).notify_all()
        return True

    def get(self, task_id, *, owner_id=None, tenant_id="default"):
        with self.database.pool.connection() as connection:
            sql = "SELECT * FROM core_background_tasks WHERE id = %s AND tenant_id = %s"
            values = [task_id, tenant_id]
            if owner_id is not None:
                sql += " AND owner_run_id = %s"
                values.append(owner_id)
            row = connection.execute(sql, values).fetchone()
        if row is None:
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
                raise CoreError("TASK_NOT_CANCELABLE")
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

    def recover(self):
        with self.database.pool.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM core_background_tasks
                   WHERE state = 'submitted'
                      OR (state = 'working'
                          AND (claim_expires_at IS NULL OR claim_expires_at <=
                               EXTRACT(EPOCH FROM clock_timestamp())))
                   ORDER BY created_at""",
            ).fetchall()
        recovered = 0
        for row in rows:
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
        for thread in threads:
            thread.join(timeout=5)


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False
