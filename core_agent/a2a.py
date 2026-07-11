from __future__ import annotations

import ipaddress
import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from urllib.parse import urlparse

from .config import RunRequest
from .errors import CoreError

CORE_EXTENSION_URI = "urn:core-agent:run-capabilities:v1"
APPROVAL_REQUEST_URI = "urn:core-agent:approval-request:v1"
APPROVAL_RESPONSE_URI = "urn:core-agent:approval-response:v1"


class TaskState(str, Enum):
    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    AUTH_REQUIRED = "auth-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    REJECTED = "rejected"


_STATE_MAP = {
    "CREATED": TaskState.SUBMITTED,
    "QUEUED": TaskState.SUBMITTED,
    "RUNNING": TaskState.WORKING,
    "WAITING_TASK": TaskState.WORKING,
    "PAUSED": TaskState.WORKING,
    "WAITING_INPUT": TaskState.INPUT_REQUIRED,
    "WAITING_APPROVAL": TaskState.INPUT_REQUIRED,
    "WAITING_AUTH": TaskState.AUTH_REQUIRED,
    "COMPLETED": TaskState.COMPLETED,
    "FAILED": TaskState.FAILED,
    "ABORTED": TaskState.FAILED,
    "CANCELLED": TaskState.CANCELED,
    "REJECTED": TaskState.REJECTED,
}


def map_core_state(state):
    try:
        return _STATE_MAP[state]
    except KeyError:
        raise CoreError("INVALID_TASK_STATE") from None


@dataclass(frozen=True)
class Part:
    kind: str
    data: object

    @classmethod
    def text(cls, value):
        return cls("text", value)


@dataclass(frozen=True)
class Message:
    role: str
    parts: tuple[Part, ...]
    extensions: tuple[str, ...] = ()
    metadata: dict = field(default_factory=dict)
    context_id: str | None = None

    @classmethod
    def user(cls, text):
        return cls("user", (Part.text(text),))

    @classmethod
    def from_run_request(cls, request, context_id=None):
        value = request.to_dict()
        prompt = value.pop("prompt")
        return cls(
            "user",
            (Part.text(prompt),),
            (CORE_EXTENSION_URI,),
            {CORE_EXTENSION_URI: value},
            context_id,
        )


def parse_run_request(message, requested_extensions):
    if (
        CORE_EXTENSION_URI not in requested_extensions
        or CORE_EXTENSION_URI not in message.extensions
    ):
        raise CoreError("A2A_EXTENSION_REQUIRED")
    if any(part.kind not in {"text", "file", "data"} for part in message.parts):
        raise CoreError("CONTENT_TYPE_NOT_SUPPORTED")
    prompt = "\n".join(
        json.dumps(part.data, sort_keys=True) if part.kind == "data" else str(part.data)
        for part in message.parts
    ).strip()
    extension = message.metadata.get(CORE_EXTENSION_URI, {})
    if not isinstance(extension, dict) or set(extension) != {"mcp", "skills"}:
        raise CoreError("INVALID_REQUEST")
    return RunRequest.from_dict(
        {
            "prompt": prompt,
            "mcp": extension.get("mcp", []),
            "skills": extension.get("skills", []),
        }
    )


@dataclass(frozen=True)
class ApprovalDecision:
    approval_id: str
    decision: str
    scope: str


def parse_approval_decision(message, requested_extensions):
    if (
        APPROVAL_RESPONSE_URI not in requested_extensions
        or APPROVAL_RESPONSE_URI not in message.extensions
    ):
        raise CoreError("A2A_EXTENSION_REQUIRED")
    payload = message.metadata.get(APPROVAL_RESPONSE_URI)
    if not isinstance(payload, dict) or set(payload) != {
        "approval_id",
        "decision",
        "scope",
    }:
        raise CoreError("INVALID_REQUEST")
    if (
        not isinstance(payload["approval_id"], str)
        or not payload["approval_id"]
        or payload["decision"] not in {"approve", "deny"}
        or not isinstance(payload["scope"], str)
        or not payload["scope"]
    ):
        raise CoreError("INVALID_REQUEST")
    return ApprovalDecision(
        payload["approval_id"], payload["decision"], payload["scope"]
    )


