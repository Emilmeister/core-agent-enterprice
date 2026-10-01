from __future__ import annotations

import threading
import time
import uuid
import math
from copy import deepcopy
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace

from psycopg.types.json import Jsonb

from .errors import CoreError


TERMINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED", "REJECTED", "ABORTED"}
WAIT_KINDS = {"timer", "task", "tool_approval", "owner_question", "guardrail"}
OWNER_WAIT_KINDS = {"tool_approval", "owner_question", "guardrail"}
DISPATCH_INTENTS = {"tool.intent", "tool.nested.intent"}
EXECUTION_ADMISSIONS = DISPATCH_INTENTS | {"model.attempt.started", "tool.attempt.started", "budget.finalization.started"}
_PRESERVE = object()


def _check_inbound_duplicate(duplicate, provenance):
    if "request_digest" in provenance and any(
        duplicate["provenance"].get(key) != provenance[key]
        for key in ("request_digest", "actor_id")
    ):
        raise CoreError("MESSAGE_ID_CONFLICT", "messageId was already used with different content or actor")


@dataclass(frozen=True)
class SuspendedRun:
    run_id: str
    task_id: str
    wait_id: str
    workflow_version: int


@dataclass(frozen=True)
class WaitRecord:
    wait_id: str
    run_id: str
    tenant_id: str
    owner_id: str
    context_id: str
    generation: int
    kind: str
    source_id: str
    subject: dict
    continuation: dict
    deadline: float | None
    outcome: dict | None
    resolved_at: float | None
    applied_at: float | None
    created_at: float


def _wait_generation(snapshot, kind, source_id, subject, continuation, deadline):
    if (
        kind not in WAIT_KINDS
        or not isinstance(source_id, str)
        or not source_id
        or not isinstance(subject, dict)
        or not isinstance(continuation, dict)
        or continuation.get("version") != 1
        or continuation.get("phase") not in {
            "tool_gate", "tool_wait", "tool_result", "input", "python_nested"
        }
        or (deadline is not None and (
            not isinstance(deadline, (int, float))
            or isinstance(deadline, bool)
            or not math.isfinite(deadline)
        ))
    ):
        raise CoreError("INVALID_TASK_STATE")
    generation = snapshot.get("wait_generation", 0)
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise CoreError("INVALID_TASK_STATE")
    return generation if snapshot.get("wait_id") else generation + 1


def _wait_outcome(wait, record, outcome, now):
    if record.cancel_requested or record.state in TERMINAL_STATES:
        return {"reason": "cancelled", "woke_at": now}
    if wait.deadline is not None and wait.deadline <= now:
        return {"reason": "time" if wait.kind == "timer" else "timeout", "woke_at": now}
    return {**deepcopy(outcome), "woke_at": outcome.get("woke_at", now)}


def _applied_wait_snapshot(current, snapshot, wait, state):
    snapshot = dict(snapshot)
    if "wait_generation" in current.snapshot and not snapshot.get("wait_id"):
        snapshot["wait_generation"] = current.snapshot["wait_generation"]
    if state in TERMINAL_STATES:
        snapshot.pop("wait_id", None)
    if wait is not None and snapshot.get("wait_id") != wait.wait_id:
        if snapshot.get("wait_id") or (wait.outcome is None and state not in TERMINAL_STATES):
            raise CoreError("INVALID_TASK_STATE")
        snapshot.pop("wait_ready", None)
    return snapshot


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
    cancel_requested: bool = False


class _ExecutionLifecycle:
    """Shared lifecycle rules; stores supply their existing run/ledger locks."""

    def execution_family(self, record, *, connection=None):
        records = self._execution_records(record, connection)
        family = {record.run_id}
        while True:
            children = {item.run_id for item in records if item.parent_run_id in family}
            if children <= family:
                return tuple(item for item in records if item.run_id in family)
            family.update(children)

    def execution_ancestors(self, record, *, connection=None):
        records = {item.run_id: item for item in self._execution_records(record, connection)}
        ancestors = []
        parent = record.parent_run_id
        while parent:
            if parent in ancestors or parent not in records:
                raise CoreError("CHECKPOINT_INVALID")
            ancestors.append(parent)
            parent = records[parent].parent_run_id
        return tuple(ancestors)

    def _check_execution_open(self, record, connection=None):
        records = {item.run_id: item for item in self._execution_records(record, connection)}
        run_id = record.run_id
        visited = set()
        while run_id:
            if run_id in visited or run_id not in records:
                raise CoreError("CHECKPOINT_INVALID")
            visited.add(run_id)
            item = records[run_id]
            if item.state in TERMINAL_STATES or item.snapshot.get("terminal_intent"):
                raise CoreError("EXECUTION_CLOSING")
            run_id = item.parent_run_id

    def register_execution(self, record, *, instance_id, worker_id, generation, lease_token):
        owner = dict(instance_id=instance_id, worker_id=worker_id, generation=generation,
                     cleanup_confirmed=False, local_execution_pending=False)
        with self._execution_lock(record, lease_token) as (current, connection):
            self._check_execution_open(current, connection)
            previous = current.snapshot.get("execution_owner")
            if previous and all(previous[key] == owner[key] for key in ("instance_id", "worker_id", "generation")):
                return current
            if previous and not previous["cleanup_confirmed"]:
                raise CoreError("EXECUTION_CLEANUP_PENDING")
            return self._save_execution_owner(current, owner, connection)

    def confirm_execution(self, record, owner):
        # A stale worker may acknowledge only its own retired generation. This
        # operational receipt must not invalidate an active child's checkpoint.
        with self._execution_lock(record) as (current, connection):
            previous = current.snapshot.get("execution_owner")
            if previous and all(previous[key] == owner[key] for key in ("instance_id", "worker_id", "generation")):
                return self._save_execution_owner(current, {**previous, "cleanup_confirmed": True,
                                                            "local_execution_pending": False}, connection)
            return current

    def begin_terminal(self, record, intent, *, lease_token):
        with self._execution_lock(record, lease_token) as (current, connection):
            if current.state in TERMINAL_STATES:
                return current
            existing = current.snapshot.get("terminal_intent")
            if existing:
                return current
            return self.transition(current.run_id, tenant_id=current.tenant_id, owner_id=current.owner_id,
                                   expected_version=record.version, state=current.state, snapshot=current.snapshot,
                                   event_kind="execution.terminal_prepared", terminal_intent=deepcopy(intent),
                                   lease_token=lease_token, connection=connection)

    def clear_terminal(self, record, intent_id, *, lease_token, snapshot=None):
        with self._execution_lock(record, lease_token) as (current, connection):
            if current.snapshot.get("terminal_intent", {}).get("id") != intent_id:
                raise CoreError("LEASE_LOST")
            return self.transition(current.run_id, tenant_id=current.tenant_id, owner_id=current.owner_id,
                                   expected_version=current.version, state="RUNNING", snapshot=current.snapshot if snapshot is None else snapshot,
                                   event_kind="execution.terminal_reopened", terminal_intent=None,
                                   lease_token=lease_token, connection=connection)

    def _execution_snapshot(self, current, snapshot, state, event_kind, connection=None, *, terminal_intent=_PRESERVE):
        if event_kind in EXECUTION_ADMISSIONS:
            self._check_execution_open(current, connection)
            if current.snapshot.get("execution_owner", {}).get("cleanup_confirmed"):
                raise CoreError("LEASE_LOST")
        snapshot = dict(snapshot)
        for key in ("execution_owner", "terminal_intent"):
            snapshot.pop(key, None)
            if key in current.snapshot:
                snapshot[key] = deepcopy(current.snapshot[key])
        owner = snapshot.get("execution_owner")
        if owner is not None:
            pending_name = (snapshot.get("pending_call") or {}).get("name")
            if event_kind == "tool.intent" and pending_name in {"core_terminal_exec", "core_python_exec"}:
                owner["local_execution_pending"] = True
            elif event_kind == "python.stopped" or (
                state == "RUNNING" and event_kind in {"tool.completed", "tool.failed", "tool.denied"}
            ):
                owner["local_execution_pending"] = False
        if terminal_intent is not _PRESERVE:
            if terminal_intent is None:
                snapshot.pop("terminal_intent", None)
            else:
                snapshot["terminal_intent"] = terminal_intent
        if state in TERMINAL_STATES:
            # Low-level historical/model-only records need no process receipt.
            family = self.execution_family(current, connection=connection)
            if any(item.snapshot.get("execution_owner") and not item.snapshot["execution_owner"]["cleanup_confirmed"] for item in family):
                raise CoreError("EXECUTION_CLEANUP_PENDING")
            snapshot.pop("terminal_intent", None)
        return snapshot


