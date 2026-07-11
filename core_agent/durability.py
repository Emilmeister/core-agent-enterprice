from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from .errors import CoreError


@dataclass(frozen=True)
class Event:
    run_id: str
    revision: int
    kind: str
    data: dict
    published_at: float


class InMemoryEventStore:
    def __init__(self):
        self._events = {}

    def append(self, run_id, kind, data):
        events = self._events.setdefault(run_id, [])
        event = Event(run_id, len(events) + 1, kind, dict(data), time.time())
        events.append(event)
        return event

    def events(self, run_id):
        return tuple(self._events.get(run_id, ()))

    def revision(self, run_id):
        return len(self._events.get(run_id, ()))

    def count(self, run_id, *, kind=None):
        return sum(
            1
            for event in self._events.get(run_id, ())
            if kind is None or event.kind == kind
        )


class CheckpointStore:
    def __init__(self):
        self._values = {}

    def save(self, run_id, revision, state):
        self._values[run_id] = (revision, dict(state))

    def load(self, run_id):
        return self._values.get(run_id)


@dataclass(frozen=True)
class Lease:
    run_id: str
    owner: str
    token: str
    expires_at: float


class LeaseManager:
    def __init__(self):
        self._leases = {}

    def acquire(self, run_id, owner, *, ttl):
        current = self._leases.get(run_id)
        now = time.monotonic()
        if current and current.expires_at > now:
            raise CoreError("LEASE_LOST")
        lease = Lease(run_id, owner, str(uuid.uuid4()), now + ttl)
        self._leases[run_id] = lease
        return lease

    def renew(self, run_id, owner, token, *, ttl):
        current = self._leases.get(run_id)
        if (
            not current
            or current.owner != owner
            or current.token != token
            or current.expires_at <= time.monotonic()
        ):
            raise CoreError("LEASE_LOST")
        lease = Lease(run_id, owner, token, time.monotonic() + ttl)
        self._leases[run_id] = lease
        return lease


@dataclass(frozen=True)
class RecoveredState:
    pending_approvals: tuple[str, ...]
    pending_inputs: tuple[str, ...]
    pending_notifications: tuple[str, ...]
    revision: int


class RecoveryManager:
    def __init__(self, events, checkpoints):
        self.events = events
        self.checkpoints = checkpoints

    def recover(self, run_id):
        events = self.events.events(run_id)
        intents = {
            event.data["tool_call_id"]
            for event in events
            if event.kind == "tool.intent" and event.data.get("mutating")
        }
        completed = {
            event.data.get("tool_call_id")
            for event in events
            if event.kind in {"tool.completed", "tool.failed"}
        }
        if intents - completed:
            raise CoreError("SIDE_EFFECT_UNKNOWN")
        return RecoveredState(
            tuple(
                event.data["approval_id"]
                for event in events
                if event.kind == "approval.required"
            ),
            tuple(
                event.data["input_id"]
                for event in events
                if event.kind == "input.required"
            ),
            tuple(
                event.data["notification_id"]
                for event in events
                if event.kind == "task.notification"
            ),
            len(events),
        )