@dataclass(frozen=True)
class Artifact:
    id: str
    parts: tuple[Part, ...]
    revision: int = 0
    provenance: dict = field(default_factory=dict)
    media_type: str = "text/plain"
    _chunks: tuple[str, ...] = field(default_factory=tuple, repr=False)
    _last: bool = field(default=False, repr=False)

    @classmethod
    def text(cls, value, provenance=None):
        return cls(str(uuid.uuid4()), (Part.text(value),), 1, provenance or {})

    @property
    def size(self):
        return sum(len(str(part.data).encode()) for part in self.parts)

    @property
    def digest(self):
        content = b"\0".join(str(part.data).encode() for part in self.parts)
        return "sha256:" + hashlib.sha256(content).hexdigest()

    def append(self, part, *, chunk_id, sequence, last_chunk):
        if chunk_id in self._chunks:
            return self
        if self._last or sequence != self.revision + 1:
            raise CoreError("ARTIFACT_CHUNK_INVALID")
        return replace(
            self,
            parts=self.parts + (part,),
            revision=sequence,
            _chunks=self._chunks + (chunk_id,),
            _last=last_chunk,
        )


@dataclass(frozen=True)
class TaskEvent:
    task_id: str
    sequence: int
    revision: int
    state: TaskState


@dataclass
class Task:
    id: str
    context_id: str
    state: TaskState = TaskState.SUBMITTED
    artifacts: list[Artifact] = field(default_factory=list)
    history: list[TaskEvent] = field(default_factory=list)
    status_metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AgentCard:
    name: str
    protocol_versions: tuple[str, ...] = ("1.0",)
    bindings: tuple[str, ...] = ("HTTP+JSON",)
    capabilities: dict = field(
        default_factory=lambda: {"streaming": True, "pushNotifications": True}
    )
    skills: tuple[str, ...] = ()
    input_modes: tuple[str, ...] = ("text/plain", "application/json")
    output_modes: tuple[str, ...] = ("text/plain", "application/json")
    authentication: tuple[str, ...] = ()
    extensions: tuple[str, ...] = (CORE_EXTENSION_URI,)
    optional_extensions: tuple[str, ...] = (
        APPROVAL_REQUEST_URI,
        APPROVAL_RESPONSE_URI,
    )

    @classmethod
    def minimal(cls, name, protocol_versions=("1.0",)):
        return cls(name, tuple(protocol_versions))

    @classmethod
    def from_effective_config(cls, effective):
        return cls(
            effective.agent_name,
            effective.a2a_protocol_versions,
            effective.a2a_bindings,
            {"streaming": True, "pushNotifications": True},
            tuple(sorted(effective.skills)),
        )

    def to_dict(self):
        return {
            "name": self.name,
            "supportedInterfaces": [
                {"protocolVersion": version, "transport": binding}
                for version in self.protocol_versions
                for binding in self.bindings
            ],
            "capabilities": dict(self.capabilities),
            "skills": list(self.skills),
            "defaultInputModes": list(self.input_modes),
            "defaultOutputModes": list(self.output_modes),
            "securitySchemes": list(self.authentication),
            "extensions": [
                *({"uri": uri, "required": True} for uri in self.extensions),
                *({"uri": uri, "required": False} for uri in self.optional_extensions),
            ],
        }


class _Subscription:
    def close(self):
        pass