class InMemoryWorkflowStore(_ExecutionLifecycle):
    """Test adapter with the same optimistic state contract as PostgreSQL."""

    atomic = False

    def __init__(self, clock=time.time):
        self.clock = clock
        self._records = {}
        self._leases = {}
        self._lock = threading.RLock()
        self._budgets = {}
        self._inbound = {}
        self._cancel_requested = set()
        self._waits = {}

    def current_time(self):
        return self.clock()

    def _execution_records(self, record, connection=None):
        root = record.snapshot.get("budget_root_id", record.run_id)
        with self._lock:
            return tuple(item for item in self._records.values() if item.tenant_id == record.tenant_id
                         and item.snapshot.get("budget_root_id", item.run_id) == root)

    @contextmanager
    def _execution_lock(self, record, lease_token=None):
        with self._lock:
            current = self.get(record.run_id, tenant_id=record.tenant_id, owner_id=record.owner_id)
            if lease_token is not None:
                lease = self._leases.get(record.run_id)
                if not lease or lease[1] != lease_token or lease[2] <= self.clock():
                    raise CoreError("LEASE_LOST")
            yield current, None

    def _save_execution_owner(self, record, owner, connection):
        updated = replace(record, snapshot={**record.snapshot, "execution_owner": owner})
        self._records[record.run_id] = updated
        return updated

    def create(
        self,
        record,
        *,
        budget_limits=(100, 200),
        reserve_model_turns=0,
        lease_owner=None,
        lease_token=None,
        lease_ttl=None,
        **_metadata,
    ):
        with self._lock:
            if any(
                value is not None for value in (lease_owner, lease_token, lease_ttl)
            ):
                if (
                    not lease_owner
                    or not lease_token
                    or not lease_ttl
                    or lease_ttl <= 0
                ):
                    raise CoreError("INVALID_TASK_STATE")
            if record.run_id in self._records:
                raise CoreError("SESSION_CONFLICT")
            if record.parent_run_id:
                parent = self._records.get(record.parent_run_id)
                if parent is None or parent.tenant_id != record.tenant_id or parent.owner_id != record.owner_id:
                    raise CoreError("TASK_NOT_FOUND")
                self._check_execution_open(parent)
                root_run_id = parent.snapshot.get("budget_root_id", parent.run_id)
            else:
                root_run_id = record.run_id
                self._budgets[root_run_id] = [*budget_limits, 0, 0]
            budget = self._budgets[root_run_id]
            if reserve_model_turns < 0:
                raise CoreError("INVALID_TASK_STATE")
            if budget[2] + reserve_model_turns > budget[0]:
                if not record.parent_run_id:
                    self._budgets.pop(root_run_id, None)
                raise CoreError(
                    "BUDGET_EXCEEDED",
                    f"model_turns budget exhausted ({budget[2]}/{budget[0]})",
                    data={
                        "dimension": "model_turns",
                        "used": budget[2],
                        "limit": budget[0],
                    },
                )
            budget[2] += reserve_model_turns
            record = WorkflowRecord(
                **{
                    **record.__dict__,
                    "snapshot": {**record.snapshot, "budget_root_id": root_run_id},
                }
            )
            self._records[record.run_id] = record
            self._inbound[record.run_id] = []
            if lease_token is not None:
                self._leases[record.run_id] = (
                    lease_owner,
                    lease_token,
                    self.clock() + lease_ttl,
                )
            return record

    def append_inbound(
        self,
        task_id,
        *,
        tenant_id,
        owner_id,
        message_id,
        context_id,
        content,
        provenance,
        connection=None,
        on_accept=None,
    ):
        with self._lock:
            record = self.by_task(task_id, tenant_id=tenant_id, owner_id=owner_id)
            messages = self._inbound[record.run_id]
            duplicate = next(
                (item for item in messages if item["message_id"] == message_id),
                None,
            )
            if duplicate is not None:
                _check_inbound_duplicate(duplicate, provenance)
                return dict(duplicate), False
            if record.context_id != context_id:
                raise CoreError("INVALID_REQUEST", "context_id does not match task")
            if record.state in TERMINAL_STATES or record.cancel_requested:
                raise CoreError("TASK_TERMINAL")
            provenance = dict(provenance)
            if on_accept is not None:
                provenance.update(on_accept(record, len(messages) + 1, None))
            provenance["history_after_sequence"] = record.snapshot.get("context", {}).get("sequence_range", [1, 0])[1]
            message = {
                "sequence": len(messages) + 1,
                "message_id": message_id,
                "context_id": context_id,
                "role": "user",
                "content": content,
                "provenance": dict(provenance),
                "consumed": False,
            }
            messages.append(message)
            wait_id = record.snapshot.get("wait_id")
            wait = self._waits.get(wait_id)
            if wait is not None and wait.kind == "timer" and wait.outcome is None:
                self.resolve_wait(
                    wait.wait_id, tenant_id=tenant_id,
                    outcome={"reason": "message", "message_id": message_id},
                )
            return dict(message), True

    def pending_inbound(self, record):
        with self._lock:
            current = self.get(
                record.run_id,
                tenant_id=record.tenant_id,
                owner_id=record.owner_id,
            )
            return tuple(
                dict(item)
                for item in self._inbound[current.run_id]
                if not item["consumed"]
            )

    def consume_inbound(
        self,
        record,
        *,
        expected_version,
        snapshot,
        sequences,
        lease_token,
        state="RUNNING",
        event_kind="input.delivered",
        inbound_event_kind=None,
        event_data=None,
        audit=(),
        result=None,
        error_code=None,
        consume_model_turns=0,
        consume_tool_calls=0,
        release_model_turns=0,
        include_shared_budget=False,
    ):
        with self._lock:
            current = self.get(
                record.run_id,
                tenant_id=record.tenant_id,
                owner_id=record.owner_id,
            )
            selected = {
                item["sequence"]: item
                for item in self._inbound[current.run_id]
                if not item["consumed"] and item["sequence"] in sequences
            }
            if set(selected) != set(sequences):
                raise CoreError("SESSION_CONFLICT")
            for sequence in sequences:
                selected[sequence]["consumed"] = True
            try:
                updated = self.transition(
                    current.run_id,
                    tenant_id=current.tenant_id,
                    owner_id=current.owner_id,
                    expected_version=expected_version,
                    state=state,
                    snapshot=snapshot,
                    event_kind=event_kind,
                    event_data=event_data or {"sequences": list(sequences)},
                    audit=audit,
                    result=result,
                    error_code=error_code,
                    lease_token=lease_token,
                    consume_model_turns=consume_model_turns,
                    consume_tool_calls=consume_tool_calls,
                    release_model_turns=release_model_turns,
                    include_shared_budget=include_shared_budget,
                )
            except Exception:
                for sequence in sequences:
                    selected[sequence]["consumed"] = False
                raise
            return updated

    def consume_budget(self, record, *, model_turns=0, tool_calls=0):
        with self._lock:
            root = record.snapshot["budget_root_id"]
            budget = self._budgets[root]
            if budget[2] + model_turns > budget[0]:
                raise CoreError(
                    "BUDGET_EXCEEDED",
                    f"model_turns budget exhausted ({budget[2]}/{budget[0]})",
                    data={
                        "dimension": "model_turns",
                        "used": budget[2],
                        "limit": budget[0],
                    },
                )
            if budget[3] + tool_calls > budget[1]:
                raise CoreError(
                    "BUDGET_EXCEEDED",
                    f"tool_calls budget exhausted ({budget[3]}/{budget[1]})",
                    data={
                        "dimension": "tool_calls",
                        "used": budget[3],
                        "limit": budget[1],
                    },
                )
            budget[2] += model_turns
            budget[3] += tool_calls

    def release_budget(self, record, *, model_turns=0, tool_calls=0):
        with self._lock:
            root = record.snapshot["budget_root_id"]
            budget = self._budgets[root]
            if (
                model_turns < 0
                or tool_calls < 0
                or budget[2] < model_turns
                or budget[3] < tool_calls
            ):
                raise CoreError("INVALID_TASK_STATE")
            budget[2] -= model_turns
            budget[3] -= tool_calls

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

    def is_cancelled(self, run_id, *, tenant_id, owner_id):
        record = self.get(run_id, tenant_id=tenant_id, owner_id=owner_id)
        return record.state == "CANCELLED" or run_id in self._cancel_requested

    def request_cancel(self, run_id, *, tenant_id, owner_id):
        with self._lock:
            record = self.get(run_id, tenant_id=tenant_id, owner_id=owner_id)
            if record.state in TERMINAL_STATES:
                raise CoreError("TASK_NOT_CANCELABLE")
            self._cancel_requested.add(run_id)
            updated = WorkflowRecord(**{**record.__dict__, "cancel_requested": True})
            self._records[run_id] = updated
            return updated

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
        consume_model_turns=0,
        consume_tool_calls=0,
        release_model_turns=0,
        include_shared_budget=False,
        **_metadata,
    ):
        with self._lock:
            current = self.get(run_id, tenant_id=tenant_id, owner_id=owner_id)
            if current.version != expected_version:
                raise CoreError("SESSION_CONFLICT")
            if current.state in TERMINAL_STATES and state != current.state:
                raise CoreError("INVALID_TASK_STATE")
            if (
                (state in {"COMPLETED", "FAILED", "REJECTED"}
                 or _metadata.get("event_kind") in DISPATCH_INTENTS)
                and current.cancel_requested
            ):
                raise CoreError("CANCEL_REQUESTED")
            if state in TERMINAL_STATES and any(
                not item["consumed"] for item in self._inbound.get(run_id, ())
            ):
                raise CoreError("INBOUND_MESSAGE_PENDING")
            if lease_token is not None:
                lease = self._leases.get(run_id)
                if not lease or lease[1] != lease_token or lease[2] <= self.clock():
                    raise CoreError("LEASE_LOST")
            snapshot = self._execution_snapshot(current, snapshot, state, _metadata.get("event_kind"),
                                                terminal_intent=_metadata.get("terminal_intent", _PRESERVE))
            wait = self._waits.get(current.snapshot.get("wait_id"))
            snapshot = _applied_wait_snapshot(current, snapshot, wait, state)
            root = current.snapshot["budget_root_id"]
            budget = self._budgets[root]
            if (
                consume_model_turns < 0
                or consume_tool_calls < 0
                or release_model_turns < 0
            ):
                raise CoreError("INVALID_TASK_STATE")
            if budget[2] + consume_model_turns > budget[0]:
                raise CoreError(
                    "BUDGET_EXCEEDED",
                    f"model_turns budget exhausted ({budget[2]}/{budget[0]})",
                    data={
                        "dimension": "model_turns",
                        "used": budget[2],
                        "limit": budget[0],
                    },
                )
            next_model_turns = budget[2] + consume_model_turns - release_model_turns
            if next_model_turns < 0:
                raise CoreError("INVALID_TASK_STATE")
            if budget[3] + consume_tool_calls > budget[1]:
                raise CoreError(
                    "BUDGET_EXCEEDED",
                    f"tool_calls budget exhausted ({budget[3]}/{budget[1]})",
                    data={
                        "dimension": "tool_calls",
                        "used": budget[3],
                        "limit": budget[1],
                    },
                )
            budget[2] = next_model_turns
            budget[3] += consume_tool_calls
            if include_shared_budget and result is not None:
                result = {
                    **result,
                    "shared_budget": {
                        "scope": "root",
                        "used": {
                            "model_turns": budget[2],
                            "tool_calls": budget[3],
                        },
                        "limits": {
                            "model_turns": budget[0],
                            "tool_calls": budget[1],
                        },
                    },
                }
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
            if wait is not None and snapshot.get("wait_id") != wait.wait_id:
                now = self.clock()
                self._waits[wait.wait_id] = replace(
                    wait, outcome=wait.outcome or {"reason": "cancelled", "woke_at": now},
                    resolved_at=wait.resolved_at if wait.resolved_at is not None else now,
                    applied_at=now,
                )
            return record

    def enter_wait(
        self, record, *, kind, source_id, subject, continuation, deadline,
        snapshot, lease_token, connection=None,
    ):
        generation = _wait_generation(snapshot, kind, source_id, subject, continuation, deadline)
        with self._lock:
            current = self.get(record.run_id, tenant_id=record.tenant_id, owner_id=record.owner_id)
            existing = next((wait for wait in self._waits.values()
                             if wait.run_id == record.run_id and wait.generation == generation), None)
            if existing is not None:
                if (existing.kind != kind or existing.source_id != source_id
                        or existing.subject != subject or existing.continuation != continuation
                        or existing.deadline != deadline):
                    raise CoreError("SESSION_CONFLICT")
                return deepcopy(existing)
            if current.cancel_requested:
                raise CoreError("CANCEL_REQUESTED")
            if current.state in TERMINAL_STATES or current.snapshot.get("wait_id"):
                raise CoreError("INVALID_TASK_STATE")
            if generation != current.snapshot.get("wait_generation", 0) + 1:
                raise CoreError("SESSION_CONFLICT")
            if not lease_token:
                raise CoreError("LEASE_LOST")
            now = self.clock()
            wait = WaitRecord(str(uuid.uuid4()), record.run_id, record.tenant_id,
                              record.owner_id, record.context_id, generation, kind, source_id,
                              deepcopy(subject), deepcopy(continuation), deadline, None, None, None, now)
            unread = next((item for item in self._inbound[record.run_id] if not item["consumed"]), None)
            outcome = None
            if deadline is not None and deadline <= now:
                outcome = _wait_outcome(wait, current, {}, now)
            elif kind == "timer" and unread is not None:
                outcome = {"reason": "message", "message_id": unread["message_id"], "woke_at": now}
            if outcome is not None:
                wait = replace(wait, outcome=outcome, resolved_at=now)
            next_snapshot = {**snapshot, "wait_id": wait.wait_id, "wait_generation": generation}
            next_snapshot.pop("wait_ready", None)
            if outcome is not None:
                next_snapshot["wait_ready"] = True
            self.transition(
                record.run_id, tenant_id=record.tenant_id, owner_id=record.owner_id,
                expected_version=record.version,
                state="WAITING_TASK" if kind in {"timer", "task"} else "WAITING_INPUT",
                snapshot=next_snapshot, lease_token=lease_token,
            )
            self._waits[wait.wait_id] = wait
            self._leases.pop(record.run_id, None)
            return deepcopy(wait)

    def get_wait(self, wait_id, *, tenant_id, owner_id=None):
        with self._lock:
            wait = self._waits.get(wait_id)
            if (wait is None or wait.tenant_id != tenant_id
                    or (owner_id is not None and wait.owner_id != owner_id)):
                raise CoreError("TASK_NOT_FOUND")
            return deepcopy(wait)

    def list_interactions(self, task_id, *, tenant_id, status="pending", limit=50, after=None):
        with self._lock:
            records = [record for record in self._records.values()
                       if record.task_id == task_id and record.tenant_id == tenant_id]
            if len(records) != 1:
                raise CoreError("TASK_NOT_FOUND")
            root = records[0].snapshot["budget_root_id"]
            runs = {record.run_id for record in self._records.values()
                    if record.tenant_id == tenant_id and record.snapshot["budget_root_id"] == root}
            waits = sorted((wait for wait in self._waits.values()
                            if wait.tenant_id == tenant_id and wait.run_id in runs
                            and wait.kind in OWNER_WAIT_KINDS
                            and (status == "all" or wait.outcome is None)
                            and (after is None or (wait.created_at, wait.wait_id) > after)),
                           key=lambda wait: (wait.created_at, wait.wait_id))
            return tuple(deepcopy(wait) for wait in waits[:max(0, limit)])

    def resolve_wait(self, wait_id, *, tenant_id, outcome, actor_id=None):
        with self._lock:
            wait = self.get_wait(wait_id, tenant_id=tenant_id)
            current = self.get(wait.run_id, tenant_id=tenant_id)
            if wait.outcome is not None:
                return wait
            now = self.clock()
            resolved = replace(wait, outcome=_wait_outcome(wait, current, outcome, now), resolved_at=now)
            if current.state in TERMINAL_STATES:
                resolved = replace(resolved, applied_at=now)
            else:
                self.transition(
                    current.run_id, tenant_id=tenant_id, owner_id=current.owner_id,
                    expected_version=current.version, state=current.state,
                    snapshot={**current.snapshot, "wait_ready": True},
                )
            self._waits[wait_id] = resolved
            return deepcopy(resolved)

    def pending_waits(self, *, kind=None, limit=100, after=None):
        with self._lock:
            waits = sorted((wait for wait in self._waits.values()
                            if wait.outcome is None and (kind is None or wait.kind == kind)
                            and (after is None or (wait.created_at, wait.wait_id) > after)),
                           key=lambda wait: (wait.created_at, wait.wait_id))
            return tuple(deepcopy(wait) for wait in waits[:max(0, limit)])

    def expire_waits(self, *, limit=100):
        with self._lock:
            due = [wait for wait in self._waits.values() if wait.outcome is None
                   and wait.deadline is not None and wait.deadline <= self.clock()]
            due.sort(key=lambda wait: (wait.deadline, wait.wait_id))
            return tuple(self.resolve_wait(wait.wait_id, tenant_id=wait.tenant_id, outcome={})
                         for wait in due[:max(0, limit)])

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
            if current and current[2] > now:
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

    def recoverable(self, *, states=None, root_only=False, limit=100):
        with self._lock:
            return tuple(
                record
                for record in self._records.values()
                if (
                    record.state in states
                    if states is not None
                    else record.state not in TERMINAL_STATES
                )
                and (not root_only or record.parent_run_id is None)
                and (record.cancel_requested or record.snapshot.get("terminal_intent") or not any(
                    wait.run_id == record.run_id and wait.outcome is None
                    for wait in self._waits.values()
                ))
                and (
                    record.run_id not in self._leases
                    or self._leases[record.run_id][2] <= self.clock()
                )
            )[:limit]


