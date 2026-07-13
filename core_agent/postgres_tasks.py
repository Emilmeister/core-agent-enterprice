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

    def __init__(self, database, telemetry=None, clock=time.time):
        self.database = database
        self.telemetry = telemetry
        self.clock = clock
        self._handlers = {}
        self._cancel_events = {}
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

    def start(
        self,
        function,
        *,
        owner_id,
        required=False,
        accepts_cancel_event=False,
        trace_context=None,
        kind,
        contract,
        recoverable=False,
        tenant_id="default",
        continue_trace=False,
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
        task = BackgroundTask(str(uuid.uuid4()), owner_id, required)
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
                    recoverable, contract, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, 'submitted', %s, %s, %s, %s, %s)""",
                (
                    task.id,
                    owner_id,
                    tenant_id,
                    kind,
                    required,
                    recoverable,
                    Jsonb(_value(stored_contract)),
                    now,
                    now,
                ),
            )
        self._launch(
            task.id,
            tenant_id,
            function,
            accepts_cancel_event=accepts_cancel_event,
            trace_context=linked_context,
            continue_trace=continue_trace,
        )
        return task

    def _launch(
        self,
        task_id,
        tenant_id,
        function,
        *,
        accepts_cancel_event,
        trace_context=None,
        continue_trace=False,
    ):
        cancel_event = self._cancel_event(task_id)
        with self.database.transaction() as connection:
            row = connection.execute(
                """UPDATE core_background_tasks SET state = 'working',
                       updated_at = %s
                   WHERE id = %s AND tenant_id = %s
                     AND state IN ('submitted', 'working')
                   RETURNING owner_run_id""",
                (self.clock(), task_id, tenant_id),
            ).fetchone()
            if row is None:
                raise CoreError("INVALID_TASK_STATE")
            owner_id = row["owner_run_id"]

        def run():
            span = None
            if self.telemetry:
                span = (
                    self.telemetry.span(
                        "core_agent.task.execute", parent=trace_context
                    )
                    if continue_trace
                    else self.telemetry.start_background_span(
                        "core_agent.task.execute", trace_context
                    )
                )
            context = span if span else _NullContext()
            try:
                with context:
                    if cancel_event.is_set():
                        self._finish(task_id, tenant_id, "canceled", None, None)
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
                        state = "failed"
                        error_code = getattr(error, "code", type(error).__name__)
                    self._finish(task_id, tenant_id, state, result, error_code)
            finally:
                with self._lock:
                    self._threads.discard(threading.current_thread())

        thread = threading.Thread(
            target=run, daemon=True, name=f"core-task-{task_id[:8]}"
        )
        with self._lock:
            self._threads.add(thread)
        thread.start()
        return owner_id

    def _finish(self, task_id, tenant_id, state, result, error_code):
        now = self.clock()
        with self.database.transaction() as connection:
            row = connection.execute(
                """SELECT * FROM core_background_tasks
                   WHERE id = %s AND tenant_id = %s FOR UPDATE""",
                (task_id, tenant_id),
            ).fetchone()
            if row is None or row["state"] in TERMINAL:
                return
            revision = row["revision"] + 1
            payload = {"result": _value(result), "error_code": error_code}
            connection.execute(
                """UPDATE core_background_tasks SET state = %s, result = %s,
                       error_code = %s, revision = %s, updated_at = %s
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
        with self._condition(task_id):
            self._condition(task_id).notify_all()

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
        self._cancel_event(task_id).set()
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
                   WHERE state IN ('submitted','working') ORDER BY created_at"""
            ).fetchall()
        recovered = 0
        for row in rows:
            if row["cancel_requested"]:
                self._finish(row["id"], row["tenant_id"], "canceled", None, None)
                continue
            handler = self._handlers.get(row["kind"])
            if not row["recoverable"] or handler is None:
                self._finish(
                    row["id"],
                    row["tenant_id"],
                    "failed",
                    None,
                    "RECOVERY_REQUIRES_RECONCILIATION",
                )
                continue

            def execute(cancel_event, current=row, callback=handler):
                return callback(current["contract"], cancel_event)

            trace_parent = row["contract"].get("_trace_parent")
            trace_context = (
                TraceContext(**trace_parent) if trace_parent is not None else None
            )

            self._launch(
                row["id"],
                row["tenant_id"],
                execute,
                accepts_cancel_event=True,
                trace_context=trace_context,
                continue_trace=trace_context is not None,
            )
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