class A2AService:
    def __init__(self, handler, agent_card, event_store=None):
        self.handler = handler
        self.agent_card = agent_card
        self._tasks = {}
        self._push = {}
        self._conditions = {}
        self._closed = False
        self.event_store = event_store

    def create_task(self, context_id=None):
        task = Task(str(uuid.uuid4()), context_id or str(uuid.uuid4()))
        self._tasks[task.id] = task
        self._conditions[task.id] = threading.Condition()
        self._record(task, TaskState.SUBMITTED)
        return task

    def send_message(
        self, message, *, return_immediately=False, protocol_version="1.0"
    ):
        if protocol_version not in self.agent_card.protocol_versions:
            raise CoreError("A2A_VERSION_UNSUPPORTED")
        request = parse_run_request(message, {CORE_EXTENSION_URI})
        task = self.create_task(message.context_id)
        thread = threading.Thread(target=self._run, args=(task, request), daemon=True)
        thread.start()
        if return_immediately:
            return task
        return self.wait_for_terminal(task.id, 30)

    def _run(self, task, request):
        self._record(task, TaskState.WORKING)
        try:
            artifact = self.handler(request, task)
            task.artifacts.append(artifact)
            self._record(task, TaskState.COMPLETED)
        except Exception as error:
            task.status_metadata = {"error": str(error)}
            self._record(task, TaskState.FAILED)

    def _record(self, task, state):
        task.state = state
        revision = len(task.history) + 1
        event = TaskEvent(task.id, revision, revision, state)
        task.history.append(event)
        if self.event_store:
            self.event_store.append(
                task.id,
                f"a2a.task.{state.value}",
                {"sequence": event.sequence, "context_id": task.context_id},
            )
        for callback in self._push.get(task.id, []):
            callback(event)
        condition = self._conditions.get(task.id)
        if condition:
            with condition:
                condition.notify_all()

    def get_task(self, task_id):
        try:
            return self._tasks[task_id]
        except KeyError:
            raise CoreError("TASK_NOT_FOUND") from None

    def list_tasks(self, context_id=None):
        return [
            task
            for task in self._tasks.values()
            if context_id is None or task.context_id == context_id
        ]

    def wait_for_terminal(self, task_id, timeout=None):
        task = self.get_task(task_id)
        end = None if timeout is None else time.monotonic() + timeout
        condition = self._conditions[task_id]
        with condition:
            while task.state not in {
                TaskState.COMPLETED,
                TaskState.FAILED,
                TaskState.CANCELED,
                TaskState.REJECTED,
            }:
                remaining = None if end is None else end - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError(task_id)
                condition.wait(remaining)
        return task

    def subscribe_to_task(self, task_id):
        self.get_task(task_id)
        return _Subscription()

    def register_push(self, task_id, callback):
        self.get_task(task_id)
        self._push.setdefault(task_id, []).append(callback)
        for event in self._tasks[task_id].history:
            callback(event)

    def redeliver_push(self, task_id):
        for callback in self._push.get(task_id, []):
            for event in self.get_task(task_id).history:
                callback(event)

    def send_to_task(self, task_id, message):
        task = self.get_task(task_id)
        if task.state in {
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELED,
            TaskState.REJECTED,
        }:
            raise CoreError("TASK_TERMINAL")
        return task

    def cancel_task(self, task_id):
        task = self.get_task(task_id)
        if task.state in {
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELED,
            TaskState.REJECTED,
        }:
            raise CoreError("TASK_NOT_CANCELABLE")
        self._record(task, TaskState.CANCELED)
        return task

    def require_input(self, task_id, *, reason, schema):
        task = self.get_task(task_id)
        task.status_metadata = {"reason": reason, "schema": schema}
        self._record(task, TaskState.INPUT_REQUIRED)
        return task

    def require_auth(self, task_id, *, scheme):
        task = self.get_task(task_id)
        task.status_metadata = {"scheme": scheme}
        self._record(task, TaskState.AUTH_REQUIRED)
        return task

    def create_push_config(self, task_id, url, authentication_ref=None):
        self.get_task(task_id)
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise CoreError("POLICY_DENIED")
        host = parsed.hostname.lower()
        if host == "localhost":
            raise CoreError("POLICY_DENIED")
        try:
            address = ipaddress.ip_address(host)
            if (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_reserved
            ):
                raise CoreError("POLICY_DENIED")
        except ValueError:
            pass
        return {
            "task_id": task_id,
            "url": url,
            "authentication_ref": authentication_ref,
        }

    def close(self):
        self._closed = True
