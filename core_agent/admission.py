"""Atomic enterprise root admission; workflow state remains the busy authority."""
from __future__ import annotations

import asyncio
import copy
from contextlib import contextmanager, nullcontext
import hashlib
import inspect
import json
import time
import uuid
from dataclasses import dataclass

from a2a.types import Message, Role, Task, TaskState, TaskStatus
from a2a.utils.errors import InvalidParamsError, TaskNotFoundError
from google.protobuf.json_format import MessageToDict

from .auth import ScopeUser
from .errors import CoreError
from .history import read_history, title_source, title_text
from .workflow import OWNER_WAIT_KINDS, TERMINAL_STATES
from .workspace import WorkspaceBinding


@dataclass(frozen=True)
class Admission:
    task: Task
    run_id: str | None = None
    lease_token: str | None = None


def fingerprint(message):
    encoded = json.dumps(
        MessageToDict(message), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def authorize(context, owner_id):
    actor = context.state["principal"]
    if not actor.is_owner and actor.owner_id != owner_id:
        raise TaskNotFoundError()
    # Company owners act on external chats with the original execution owner.
    context.user = ScopeUser(owner_id)


def require_open_chat(chat):
    if chat.get("archived_at") is not None:
        raise TaskNotFoundError()


def check_duplicate(context, row, digest):
    authorize(context, row["owner_id"])
    if row["request_digest"] != digest:
        raise InvalidParamsError(
            "messageId was already used with different content",
            data={"code": "MESSAGE_ID_CONFLICT"},
        )


def cursor_context(after):
    """Version-two chat cursor payload; legacy task IDs remain strings."""
    if isinstance(after, str) and after.startswith("["):
        try:
            after = json.loads(after)
        except (ValueError, RecursionError):
            raise CoreError("REQUEST_INVALID") from None
    if isinstance(after, list):
        try:
            if (len(after) != 2 or type(after[0]) is not int or after[0] != 2
                    or not isinstance(after[1], str) or not after[1] or "\0" in after[1]):
                raise ValueError()
            after[1].encode("utf-8")
            return after[1]
        except (ValueError, UnicodeError):
            raise CoreError("REQUEST_INVALID") from None
    if after is not None and not isinstance(after, str):
        raise CoreError("REQUEST_INVALID")
    return None


class _PrepareFiles(Exception):
    """Leave the admission transaction before durable staging needs the pool."""


def validate_message(message):
    if not message.message_id.strip() or message.role != Role.ROLE_USER:
        raise InvalidParamsError("A nonempty messageId and ROLE_USER are required")
    if any(part.WhichOneof("content") not in {"text", "data", "raw"} for part in message.parts):
        raise CoreError("CONTENT_TYPE_NOT_SUPPORTED")


def original_text(message):
    """Accepted transport parts, before runtime attachment instructions are added."""
    return "\n".join(part.text if part.HasField("text") else json.dumps(
        MessageToDict(part.data), sort_keys=True) for part in message.parts
        if part.HasField("text") or part.HasField("data"))


def validate_title(title, expected_revision):
    if not isinstance(title, str):
        raise CoreError("REQUEST_INVALID")
    title = title.strip()
    if (not 1 <= len(title) <= 120 or any(ord(char) < 32 for char in title)
            or type(expected_revision) is not int or not 0 <= expected_revision < 2**63 - 1):
        raise CoreError("REQUEST_INVALID")
    try:
        title.encode("utf-8")
    except UnicodeError:
        raise CoreError("REQUEST_INVALID") from None
    return title


def generated_title(message, receipt):
    title = " ".join(original_text(message).split())
    if not title:
        title = next((entry["actual_name"] for entry in receipt.get("file_receipt", {}).get("entries", ())), "")
    return title[:120]


def record_chat_input(admission, chat, tenant, context_id, message, receipt, run_id, *, connection=None,
                      sequence=None, cron_origin=None):
    """Chat metadata joins the accepted input transaction, never the model context."""
    actor = receipt.get("actor")
    title = generated_title(message, receipt) if actor and actor.is_owner and cron_origin is None else ""
    assign = bool(title and chat.get("title_revision", 0) == 0)
    if title and chat.get("title_source") is not None and not assign:
        item = title_source(admission, tenant, {**chat, "context_id": context_id}, chat["title_source"], connection)
        assign = item is not None and item["status"] in {"rejected", "timed_out", "unprocessed_due_to_failure", "unprocessed_due_to_cancel"}
    revision = chat.get("title_revision", 0) + 1
    source = {"run_id": run_id, "sequence": sequence} if assign else chat.get("title_source")
    if connection is None:
        chat["updated_at"] = time.time()
        if assign:
            chat.update(title=title, title_revision=revision, title_source=source)
    else:
        from psycopg.types.json import Jsonb

        connection.execute("""UPDATE core_chats SET updated_at=clock_timestamp(),
            title=CASE WHEN %s THEN %s ELSE title END,
            title_revision=CASE WHEN %s THEN title_revision+1 ELSE title_revision END,
            title_source=CASE WHEN %s THEN %s ELSE title_source END
            WHERE tenant_id=%s AND context_id=%s""",
            (assign, title, assign, assign, Jsonb(source), tenant, context_id))


def prepare_files(agent, message, context, digest):
    files = [dict(raw=part.raw, name=part.filename, media_type=part.media_type or "application/octet-stream",
                  metadata=MessageToDict(part.metadata)) for part in message.parts if part.HasField("raw")]
    if not files:
        return None
    if agent.chat_file_service is None or agent.interaction_store is None:
        raise CoreError("CONTENT_TYPE_NOT_SUPPORTED")
    return agent.chat_file_service.prepare(files, tenant_id=context.tenant,
        actor_id=context.state["principal"].actor_id, message_id=message.message_id,
        request_digest=digest, source="a2a", request_metadata=MessageToDict(message.metadata),
        limit_bytes=agent.interaction_store.get_settings(context.tenant).attachment_limit_bytes)


def reject_files(agent, batch):
    if batch is not None:
        try:
            agent.chat_file_service.reject(batch["batch_id"], batch["tenant_id"], batch["lease_token"])
        except Exception as error:
            # Admission already rolled back, or returned a duplicate/busy receipt.
            # Preserve that result; original-age staging metadata and the upload
            # lease let the sweeper retry cleanup after a storage outage.
            agent._log("file.upload.cleanup_pending", batch_id=batch["batch_id"],
                       error_code=getattr(error, "code", type(error).__name__))


def bind_files(agent, batch, task, run_id, context, *, sequence=None, connection=None):
    if batch is None:
        return {}
    bound = agent.chat_file_service.bind(batch["batch_id"],
        WorkspaceBinding(context.tenant, context.user.user_name, task.context_id),
        task_id=task.id, run_id=run_id, actor_id=batch["actor_id"], message_id=batch["message_id"],
        request_digest=batch["request_digest"], lease_token=batch["lease_token"],
        sequence=sequence, connection=connection)
    manifest = bound["manifest"]
    # Original names and transport metadata stay in the private immutable batch.
    # The receipt contains only server-owned values, safe for inbox JSONB too.
    receipt = {key: manifest[key] for key in (
        "schema_version", "batch_id", "created_at", "source", "total_bytes")}
    receipt["entries"] = [{key: entry[key] for key in (
        "index", "actual_name", "relative_path", "size_bytes", "sha256")}
        for entry in manifest["entries"]]
    return {"file_batch_id": batch["batch_id"], "file_receipt": receipt}


@contextmanager
def memory_transaction(agent, task_id, batch=None):
    """Rollback only this admission, while recovery/cancel cannot observe it."""
    workflow = agent.workflow_store
    files = agent.chat_file_service.store if batch is not None else None
    with workflow._lock, (files.lock if files is not None else nullcontext()):
        before = next((r for r in workflow._records.values() if r.task_id == task_id), None)
        messages = copy.deepcopy(workflow._inbound.get(before.run_id)) if before else None
        waits = {key: value for key, value in workflow._waits.items() if before and value.run_id == before.run_id}
        try:
            yield
        except BaseException:
            if files is not None:
                files.rows[batch["batch_id"]] = copy.deepcopy(batch)
            if before:
                workflow._records[before.run_id] = before
                workflow._inbound[before.run_id] = messages
                workflow._waits.update(waits)
            else:
                for run_id, record in tuple(workflow._records.items()):
                    if record.task_id == task_id:
                        for values in (workflow._records, workflow._inbound, workflow._leases, workflow._budgets,
                                       agent._run_scopes, agent.event_store._events,
                                       agent.checkpoint_store._values, agent.audit_log._records):
                            values.pop(run_id, None)
            raise


def accept_followup(admission, message, task, request, context):
    """Transport decoding is complete; all admission pointers are server-owned."""
    validate_message(message)
    if message.task_id != task.id or (message.context_id and message.context_id != task.context_id):
        raise CoreError("INVALID_REQUEST", "context_id does not match task")
    agent, batch = admission.agent, None
    digest = fingerprint(message)
    database = getattr(admission, "database", None)
    workflow = agent.workflow_store
    def transaction():
        with (database.transaction() if database else workflow._lock) as connection:
            if database:
                chat = connection.execute(
                    "SELECT * FROM core_chats WHERE tenant_id=%s AND context_id=%s FOR NO KEY UPDATE",
                    (context.tenant, task.context_id),
                ).fetchone()
            else:
                chat = admission.chats.get((context.tenant, task.context_id))
                connection = None
            if chat is None:
                raise TaskNotFoundError()
            authorize(context, chat["owner_id"])
            require_open_chat(chat)
            # Both passes repeat the same locked duplicate/terminal checks.
            # Preparation commits independently, without holding a pool slot.
            def bind(record, sequence, conn):
                nonlocal batch
                if database:
                    if batch is None and any(part.HasField("raw") for part in message.parts):
                        raise _PrepareFiles()
                else:
                    batch = prepare_files(agent, message, context, digest)
                receipt = bind_files(agent, batch, task, record.run_id, context, sequence=sequence, connection=conn)
                record_chat_input(admission, chat, context.tenant, task.context_id, message,
                    {**receipt, "actor": context.state["principal"]}, record.run_id,
                    connection=conn, sequence=sequence)
                return receipt

            with (nullcontext() if database else memory_transaction(agent, task.id)):
                previous_chat = copy.deepcopy(chat) if not database else None
                try:
                    return workflow.append_inbound(task.id, tenant_id=context.tenant,
                        owner_id=chat["owner_id"], message_id=message.message_id, context_id=task.context_id,
                        content=request.prompt, provenance={"owner_id": chat["owner_id"], "tenant_id": context.tenant,
                            "actor_id": context.state["principal"].actor_id, "request_digest": digest,
                            "display_text": original_text(message)},
                        connection=connection, on_accept=bind)
                except BaseException:
                    if not database:
                        chat.clear()
                        chat.update(previous_chat)
                    if batch is not None and not database:
                        with agent.chat_file_service.store.lock:
                            agent.chat_file_service.store.rows[batch["batch_id"]] = copy.deepcopy(batch)
                    raise
    try:
        try:
            receipt, accepted = transaction()
        except _PrepareFiles:
            batch = prepare_files(agent, message, context, digest)
            receipt, accepted = transaction()
    except BaseException:
        reject_files(agent, batch)
        raise
    if not accepted:
        reject_files(agent, batch)
    return receipt


def new_task(message, context_id, active_task_id=None):
    task = Task(
        id=str(uuid.uuid4()), context_id=context_id,
        status=TaskStatus(state=(
            TaskState.TASK_STATE_FAILED if active_task_id else TaskState.TASK_STATE_SUBMITTED
        )),
        history=[message],
    )
    task.status.timestamp.GetCurrentTime()
    task.history[0].task_id = task.id
    task.history[0].context_id = context_id
    if active_task_id:
        task.metadata.update({"error": {"code": "CONTEXT_BUSY", "activeTaskId": active_task_id}})
        task.status.message.CopyFrom(Message(
            message_id=str(uuid.uuid4()), task_id=task.id, context_id=context_id,
            role=Role.ROLE_AGENT,
            parts=[{"text": "This chat already has an active task. Wait for it to finish before starting another."}],
        ))
    return task


def create_workflow(agent, request, task, context, *, connection=None, batch=None, previous_root_run_id=None, cron_origin=None):
    token = str(uuid.uuid4())
    record, *_ = agent._new_workflow(
        request, task_id=task.id, identity=context.user.user_name,
        session_id=task.context_id, tenant_id=context.tenant,
        actor_id=context.state["principal"].actor_id,
        connection=connection, defer_initialization=True,
        file_batch_id=batch["batch_id"] if batch else None,
        previous_root_run_id=previous_root_run_id,
        initial_lease_owner=agent._worker_id, initial_lease_token=token,
        **({"cron_origin": cron_origin} if cron_origin is not None else {}),
    )
    receipt = bind_files(agent, batch, task, record.run_id, context, connection=connection)
    if receipt:
        task.metadata.update(receipt)
    return Admission(task, record.run_id, token)


class MemoryRootAdmission:
    def __init__(self, agent, task_store):
        self.agent = agent
        self.task_store = task_store
        self.chats = {}
        self.messages = {}
        # ponytail: development admission is serialized; PostgreSQL locks per chat.
        self.lock = asyncio.Lock()

    async def workspace_scope(self, tenant_id, context_id):
        async with self.lock:
            with self.agent.workflow_store._lock:
                chat = self.chats.get((tenant_id, context_id))
                if chat is not None and chat["latest_root_run_id"] is None:
                    return WorkspaceBinding(tenant_id, chat["owner_id"], context_id), False
                record = self.agent.workflow_store._records.get(chat["latest_root_run_id"]) if chat else None
                if (record is None or record.parent_run_id is not None
                        or (record.tenant_id, record.owner_id, record.context_id) != (tenant_id, chat["owner_id"], context_id)
                        or not any(key[0] == tenant_id and row["task_id"] == record.task_id and row["owner_id"] == record.owner_id
                                   for key, row in self.messages.items())):
                    raise CoreError("TASK_NOT_FOUND")
                return WorkspaceBinding(tenant_id, record.owner_id, context_id), record.state not in TERMINAL_STATES

    async def history(self, tenant_id, context_id, *, limit, after=None):
        async with self.lock:
            with self.agent.workflow_store._lock:
                return read_history(self, tenant_id, context_id, limit=limit, after=after)

    async def list_chats(self, tenant_id, *, limit, after=None):
        async with self.lock:
            with self.agent.workflow_store._lock:
                after_context = cursor_context(after)
                if after_context is not None:
                    if (tenant_id, after_context) not in self.chats:
                        raise CoreError("REQUEST_INVALID")
                elif after is not None:
                    previous = next((record for record in self.agent.workflow_store._records.values()
                                     if record.tenant_id == tenant_id and record.task_id == after
                                     and record.parent_run_id is None), None)
                    chat = self.chats.get((tenant_id, previous.context_id)) if previous else None
                    if chat is None or chat["owner_id"] != previous.owner_id:
                        raise CoreError("REQUEST_INVALID")
                    after_context = previous.context_id
                rows = []
                for (tenant, context_id), chat in sorted(self.chats.items()):
                    if (tenant != tenant_id or chat.get("archived_at") is not None
                            or (after_context is not None and context_id <= after_context)):
                        continue
                    record = self.agent.workflow_store._records.get(chat["latest_root_run_id"])
                    if chat["latest_root_run_id"] is not None and (record is None or record.parent_run_id is not None
                            or (record.tenant_id, record.owner_id, record.context_id) != (tenant, chat["owner_id"], context_id)):
                        continue
                    rows.append({"context_id": context_id, "latest_task_id": record.task_id if record else None,
                                 "active": record is not None and record.state not in TERMINAL_STATES,
                                 **self.chat_metadata(tenant, context_id, chat, record)})
                    if len(rows) == limit:
                        break
                return rows

    def chat_metadata(self, tenant_id, context_id, chat, record=None):
        task = self.task_store._impl.tasks.get(json.dumps([tenant_id, chat["owner_id"]]), {}).get(
            record.task_id if record else None)
        workflow = self.agent.workflow_store
        now = workflow.current_time()
        needs_attention = bool(record and record.state not in TERMINAL_STATES and not record.cancel_requested and any(
            wait.tenant_id == tenant_id and wait.owner_id == chat["owner_id"] and wait.context_id == context_id
            and wait.kind in OWNER_WAIT_KINDS and wait.outcome is None
            and (wait.deadline is None or wait.deadline > now)
            and (current := workflow._records.get(wait.run_id)) is not None
            and (current.tenant_id, current.owner_id, current.context_id) == (tenant_id, chat["owner_id"], context_id)
            and current.snapshot.get("budget_root_id", current.run_id) == record.snapshot.get("budget_root_id", record.run_id)
            and current.snapshot.get("wait_id") == wait.wait_id
            and current.state not in TERMINAL_STATES and not current.cancel_requested
            for wait in workflow._waits.values()))
        return {"title": title_text(self, tenant_id, {**chat, "context_id": context_id}),
                "title_revision": chat.get("title_revision", 0),
                "updated_at": max(chat.get("updated_at", 0), task.status.timestamp.ToMilliseconds() / 1000 if task else 0),
                "status": TaskState.Name(task.status.state) if task else None, "needs_attention": needs_attention}

    async def rename_chat(self, tenant_id, context_id, *, title, expected_revision):
        title = validate_title(title, expected_revision)
        cursor_context([2, context_id])
        async with self.lock:
            with self.agent.workflow_store._lock:
                chat = self.chats.get((tenant_id, context_id))
                if chat is None or chat.get("archived_at") is not None:
                    raise CoreError("TASK_NOT_FOUND")
                if chat.get("title_revision", 0) != expected_revision:
                    raise CoreError("CHAT_TITLE_CONFLICT")
                chat.update(title=title, title_revision=expected_revision + 1, title_source=None, updated_at=time.time())
                record = self.agent.workflow_store._records.get(chat["latest_root_run_id"])
                metadata = self.chat_metadata(tenant_id, context_id, chat, record)
                return {"context_id": context_id, **{key: metadata[key] for key in ("title", "title_revision", "updated_at")}}

    async def archive_chat(self, tenant_id, context_id, *, actor_id):
        cursor_context([2, context_id])
        async with self.lock:
            with self.agent.workflow_store._lock:
                chat = self.chats.get((tenant_id, context_id))
                if chat is None:
                    raise CoreError("TASK_NOT_FOUND")
                if chat.get("archived_at") is None:
                    record = self.agent.workflow_store._records.get(chat["latest_root_run_id"])
                    if chat["latest_root_run_id"] is not None:
                        if (record is None or record.parent_run_id is not None
                                or (record.tenant_id, record.owner_id, record.context_id) != (tenant_id, chat["owner_id"], context_id)):
                            raise CoreError("TASK_NOT_FOUND")
                        if record.state not in TERMINAL_STATES:
                            raise CoreError("CONTEXT_BUSY")
                    self.cron_store.disable_context(tenant_id, context_id, chat["owner_id"], actor_id=actor_id)
                    chat.update(archived_at=time.time(), archived_by=actor_id, updated_at=time.time())
                return {"context_id": context_id, "archived": True}

    def validate_workspace_scope(self, binding):
        # Ownership is immutable and published before the workflow worker starts.
        chat = self.chats.get((binding.tenant_id, binding.context_id))
        if chat is None or chat["owner_id"] != binding.owner_id:
            raise CoreError("WORKSPACE_SCOPE_REQUIRED")

    def followup(self, message, task, request, context):
        return accept_followup(self, message, task, request, context)

    async def transaction(self, context, callback):
        """One synchronous admission callback after every coroutine lock is held.

        The callback owns rollback of its cron maps; this wrapper owns new SDK
        Tasks, chat receipts, workflows and file bindings. No await spans the
        workflow RLock. Existing unrelated telemetry is never rolled back.
        """
        batches = []
        workflow = self.agent.workflow_store
        def commit():
            with workflow._lock:
                chats, messages = copy.deepcopy(self.chats), copy.deepcopy(self.messages)
                maps = (workflow._records, workflow._inbound, workflow._leases, workflow._budgets,
                        self.agent._run_scopes, self.agent.event_store._events,
                        self.agent.checkpoint_store._values, self.agent.audit_log._records)
                keys = [set(values) for values in maps]
                def admit(message, request, call_context, *, cron_origin=None):
                    if call_context.tenant != context.tenant:
                        raise TaskNotFoundError()
                    return self._admit_sync(message, request, call_context, batches, cron_origin=cron_origin)
                try:
                    result = callback(admit)
                    if inspect.isawaitable(result):
                        if inspect.iscoroutine(result):
                            result.close()
                        raise CoreError("INVALID_REQUEST", "Admission callback must be synchronous")
                    return result
                except BaseException:
                    created = set(workflow._records) - keys[0]
                    for values, original in zip(maps, keys):
                        for key in set(values) - original:
                            if key in created:
                                values.pop(key, None)
                    workflow._cancel_requested.difference_update(created)
                    for key, wait in tuple(workflow._waits.items()):
                        if wait.run_id in created:
                            workflow._waits.pop(key, None)
                    self.chats.clear()
                    self.chats.update(chats)
                    self.messages.clear()
                    self.messages.update(messages)
                    if batches:
                        with self.agent.chat_file_service.store.lock:
                            for batch in batches:
                                self.agent.chat_file_service.store.rows[batch["batch_id"]] = copy.deepcopy(batch)
                    raise
        async with self.lock:
            try:
                return await self.task_store.admission_transaction(commit)
            except BaseException:
                for batch in batches:
                    reject_files(self.agent, batch)
                raise

    async def admit(self, message, request, context):
        return await self.transaction(context, lambda admit: admit(message, request, context))

    def _admit_sync(self, message, request, context, batches, *, cron_origin=None):
        validate_message(message)
        actor = context.state["principal"]
        key = (context.tenant, actor.actor_id, message.message_id)
        digest = fingerprint(message)
        duplicate = self.messages.get(key)
        if duplicate:
            check_duplicate(context, duplicate, digest)
            task = self.task_store.get_admitted(duplicate["task_id"], context)
            if task is None:
                raise TaskNotFoundError()
            return Admission(task)
        context_id = message.context_id or str(uuid.uuid4())
        chat_key = (context.tenant, context_id)
        chat = self.chats.get(chat_key)
        if chat is None:
            legacy_run = any(record.context_id == context_id and record.tenant_id == context.tenant
                             for record in self.agent.workflow_store._records.values())
            if legacy_run or self.task_store.has_admitted_context(context_id, context):
                raise TaskNotFoundError()
            chat = {"owner_id": actor.owner_id, "latest_root_run_id": None, "workspace_revision": 0,
                    "title": "", "title_revision": 0, "title_source": None, "updated_at": time.time()}
        authorize(context, chat["owner_id"])
        require_open_chat(chat)
        cleanup = getattr(self, "workspace_cleanup", None)
        if cleanup:
            cleanup.check_ready(WorkspaceBinding(context.tenant, chat["owner_id"], context_id))
        active_task_id = None
        if chat["latest_root_run_id"]:
            record = self.agent.workflow_store.get(chat["latest_root_run_id"], tenant_id=context.tenant, owner_id=chat["owner_id"])
            if record.parent_run_id is not None or record.context_id != context_id:
                raise TaskNotFoundError()
            if record.state not in TERMINAL_STATES:
                active_task_id = record.task_id
        batch = None if active_task_id else prepare_files(self.agent, message, context, digest)
        if batch is not None:
            batches.append(batch)
        task = new_task(message, context_id, active_task_id)
        self.chats[chat_key] = dict(chat)
        admitted = Admission(task) if active_task_id else create_workflow(
            self.agent, request, task, context, batch=batch,
            previous_root_run_id=chat["latest_root_run_id"], cron_origin=cron_origin)
        self.task_store._save_admission(task, context)
        if admitted.run_id:
            self.chats[chat_key]["latest_root_run_id"] = admitted.run_id
            record_chat_input(self, self.chats[chat_key], context.tenant, context_id, message,
                {**MessageToDict(task.metadata), "actor": actor}, admitted.run_id, cron_origin=cron_origin)
        self.messages[key] = {"request_digest": digest, "owner_id": chat["owner_id"], "task_id": task.id,
                              "display_text": original_text(message)}
        return admitted

    def ensure_chat(self, context, context_id=None, *, connection=None, lock=False):
        """Create a server-named empty chat, or authorize an existing canonical chat.

        Creation runs only inside transaction's synchronous callback. Looking up
        an existing binding is also safe inside a tool's held workflow guard.
        """
        actor = context.state["principal"]
        if context_id is None:
            if not actor.is_owner or actor.is_external:
                raise CoreError("ACCESS_DENIED")
            context_id = str(uuid.uuid4())
            self.chats[(context.tenant, context_id)] = {
                "owner_id": actor.owner_id, "latest_root_run_id": None, "workspace_revision": 0,
                "title": "", "title_revision": 0, "title_source": None, "updated_at": time.time()}
        cursor_context([2, context_id])
        chat = self.chats.get((context.tenant, context_id))
        if chat is None:
            raise TaskNotFoundError()
        authorize(context, chat["owner_id"])
        require_open_chat(chat)
        return WorkspaceBinding(context.tenant, chat["owner_id"], context_id)


class PostgresRootAdmission:
    def __init__(self, agent, task_store):
        self.agent = agent
        self.task_store = task_store
        self.database = task_store.database

    async def workspace_scope(self, tenant_id, context_id):
        def read():
            with self.database.transaction() as connection:
                row = connection.execute("""SELECT c.owner_id,r.state FROM core_chats c LEFT JOIN core_runs r
                    ON r.run_id=c.latest_root_run_id AND r.tenant_id=c.tenant_id AND r.owner_id=c.owner_id
                    AND r.context_id=c.context_id AND r.parent_run_id IS NULL
                    AND EXISTS(SELECT 1 FROM core_root_messages m
                        WHERE m.tenant_id=c.tenant_id AND m.owner_id=c.owner_id AND m.context_id=c.context_id AND m.task_id=r.task_id)
                    WHERE c.tenant_id=%s AND c.context_id=%s
                        AND (c.latest_root_run_id IS NULL OR r.run_id IS NOT NULL)""",
                    (tenant_id, context_id)).fetchone()
                if row is None:
                    raise CoreError("TASK_NOT_FOUND")
                return WorkspaceBinding(tenant_id, row["owner_id"], context_id), row["state"] is not None and row["state"] not in TERMINAL_STATES
        return await asyncio.to_thread(read)

    async def history(self, tenant_id, context_id, *, limit, after=None):
        def read():
            with self.database.transaction() as connection:
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                return read_history(self, tenant_id, context_id, limit=limit, after=after, connection=connection)
        return await asyncio.to_thread(read)

    async def list_chats(self, tenant_id, *, limit, after=None):
        def read():
            with self.database.transaction() as connection:
                after_context = cursor_context(after)
                if after_context is not None:
                    if connection.execute("SELECT 1 FROM core_chats WHERE tenant_id=%s AND context_id=%s",
                                          (tenant_id, after_context)).fetchone() is None:
                        raise CoreError("REQUEST_INVALID")
                elif after is not None:
                    row = connection.execute(
                        """SELECT r.context_id FROM core_runs r JOIN core_chats c
                           ON c.tenant_id=r.tenant_id AND c.context_id=r.context_id AND c.owner_id=r.owner_id
                           WHERE r.tenant_id=%s AND r.task_id=%s AND r.parent_run_id IS NULL""",
                        (tenant_id, after),
                    ).fetchone()
                    if row is None:
                        raise CoreError("REQUEST_INVALID")
                    after_context = row["context_id"]
                query = """SELECT c.*, r.task_id AS latest_task_id, t.state AS task_state,
                           GREATEST(extract(epoch FROM c.updated_at), t.status_timestamp,
                                    extract(epoch FROM t.updated_at))::double precision AS display_updated_at,
                           COALESCE(NOT (r.state = ANY(%s)), false) AS active,
                           COALESCE(NOT (r.state = ANY(%s)) AND NOT r.cancel_requested AND EXISTS (
                               SELECT 1 FROM core_waits w JOIN core_runs waiting
                                 ON waiting.run_id=w.run_id AND waiting.tenant_id=w.tenant_id
                               WHERE w.tenant_id=c.tenant_id AND w.owner_id=c.owner_id AND w.context_id=c.context_id
                                 AND waiting.owner_id=c.owner_id AND waiting.context_id=c.context_id
                                 AND waiting.snapshot->>'budget_root_id'=r.snapshot->>'budget_root_id'
                                 AND waiting.snapshot->>'wait_id'=w.wait_id
                                 AND w.kind=ANY(%s) AND w.resolved_at IS NULL
                                 AND (w.deadline IS NULL OR w.deadline>extract(epoch FROM clock_timestamp()))
                                 AND NOT (waiting.state=ANY(%s)) AND NOT waiting.cancel_requested),false) AS needs_attention
                           FROM core_chats c LEFT JOIN core_runs r
                             ON r.run_id=c.latest_root_run_id AND r.tenant_id=c.tenant_id
                             AND r.owner_id=c.owner_id AND r.context_id=c.context_id AND r.parent_run_id IS NULL
                           LEFT JOIN core_a2a_tasks t ON t.task_id=r.task_id AND t.tenant=c.tenant_id AND t.owner=c.owner_id
                           WHERE c.tenant_id=%s AND c.archived_at IS NULL
                             AND (c.latest_root_run_id IS NULL OR r.run_id IS NOT NULL)"""
                values = [sorted(TERMINAL_STATES), sorted(TERMINAL_STATES), sorted(OWNER_WAIT_KINDS), sorted(TERMINAL_STATES), tenant_id]
                if after_context is not None:
                    query += " AND c.context_id > %s"
                    values.append(after_context)
                return [{"context_id": row["context_id"], "latest_task_id": row["latest_task_id"], "active": row["active"],
                         "title": title_text(self, tenant_id, row, connection=connection),
                         "title_revision": row["title_revision"], "updated_at": row["display_updated_at"],
                         "status": TaskState.Name(row["task_state"]) if row["task_state"] is not None else None,
                         "needs_attention": row["needs_attention"]}
                        for row in connection.execute(query + " ORDER BY c.context_id LIMIT %s", [*values, limit]).fetchall()]
        return await asyncio.to_thread(read)

    async def rename_chat(self, tenant_id, context_id, *, title, expected_revision):
        title = validate_title(title, expected_revision)
        cursor_context([2, context_id])
        def rename():
            with self.database.transaction() as connection:
                chat = connection.execute("SELECT title_revision,archived_at FROM core_chats WHERE tenant_id=%s AND context_id=%s FOR NO KEY UPDATE",
                                          (tenant_id, context_id)).fetchone()
                if chat is None or chat["archived_at"] is not None:
                    raise CoreError("TASK_NOT_FOUND")
                if chat["title_revision"] != expected_revision:
                    raise CoreError("CHAT_TITLE_CONFLICT")
                row = connection.execute("""UPDATE core_chats SET title=%s,title_revision=title_revision+1,
                    title_source=NULL,updated_at=clock_timestamp() WHERE tenant_id=%s AND context_id=%s
                    RETURNING context_id,title,title_revision,extract(epoch FROM updated_at)::double precision AS updated_at""",
                    (title, tenant_id, context_id)).fetchone()
                return dict(row)
        return await asyncio.to_thread(rename)

    async def archive_chat(self, tenant_id, context_id, *, actor_id):
        cursor_context([2, context_id])
        def archive():
            with self.database.transaction() as connection:
                chat = connection.execute("SELECT * FROM core_chats WHERE tenant_id=%s AND context_id=%s FOR NO KEY UPDATE",
                                          (tenant_id, context_id)).fetchone()
                if chat is None:
                    raise CoreError("TASK_NOT_FOUND")
                if chat["archived_at"] is None:
                    if chat["latest_root_run_id"] is not None:
                        # No workflow lock follows this chat lock: tool writers
                        # fence their active root and only read immutable binding.
                        root = connection.execute("""SELECT state FROM core_runs WHERE run_id=%s AND tenant_id=%s
                            AND owner_id=%s AND context_id=%s AND parent_run_id IS NULL""",
                            (chat["latest_root_run_id"], tenant_id, chat["owner_id"], context_id)).fetchone()
                        if root is None:
                            raise CoreError("TASK_NOT_FOUND")
                        if root["state"] not in TERMINAL_STATES:
                            raise CoreError("CONTEXT_BUSY")
                    self.cron_store.disable_context(tenant_id, context_id, chat["owner_id"], actor_id=actor_id, connection=connection)
                    connection.execute("""UPDATE core_chats SET archived_at=clock_timestamp(),archived_by=%s,
                        updated_at=clock_timestamp() WHERE tenant_id=%s AND context_id=%s""", (actor_id, tenant_id, context_id))
                return {"context_id": context_id, "archived": True}
        return await asyncio.to_thread(archive)

    def validate_workspace_scope(self, binding):
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT owner_id FROM core_chats WHERE tenant_id = %s AND context_id = %s",
                (binding.tenant_id, binding.context_id),
            ).fetchone()
        if row is None or row["owner_id"] != binding.owner_id:
            raise CoreError("WORKSPACE_SCOPE_REQUIRED")

    def ensure_chat(self, context, context_id=None, *, connection=None, lock=False):
        """Owner mutations lock chat first; workflow-fenced tools only read binding."""
        actor = context.state["principal"]
        with self.database.transaction() if connection is None else nullcontext(connection) as connection:
            if context_id is None:
                if not actor.is_owner or actor.is_external:
                    raise CoreError("ACCESS_DENIED")
                context_id = str(uuid.uuid4())
                connection.execute("INSERT INTO core_chats(tenant_id,context_id,owner_id) VALUES(%s,%s,%s)",
                                   (context.tenant, context_id, actor.owner_id))
            cursor_context([2, context_id])
            row = connection.execute("SELECT owner_id,archived_at FROM core_chats WHERE tenant_id=%s AND context_id=%s" +
                                     (" FOR NO KEY UPDATE" if lock else ""),
                                     (context.tenant, context_id)).fetchone()
            if row is None:
                raise TaskNotFoundError()
            authorize(context, row["owner_id"])
            require_open_chat(row)
            return WorkspaceBinding(context.tenant, row["owner_id"], context_id)

    def followup(self, message, task, request, context):
        return accept_followup(self, message, task, request, context)

    async def admit(self, message, request, context):
        return await asyncio.to_thread(self._admit, message, request, context)

    def _admit(self, message, request, context):
        batch = None
        try:
            try:
                admitted = self._admit_transaction(message, request, context)
            except _PrepareFiles:
                # The preflight transaction rolled back, including a provisional
                # chat. No locks or pooled connection span the durable upload.
                batch = prepare_files(self.agent, message, context, fingerprint(message))
                admitted = self._admit_transaction(message, request, context, batch)
        except BaseException:
            reject_files(self.agent, batch)
            raise
        if admitted.run_id is None:
            reject_files(self.agent, batch)
        return admitted

    def _admit_transaction(self, message, request, context, batch=None, *, connection=None, cron_origin=None):
        """Admit on an existing transaction, or own one for the public path.

        A borrowed connection must already be inside its caller's transaction.
        This helper never commits or starts workers. The caller owns rollback
        and any prepared batch cleanup; raw Parts without a batch raise
        _PrepareFiles so preparation can happen after releasing the connection.
        Keep the root-message advisory lock before the canonical chat row lock.
        """
        validate_message(message)
        actor = context.state["principal"]
        key = (context.tenant, actor.actor_id, message.message_id)
        digest = fingerprint(message)
        with (self.database.transaction() if connection is None else nullcontext(connection)) as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (json.dumps(key, separators=(",", ":")),),
            )
            duplicate = connection.execute(
                """SELECT * FROM core_root_messages
                   WHERE tenant_id = %s AND actor_id = %s AND message_id = %s""", key,
            ).fetchone()
            if duplicate:
                check_duplicate(context, duplicate, digest)
                row = connection.execute(
                    "SELECT payload FROM core_a2a_tasks WHERE tenant = %s AND owner = %s AND task_id = %s",
                    (context.tenant, duplicate["owner_id"], duplicate["task_id"]),
                ).fetchone()
                if row is None:
                    raise TaskNotFoundError()
                return Admission(Task.FromString(bytes(row["payload"])))
            context_id = message.context_id or str(uuid.uuid4())
            created = connection.execute(
                """INSERT INTO core_chats (tenant_id, context_id, owner_id)
                   VALUES (%s, %s, %s) ON CONFLICT DO NOTHING RETURNING context_id""",
                (context.tenant, context_id, actor.owner_id),
            ).fetchone()
            chat = connection.execute(
                "SELECT * FROM core_chats WHERE tenant_id = %s AND context_id = %s FOR NO KEY UPDATE",
                (context.tenant, context_id),
            ).fetchone()
            authorize(context, chat["owner_id"])
            require_open_chat(chat)
            cleanup = getattr(self, "workspace_cleanup", None)
            if cleanup:
                cleanup.check_ready(WorkspaceBinding(context.tenant, chat["owner_id"], context_id), connection)
            if created and connection.execute(
                """SELECT 1 FROM core_a2a_tasks WHERE tenant = %s AND context_id = %s
                   UNION ALL
                   SELECT 1 FROM core_runs WHERE tenant_id = %s AND context_id = %s LIMIT 1""",
                (context.tenant, context_id, context.tenant, context_id),
            ).fetchone():
                raise TaskNotFoundError()
            active_task_id = None
            if chat["latest_root_run_id"]:
                record = connection.execute(
                    "SELECT task_id, state FROM core_runs WHERE run_id = %s AND tenant_id = %s",
                    (chat["latest_root_run_id"], context.tenant),
                ).fetchone()
                if record["state"] not in TERMINAL_STATES:
                    active_task_id = record["task_id"]
            if not active_task_id and batch is None and any(part.HasField("raw") for part in message.parts):
                raise _PrepareFiles()
            task = new_task(message, context_id, active_task_id)
            # File binding references the Task row. Both writes remain private
            # until this admission transaction commits.
            self.task_store._save(task, context, connection=connection)
            admitted = Admission(task) if active_task_id else create_workflow(
                self.agent, request, task, context, connection=connection, batch=batch,
                previous_root_run_id=chat["latest_root_run_id"],
                cron_origin=cron_origin,
            )
            if batch is not None and admitted.run_id is not None:
                self.task_store._save(task, context, connection=connection)
            connection.execute(
                """INSERT INTO core_root_messages
                   (tenant_id, actor_id, message_id, request_digest, owner_id, context_id, task_id, display_text)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (*key, digest, chat["owner_id"], context_id, task.id, original_text(message)),
            )
            if admitted.run_id:
                connection.execute(
                    """UPDATE core_chats SET latest_root_run_id = %s
                       WHERE tenant_id = %s AND context_id = %s""",
                    (admitted.run_id, context.tenant, context_id),
                )
                record_chat_input(self, chat, context.tenant, context_id, message,
                    {**MessageToDict(task.metadata), "actor": actor}, admitted.run_id,
                    connection=connection, cron_origin=cron_origin)
        return admitted
