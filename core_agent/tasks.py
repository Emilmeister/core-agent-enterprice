from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

from .config import MAX_SUBAGENT_DEPTH
from .errors import CoreError
from .mcp import mcp_tool_index


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
        self._lock = threading.Lock()

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
    def __init__(self, telemetry=None):
        self._tasks = {}
        self._mailboxes = {}
        self._closed = False
        self._kinds = {}
        self.telemetry = telemetry
        self.active_compute_waiters = 0

    def mailbox(self, owner_id, tenant_id="default"):
        return self._mailboxes.setdefault(owner_id, DurableMailbox(owner_id))

    def start(
        self,
        function,
        *,
        owner_id,
        required=False,
        accepts_cancel_event=False,
        trace_context=None,
        kind=None,
        contract=None,
        recoverable=False,
        tenant_id="default",
        continue_trace=False,
    ):
        task = BackgroundTask(str(uuid.uuid4()), owner_id, required)
        self._tasks[task.id] = task
        self._kinds[task.id] = kind
        linked_context = trace_context
        if self.telemetry and linked_context is None:
            with self.telemetry.span("core_agent.task.submit") as submission:
                linked_context = submission.context

        def run():
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
                    value = (
                        function(task.cancel_event)
                        if accepts_cancel_event
                        else function()
                    )
                    final_state = (
                        "canceled" if task.cancel_event.is_set() else "completed"
                    )
                    captured_error = None
                except Exception as error:
                    value = None
                    final_state = "failed"
                    captured_error = error
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
                    {"result": task.result},
                )
                self.mailbox(owner_id).deliver(event)
                task.condition.notify_all()

        threading.Thread(target=run, daemon=True).start()
        return task

    def count(self, *, owner_id, kind=None, active_only=False, tenant_id="default"):
        terminal = {"completed", "failed", "canceled"}
        return sum(
            task.owner_id == owner_id
            and (kind is None or self._kinds.get(task.id) == kind)
            and (not active_only or task.state not in terminal)
            for task in self._tasks.values()
        )

    def get(self, task_id, *, owner_id=None, tenant_id="default"):
        try:
            task = self._tasks[task_id]
        except KeyError:
            raise CoreError("TASK_NOT_FOUND") from None
        if owner_id is not None and task.owner_id != owner_id:
            raise CoreError("POLICY_DENIED")
        return task

    def list(self, *, owner_id, tenant_id="default"):
        return tuple(task for task in self._tasks.values() if task.owner_id == owner_id)

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
        if (
            task.state in {"completed", "failed", "canceled"}
            or task.cancel_event.is_set()
        ):
            raise CoreError("TASK_NOT_CANCELABLE")
        task.cancel_event.set()
        return task

    def assert_can_complete_parent(self, owner_id, tenant_id="default"):
        if any(
            task.owner_id == owner_id
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
                f"this agent does not hold skill {', '.join(refused)}",
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
            or not raw["budget"]
            or set(raw["budget"]) - {"turns", "tool_calls"}
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
                "names, skills and a budget of turns and/or tool_calls above zero",
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
        raise CoreError("BUDGET_EXCEEDED")
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
