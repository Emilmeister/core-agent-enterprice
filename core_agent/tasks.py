from __future__ import annotations

import threading
import time
import uuid
import copy
import math
from dataclasses import dataclass, field

from .config import MAX_SUBAGENT_DEPTH
from .errors import CoreError
from .mcp import mcp_tool_index
from .workflow import SuspendedRun
from .peer_conversations import initial_conversation, merge_observation, OBSERVATION_TTL, validate_request_provenance


REMOTE_TASK_PENDING = object()
REMOTE_KIND = "remote_a2a"
_REMOTE_TERMINAL = {"completed", "failed", "canceled"}
REMOTE_PROGRESS_STATES = frozenset({"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING", "TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"})


@dataclass(frozen=True)
class RemoteTaskClaim:
    task_id: str
    tenant_id: str
    owner_id: str
    token: str


def remote_timeout_result(contract):
    return {"agent_name": contract["peer_name"], "reason": "timeout",
            "message": "Мы уже ожидали эту задачу, срок истёк. Повторное ожидание недоступно; можно создать новую задачу. Исход предыдущей неизвестен.",
            "remote_outcome": "unknown", "can_create_new_task": True}


def _remote_text(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise CoreError("CHECKPOINT_INVALID")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise CoreError("CHECKPOINT_INVALID") from None


def _remote_contract(contract, tenant_id, owner_id):
    required = {"version", "tenant_id", "owner_id", "peer_id", "peer_revision", "peer_name", "url", "binding",
                "message_id", "task", "timeout_seconds", "poll_interval_seconds"}
    if (not isinstance(contract, dict) or type(contract.get("version")) is not int
            or contract["version"] not in {1, 2, 3}):
        raise CoreError("CHECKPOINT_INVALID")
    if contract["version"] >= 2:
        required |= {"caller_scope", "attachment_limit_bytes", "outgoing_files"}
    if (not required <= contract.keys() <= required | {"_trace_parent"}
            or contract["tenant_id"] != tenant_id or contract["owner_id"] != owner_id):
        raise CoreError("CHECKPOINT_INVALID")
    for key in ("tenant_id", "owner_id", "peer_id", "peer_name", "url", "message_id", "task"):
        _remote_text(contract[key])
    for key in ("peer_revision", "timeout_seconds", "poll_interval_seconds"):
        maximum = 2**63 - 1 if key == "peer_revision" else 2147483647
        if type(contract[key]) is not int or not 1 <= contract[key] <= maximum:
            raise CoreError("CHECKPOINT_INVALID")
    if not isinstance(contract["binding"], str) or contract["binding"] not in {"JSONRPC", "HTTP+JSON"}:
        raise CoreError("CHECKPOINT_INVALID")
    if contract["version"] >= 2:
        from .response_files import ResponseFileService
        from .workspace import WorkspaceBinding

        scope, limit, refs = contract["caller_scope"], contract["attachment_limit_bytes"], contract["outgoing_files"]
        if (not isinstance(scope, dict) or scope.keys() != {"owner_id", "context_id", "task_id", "run_id"}
                or scope["run_id"] != owner_id or type(limit) is not int or not 1 <= limit <= 2147483647
                or not isinstance(refs, list)):
            raise CoreError("CHECKPOINT_INVALID")
        for value in scope.values():
            _remote_text(value)
        try:
            validated = ResponseFileService.validate_refs(
                WorkspaceBinding(tenant_id, scope["owner_id"], scope["context_id"]), refs,
                task_id=scope["task_id"], run_id=scope["run_id"], limit_bytes=limit,
            )
            if any(ref["limit_bytes"] != limit for ref in validated):
                raise CoreError("CHECKPOINT_INVALID")
        except CoreError:
            raise CoreError("CHECKPOINT_INVALID") from None
    if "_trace_parent" in contract:
        trace = contract["_trace_parent"]
        if not isinstance(trace, dict) or trace.keys() != {"trace_id", "span_id", "trace_flags"}:
            raise CoreError("CHECKPOINT_INVALID")
        for key, length in (("trace_id", 32), ("span_id", 16), ("trace_flags", 2)):
            value = trace[key]
            if not isinstance(value, str) or len(value) != length or any(c not in "0123456789abcdef" for c in value):
                raise CoreError("CHECKPOINT_INVALID")
    from .remote_agents import _trusted_endpoint
    try:
        _trusted_endpoint(contract["url"])
    except CoreError:
        raise CoreError("CHECKPOINT_INVALID") from None


def _initial_remote_checkpoint(contract=None):
    checkpoint = {"version": 1, "send_started": False, "deadline": None, "remote_task_id": None,
                  "remote_context_id": None, "next_poll_at": None, "cancel_started": False}
    if contract is not None and contract["version"] == 3:
        checkpoint.update(version=2, input_request=None, reply=None, answered_inputs=[])
    return checkpoint


def _remote_input_id(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise CoreError("CHECKPOINT_INVALID")


def _remote_reply(reply, contract=None):
    keys = {"input_id", "message_id", "task", "outgoing_files", "request_provenance", "status"}
    if (not isinstance(reply, dict) or reply.keys() != keys
            or reply.get("status") not in {"queued", "started", "confirmed"}):
        raise CoreError("CHECKPOINT_INVALID")
    _remote_input_id(reply["input_id"])
    for key in ("message_id", "task"):
        _remote_text(reply[key])
    if len(reply["message_id"].encode("utf-8")) > 4096 or len(reply["task"].encode("utf-8")) > 256 * 1024:
        raise CoreError("CHECKPOINT_INVALID")
    validate_request_provenance(reply["request_provenance"])
    if not isinstance(reply["outgoing_files"], list):
        raise CoreError("CHECKPOINT_INVALID")
    if contract is not None:
        if contract["version"] != 3:
            raise CoreError("CHECKPOINT_INVALID")
        _remote_contract({**contract, "outgoing_files": reply["outgoing_files"]}, contract["tenant_id"], contract["owner_id"])


def validate_remote_reply(reply, contract):
    _remote_contract(contract, contract.get("tenant_id") if isinstance(contract, dict) else None,
                     contract.get("owner_id") if isinstance(contract, dict) else None)
    if not isinstance(reply, dict) or "status" in reply:
        raise CoreError("CHECKPOINT_INVALID")
    _remote_reply({**reply, "status": "queued"}, contract)
    return copy.deepcopy(reply)


def _remote_checkpoint(checkpoint, contract=None):
    baseline = _initial_remote_checkpoint()
    if (not isinstance(checkpoint, dict) or type(checkpoint.get("version")) is not int
            or checkpoint["version"] not in {1, 2, 3}
            or checkpoint.keys() != baseline.keys() | ({"input_request", "reply", "answered_inputs"} if checkpoint["version"] >= 2 else set())
                | ({"received_artifacts", "file_batch_ids"} if checkpoint["version"] == 3 else set())
            or type(checkpoint["send_started"]) is not bool or type(checkpoint["cancel_started"]) is not bool):
        raise CoreError("CHECKPOINT_INVALID")
    for key in ("deadline", "next_poll_at"):
        value = checkpoint[key]
        if value is None:
            continue
        try:
            valid = type(value) in (int, float) and math.isfinite(value) and value > 0
        except OverflowError:
            valid = False
        if not valid:
            raise CoreError("CHECKPOINT_INVALID")
    for key in ("remote_task_id", "remote_context_id"):
        if checkpoint[key] is not None:
            _remote_text(checkpoint[key])
    if (not checkpoint["send_started"] and any(checkpoint[key] != value for key, value in baseline.items() if key != "version")
            or checkpoint["send_started"] and checkpoint["deadline"] is None
            or checkpoint["cancel_started"] and checkpoint["remote_task_id"] is None):
        raise CoreError("CHECKPOINT_INVALID")
    if checkpoint["version"] >= 2:
        if contract is not None and contract["version"] != 3:
            raise CoreError("CHECKPOINT_INVALID")
        pending, reply, answered = checkpoint["input_request"], checkpoint["reply"], checkpoint["answered_inputs"]
        if not isinstance(answered, list) or len(answered) > 200:
            raise CoreError("CHECKPOINT_INVALID")
        for identity in answered:
            _remote_input_id(identity)
        if len(set(answered)) != len(answered):
            raise CoreError("CHECKPOINT_INVALID")
        if pending is not None:
            if not isinstance(pending, dict) or pending.keys() != {"input_id", "question"}:
                raise CoreError("CHECKPOINT_INVALID")
            _remote_input_id(pending["input_id"])
            _remote_text(pending["question"])
            if len(pending["question"].encode("utf-8")) > 16384 or pending["input_id"] in answered:
                raise CoreError("CHECKPOINT_INVALID")
        if reply is not None:
            _remote_reply(reply, contract)
            if (reply["status"] == "confirmed" and reply["input_id"] not in answered
                    or reply["status"] != "confirmed" and (pending is None or reply["input_id"] != pending["input_id"])
                    or reply["status"] != "confirmed" and reply["input_id"] in answered):
                raise CoreError("CHECKPOINT_INVALID")
        if (pending is not None or reply is not None or answered) and (not checkpoint["send_started"] or checkpoint["remote_task_id"] is None):
            raise CoreError("CHECKPOINT_INVALID")
    if checkpoint["version"] == 3:
        receipts, batches = checkpoint["received_artifacts"], checkpoint["file_batch_ids"]
        if not isinstance(receipts, list) or len(receipts) > 64 or not isinstance(batches, list) or len(batches) > 65:
            raise CoreError("CHECKPOINT_INVALID")
        seen, size, parts = set(), 0, 0
        for receipt in receipts:
            if not isinstance(receipt, dict) or receipt.keys() != {"artifact_id", "digest", "size_bytes", "part_count"}:
                raise CoreError("CHECKPOINT_INVALID")
            _remote_text(receipt["artifact_id"])
            _remote_input_id(receipt["digest"])
            if (receipt["artifact_id"] in seen or type(receipt["size_bytes"]) is not int or receipt["size_bytes"] < 0
                    or type(receipt["part_count"]) is not int or not 1 <= receipt["part_count"] <= 256):
                raise CoreError("CHECKPOINT_INVALID")
            seen.add(receipt["artifact_id"])
            size += receipt["size_bytes"]
            parts += receipt["part_count"]
        if parts > 256 or contract is not None and size > contract["attachment_limit_bytes"]:
            raise CoreError("CHECKPOINT_INVALID")
        for batch in batches:
            _remote_text(batch)
        if len(set(batches)) != len(batches) or not checkpoint["send_started"] or checkpoint["remote_task_id"] is None:
            raise CoreError("CHECKPOINT_INVALID")


def _advance_remote_checkpoint(old, new, contract, now, cancel_requested, outcome, *, file_batch_id=None):
    _remote_checkpoint(old, contract)
    new = copy.deepcopy(new)
    if isinstance(new, dict) and new.get("send_started") is True and not old["send_started"]:
        if cancel_requested:
            raise CoreError("CANCEL_REQUESTED")
        if new.get("deadline") is not None:
            raise CoreError("CHECKPOINT_INVALID")
        new["deadline"] = now + contract["timeout_seconds"]
    _remote_checkpoint(new, contract)
    if (new["version"] < old["version"] or new["version"] >= 2 and contract["version"] != 3
            or old["send_started"] and not new["send_started"] or old["cancel_started"] and not new["cancel_started"]
            or old["deadline"] is not None and new["deadline"] != old["deadline"]
            or any(old[key] is not None and new[key] != old[key] for key in ("remote_task_id", "remote_context_id"))
            or new["cancel_started"] and not old["cancel_started"] and not cancel_requested):
        raise CoreError("CHECKPOINT_INVALID")
    if new["version"] >= 2:
        old_answered = old.get("answered_inputs", [])
        old_reply, new_reply = old.get("reply"), new["reply"]
        if new["answered_inputs"][:len(old_answered)] != old_answered:
            raise CoreError("CHECKPOINT_INVALID")
        added = new["answered_inputs"][len(old_answered):]
        if added and (len(added) != 1 or new_reply is None or new_reply["status"] != "confirmed"
                      or added != [new_reply["input_id"]]):
            raise CoreError("CHECKPOINT_INVALID")
        if old_reply is not None:
            if new_reply is None or any(new_reply[key] != old_reply[key] for key in old_reply if key != "status"):
                raise CoreError("CHECKPOINT_INVALID")
            allowed = {"queued": {"queued", "started"}, "started": {"started", "confirmed"}, "confirmed": {"confirmed"}}
            if new_reply["status"] not in allowed[old_reply["status"]]:
                raise CoreError("CHECKPOINT_INVALID")
        elif new_reply is not None:
            # Reply admission is handled separately under the operation lock.
            raise CoreError("CHECKPOINT_INVALID")
        old_input = old.get("input_request")
        if old_input is not None and old_input != new["input_request"]:
            if new_reply is None or new_reply["status"] != "confirmed" or new_reply["input_id"] != old_input["input_id"]:
                raise CoreError("CHECKPOINT_INVALID")
        if new_reply is not None:
            _remote_reply(new_reply, contract)
    if new["version"] == 3:
        previous = old.get("received_artifacts", [])
        old_batches = old.get("file_batch_ids", [])
        if (new["received_artifacts"][:len(previous)] != previous or new["file_batch_ids"] != old_batches
                or file_batch_id is None and new["received_artifacts"] != previous):
            raise CoreError("CHECKPOINT_INVALID")
        if file_batch_id is not None:
            if file_batch_id in old_batches:
                raise CoreError("CHECKPOINT_INVALID")
            new["file_batch_ids"].append(file_batch_id)
            _remote_checkpoint(new, contract)
    if outcome is not None:
        if (not isinstance(outcome, tuple) or len(outcome) != 3 or not isinstance(outcome[0], str) or outcome[0] not in _REMOTE_TERMINAL
                or outcome[2] is not None and not isinstance(outcome[2], str)):
            raise CoreError("CHECKPOINT_INVALID")
        new["next_poll_at"] = None
    return new


def _pending_remote_input(contract, checkpoint, state, cancel_requested, now):
    _remote_contract(contract, contract["tenant_id"], contract["owner_id"])
    _remote_checkpoint(checkpoint, contract)
    if (contract["version"] != 3 or checkpoint["version"] < 2 or state != "working"
            or cancel_requested or _remote_expired(checkpoint, now) or checkpoint["cancel_started"]):
        return None
    pending, reply = checkpoint["input_request"], checkpoint["reply"]
    if pending is None or reply is not None and reply["status"] != "confirmed":
        return None
    return {**copy.deepcopy(pending), "contract": copy.deepcopy(contract)}


def _remote_reply_status(checkpoint, input_id, state, cancel_requested, now):
    _remote_input_id(input_id)
    if input_id in checkpoint.get("answered_inputs", ()):
        return "confirmed"
    reply = checkpoint.get("reply")
    if reply is None or reply["input_id"] != input_id:
        return None
    if reply["status"] == "queued" and state == "working" and not cancel_requested and not _remote_expired(checkpoint, now):
        return "queued"
    return "unconfirmed"


def _queue_remote_reply(contract, checkpoint, state, cancel_requested, now, reply):
    _remote_contract(contract, contract["tenant_id"], contract["owner_id"])
    _remote_checkpoint(checkpoint, contract)
    if not isinstance(reply, dict) or "status" in reply:
        raise CoreError("CHECKPOINT_INVALID")
    queued = {**copy.deepcopy(reply), "status": "queued"}
    _remote_reply(queued, contract)
    previous = checkpoint.get("reply")
    if previous is not None and previous["message_id"] == queued["message_id"]:
        if {**previous, "status": "queued"} != queued:
            raise CoreError("MESSAGE_ID_CONFLICT")
        return copy.deepcopy(checkpoint), False
    if state != "working":
        raise CoreError("INVALID_TASK_STATE")
    if cancel_requested or checkpoint["cancel_started"]:
        raise CoreError("CANCEL_REQUESTED")
    if _remote_expired(checkpoint, now):
        raise CoreError("REMOTE_OPERATION_TIMEOUT")
    pending = _pending_remote_input(contract, checkpoint, state, cancel_requested, now)
    if pending is None or pending["input_id"] != queued["input_id"]:
        raise CoreError("REMOTE_INPUT_STALE")
    new = {**copy.deepcopy(checkpoint), "reply": queued, "next_poll_at": now}
    _remote_checkpoint(new, contract)
    return new, True


def _new_remote_input(old, new):
    pending = new.get("input_request")
    return pending is not None and old.get("input_request") != pending


def _remote_expired(checkpoint, now):
    return checkpoint["deadline"] is not None and checkpoint["deadline"] <= now


def _remote_due(checkpoint, now, cancel_requested):
    return cancel_requested or _remote_expired(checkpoint, now) or checkpoint["next_poll_at"] is None or checkpoint["next_poll_at"] <= now


def _remote_progress(progress, contract, checkpoint, outcome):
    if progress is None:
        return None
    required = {"agent_name", "remote_state"}
    extra = {"input_id", "question", "owner_wait"} if contract["version"] == 3 else set()
    question = {"input_id", "question"}
    if (not isinstance(progress, dict) or not required <= progress.keys() <= required | extra
            or bool(question & progress.keys()) and not question <= progress.keys()
            or "owner_wait" in progress and (progress["owner_wait"] is not True or bool(question & progress.keys()))
            or progress["agent_name"] != contract["peer_name"]
            or not isinstance(progress["remote_state"], str) or progress["remote_state"] not in REMOTE_PROGRESS_STATES
            or checkpoint["remote_task_id"] is None or outcome is not None):
        raise CoreError("CHECKPOINT_INVALID")
    if "input_id" in progress and (checkpoint.get("input_request") != {key: progress[key] for key in question}
            or progress["remote_state"] != "TASK_STATE_INPUT_REQUIRED"):
        raise CoreError("CHECKPOINT_INVALID")
    return copy.deepcopy(progress)


def remote_result_projection(result):
    """The accepted batch is an internal publication capability, never mailbox data."""
    if isinstance(result, dict):
        return {key: copy.deepcopy(value) for key, value in result.items() if key not in {"file_batch_id", "file_batch_ids"}}
    return copy.deepcopy(result)


def _remote_cancelled_outcome(contract, outcome, cancel_requested):
    if contract["version"] == 3 and cancel_requested and outcome is not None and outcome[0] == "completed":
        return ("canceled", {"agent_name": contract["peer_name"], "reason": "caller_cancelled",
                             "remote_outcome": "completed"}, None)
    return outcome


def _remote_file_binding(contract, batch, outcome, task_id, checkpoint=None):
    from .workspace import WorkspaceBinding

    early = contract["version"] == 3 and checkpoint is not None and checkpoint.get("version") == 3
    if (not isinstance(batch, dict) or batch.keys() != {
            "batch_id", "lease_token", "actor_id", "message_id", "request_digest"}
            or contract["version"] not in {2, 3}):
        raise CoreError("CHECKPOINT_INVALID")
    if (not early or outcome is not None) and (not isinstance(outcome, tuple) or len(outcome) != 3
            or outcome[0] != "completed" or outcome[2] is not None
            or not isinstance(outcome[1], dict) or outcome[1].keys() != {"agent_name", "remote_state", "text"}
            or outcome[1]["agent_name"] != contract["peer_name"]
            or outcome[1]["remote_state"] not in {None, "TASK_STATE_COMPLETED"}
            or not isinstance(outcome[1]["text"], str)):
        raise CoreError("CHECKPOINT_INVALID")
    for value in batch.values():
        _remote_text(value)
    scope = contract["caller_scope"]
    message_id = task_id + ":artifact:" + batch["request_digest"] if early else task_id
    if batch["actor_id"] != scope["owner_id"] or batch["message_id"] != message_id:
        raise CoreError("FILE_BATCH_NOT_FOUND")
    return WorkspaceBinding(contract["tenant_id"], scope["owner_id"], scope["context_id"])


def remote_file_batch_ids(result):
    if not isinstance(result, dict):
        return []
    return list(result.get("file_batch_ids", [])) or ([result["file_batch_id"]] if result.get("file_batch_id") else [])


def _remote_result_batches(result, checkpoint):
    if checkpoint.get("file_batch_ids"):
        return {**(result or {}), "file_batch_ids": list(checkpoint["file_batch_ids"])}
    return result


@dataclass(frozen=True)
class Notification:
    id: str
    owner_id: str
    task_id: str
    kind: str
    revision: int
    payload: dict


class DurableMailbox:
    def __init__(self, owner_id):
        self.owner_id = owner_id
        self._events = {}
        self._acked = set()
        self._lock = threading.RLock()

    def deliver(self, event):
        key = (event.task_id, event.kind, event.revision)
        with self._lock:
            if key in self._events:
                return False
            self._events[key] = event
            return True

    def poll(self):
        with self._lock:
            return tuple(
                event for event in self._events.values() if event.id not in self._acked
            )

    def ack(self, notification_id):
        self._acked.add(notification_id)


class _ScopedMailbox:
    """Include legacy local notifications without exposing another tenant's remote work."""

    def __init__(self, local, remote):
        self.local, self.remote = local, remote

    def poll(self):
        return self.local.poll() + self.remote.poll()

    def ack(self, notification_id):
        self.local.ack(notification_id)
        self.remote.ack(notification_id)

    def deliver(self, event):
        return self.local.deliver(event)


@dataclass
class BackgroundTask:
    id: str
    owner_id: str
    required: bool
    state: str = "submitted"
    result: object = None
    error: object = None
    revision: int = 0
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    condition: threading.Condition = field(
        default_factory=threading.Condition, repr=False
    )


class TaskScheduler:
    def __init__(self, telemetry=None, *, clock=time.time):
        self._tasks = {}
        self._admitting = set()
        self._mailboxes = {}
        self._closed = False
        self._kinds = {}
        self._cancel_callbacks = {}
        self._handlers = {}
        self._workflow_outcome = None
        self._recovery = {}
        self._suspended = set()
        self._active = set()
        self._threads = set()
        self._lock = threading.RLock()
        self.telemetry = telemetry
        self.active_compute_waiters = 0
        self.clock = clock
        self._remote = {}

    def start_remote(self, contract, *, owner_id, tenant_id, task_id, required=True, admission=None, trace_context=None):
        _remote_contract(contract, tenant_id, owner_id)
        _remote_text(task_id)
        if REMOTE_KIND not in self._handlers or self._closed:
            raise CoreError("INVALID_TASK_STATE")
        return self.start(None, owner_id=owner_id, tenant_id=tenant_id, task_id=task_id, required=required,
                          kind=REMOTE_KIND, contract=copy.deepcopy(contract), recoverable=True, mutating=True,
                          admission=admission, trace_context=trace_context)

    def is_remote(self, task_id, *, owner_id, tenant_id):
        self.get(task_id, owner_id=owner_id, tenant_id=tenant_id)
        return self._kinds.get(task_id) == REMOTE_KIND

    def remote_progress(self, *, owner_id, tenant_id):
        """Read safe projection data without invoking the model's task-list path."""
        values = []
        with self._lock:
            now = self.clock()
            for task_id, row in sorted(self._remote.items()):
                contract, checkpoint = row["contract"], row["checkpoint"]
                task = self._tasks[task_id]
                if contract["tenant_id"] != tenant_id or contract["owner_id"] != owner_id or task.state != "working":
                    continue
                _remote_contract(contract, tenant_id, owner_id)
                _remote_checkpoint(checkpoint)
                if _remote_expired(checkpoint, now):
                    continue
                progress = _remote_progress(task.result, contract, checkpoint, None)
                if progress is not None:
                    values.append({"task_id": task_id, "revision": task.revision,
                                   "agent_name": progress["agent_name"], "remote_state": progress["remote_state"]})
        return tuple(values)

    def remote_input(self, task_id, *, owner_id, tenant_id):
        with self._lock:
            task = self.get(task_id, owner_id=owner_id, tenant_id=tenant_id)
            row = self._remote.get(task_id)
            if row is None:
                raise CoreError("TASK_NOT_FOUND")
            return _pending_remote_input(row["contract"], row["checkpoint"], task.state,
                                         row["cancel_requested"], self.clock())

    def remote_reply_status(self, task_id, input_id, *, owner_id, tenant_id):
        with self._lock:
            task = self.get(task_id, owner_id=owner_id, tenant_id=tenant_id)
            row = self._remote.get(task_id)
            if row is None:
                raise CoreError("TASK_NOT_FOUND")
            _remote_checkpoint(row["checkpoint"], row["contract"])
            return _remote_reply_status(row["checkpoint"], input_id, task.state, row["cancel_requested"], self.clock())

    def queue_remote_reply(self, task_id, reply, *, owner_id, tenant_id, admission=None):
        with self._lock:
            task = self.get(task_id, owner_id=owner_id, tenant_id=tenant_id)
            row = self._remote.get(task_id)
            if row is None:
                raise CoreError("TASK_NOT_FOUND")
            now = self.clock()
            checkpoint, changed = _queue_remote_reply(row["contract"], row["checkpoint"], task.state,
                                                     row["cancel_requested"], now, reply)
            if admission is not None:
                admission(None)
            if changed:
                now = self.clock()
                if _remote_expired(row["checkpoint"], now):
                    # The memory workflow callback has already committed its receipt;
                    # publish the original timeout instead of admitting a late Send.
                    row["checkpoint"] = checkpoint
                    self._finish_remote_locked(row, task, ("failed", remote_timeout_result(row["contract"]), "REMOTE_OPERATION_TIMEOUT"))
                    return task
                row["checkpoint"], row["updated_at"] = checkpoint, now
                task.result = {key: value for key, value in (task.result or {}).items() if key not in {"input_id", "question"}}
                task.revision += 1
                with task.condition:
                    task.condition.notify_all()
            return task

    def observe_remote(self, task_id, *, owner_id, tenant_id, visible):
        if type(visible) is not bool:
            raise CoreError("REQUEST_INVALID")
        with self._lock:
            self.get(task_id, owner_id=owner_id, tenant_id=tenant_id)
            row = self._remote.get(task_id)
            if row is None:
                raise CoreError("TASK_NOT_FOUND")
            now = self.clock()
            if visible and self._tasks[task_id].state not in _REMOTE_TERMINAL and not _remote_expired(row["checkpoint"], now):
                row["observed_until"] = now + OBSERVATION_TTL
                checkpoint = row["checkpoint"]
                if checkpoint["remote_task_id"] is not None:
                    # Never invalidate the revision used by an outstanding compute claim.
                    checkpoint["next_poll_at"] = min(checkpoint["next_poll_at"] or now, now + 15, checkpoint["deadline"])
            return row.get("observed_until")

    def _remote_claim(self, claim, *, terminal=False):
        if self._closed or not isinstance(claim, RemoteTaskClaim) or not isinstance(claim.token, str) or not claim.token:
            raise CoreError("LEASE_LOST")
        row = self._remote.get(claim.task_id)
        if row is None or row["contract"]["tenant_id"] != claim.tenant_id or row["contract"]["owner_id"] != claim.owner_id:
            raise CoreError("TASK_NOT_FOUND")
        task = self._tasks[claim.task_id]
        _remote_contract(row["contract"], claim.tenant_id, claim.owner_id)
        _remote_checkpoint(row["checkpoint"], row["contract"])
        if terminal and task.state in _REMOTE_TERMINAL:
            return row, task
        if task.state != "working" or row["token"] != claim.token:
            raise CoreError("LEASE_LOST")
        return row, task

    def _finish_remote_locked(self, row, task, outcome):
        state, result, error_code = outcome
        result = _remote_result_batches(result, row["checkpoint"])
        row["checkpoint"]["next_poll_at"] = None
        row["token"] = None
        task.state, task.result, task.error = state, copy.deepcopy(result), CoreError(error_code) if error_code else None
        task.revision += 1
        self._suspended.discard(task.id)
        self._recovery.pop(task.id, None)
        self.mailbox(task.owner_id, row["contract"]["tenant_id"], remote=True).deliver(Notification(
            str(uuid.uuid4()), task.owner_id, task.id, "task." + state, task.revision,
            {"result": remote_result_projection(result), "error_code": error_code},
        ))
        with task.condition:
            task.condition.notify_all()

    def read_remote_claim(self, claim):
        with self._lock:
            row, task = self._remote_claim(claim)
            now = self.clock()
            if _remote_expired(row["checkpoint"], now):
                self._finish_remote_locked(row, task, ("failed", remote_timeout_result(row["contract"]), "REMOTE_OPERATION_TIMEOUT"))
                raise CoreError("LEASE_LOST")
            return {"contract": copy.deepcopy(row["contract"]), "checkpoint": copy.deepcopy(row["checkpoint"]),
                    "cancel_requested": row["cancel_requested"], "now": now, "revision": task.revision,
                    "observed_until": row.get("observed_until")}

    def commit_remote_claim(self, claim, *, expected_revision, checkpoint, outcome=None, progress=None,
                            prepared_file_batch=None, conversation_observation=None):
        if prepared_file_batch is not None:
            return self._commit_remote_files(claim, expected_revision, checkpoint, outcome, progress, prepared_file_batch, conversation_observation)
        if isinstance(outcome, tuple) and len(outcome) > 1 and isinstance(outcome[1], dict) and {"file_batch_id", "file_batch_ids"} & outcome[1].keys():
            raise CoreError("CHECKPOINT_INVALID")
        with self._lock:
            row, task = self._remote_claim(claim, terminal=True)
            if task.state in _REMOTE_TERMINAL:
                return task
            now = self.clock()
            if _remote_expired(row["checkpoint"], now):
                self._finish_remote_locked(row, task, ("failed", remote_timeout_result(row["contract"]), "REMOTE_OPERATION_TIMEOUT"))
                return task
            if type(expected_revision) is not int or task.revision != expected_revision:
                raise CoreError("SESSION_CONFLICT")
            if row["contract"]["version"] == 3 and row["cancel_requested"]:
                conversation_observation, progress = None, None
                reply = checkpoint.get("reply")
                pending = row["checkpoint"]["input_request"]
                checkpoint = {**checkpoint, "input_request": None if reply and reply["status"] == "confirmed"
                              and (pending is None or reply["input_id"] == pending["input_id"]) else pending}
            outcome = _remote_cancelled_outcome(row["contract"], outcome, row["cancel_requested"])
            checkpoint = _advance_remote_checkpoint(row["checkpoint"], checkpoint, row["contract"], now, row["cancel_requested"], outcome)
            progress = _remote_progress(progress, row["contract"], checkpoint, outcome)
            conversation = merge_observation(row.get("conversation"), conversation_observation, now)
            if outcome is None and (row.get("observed_until") or 0) > now and checkpoint["remote_task_id"]:
                checkpoint["next_poll_at"] = min(checkpoint["next_poll_at"] or now, now + 15, checkpoint["deadline"])
            new_input = _new_remote_input(row["checkpoint"], checkpoint)
            row["checkpoint"] = checkpoint
            row["conversation"] = conversation
            row["updated_at"] = now
            if outcome is None:
                if progress is not None:
                    task.result = _remote_result_batches(progress, checkpoint)
                task.revision += 1
                if new_input:
                    self.mailbox(task.owner_id, row["contract"]["tenant_id"], remote=True).deliver(Notification(
                        str(uuid.uuid4()), task.owner_id, task.id, "task.input-required", task.revision,
                        {"task_id": task.id, "input_id": checkpoint["input_request"]["input_id"]},
                    ))
                    with task.condition:
                        task.condition.notify_all()
            else:
                self._finish_remote_locked(row, task, outcome)
            return task

    def _commit_remote_files(self, claim, expected_revision, checkpoint, outcome, progress, batch, conversation_observation):
        service = getattr(self, "chat_file_service", None)
        if service is None:
            raise CoreError("FILE_ADMISSION_TRANSACTION_REQUIRED")
        with self._lock:
            row, _ = self._remote_claim(claim, terminal=True)
            contract = copy.deepcopy(row["contract"])
        binding = _remote_file_binding(contract, batch, outcome, claim.task_id, checkpoint)
        scope = contract["caller_scope"]
        with service.caller_scope(binding, task_id=scope["task_id"], run_id=scope["run_id"]):
            with self._lock, service.store.lock:
                row, task = self._remote_claim(claim, terminal=True)
                if task.state in _REMOTE_TERMINAL:
                    return task
                if row["contract"] != contract or type(expected_revision) is not int or task.revision != expected_revision:
                    raise CoreError("SESSION_CONFLICT")
                if _remote_expired(row["checkpoint"], self.clock()):
                    self._finish_remote_locked(row, task, ("failed", remote_timeout_result(contract), "REMOTE_OPERATION_TIMEOUT"))
                    return task
                if row["cancel_requested"] or row["checkpoint"]["cancel_started"]:
                    raise CoreError("CANCEL_REQUESTED")
                updated = _advance_remote_checkpoint(row["checkpoint"], checkpoint, contract, self.clock(), False, outcome,
                    file_batch_id=batch["batch_id"] if checkpoint["version"] == 3 else None)
                _remote_progress(progress, contract, updated, outcome)
                if not updated["send_started"]:
                    raise CoreError("CHECKPOINT_INVALID")
                mailbox = self.mailbox(task.owner_id, claim.tenant_id, remote=True)
                with mailbox._lock, task.condition:
                    before_batch = service.store.get(batch["batch_id"], claim.tenant_id)
                    before_row = copy.deepcopy(row)
                    before_task = (task.state, copy.deepcopy(task.result), task.error, task.revision)
                    before_events = dict(mailbox._events)
                    suspended, recovery = task.id in self._suspended, self._recovery.get(task.id)
                    try:
                        service.bind(batch["batch_id"], binding, task_id=scope["task_id"], run_id=scope["run_id"],
                            actor_id=batch["actor_id"], message_id=batch["message_id"],
                            request_digest=batch["request_digest"], lease_token=batch["lease_token"])
                        with service.caller_scope(binding, task_id=scope["task_id"], run_id=scope["run_id"]):
                            self._remote_claim(claim)
                            if task.revision != expected_revision or row["contract"] != contract:
                                raise CoreError("SESSION_CONFLICT")
                            if _remote_expired(updated, self.clock()):
                                raise CoreError("LEASE_LOST")
                            if row["cancel_requested"]:
                                raise CoreError("CANCEL_REQUESTED")
                            row["conversation"] = merge_observation(row.get("conversation"), conversation_observation, self.clock())
                            row["updated_at"] = self.clock()
                            row["checkpoint"] = updated
                            if outcome is None:
                                task.result = _remote_result_batches(progress, updated)
                                task.revision += 1
                                if _new_remote_input(before_row["checkpoint"], updated):
                                    mailbox.deliver(Notification(str(uuid.uuid4()), task.owner_id, task.id,
                                        "task.input-required", task.revision,
                                        {"task_id": task.id, "input_id": updated["input_request"]["input_id"]}))
                                    task.condition.notify_all()
                            else:
                                self._finish_remote_locked(row, task, ("completed", {
                                    **outcome[1], **({} if updated["version"] == 3 else {"file_batch_id": batch["batch_id"]})}, None))
                    except BaseException:
                        service.store.rows[batch["batch_id"]] = before_batch
                        row.clear()
                        row.update(before_row)
                        task.state, task.result, task.error, task.revision = before_task
                        mailbox._events = before_events
                        if suspended:
                            self._suspended.add(task.id)
                        if recovery is not None:
                            self._recovery[task.id] = recovery
                        raise
                return task

    def expire_remote(self, limit=100, *, tenant_id=None):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise CoreError("CONFIG_INVALID")
        expired = 0
        with self._lock:
            for identity, row in tuple(self._remote.items()):
                if tenant_id is not None and row["contract"]["tenant_id"] != tenant_id:
                    continue
                task = self._tasks[identity]
                _remote_checkpoint(row["checkpoint"])
                if task.state not in _REMOTE_TERMINAL and _remote_expired(row["checkpoint"], self.clock()):
                    self._finish_remote_locked(row, task, ("failed", remote_timeout_result(row["contract"]), "REMOTE_OPERATION_TIMEOUT"))
                    expired += 1
                    if expired == limit:
                        break
        return expired

    def _execute_remote(self, task, tenant_id):
        with self._lock:
            row = self._remote[task.id]
            if task.state in _REMOTE_TERMINAL:
                return
            token = str(uuid.uuid4())
            row["token"] = token
            task.state = "working"
            claim = RemoteTaskClaim(task.id, tenant_id, task.owner_id, token)
        try:
            self.read_remote_claim(claim)
            self._handlers[REMOTE_KIND](claim, task.cancel_event)
        except CoreError as error:
            if error.code not in {"LEASE_LOST", "WORKER_STOPPED"}:
                raise
        finally:
            with self._lock:
                if row["token"] == token:
                    row["token"] = None
                if task.state not in _REMOTE_TERMINAL:
                    self._suspended.add(task.id)

    def register(self, kind, handler):
        if kind in self._handlers:
            raise CoreError("CONFIG_CONFLICT", f"duplicate task handler: {kind}")
        self._handlers[kind] = handler

    def mailbox(self, owner_id, tenant_id="default", *, remote=False):
        key = (tenant_id, owner_id)
        if remote:
            return self._mailboxes.setdefault(key, DurableMailbox(owner_id))
        local = self._mailboxes.setdefault(owner_id, DurableMailbox(owner_id))
        return _ScopedMailbox(local, self._mailboxes[key]) if key in self._mailboxes else local

    def start(
        self,
        function,
        *,
        owner_id,
        task_id=None,
        required=False,
        accepts_cancel_event=False,
        trace_context=None,
        kind=None,
        contract=None,
        recoverable=False,
        tenant_id="default",
        continue_trace=False,
        admission=None,
        on_cancel=None,
        mutating=False,
    ):
        task_id = task_id or str(uuid.uuid4())
        if not isinstance(task_id, str) or not task_id:
            raise CoreError("SESSION_CONFLICT", "task_id must be unique")
        task = BackgroundTask(task_id, owner_id, required)
        linked_context = trace_context
        if self.telemetry and linked_context is None:
            with self.telemetry.span("core_agent.task.submit") as submission:
                linked_context = submission.context
        ready = threading.Event()
        admitted = threading.Event()

        def execute(function, accepts_cancel_event):
            ready.wait()
            if not admitted.is_set():
                return
            if kind == REMOTE_KIND:
                self._execute_remote(task, tenant_id)
                return
            span = None
            if self.telemetry:
                span = (
                    self.telemetry.span(
                        "core_agent.task.execute", parent=linked_context
                    )
                    if continue_trace
                    else self.telemetry.start_background_span(
                        "core_agent.task.execute", linked_context
                    )
                )
            context = span if span else _NullContext()
            with context:
                task.state = "working"
                try:
                    outcome = (
                        self._workflow_outcome(task.id, tenant_id, kind, contract or {})
                        if self._workflow_outcome is not None else None
                    )
                    value = None
                    if outcome is None or isinstance(outcome, SuspendedRun):
                        value = (
                            function(task.cancel_event)
                            if accepts_cancel_event
                            else function()
                        )
                    if isinstance(value, SuspendedRun):
                        with self._lock:
                            self._suspended.add(task.id)
                        return
                    final_state = (
                        "canceled" if task.cancel_event.is_set() else "completed"
                    )
                    captured_error = None
                except Exception as error:
                    value = None
                    canceled = task.cancel_event.is_set() and getattr(
                        error, "code", None
                    ) not in {"SIDE_EFFECT_UNKNOWN", "RECOVERY_REQUIRES_RECONCILIATION"}
                    final_state = "canceled" if canceled else "failed"
                    captured_error = None if canceled else error
            if self._workflow_outcome is not None:
                outcome = self._workflow_outcome(task.id, tenant_id, kind, contract or {})
                if isinstance(outcome, SuspendedRun):
                    with self._lock:
                        self._suspended.add(task.id)
                    return
                if outcome is not None:
                    final_state, value, error_code = outcome
                    captured_error = CoreError(error_code) if error_code else None
            with task.condition:
                task.result = value
                task.error = captured_error
                task.state = final_state
                task.revision += 1
                event = Notification(
                    str(uuid.uuid4()),
                    owner_id,
                    task.id,
                    f"task.{task.state}",
                    task.revision,
                    {"result": task.result, "error_code": getattr(task.error, "code", None)},
                )
                self.mailbox(owner_id).deliver(event)
                with self._lock:
                    self._cancel_callbacks.pop(task.id, None)
                    self._recovery.pop(task.id, None)
                task.condition.notify_all()

        def run(function=function, accepts_cancel_event=accepts_cancel_event):
            try:
                execute(function, accepts_cancel_event)
            finally:
                with self._lock:
                    self._active.discard(task.id)
                    self._threads.discard(threading.current_thread())

        thread = threading.Thread(target=run, daemon=True)
        with self._lock:
            if task.id in self._tasks or task.id in self._admitting:
                raise CoreError("SESSION_CONFLICT", "task_id must be unique")
            self._admitting.add(task.id)
            self._active.add(task.id)
            self._threads.add(thread)
        try:
            thread.start()
            if admission is not None:
                admission(None)
            with self._lock:
                self._tasks[task.id] = task
                self._kinds[task.id] = kind
                if kind == REMOTE_KIND:
                    self._remote[task.id] = {"contract": copy.deepcopy(contract), "checkpoint": _initial_remote_checkpoint(contract),
                                             "token": None, "cancel_requested": False, "conversation": initial_conversation(),
                                             "created_at": self.clock(), "updated_at": self.clock(), "observed_until": None}
                    self.mailbox(owner_id, tenant_id, remote=True)
                if recoverable:
                    self._recovery[task.id] = (run, dict(contract or {}), tenant_id)
                if on_cancel is not None:
                    self._cancel_callbacks[task.id] = on_cancel
                self._admitting.remove(task.id)
                admitted.set()
        except BaseException:
            with self._lock:
                self._admitting.discard(task.id)
                self._active.discard(task.id)
                self._threads.discard(thread)
            raise
        finally:
            ready.set()
        return task

    def recover(self, *, ready=None, tenant_id=None):
        if self._closed:
            return 0
        recovered = 0
        with self._lock:
            candidates = tuple((task_id, self._recovery.get(task_id), self._handlers.get(self._kinds.get(task_id)),
                                self._kinds.get(task_id) == REMOTE_KIND or self._tasks[task_id].cancel_event.is_set())
                               for task_id in self._suspended - self._active
                               if tenant_id is None or self._recovery.get(task_id, (None, None, None))[2] == tenant_id)
        for task_id, recovery, handler, bypass_ready in candidates:
            if recovery is None or handler is None:
                continue
            run, contract, task_tenant_id = recovery
            # Readiness reads workflow state; SDK projection takes workflow then
            # scheduler locks. Never call the workflow while holding this lock.
            ready_now = bypass_ready or ready is not None and ready(task_id, task_tenant_id)
            with self._lock:
                if (
                    self._closed
                    or self._recovery.get(task_id) is not recovery
                    or self._handlers.get(self._kinds.get(task_id)) is not handler
                    or task_id in self._active
                    or task_id not in self._suspended
                ):
                    continue
                canceled = self._tasks[task_id].cancel_event.is_set()
                remote = self._remote.get(task_id)
                if remote is not None:
                    canceled = remote["cancel_requested"]
                if remote is not None and not _remote_due(remote["checkpoint"], self.clock(), canceled):
                    continue
                if remote is None and not canceled and not ready_now:
                    continue
                # A concurrent pass must not reuse old readiness if this worker
                # resumes and reaches another wait before that pass returns.
                self._recovery[task_id] = (run, contract, task_tenant_id)
                self._suspended.remove(task_id)
                self._active.add(task_id)
                thread = threading.Thread(
                    target=run,
                    args=(
                        lambda cancel, callback=handler, data=contract: callback(data, cancel),
                        True,
                    ),
                    daemon=True,
                )
                self._threads.add(thread)
                try:
                    thread.start()
                except BaseException:
                    self._threads.discard(thread)
                    self._active.remove(task_id)
                    self._suspended.add(task_id)
                    raise
                if not canceled:
                    recovered += 1
        return recovered

    def count(self, *, owner_id, kind=None, active_only=False, tenant_id="default"):
        terminal = {"completed", "failed", "canceled"}
        return sum(
            task.owner_id == owner_id
            and (task.id not in self._remote or self._remote[task.id]["contract"]["tenant_id"] == tenant_id)
            and (kind is None or self._kinds.get(task.id) == kind)
            and (not active_only or task.state not in terminal)
            for task in self._tasks.values()
        )

    def get(self, task_id, *, owner_id=None, tenant_id="default"):
        try:
            task = self._tasks[task_id]
        except KeyError:
            raise CoreError("TASK_NOT_FOUND") from None
        if task_id in self._remote:
            contract = self._remote[task_id]["contract"]
            if contract["tenant_id"] != tenant_id or contract["owner_id"] != owner_id:
                raise CoreError("TASK_NOT_FOUND")
        if owner_id is not None and task.owner_id != owner_id:
            raise CoreError("POLICY_DENIED")
        return task

    def list(self, *, owner_id, tenant_id="default"):
        return tuple(task for task in self._tasks.values() if task.owner_id == owner_id
                     and (task.id not in self._remote or self._remote[task.id]["contract"]["tenant_id"] == tenant_id))

    def wait(self, task_id, timeout=None, *, owner_id=None, tenant_id="default"):
        task = self.get(task_id, owner_id=owner_id, tenant_id=tenant_id)
        end = None if timeout is None else time.monotonic() + timeout
        with task.condition:
            while task.state not in {"completed", "failed", "canceled"}:
                remaining = None if end is None else end - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError(task_id)
                task.condition.wait(remaining)
        return task

    def cancel(self, task_id, *, owner_id=None, tenant_id="default"):
        task = self.get(task_id, owner_id=owner_id, tenant_id=tenant_id)
        if task_id in self._remote:
            with self._lock:
                row = self._remote[task_id]
                if task.state in _REMOTE_TERMINAL:
                    return task
                if _remote_expired(row["checkpoint"], self.clock()):
                    self._finish_remote_locked(row, task, ("failed", remote_timeout_result(row["contract"]), "REMOTE_OPERATION_TIMEOUT"))
                    return task
                task.cancel_event.set()
                row["cancel_requested"] = True
                return task
        if (
            task.state in {"completed", "failed", "canceled"}
            or task.cancel_event.is_set()
        ):
            raise CoreError("TASK_NOT_CANCELABLE")
        task.cancel_event.set()
        callback = self._cancel_callbacks.pop(task.id, None)
        if callback is not None:
            callback()
        return task

    def assert_can_complete_parent(self, owner_id, tenant_id="default"):
        if any(
            task.owner_id == owner_id
            and (task.id not in self._remote or self._remote[task.id]["contract"]["tenant_id"] == tenant_id)
            and task.required
            and task.state not in {"completed", "failed", "canceled"}
            for task in self._tasks.values()
        ):
            raise CoreError("REQUIRED_TASK_PENDING")

    def close(self):
        self._closed = True


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


@dataclass(frozen=True)
class CapabilitySet:
    tools: frozenset[str]
    mcp: dict[str, frozenset[str]]
    skills: frozenset[str]
    features: frozenset[str]
    budgets: dict[str, int]
    kernel_version: str
    tenant_id: str
    memory_namespace: str | None = None


@dataclass(frozen=True)
class DelegationContract:
    """One tool list, whatever kind of tool it names.

    Built-in and MCP tools reach the model as one flat catalogue of canonical
    names, so asking it to sort them back into two arguments — the second keyed
    by server, under names the catalogue never showed — is asking it to know
    something only the runtime knows. The runtime holds that index already.
    """

    instruction: str
    tools: tuple[str, ...]
    skills: tuple[str, ...]
    budget: dict[str, int]
    background: bool = False

    def resolve(self, *, tools, mcp, skills, budgets):
        """Split the tool list against what the caller can actually delegate.

        The single place any of this is refused, so every refusal names what it
        refused and the three callers cannot drift apart on the rule.
        """
        index = mcp_tool_index(mcp)
        builtins = []
        delegated = {}
        for name in self.tools:
            if name in index:
                server, remote_tool = index[name]
                delegated.setdefault(server, set()).add(remote_tool)
            elif name in tools:
                builtins.append(name)
            else:
                raise CoreError(
                    "CAPABILITY_DISABLED",
                    f"this agent does not hold {name}; it holds "
                    f"{', '.join(sorted(set(tools) | set(index))) or 'no tools'}",
                )
        refused = sorted(set(self.skills) - set(skills))
        if refused:
            raise CoreError(
                "CAPABILITY_DISABLED",
                f"this agent does not hold skill {', '.join(refused)}; it holds "
                f"{', '.join(sorted(skills)) or 'no skills'}",
            )
        for key, value in self.budget.items():
            if value > budgets.get(key, value):
                raise CoreError(
                    "CAPABILITY_DISABLED",
                    f"budget {key} of {value} is above the {budgets[key]} "
                    "this agent holds",
                )
        return tuple(builtins), {
            server: frozenset(names) for server, names in delegated.items()
        }

    @classmethod
    def from_dict(cls, raw):
        required = {"instruction", "tools", "skills", "budget"}
        allowed = required | {"background"}
        if (
            not isinstance(raw, dict)
            or not required <= set(raw)
            or set(raw) - allowed
            or not isinstance(raw["instruction"], str)
            or not raw["instruction"].strip()
            or not isinstance(raw["tools"], list)
            or not all(isinstance(value, str) for value in raw["tools"])
            or not isinstance(raw["skills"], list)
            or not all(isinstance(value, str) for value in raw["skills"])
            or not isinstance(raw["budget"], dict)
            or set(raw["budget"]) != {"turns", "tool_calls"}
            or not all(
                isinstance(value, int)
                and not isinstance(value, bool)
                and value > 0
                for value in raw["budget"].values()
            )
            or not isinstance(raw.get("background", False), bool)
        ):
            # The schema already rejected the shape; reaching here means the
            # contract has a rule the schema cannot state, and a bare code would
            # send the model guessing at which one.
            raise CoreError(
                "TOOL_ARGUMENT_INVALID",
                "delegation needs instruction, a list of tools by their catalogue "
                "names, skills and both budget.turns and budget.tool_calls above zero",
            )
        return cls(
            raw["instruction"],
            tuple(raw["tools"]),
            tuple(raw["skills"]),
            dict(raw["budget"]),
            raw.get("background", False),
        )


def derive_child_capabilities(parent, contract, *, current_depth):
    depth_limit = min(parent.budgets.get("depth", 0), MAX_SUBAGENT_DEPTH)
    if current_depth >= depth_limit:
        raise CoreError(
            "BUDGET_EXCEEDED",
            f"delegation depth budget exhausted ({current_depth}/{depth_limit})",
            data={
                "dimension": "depth",
                "used": current_depth,
                "limit": depth_limit,
            },
        )
    builtins, mcp = contract.resolve(
        tools=parent.tools,
        mcp=parent.mcp,
        skills=parent.skills,
        budgets=parent.budgets,
    )
    budgets = {**parent.budgets, **contract.budget}
    tools = frozenset(
        tool
        for tool in builtins
        if tool != "core_delegate" or current_depth + 1 < depth_limit
    )
    return CapabilitySet(
        tools,
        mcp,
        frozenset(contract.skills),
        parent.features,
        budgets,
        parent.kernel_version,
        parent.tenant_id,
        # Shared memory now travels as a delegated core_memory_* tool, not as an
        # MCP server the parent handed over.
        parent.memory_namespace
        if any(tool.startswith("core_memory_") for tool in tools)
        else None,
    )