class PostgresWorkflowStore(_ExecutionLifecycle):
    """Atomic source of truth for run state, events, checkpoints and outbox."""

    atomic = True

    def __init__(self, database, clock=time.time):
        self.database = database
        self.clock = clock

    @staticmethod
    def _current_time(connection):
        return connection.execute(
            "SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision AS now"
        ).fetchone()["now"]

    def current_time(self):
        with self.database.pool.connection() as connection:
            return self._current_time(connection)

    def _execution_records(self, record, connection=None):
        with (self.database.pool.connection() if connection is None else nullcontext(connection)) as db:
            rows = db.execute("SELECT * FROM core_runs WHERE tenant_id = %s AND snapshot->>'budget_root_id' = %s",
                              (record.tenant_id, record.snapshot.get("budget_root_id", record.run_id))).fetchall()
        return tuple(self._record(row) for row in rows)

    def _lock_execution_ledger(self, record, connection):
        if connection.execute("SELECT 1 FROM core_budget_ledgers WHERE root_run_id = %s AND tenant_id = %s FOR UPDATE",
                              (record.snapshot["budget_root_id"], record.tenant_id)).fetchone() is None:
            raise CoreError("TASK_NOT_FOUND")

    @contextmanager
    def _execution_lock(self, record, lease_token=None):
        with self.database.transaction() as connection:
            current = self.get(record.run_id, tenant_id=record.tenant_id, owner_id=record.owner_id, connection=connection, lock=True)
            self._lock_execution_ledger(current, connection)
            if lease_token is not None and connection.execute(
                "SELECT 1 FROM core_runs WHERE run_id = %s AND lease_token = %s AND lease_expires_at > EXTRACT(EPOCH FROM clock_timestamp())",
                (current.run_id, lease_token),
            ).fetchone() is None:
                raise CoreError("LEASE_LOST")
            yield current, connection

    def _save_execution_owner(self, record, owner, connection):
        updated = replace(record, snapshot={**record.snapshot, "execution_owner": owner})
        connection.execute("UPDATE core_runs SET snapshot = %s WHERE run_id = %s AND tenant_id = %s",
                           (Jsonb(updated.snapshot), record.run_id, record.tenant_id))
        return updated

    def get_wait(self, wait_id, *, tenant_id, owner_id=None, connection=None, lock=False):
        with (self.database.pool.connection() if connection is None else nullcontext(connection)) as db:
            sql = "SELECT * FROM core_waits WHERE wait_id = %s AND tenant_id = %s"
            values = [wait_id, tenant_id]
            if owner_id is not None:
                sql += " AND owner_id = %s"
                values.append(owner_id)
            if lock:
                sql += " FOR UPDATE"
            row = db.execute(sql, values).fetchone()
            if row is None:
                raise CoreError("TASK_NOT_FOUND")
            return WaitRecord(**row)

    def list_interactions(self, task_id, *, tenant_id, status="pending", limit=50, after=None):
        with self.database.pool.connection() as connection:
            rows = connection.execute(
                "SELECT snapshot FROM core_runs WHERE task_id = %s AND tenant_id = %s LIMIT 2",
                (task_id, tenant_id),
            ).fetchall()
            if len(rows) != 1:
                raise CoreError("TASK_NOT_FOUND")
            filters = " AND w.resolved_at IS NULL" if status != "all" else ""
            values = [tenant_id, rows[0]["snapshot"]["budget_root_id"], sorted(OWNER_WAIT_KINDS)]
            if after is not None:
                filters += " AND (w.created_at, w.wait_id) > (%s, %s)"
                values.extend(after)
            rows = connection.execute(
                """SELECT w.* FROM core_waits w JOIN core_runs r
                   ON r.run_id = w.run_id AND r.tenant_id = w.tenant_id
                   WHERE w.tenant_id = %s AND r.snapshot->>'budget_root_id' = %s
                   AND w.kind = ANY(%s)""" + filters
                + " ORDER BY w.created_at, w.wait_id LIMIT %s", (*values, max(0, limit)),
            ).fetchall()
            return tuple(WaitRecord(**row) for row in rows)

    def enter_wait(
        self, record, *, kind, source_id, subject, continuation, deadline,
        snapshot, lease_token, connection=None,
    ):
        generation = _wait_generation(snapshot, kind, source_id, subject, continuation, deadline)
        with (self.database.transaction() if connection is None else nullcontext(connection)) as db:
            current = self.get(record.run_id, tenant_id=record.tenant_id,
                               owner_id=record.owner_id, connection=db, lock=True)
            existing = db.execute(
                "SELECT * FROM core_waits WHERE run_id = %s AND generation = %s FOR UPDATE",
                (record.run_id, generation),
            ).fetchone()
            if existing is not None:
                if (existing["kind"] != kind or existing["source_id"] != source_id
                        or existing["subject"] != subject or existing["continuation"] != continuation
                        or existing["deadline"] != deadline):
                    raise CoreError("SESSION_CONFLICT")
                return WaitRecord(**existing)
            if current.cancel_requested:
                raise CoreError("CANCEL_REQUESTED")
            if current.state in TERMINAL_STATES or current.snapshot.get("wait_id"):
                raise CoreError("INVALID_TASK_STATE")
            if generation != current.snapshot.get("wait_generation", 0) + 1:
                raise CoreError("SESSION_CONFLICT")
            if not lease_token:
                raise CoreError("LEASE_LOST")
            now = self._current_time(db)
            wait = WaitRecord(str(uuid.uuid4()), record.run_id, record.tenant_id,
                              record.owner_id, record.context_id, generation, kind, source_id,
                              deepcopy(subject), deepcopy(continuation), deadline, None, None, None, now)
            unread = None
            if kind == "timer":
                unread = db.execute(
                    """SELECT message_id FROM core_inbound_messages WHERE run_id = %s
                       AND consumed_at IS NULL ORDER BY sequence LIMIT 1""",
                    (record.run_id,),
                ).fetchone()
            outcome = None
            if deadline is not None and deadline <= now:
                outcome = _wait_outcome(wait, current, {}, now)
            elif unread is not None:
                outcome = {"reason": "message", "message_id": unread["message_id"], "woke_at": now}
            if outcome is not None:
                wait = replace(wait, outcome=outcome, resolved_at=now)
            db.execute(
                """INSERT INTO core_waits
                   (wait_id, run_id, tenant_id, owner_id, context_id, generation,
                    kind, source_id, subject, continuation, deadline, outcome,
                    resolved_at, applied_at, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s)""",
                (wait.wait_id, wait.run_id, wait.tenant_id, wait.owner_id, wait.context_id,
                 generation, kind, source_id, Jsonb(wait.subject), Jsonb(wait.continuation),
                 deadline, Jsonb(outcome) if outcome is not None else None, wait.resolved_at, now),
            )
            next_snapshot = {**snapshot, "wait_id": wait.wait_id, "wait_generation": generation}
            next_snapshot.pop("wait_ready", None)
            if outcome is not None:
                next_snapshot["wait_ready"] = True
            state = "WAITING_TASK" if kind in {"timer", "task"} else "WAITING_INPUT"
            self._transition_locked(
                db, current, expected_version=record.version, state=state,
                snapshot=next_snapshot, event_kind="wait.entered", event_data={"state": state},
                audit=(("wait.entered", {"state": state}),), lease_token=lease_token, now=now,
            )
            db.execute(
                """UPDATE core_runs SET lease_owner = NULL, lease_token = NULL,
                   lease_expires_at = NULL WHERE run_id = %s AND tenant_id = %s""",
                (record.run_id, record.tenant_id),
            )
            return wait

    def _resolve_wait_locked(self, connection, current, wait, outcome, actor_id=None):
        if wait.outcome is not None:
            return wait
        now = self._current_time(connection)
        outcome = _wait_outcome(wait, current, outcome, now)
        applied_at = now if current.state in TERMINAL_STATES else None
        connection.execute(
            """UPDATE core_waits SET outcome = %s, resolved_at = %s, applied_at = %s
               WHERE wait_id = %s AND tenant_id = %s""",
            (Jsonb(outcome), now, applied_at, wait.wait_id, wait.tenant_id),
        )
        if current.state not in TERMINAL_STATES:
            self._transition_locked(
                connection, current, expected_version=current.version, state=current.state,
                snapshot={**current.snapshot, "wait_ready": True}, event_kind="wait.resolved",
                event_data={"state": current.state},
                audit=(("wait.resolved", {"state": current.state, "actor_id": actor_id}),),
                now=now,
            )
        return replace(wait, outcome=outcome, resolved_at=now, applied_at=applied_at)

    def resolve_wait(self, wait_id, *, tenant_id, outcome, actor_id=None):
        with self.database.transaction() as connection:
            # Discover the run without a wait lock; all mutations lock run then wait.
            wait = self.get_wait(wait_id, tenant_id=tenant_id, connection=connection)
            current = self.get(wait.run_id, tenant_id=tenant_id, connection=connection, lock=True)
            wait = self.get_wait(wait_id, tenant_id=tenant_id, connection=connection, lock=True)
            return self._resolve_wait_locked(connection, current, wait, outcome, actor_id)

    def pending_waits(self, *, kind=None, limit=100, after=None):
        if limit <= 0:
            return ()
        with self.database.pool.connection() as connection:
            kind_filter = " AND kind = %s" if kind is not None else ""
            values = [kind] if kind is not None else []
            if after is not None:
                kind_filter += " AND (created_at, wait_id) > (%s, %s)"
                values.extend(after)
            rows = connection.execute(
                "SELECT * FROM core_waits WHERE resolved_at IS NULL" + kind_filter
                + " ORDER BY created_at, wait_id LIMIT %s", (*values, limit),
            ).fetchall()
        return tuple(WaitRecord(**row) for row in rows)

    def expire_waits(self, *, limit=100):
        if limit <= 0:
            return ()
        with self.database.pool.connection() as connection:
            rows = connection.execute(
                """SELECT wait_id, tenant_id FROM core_waits WHERE resolved_at IS NULL
                   AND deadline <= EXTRACT(EPOCH FROM clock_timestamp())
                   ORDER BY deadline, wait_id LIMIT %s""", (limit,),
            ).fetchall()
        return tuple(self.resolve_wait(row["wait_id"], tenant_id=row["tenant_id"], outcome={})
                     for row in rows)

    @staticmethod
    def _record(row):
        return WorkflowRecord(
            **{key: row[key] for key in WorkflowRecord.__dataclass_fields__}
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
        reserve_model_turns=0,
        lease_owner=None,
        lease_token=None,
        lease_ttl=None,
        connection=None,
    ):
        now = self.clock()
        if record.version != 1:
            raise CoreError("INVALID_TASK_STATE")
        if reserve_model_turns < 0:
            raise CoreError("INVALID_TASK_STATE")
        if any(value is not None for value in (lease_owner, lease_token, lease_ttl)):
            if not lease_owner or not lease_token or not lease_ttl or lease_ttl <= 0:
                raise CoreError("INVALID_TASK_STATE")

        def create(db):
            nonlocal record
            if record.parent_run_id:
                parent = db.execute(
                    """SELECT * FROM core_runs
                       WHERE run_id = %s AND tenant_id = %s FOR SHARE""",
                    (record.parent_run_id, record.tenant_id),
                ).fetchone()
                if parent is None or parent["owner_id"] != record.owner_id:
                    raise CoreError("TASK_NOT_FOUND")
                root_run_id = parent["snapshot"].get(
                    "budget_root_id", record.parent_run_id
                )
                parent_record = self._record(parent)
                self._lock_execution_ledger(parent_record, db)
                self._check_execution_open(parent_record, db)
            else:
                root_run_id = record.run_id
                if reserve_model_turns > budget_limits[0]:
                    raise CoreError(
                        "BUDGET_EXCEEDED",
                        f"model_turns budget exhausted (0/{budget_limits[0]})",
                        data={
                            "dimension": "model_turns",
                            "used": 0,
                            "limit": budget_limits[0],
                        },
                    )
                db.execute(
                    """INSERT INTO core_budget_ledgers
                       (root_run_id, tenant_id, max_model_turns, max_tool_calls,
                        used_model_turns, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (
                        root_run_id,
                        record.tenant_id,
                        budget_limits[0],
                        budget_limits[1],
                        reserve_model_turns,
                        now,
                    ),
                )
            if record.parent_run_id and reserve_model_turns:
                reserved = db.execute(
                    """UPDATE core_budget_ledgers SET
                         used_model_turns = used_model_turns + %s,
                         updated_at = %s
                       WHERE root_run_id = %s AND tenant_id = %s
                         AND used_model_turns + %s <= max_model_turns""",
                    (
                        reserve_model_turns,
                        now,
                        root_run_id,
                        record.tenant_id,
                        reserve_model_turns,
                    ),
                )
                if reserved.rowcount != 1:
                    budget = db.execute(
                        """SELECT max_model_turns, used_model_turns
                           FROM core_budget_ledgers
                           WHERE root_run_id = %s AND tenant_id = %s""",
                        (root_run_id, record.tenant_id),
                    ).fetchone()
                    if budget is None:
                        raise CoreError("TASK_NOT_FOUND")
                    raise CoreError(
                        "BUDGET_EXCEEDED",
                        "model_turns budget exhausted "
                        f"({budget['used_model_turns']}/{budget['max_model_turns']})",
                        data={
                            "dimension": "model_turns",
                            "used": budget["used_model_turns"],
                            "limit": budget["max_model_turns"],
                        },
                    )
            record = WorkflowRecord(
                **{
                    **record.__dict__,
                    "snapshot": {**record.snapshot, "budget_root_id": root_run_id},
                }
            )
            lease_expires_at = None
            if lease_token is not None:
                lease_now = db.execute(
                    """SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision
                              AS now"""
                ).fetchone()["now"]
                lease_expires_at = lease_now + lease_ttl
            db.execute(
                """INSERT INTO core_runs
                   (run_id, task_id, context_id, tenant_id, owner_id,
                    parent_run_id, state, version, request, snapshot,
                    pending_approval_id, result, error_code, lease_owner,
                    lease_token, lease_expires_at, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, %s,
                           %s, %s, %s, %s, %s, %s, %s, %s)""",
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
                    lease_owner,
                    lease_token,
                    lease_expires_at,
                    now,
                    now,
                ),
            )
            revision = self._event(
                db, record, "task.created", {"state": record.state}, now
            )
            db.execute(
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
            self._audit(db, record, audit, now)
            self._outbox(
                db,
                record,
                "task.created",
                revision,
                outbox_payload or {"task_id": record.task_id, "state": record.state},
                now,
            )
            return record

        if connection is not None:
            return create(connection)
        with self.database.transaction() as transaction:
            return create(transaction)

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
            with self.database.pool.connection() as connection:
                budget = connection.execute(
                    """SELECT max_model_turns, max_tool_calls, used_model_turns,
                              used_tool_calls
                       FROM core_budget_ledgers
                       WHERE root_run_id = %s AND tenant_id = %s""",
                    (root_run_id, record.tenant_id),
                ).fetchone()
            if budget is None:
                raise CoreError("TASK_NOT_FOUND")
            dimension = (
                "model_turns"
                if budget["used_model_turns"] + model_turns > budget["max_model_turns"]
                else "tool_calls"
            )
            used = budget[f"used_{dimension}"]
            limit = budget[f"max_{dimension}"]
            raise CoreError(
                "BUDGET_EXCEEDED",
                f"{dimension} budget exhausted ({used}/{limit})",
                data={"dimension": dimension, "used": used, "limit": limit},
            )

    def release_budget(self, record, *, model_turns=0, tool_calls=0):
        if model_turns < 0 or tool_calls < 0:
            raise CoreError("INVALID_TASK_STATE")
        root_run_id = record.snapshot["budget_root_id"]
        with self.database.transaction() as connection:
            updated = connection.execute(
                """UPDATE core_budget_ledgers SET
                     used_model_turns = used_model_turns - %s,
                     used_tool_calls = used_tool_calls - %s,
                     updated_at = %s
                   WHERE root_run_id = %s AND tenant_id = %s
                     AND used_model_turns >= %s
                     AND used_tool_calls >= %s""",
                (
                    model_turns,
                    tool_calls,
                    self.clock(),
                    root_run_id,
                    record.tenant_id,
                    model_turns,
                    tool_calls,
                ),
            )
            if updated.rowcount != 1:
                raise CoreError("INVALID_TASK_STATE")

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

    def is_cancelled(self, run_id, *, tenant_id, owner_id):
        with self.database.pool.connection() as connection:
            row = connection.execute(
                """SELECT state = 'CANCELLED' OR cancel_requested AS cancelled
                   FROM core_runs
                   WHERE run_id = %s AND tenant_id = %s AND owner_id = %s""",
                (run_id, tenant_id, owner_id),
            ).fetchone()
        if row is None:
            raise CoreError("TASK_NOT_FOUND")
        return row["cancelled"]

    def request_cancel(self, run_id, *, tenant_id, owner_id):
        with self.database.transaction() as connection:
            updated = connection.execute(
                """UPDATE core_runs SET cancel_requested = true,
                       updated_at = EXTRACT(EPOCH FROM clock_timestamp())
                   WHERE run_id = %s AND tenant_id = %s AND owner_id = %s
                     AND state NOT IN
                         ('COMPLETED','FAILED','CANCELLED','REJECTED','ABORTED')""",
                (run_id, tenant_id, owner_id),
            )
            if updated.rowcount != 1:
                raise CoreError("TASK_NOT_CANCELABLE")
            return self.get(
                run_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                connection=connection,
            )

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

    def append_inbound(
        self,
        task_id,
        *,
        tenant_id,
        owner_id,
        message_id,
        context_id,
        content,
        provenance,
        connection=None,
        on_accept=None,
    ):
        now = self.clock()
        with (nullcontext(connection) if connection is not None else self.database.transaction()) as connection:
            record = connection.execute(
                """SELECT * FROM core_runs
                   WHERE task_id = %s AND tenant_id = %s AND owner_id = %s
                   FOR UPDATE""",
                (task_id, tenant_id, owner_id),
            ).fetchone()
            if record is None:
                raise CoreError("TASK_NOT_FOUND")
            record = self._record(record)
            duplicate = connection.execute(
                """SELECT sequence, message_id, context_id, role, content,
                          provenance, consumed_at
                   FROM core_inbound_messages
                   WHERE run_id = %s AND message_id = %s""",
                (record.run_id, message_id),
            ).fetchone()
            if duplicate is not None:
                _check_inbound_duplicate(duplicate, provenance)
                return {
                    **{key: value for key, value in duplicate.items() if key != "consumed_at"},
                    "consumed": duplicate["consumed_at"] is not None,
                }, False
            if record.context_id != context_id:
                raise CoreError("INVALID_REQUEST", "context_id does not match task")
            if record.state in TERMINAL_STATES or record.cancel_requested:
                raise CoreError("TASK_TERMINAL")
            sequence = connection.execute(
                """SELECT COALESCE(max(sequence), 0) + 1 AS sequence
                   FROM core_inbound_messages WHERE run_id = %s""",
                (record.run_id,),
            ).fetchone()["sequence"]
            provenance = dict(provenance)
            if on_accept is not None:
                provenance.update(on_accept(record, sequence, connection))
            provenance["history_after_sequence"] = record.snapshot.get("context", {}).get("sequence_range", [1, 0])[1]
            connection.execute(
                """INSERT INTO core_inbound_messages
                   (run_id, sequence, message_id, context_id, role, content,
                    provenance, received_at)
                   VALUES (%s, %s, %s, %s, 'user', %s, %s, %s)""",
                (
                    record.run_id,
                    sequence,
                    message_id,
                    context_id,
                    content,
                    Jsonb(dict(provenance)),
                    now,
                ),
            )
            self._event(
                connection,
                record,
                "input.accepted",
                {"message_id": message_id, "sequence": sequence},
                now,
            )
            self._audit(
                connection,
                record,
                (
                    (
                        "input.accepted",
                        {"message_id": message_id, "sequence": sequence},
                    ),
                ),
                now,
            )
            wait_id = record.snapshot.get("wait_id")
            if wait_id is not None:
                wait = self.get_wait(wait_id, tenant_id=tenant_id, connection=connection, lock=True)
                if wait.kind == "timer" and wait.outcome is None:
                    self._resolve_wait_locked(
                        connection, record, wait, {"reason": "message", "message_id": message_id}
                    )
        return {
            "sequence": sequence,
            "message_id": message_id,
            "context_id": context_id,
            "role": "user",
            "content": content,
            "provenance": dict(provenance),
            "consumed": False,
        }, True

    def pending_inbound(self, record):
        with self.database.pool.connection() as connection:
            rows = connection.execute(
                """SELECT sequence, message_id, context_id, role, content,
                          provenance
                   FROM core_inbound_messages
                   WHERE run_id = %s AND consumed_at IS NULL
                   ORDER BY sequence""",
                (record.run_id,),
            ).fetchall()
        return tuple({**dict(row), "consumed": False} for row in rows)

    def consume_inbound(
        self,
        record,
        *,
        expected_version,
        snapshot,
        sequences,
        lease_token,
        state="RUNNING",
        event_kind="input.delivered",
        inbound_event_kind=None,
        event_data=None,
        audit=(),
        result=None,
        error_code=None,
        consume_model_turns=0,
        consume_tool_calls=0,
        release_model_turns=0,
        include_shared_budget=False,
    ):
        now = self.clock()
        with self.database.transaction() as connection:
            current = self.get(
                record.run_id,
                tenant_id=record.tenant_id,
                owner_id=record.owner_id,
                connection=connection,
                lock=True,
            )
            rows = connection.execute(
                """SELECT sequence FROM core_inbound_messages
                   WHERE run_id = %s AND consumed_at IS NULL
                     AND sequence = ANY(%s)
                   FOR UPDATE""",
                (record.run_id, list(sequences)),
            ).fetchall()
            if {row["sequence"] for row in rows} != set(sequences):
                raise CoreError("SESSION_CONFLICT")
            if inbound_event_kind is not None:
                self._event(
                    connection,
                    current,
                    inbound_event_kind,
                    {"sequences": list(sequences)},
                    now,
                )
            connection.execute(
                """UPDATE core_inbound_messages SET consumed_at = %s
                   WHERE run_id = %s AND sequence = ANY(%s)
                     AND consumed_at IS NULL""",
                (now, record.run_id, list(sequences)),
            )
            updated = self._transition_locked(
                connection,
                current,
                expected_version=expected_version,
                state=state,
                snapshot=snapshot,
                event_kind=event_kind,
                event_data=event_data or {"sequences": list(sequences)},
                audit=audit,
                result=result,
                error_code=error_code,
                lease_token=lease_token,
                consume_model_turns=consume_model_turns,
                consume_tool_calls=consume_tool_calls,
                release_model_turns=release_model_turns,
                include_shared_budget=include_shared_budget,
            )
        return updated

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
        consume_model_turns=0,
        consume_tool_calls=0,
        release_model_turns=0,
        include_shared_budget=False,
        terminal_intent=_PRESERVE,
        now=None,
    ):
        now = self.clock() if now is None else now
        if current.version != expected_version:
            raise CoreError("SESSION_CONFLICT")
        if current.state in TERMINAL_STATES and state != current.state:
            raise CoreError("INVALID_TASK_STATE")
        if current.cancel_requested and (
            state in {"COMPLETED", "FAILED", "REJECTED"} or event_kind in DISPATCH_INTENTS
        ):
            raise CoreError("CANCEL_REQUESTED")
        if state in TERMINAL_STATES:
            pending = connection.execute(
                """SELECT 1 FROM core_inbound_messages
                   WHERE run_id = %s AND consumed_at IS NULL LIMIT 1""",
                (current.run_id,),
            ).fetchone()
            if pending is not None:
                raise CoreError("INBOUND_MESSAGE_PENDING")
        self._lock_execution_ledger(current, connection)
        if lease_token is not None:
            lease = connection.execute(
                """SELECT 1 FROM core_runs
                   WHERE run_id = %s AND tenant_id = %s AND lease_token = %s
                     AND lease_expires_at >
                         EXTRACT(EPOCH FROM clock_timestamp())""",
                (current.run_id, current.tenant_id, lease_token),
            ).fetchone()
            if lease is None:
                raise CoreError("LEASE_LOST")
        snapshot = self._execution_snapshot(current, snapshot, state, event_kind, connection,
                                            terminal_intent=terminal_intent)
        wait = None
        if current.snapshot.get("wait_id"):
            wait = self.get_wait(current.snapshot["wait_id"], tenant_id=current.tenant_id,
                                 connection=connection, lock=True)
        snapshot = _applied_wait_snapshot(current, snapshot, wait, state)
        if wait is not None and snapshot.get("wait_id") != wait.wait_id:
            applied_at = self._current_time(connection)
            connection.execute(
                """UPDATE core_waits SET outcome = COALESCE(outcome, %s),
                   resolved_at = COALESCE(resolved_at, %s), applied_at = %s
                   WHERE wait_id = %s AND tenant_id = %s""",
                (Jsonb({"reason": "cancelled", "woke_at": applied_at}), applied_at,
                 applied_at, wait.wait_id, current.tenant_id),
            )
        if consume_model_turns < 0 or consume_tool_calls < 0 or release_model_turns < 0:
            raise CoreError("INVALID_TASK_STATE")
        if consume_model_turns:
            consumed = connection.execute(
                """UPDATE core_budget_ledgers SET
                     used_model_turns = used_model_turns + %s,
                     updated_at = %s
                   WHERE root_run_id = %s AND tenant_id = %s
                     AND used_model_turns + %s <= max_model_turns""",
                (
                    consume_model_turns,
                    now,
                    current.snapshot["budget_root_id"],
                    current.tenant_id,
                    consume_model_turns,
                ),
            )
            if consumed.rowcount != 1:
                budget = connection.execute(
                    """SELECT max_model_turns, used_model_turns
                       FROM core_budget_ledgers
                       WHERE root_run_id = %s AND tenant_id = %s FOR UPDATE""",
                    (current.snapshot["budget_root_id"], current.tenant_id),
                ).fetchone()
                if budget is None:
                    raise CoreError("TASK_NOT_FOUND")
                raise CoreError(
                    "BUDGET_EXCEEDED",
                    "model_turns budget exhausted "
                    f"({budget['used_model_turns']}/{budget['max_model_turns']})",
                    data={
                        "dimension": "model_turns",
                        "used": budget["used_model_turns"],
                        "limit": budget["max_model_turns"],
                    },
                )
        if consume_tool_calls:
            consumed = connection.execute(
                """UPDATE core_budget_ledgers SET
                     used_tool_calls = used_tool_calls + %s,
                     updated_at = %s
                   WHERE root_run_id = %s AND tenant_id = %s
                     AND used_tool_calls + %s <= max_tool_calls""",
                (
                    consume_tool_calls,
                    now,
                    current.snapshot["budget_root_id"],
                    current.tenant_id,
                    consume_tool_calls,
                ),
            )
            if consumed.rowcount != 1:
                budget = connection.execute(
                    """SELECT max_tool_calls, used_tool_calls
                       FROM core_budget_ledgers
                       WHERE root_run_id = %s AND tenant_id = %s FOR UPDATE""",
                    (current.snapshot["budget_root_id"], current.tenant_id),
                ).fetchone()
                if budget is None:
                    raise CoreError("TASK_NOT_FOUND")
                raise CoreError(
                    "BUDGET_EXCEEDED",
                    "tool_calls budget exhausted "
                    f"({budget['used_tool_calls']}/{budget['max_tool_calls']})",
                    data={
                        "dimension": "tool_calls",
                        "used": budget["used_tool_calls"],
                        "limit": budget["max_tool_calls"],
                    },
                )
        if release_model_turns:
            released = connection.execute(
                """UPDATE core_budget_ledgers SET
                     used_model_turns = used_model_turns - %s,
                     updated_at = %s
                   WHERE root_run_id = %s AND tenant_id = %s
                     AND used_model_turns >= %s""",
                (
                    release_model_turns,
                    now,
                    current.snapshot["budget_root_id"],
                    current.tenant_id,
                    release_model_turns,
                ),
            )
            if released.rowcount != 1:
                raise CoreError("INVALID_TASK_STATE")
        if include_shared_budget and result is not None:
            budget = connection.execute(
                """SELECT max_model_turns, max_tool_calls, used_model_turns,
                          used_tool_calls
                   FROM core_budget_ledgers
                   WHERE root_run_id = %s AND tenant_id = %s FOR UPDATE""",
                (current.snapshot["budget_root_id"], current.tenant_id),
            ).fetchone()
            if budget is None:
                raise CoreError("TASK_NOT_FOUND")
            result = {
                **result,
                "shared_budget": {
                    "scope": "root",
                    "used": {
                        "model_turns": budget["used_model_turns"],
                        "tool_calls": budget["used_tool_calls"],
                    },
                    "limits": {
                        "model_turns": budget["max_model_turns"],
                        "tool_calls": budget["max_tool_calls"],
                    },
                },
            }
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
        lease_fence = ""
        parameters = [
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
        ]
        if lease_token is not None:
            lease_fence = """ AND lease_token = %s
                AND lease_expires_at > EXTRACT(EPOCH FROM clock_timestamp())"""
            parameters.append(lease_token)
        updated = connection.execute(
            """UPDATE core_runs SET state = %s, version = version + 1,
                   snapshot = %s, pending_approval_id = %s, result = %s,
                   error_code = %s, updated_at = %s
               WHERE run_id = %s AND tenant_id = %s AND owner_id = %s
                 AND version = %s"""
            + lease_fence,
            parameters,
        )
        if updated.rowcount != 1:
            raise CoreError(
                "LEASE_LOST" if lease_token is not None else "SESSION_CONFLICT"
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
        consume_model_turns=0,
        consume_tool_calls=0,
        release_model_turns=0,
        include_shared_budget=False,
        connection=None,
        terminal_intent=_PRESERVE,
    ):
        with (self.database.transaction() if connection is None else nullcontext(connection)) as connection:
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
                consume_model_turns=consume_model_turns,
                consume_tool_calls=consume_tool_calls,
                release_model_turns=release_model_turns,
                include_shared_budget=include_shared_budget,
                terminal_intent=terminal_intent,
            )

    def acquire_lease(self, run_id, *, tenant_id, owner_id, worker_id, ttl):
        token = str(uuid.uuid4())
        with self.database.transaction() as connection:
            row = connection.execute(
                """SELECT state, lease_expires_at FROM core_runs
                   WHERE run_id = %s AND tenant_id = %s AND owner_id = %s
                   FOR UPDATE""",
                (run_id, tenant_id, owner_id),
            ).fetchone()
            now = connection.execute(
                """SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision
                          AS now"""
            ).fetchone()["now"]
            if (
                row is None
                or row["state"] in TERMINAL_STATES
                or (
                    row["lease_expires_at"] is not None
                    and row["lease_expires_at"] > now
                )
            ):
                raise CoreError("LEASE_LOST")
            connection.execute(
                """UPDATE core_runs SET lease_owner = %s, lease_token = %s,
                       lease_expires_at = %s, updated_at = %s
                   WHERE run_id = %s AND tenant_id = %s""",
                (worker_id, token, now + ttl, now, run_id, tenant_id),
            )
        return token

    def renew_lease(self, run_id, *, tenant_id, worker_id, token, ttl):
        with self.database.transaction() as connection:
            row = connection.execute(
                """SELECT lease_owner, lease_token, lease_expires_at
                   FROM core_runs WHERE run_id = %s AND tenant_id = %s
                   FOR UPDATE""",
                (run_id, tenant_id),
            ).fetchone()
            now = connection.execute(
                """SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision
                          AS now"""
            ).fetchone()["now"]
            if (
                row is None
                or row["lease_owner"] != worker_id
                or row["lease_token"] != token
                or (row["lease_expires_at"] or 0) <= now
            ):
                raise CoreError("LEASE_LOST")
            connection.execute(
                """UPDATE core_runs SET lease_expires_at = %s, updated_at = %s
                   WHERE run_id = %s AND tenant_id = %s""",
                (now + ttl, now, run_id, tenant_id),
            )

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

    def recoverable(self, *, states=None, root_only=False, limit=100):
        with self.database.pool.connection() as connection:
            state_filter = (
                "state = ANY(%s)"
                if states is not None
                else "state NOT IN ('COMPLETED','FAILED','CANCELLED','REJECTED','ABORTED')"
            )
            values = [list(states)] if states is not None else []
            root_filter = "AND parent_run_id IS NULL" if root_only else ""
            rows = connection.execute(
                f"""SELECT * FROM core_runs
                    WHERE {state_filter}
                      {root_filter}
                      AND (cancel_requested OR snapshot ? 'terminal_intent' OR NOT EXISTS (
                          SELECT 1 FROM core_waits wait WHERE wait.run_id = core_runs.run_id
                          AND wait.resolved_at IS NULL))
                      AND (lease_expires_at IS NULL OR lease_expires_at <=
                           EXTRACT(EPOCH FROM clock_timestamp()))
                    ORDER BY updated_at LIMIT %s""",
                (*values, limit),
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
