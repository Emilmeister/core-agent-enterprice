"""Atomic enterprise root admission; workflow state remains the busy authority."""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass

from a2a.types import Message, Role, Task, TaskState, TaskStatus
from a2a.utils.errors import InvalidParamsError, TaskNotFoundError
from google.protobuf.json_format import MessageToDict

from .auth import ScopeUser
from .workflow import TERMINAL_STATES


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


def check_duplicate(context, row, digest):
    authorize(context, row["owner_id"])
    if row["request_digest"] != digest:
        raise InvalidParamsError(
            "messageId was already used with different content",
            data={"code": "MESSAGE_ID_CONFLICT"},
        )


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


def create_workflow(agent, request, task, context, *, connection=None):
    token = str(uuid.uuid4())
    record, *_ = agent._new_workflow(
        request, task_id=task.id, identity=context.user.user_name,
        session_id=task.context_id, tenant_id=context.tenant,
        actor_id=context.state["principal"].actor_id,
        connection=connection, defer_initialization=True,
        initial_lease_owner=agent._worker_id, initial_lease_token=token,
    )
    return Admission(task, record.run_id, token)


class MemoryRootAdmission:
    def __init__(self, agent, task_store):
        self.agent = agent
        self.task_store = task_store
        self.chats = {}
        self.messages = {}
        # ponytail: development admission is serialized; PostgreSQL locks per chat.
        self.lock = asyncio.Lock()

    async def admit(self, message, request, context):
        actor = context.state["principal"]
        key = (context.tenant, actor.actor_id, message.message_id)
        digest = fingerprint(message)
        async with self.lock:
            duplicate = self.messages.get(key)
            if duplicate:
                check_duplicate(context, duplicate, digest)
                task = await self.task_store.get(duplicate["task_id"], context)
                if task is None:
                    raise TaskNotFoundError()
                return Admission(task)
            context_id = message.context_id or str(uuid.uuid4())
            chat_key = (context.tenant, context_id)
            chat = self.chats.get(chat_key)
            if chat is None:
                # Legacy rows do not establish an authenticated chat owner.
                legacy_task = await self.task_store.has_context(context_id, context)
                with self.agent.workflow_store._lock:
                    legacy_run = any(
                        record.context_id == context_id and record.tenant_id == context.tenant
                        for record in self.agent.workflow_store._records.values()
                    )
                if legacy_task or legacy_run:
                    raise TaskNotFoundError()
                chat = {"owner_id": actor.owner_id, "latest_root_run_id": None}
            authorize(context, chat["owner_id"])
            active_task_id = None
            if chat["latest_root_run_id"]:
                record = self.agent.workflow_store.get(
                    chat["latest_root_run_id"], tenant_id=context.tenant, owner_id=chat["owner_id"]
                )
                if record.state not in TERMINAL_STATES:
                    active_task_id = record.task_id
            task = new_task(message, context_id, active_task_id)
            admitted = Admission(task) if active_task_id else create_workflow(self.agent, request, task, context)
            await self.task_store.save(task, context)
            if admitted.run_id:
                chat["latest_root_run_id"] = admitted.run_id
            self.chats[chat_key] = chat
            self.messages[key] = {
                "request_digest": digest, "owner_id": chat["owner_id"], "task_id": task.id,
            }
            return admitted


class PostgresRootAdmission:
    def __init__(self, agent, task_store):
        self.agent = agent
        self.task_store = task_store
        self.database = task_store.database

    async def admit(self, message, request, context):
        return await asyncio.to_thread(self._admit, message, request, context)

    def _admit(self, message, request, context):
        actor = context.state["principal"]
        key = (context.tenant, actor.actor_id, message.message_id)
        digest = fingerprint(message)
        with self.database.transaction() as connection:
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
                "SELECT * FROM core_chats WHERE tenant_id = %s AND context_id = %s FOR UPDATE",
                (context.tenant, context_id),
            ).fetchone()
            authorize(context, chat["owner_id"])
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
            task = new_task(message, context_id, active_task_id)
            admitted = Admission(task) if active_task_id else create_workflow(
                self.agent, request, task, context, connection=connection
            )
            self.task_store._save(task, context, connection=connection)
            connection.execute(
                """INSERT INTO core_root_messages
                   (tenant_id, actor_id, message_id, request_digest, owner_id, context_id, task_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (*key, digest, chat["owner_id"], context_id, task.id),
            )
            if admitted.run_id:
                connection.execute(
                    """UPDATE core_chats SET latest_root_run_id = %s
                       WHERE tenant_id = %s AND context_id = %s""",
                    (admitted.run_id, context.tenant, context_id),
                )
        return admitted
