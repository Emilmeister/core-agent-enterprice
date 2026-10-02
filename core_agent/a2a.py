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

from .config import A2A_INTERFACES, RunRequest
from .errors import CoreError

ATTACHMENTS_ONLY_PROMPT = "The user sent attachments without any accompanying text."
# Removed input extension. Still recognised only to reject a payload that would
# otherwise be silently ignored; never required and never advertised.
LEGACY_RUN_CAPABILITIES_URI = "urn:core-agent:run-capabilities:v1"


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
    "APPROVED_RESERVED": TaskState.WORKING,
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

    @classmethod
    def file(cls, data, *, filename=None, media_type=None):
        return cls(
            "file", {"bytes": data, "filename": filename, "media_type": media_type}
        )


@dataclass(frozen=True)
class Message:
    role: str
    parts: tuple[Part, ...]
    extensions: tuple[str, ...] = ()
    metadata: dict = field(default_factory=dict)
    context_id: str | None = None
    message_id: str | None = None
    task_id: str | None = None

    @classmethod
    def user(cls, text):
        return cls("user", (Part.text(text),))

    @classmethod
    def from_run_request(cls, request, context_id=None):
        return cls("user", (Part.text(request.prompt),), (), {}, context_id)


def _reject_stale_capabilities(message):
    """A client still sending the removed extension must not lose capabilities silently."""
    payload = message.metadata.get(LEGACY_RUN_CAPABILITIES_URI)
    if not isinstance(payload, dict):
        return
    if payload.get("mcp") or payload.get("skills"):
        raise CoreError(
            "CONFIG_INVALID",
            "mcp and skills are deployment configuration and cannot be sent per request",
        )


def parse_run_request(message):
    _reject_stale_capabilities(message)
    if any(part.kind not in {"text", "file", "data"} for part in message.parts):
        raise CoreError("CONTENT_TYPE_NOT_SUPPORTED")
    # Binary parts never reach the prompt: the model reads them back as artifacts.
    attachments = tuple(part.data for part in message.parts if part.kind == "file")
    prompt = "\n".join(
        json.dumps(part.data, sort_keys=True) if part.kind == "data" else str(part.data)
        for part in message.parts
        if part.kind != "file"
    ).strip()
    if not prompt and attachments:
        prompt = ATTACHMENTS_ONLY_PROMPT
    request = RunRequest.from_dict({"prompt": prompt})
    return replace(request, attachments=attachments) if attachments else request


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
        return sum(len(self._content(part)) for part in self.parts)

    @property
    def digest(self):
        content = b"\0".join(self._content(part) for part in self.parts)
        return "sha256:" + hashlib.sha256(content).hexdigest()

    @staticmethod
    def _content(part):
        if part.kind == "file":
            content = part.data.get("bytes") if isinstance(part.data, dict) else None
            if not isinstance(content, bytes):
                raise CoreError("ARTIFACT_INTEGRITY_FAILED")
            return content
        return str(part.data).encode()

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


def workflow_result_artifact(record, response_files_service=None, *, connection=None):
    """Build the complete final batch from an already authorized canonical run."""
    from .workspace import WorkspaceBinding

    values = record if isinstance(record, dict) else vars(record)
    result = values.get("result") or {}
    message = result.get("message", "")
    refs = result.get("outgoing_files", ())
    if values.get("state") != "COMPLETED" or not isinstance(message, str):
        raise CoreError("ARTIFACT_INTEGRITY_FAILED")
    if not isinstance(refs, (tuple, list)):
        raise CoreError("ARTIFACT_INTEGRITY_FAILED")
    files, receipts = (), ()
    if refs:
        if response_files_service is None:
            raise CoreError("ARTIFACT_INTEGRITY_FAILED")
        try:
            pinned_limit = refs[0]["limit_bytes"]
            binding = WorkspaceBinding(values["tenant_id"], values["owner_id"], values["context_id"])
        except (KeyError, TypeError):
            raise CoreError("ARTIFACT_INTEGRITY_FAILED") from None
        files = response_files_service.load(binding, refs, task_id=values["task_id"],
            run_id=values["run_id"], limit_bytes=pinned_limit, connection=connection)
        receipts = response_files_service.receipts(refs)
    provenance = {"run_id": values["run_id"], "task_id": values["task_id"],
        "complete": result.get("complete", True), "completion_reason": result.get("completion_reason", "completed"),
        "usage": result.get("usage", {"model_turns": 0, "tool_calls": 0})}
    for key in ("shared_budget", "exhausted_dimension", "pending_tasks"):
        if key in result and result[key] is not None:
            provenance[key] = result[key]
    if receipts:
        provenance["outgoingFiles"] = list(receipts)
    parts = (Part.text(message),) if message else ()
    parts += tuple(Part.file(content, filename=ref["name"], media_type=ref["media_type"]) for ref, content in files)
    identity = (json.dumps({"message": message, "outgoingFiles": receipts}, sort_keys=True,
        separators=(",", ":")).encode() if receipts else message.encode())
    return Artifact("sha256:" + hashlib.sha256(identity).hexdigest(), parts, 1, provenance)


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
    # (binding, version) pairs, each exactly as its endpoint accepts it.
    interfaces: tuple[tuple[str, str], ...] = A2A_INTERFACES
    capabilities: dict = field(
        default_factory=lambda: {"streaming": True, "pushNotifications": True}
    )
    skills: tuple[str, ...] = ()
    input_modes: tuple[str, ...] = ("text/plain", "application/json")
    output_modes: tuple[str, ...] = ("text/plain", "application/json")
    authentication: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    optional_extensions: tuple[str, ...] = ()
    description: str = "Policy-enforced core agent runtime"
    version: str = "1.0.0"

    @property
    def protocol_versions(self):
        return tuple(dict.fromkeys(version for _, version in self.interfaces))

    @property
    def bindings(self):
        return tuple(dict.fromkeys(binding for binding, _ in self.interfaces))

    @classmethod
    def minimal(cls, name, protocol_versions=("1.0",)):
        versions = set(protocol_versions)
        return cls(
            name,
            tuple(pair for pair in A2A_INTERFACES if pair[1] in versions)
            or tuple(("JSONRPC", version) for version in protocol_versions),
        )

    @classmethod
    def from_effective_config(cls, effective):
        return cls(
            effective.agent_name,
            effective.a2a_interfaces,
            {"streaming": True, "pushNotifications": True},
            tuple(sorted(effective.skills)),
        )

    def to_dict(self):
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "supportedInterfaces": [
                {"protocolVersion": version, "transport": binding}
                for binding, version in self.interfaces
            ],
            "capabilities": dict(self.capabilities),
            "skills": list(self.skills),
            "defaultInputModes": list(self.input_modes),
            "defaultOutputModes": list(self.output_modes),
            "securitySchemes": list(self.authentication),
            "extensions": [
                *({"uri": uri, "required": True} for uri in self.extensions),
                *(
                    {
                        "uri": uri,
                        "required": False,
                        "description": "Reports private local-operator authorization wait; the A2A caller cannot resolve it.",
                        "params": {
                            "authorizationOwner": "serving_agent_local_operator",
                            "callerCanResolve": False,
                            "publicTaskState": "TASK_STATE_WORKING",
                            "decisionTransport": "private_out_of_band",
                        },
                    }
                    for uri in self.optional_extensions
                ),
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
        request = parse_run_request(message)
        if request.attachments:
            raise CoreError("CONTENT_TYPE_NOT_SUPPORTED")
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
