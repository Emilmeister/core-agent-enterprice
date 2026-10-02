"""Company schedules and immutable occurrences share canonical root admission."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import uuid
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from a2a.server.context import ServerCallContext
from a2a.types import Message, Role, Task
from a2a.utils.errors import TaskNotFoundError
from psycopg.types.json import Jsonb

from .admission import Admission
from .auth import ScopeUser
from .config import RunRequest
from .cron_expression import next_due, normalize_expression
from .errors import CoreError
from .workflow import TERMINAL_STATES
from .workspace import WorkspaceBinding


_UNSET = object()
_METADATA = ("id", "revision", "context_id", "prompt", "expression", "timezone", "enabled", "next_due_at")


def _text(value, maximum=None):
    try:
        if not isinstance(value, str) or not value or "\0" in value:
            raise ValueError()
        size = len(value.encode("utf-8"))
        if maximum is not None and size > maximum:
            raise ValueError()
        return value
    except (ValueError, UnicodeError):
        raise CoreError("CRON_INVALID") from None


def _revision(value):
    if type(value) is not int or not 1 <= value < 2**63:
        raise CoreError("CRON_INVALID")
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _instant(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise CoreError("CRON_INVALID")
    return value.astimezone(UTC)


def validate_origin(value, prompt=None):
    try:
        if (not isinstance(value, dict) or value.keys() != {
                "version", "schedule_id", "revision", "source", "due_at", "prompt", "expression", "timezone"}
                or type(value["version"]) is not int or value["version"] != 1
                or value["source"] not in ("manual", "automatic")):
            raise CoreError("CRON_INVALID")
        _text(value["schedule_id"], 256)
        _revision(value["revision"])
        _text(value["prompt"])
        RunRequest.from_dict({"prompt": value["prompt"]})
        if prompt is not None and value["prompt"] != prompt:
            raise CoreError("CRON_INVALID")
        result = copy.deepcopy(value)
        result["expression"] = normalize_expression(value["expression"])
        _text(value["timezone"], 256)
        if value["source"] == "manual":
            if value["due_at"] is not None:
                raise CoreError("CRON_INVALID")
            anchor = datetime.now(UTC)
        else:
            anchor = _instant(datetime.fromisoformat(_text(value["due_at"], 64)))
            result["due_at"] = anchor.isoformat()
        next_due(result["expression"], result["timezone"], after_utc=anchor)
        return result
    except (ValueError, TypeError, OverflowError, KeyError, CoreError):
        raise CoreError("CRON_INVALID") from None


class CronStore:
    def __init__(self, admission, *, clock=None):
        self.admission = admission
        self.database = getattr(admission, "database", None)
        self.clock = clock or (lambda: datetime.now(UTC))
        if self.database is None:
            for name in ("_cron_schedules", "_cron_events"):
                if not hasattr(admission, name):
                    setattr(admission, name, {})

    @contextmanager
    def _transaction(self, connection=None, *, readonly=False):
        if self.database:
            with self.database.transaction() if connection is None else nullcontext(connection) as conn:
                yield conn
        else:
            with self.admission.agent.workflow_store._lock:
                if readonly:
                    yield None
                    return
                schedules = copy.deepcopy(self.admission._cron_schedules)
                events = copy.deepcopy(self.admission._cron_events)
                try:
                    yield None
                except BaseException:
                    self.admission._cron_schedules = schedules
                    self.admission._cron_events = events
                    raise

    def _now(self, connection):
        return (connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"] if connection is not None
                else _instant(self.clock()))

    def _request_lock(self, tenant, request_id, connection):
        if connection is not None:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                               (json.dumps(["cron-request", tenant, request_id], separators=(",", ":")),))

    @staticmethod
    def _admission_lock(context, message_id, connection):
        if connection is not None:
            key = (context.tenant, context.state["principal"].actor_id, message_id)
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                               (json.dumps(key, separators=(",", ":")),))

    def _row(self, tenant, schedule_id, connection, *, lock=False, deleted=False):
        _text(tenant)
        _text(schedule_id)
        if connection is not None:
            row = connection.execute("SELECT * FROM core_cron_schedules WHERE tenant_id=%s AND id=%s" +
                                     (" FOR UPDATE" if lock else ""), (tenant, schedule_id)).fetchone()
        else:
            row = self.admission._cron_schedules.get((tenant, schedule_id))
        if row is None or row["deleted"] and not deleted:
            raise CoreError("CRON_NOT_FOUND")
        if type(row["storage_version"]) is not int or row["storage_version"] != 1:
            raise CoreError("CRON_INVALID")
        _revision(row["revision"])
        return copy.deepcopy(row)

    def _chat(self, row, connection, *, lock=False):
        tenant, context = row["tenant_id"], row["context_id"]
        if connection is not None:
            chat = connection.execute("SELECT * FROM core_chats WHERE tenant_id=%s AND context_id=%s" +
                                      (" FOR NO KEY UPDATE" if lock else ""), (tenant, context)).fetchone()
        else:
            chat = self.admission.chats.get((tenant, context))
        if chat is None or chat["owner_id"] != row["owner_id"]:
            raise CoreError("CRON_INVALID")
        return {**chat, "context_id": context}

    def _root(self, row, chat, connection, *, lock=False):
        run_id = chat.get("latest_root_run_id")
        if not run_id:
            return None
        if connection is not None:
            root = connection.execute("SELECT run_id,task_id,state,tenant_id,owner_id,context_id,parent_run_id FROM core_runs "
                                      "WHERE tenant_id=%s AND run_id=%s" + (" FOR UPDATE" if lock else ""),
                                      (row["tenant_id"], run_id)).fetchone()
        else:
            record = self.admission.agent.workflow_store._records.get(run_id)
            root = {key: getattr(record, key) for key in ("run_id", "task_id", "state", "tenant_id", "owner_id", "context_id", "parent_run_id")} if record else None
        if (root is None or root["parent_run_id"] is not None
                or (root["tenant_id"], root["owner_id"], root["context_id"]) != (row["tenant_id"], row["owner_id"], row["context_id"])):
            raise CoreError("CRON_INVALID")
        return root

    def _metadata(self, row, connection):
        result = {key: row[key] for key in _METADATA}
        result["next_due_at"] = result["next_due_at"].isoformat() if result["next_due_at"] is not None else None
        root = self._root(row, self._chat(row, connection), connection)
        result["active_task_id"] = root["task_id"] if root and root["state"] not in TERMINAL_STATES else None
        return copy.deepcopy(result)

    def _save(self, row, connection, *, insert=False):
        if connection is None:
            self.admission._cron_schedules[(row["tenant_id"], row["id"])] = copy.deepcopy(row)
        elif insert:
            keys = tuple(row)
            connection.execute("INSERT INTO core_cron_schedules (" + ",".join(keys) + ") VALUES (" +
                               ",".join(["%s"] * len(keys)) + ")", [row[key] for key in keys])
        else:
            connection.execute("""UPDATE core_cron_schedules SET prompt=%s,expression=%s,timezone=%s,enabled=%s,
                deleted=%s,revision=%s,next_due_at=%s,updated_at=%s WHERE tenant_id=%s AND id=%s""",
                [row[key] for key in ("prompt", "expression", "timezone", "enabled", "deleted", "revision",
                                     "next_due_at", "updated_at", "tenant_id", "id")])

    def _event(self, row, kind, actor, source, connection, **values):
        event = {"id": str(uuid.uuid4()), "tenant_id": row["tenant_id"], "schedule_id": row["id"],
                 "context_id": row["context_id"], "owner_id": row["owner_id"], "storage_version": 1,
                 "kind": kind, "schedule_revision": row["revision"], "actor_id": actor, "source": source,
                 "request_id": None, "request_digest": None, "task_id": None, "run_id": None,
                 "due_at": None, "through": None, "reason": None, "history_run_id": None,
                 "history_position": None, "payload": {}, "created_at": self._now(connection), **values}
        if connection is None:
            event["seq"] = max(self.admission._cron_events, default=0) + 1
            self.admission._cron_events[event["seq"]] = copy.deepcopy(event)
        else:
            keys = tuple(event)
            event["seq"] = connection.execute("INSERT INTO core_cron_events (" + ",".join(keys) + ") VALUES (" +
                ",".join(["%s"] * len(keys)) + ") RETURNING seq",
                [Jsonb(event[key]) if key in {"payload", "history_position"} and event[key] is not None else event[key] for key in keys]).fetchone()["seq"]
        return event

    def _receipt(self, tenant, request_id, digest, connection):
        if connection is None:
            row = next((row for row in self.admission._cron_events.values()
                        if row["tenant_id"] == tenant and row["request_id"] == request_id), None)
        else:
            row = connection.execute("SELECT * FROM core_cron_events WHERE tenant_id=%s AND request_id=%s",
                                     (tenant, request_id)).fetchone()
        if row:
            if row["storage_version"] != 1:
                raise CoreError("CRON_INVALID")
            if row["request_digest"] != digest:
                raise CoreError("CRON_CONFLICT")
        return copy.deepcopy(row)

    def _values(self, values, now):
        try:
            _text(values["prompt"])
            RunRequest.from_dict({"prompt": values["prompt"]})
            expression = normalize_expression(values["expression"])
            timezone = _text(values.get("timezone", "Europe/Moscow"), 256)
            due = next_due(expression, timezone, after_utc=now)
            return {"prompt": values["prompt"], "expression": expression, "timezone": timezone}, due
        except (KeyError, CoreError):
            raise CoreError("CRON_INVALID") from None

    async def create(self, context, payload):
        if self.database:
            return await asyncio.to_thread(self.create_in_transaction, context, payload)
        return await self.admission.transaction(context, lambda admit: self.create_in_transaction(context, payload))

    def create_from_tool(self, record, lease_token, call_id, arguments):
        """Request lock precedes the workflow fence; never lock the chat after it."""
        if (not isinstance(arguments, dict) or not {"prompt", "expression"} <= arguments.keys()
                or arguments.keys() - {"prompt", "expression", "timezone"} or not lease_token):
            raise CoreError("CRON_INVALID")
        _text(call_id)
        attempt = record.snapshot.get("tool_calls")
        request_id = "cron:tool:" + _digest([record.run_id, attempt, call_id])
        workflow = self.admission.agent.workflow_store
        with self._transaction() as connection:
            self._request_lock(record.tenant_id, request_id, connection)
            ancestors = workflow.execution_ancestors(record, connection=connection) if record.parent_run_id else ()
            parent_id = None
            for run_id in (*reversed(ancestors), record.run_id):
                current = workflow.get(run_id, tenant_id=record.tenant_id, owner_id=record.owner_id,
                                       connection=connection, lock=True)
                if current.context_id != record.context_id or current.parent_run_id != parent_id:
                    raise CoreError("CRON_INVALID")
                if current.cancel_requested or current.state in TERMINAL_STATES or current.snapshot.get("terminal_intent"):
                    raise CoreError("TASK_CANCELLED")
                parent_id = current.run_id
            if connection is not None:
                valid = connection.execute("""SELECT 1 FROM core_runs WHERE run_id=%s AND tenant_id=%s
                    AND lease_token=%s AND lease_expires_at > EXTRACT(EPOCH FROM clock_timestamp())""",
                    (current.run_id, current.tenant_id, lease_token)).fetchone()
            else:
                lease = workflow._leases.get(current.run_id)
                valid = lease and lease[1] == lease_token and lease[2] > workflow.clock()
            if not valid:
                raise CoreError("LEASE_LOST")
            pending = current.snapshot.get("pending_call", {})
            nested = current.snapshot.get("nested_dispatch", {})
            if nested.get("state") == "executing" and nested.get("subject", {}).get("tool_name") == "core_cron_create":
                pending = {"id": nested["call_id"], "name": nested["subject"]["tool_name"],
                           "arguments": nested["subject"]["arguments"]}
            if (current.state != "EXECUTING" or current.snapshot.get("tool_calls") != attempt
                    or pending != {"id": call_id, "name": "core_cron_create", "arguments": arguments}):
                raise CoreError("CRON_INVALID")
            actor = SimpleNamespace(actor_id="tool:" + current.run_id, owner_id=current.owner_id,
                                    is_owner=False, is_external=False)
            context = ServerCallContext(user=ScopeUser(current.owner_id), tenant=current.tenant_id,
                                        state={"principal": actor})
            return self.create_in_transaction(context, {**arguments, "context_id": current.context_id,
                "request_id": request_id}, connection=connection, source="tool")

    def create_in_transaction(self, context, payload, *, connection=None, source="owner"):
        if (not isinstance(payload, dict) or not {"request_id", "prompt", "expression"} <= payload.keys()
                or payload.keys() - {"request_id", "prompt", "expression", "timezone", "context_id"}
                or source not in {"owner", "tool"}):
            raise CoreError("CRON_INVALID")
        request_id = _text(payload["request_id"], 256)
        if "context_id" in payload:
            _text(payload["context_id"])
        with self._transaction(connection) as conn:
            now = self._now(conn)
            values, due = self._values(payload, now)
            digest = _digest(["create", values, payload.get("context_id")])
            self._request_lock(context.tenant, request_id, conn)
            previous = self._receipt(context.tenant, request_id, digest, conn)
            if previous:
                return copy.deepcopy(previous["payload"])
            try:
                binding = self.admission.ensure_chat(context, payload.get("context_id"), connection=conn)
            except TaskNotFoundError:
                raise CoreError("CRON_NOT_FOUND") from None
            row = {"id": str(uuid.uuid4()), "tenant_id": binding.tenant_id, "context_id": binding.context_id,
                   "owner_id": binding.owner_id, "storage_version": 1, "revision": 1, **values,
                   "enabled": True, "deleted": False, "next_due_at": due, "created_at": now, "updated_at": now}
            self._save(row, conn, insert=True)
            result = self._metadata(row, conn)
            self._event(row, "created", context.state["principal"].actor_id, source, conn,
                        request_id=request_id, request_digest=digest, payload=result)
            return result

    def get(self, tenant_id, schedule_id, *, connection=None):
        with self._transaction(connection, readonly=True) as conn:
            return self._metadata(self._row(tenant_id, schedule_id, conn), conn)

    def list(self, tenant_id, *, limit=50, after=None, connection=None):
        _text(tenant_id)
        if type(limit) is not int or not 1 <= limit <= 101:
            raise CoreError("CRON_INVALID")
        if after is not None:
            _text(after)
        with self._transaction(connection, readonly=True) as conn:
            if conn is None:
                rows = sorted((row for row in self.admission._cron_schedules.values()
                               if row["tenant_id"] == tenant_id and not row["deleted"]
                               and (after is None or row["id"] > after)), key=lambda row: row["id"])[:limit]
            else:
                rows = conn.execute("SELECT * FROM core_cron_schedules WHERE tenant_id=%s AND NOT deleted AND id>%s ORDER BY id LIMIT %s",
                                    (tenant_id, after or "", limit)).fetchall()
            for row in rows:
                if row["storage_version"] != 1:
                    raise CoreError("CRON_INVALID")
            return [self._metadata(row, conn) for row in rows]

    def update(self, tenant_id, schedule_id, payload, *, actor_id):
        if (not isinstance(payload, dict) or payload.keys() != {"prompt", "expression", "timezone", "enabled", "expected_revision"}
                or type(payload["enabled"]) is not bool):
            raise CoreError("CRON_INVALID")
        expected = _revision(payload["expected_revision"])
        _text(actor_id)
        with self._transaction() as conn:
            row = self._row(tenant_id, schedule_id, conn, lock=True)
            if row["revision"] != expected:
                raise CoreError("CRON_CONFLICT")
            now = self._now(conn)
            values, due = self._values(payload, now)
            row.update(values, enabled=payload["enabled"], next_due_at=due if payload["enabled"] else None,
                       revision=expected + 1, updated_at=now)
            self._save(row, conn)
            result = self._metadata(row, conn)
            self._event(row, "updated", actor_id, "owner", conn, payload=result)
            return result

    def delete(self, tenant_id, schedule_id, *, expected_revision, actor_id):
        expected = _revision(expected_revision)
        _text(actor_id)
        with self._transaction() as conn:
            row = self._row(tenant_id, schedule_id, conn, lock=True, deleted=True)
            if row["deleted"]:
                if row["revision"] != expected + 1:
                    raise CoreError("CRON_NOT_FOUND")
                events = self.events(tenant_id, exact_id=None, schedule_id=schedule_id, kind="deleted", connection=conn)
                return copy.deepcopy(events[-1]["payload"])
            if row["revision"] != expected:
                raise CoreError("CRON_CONFLICT")
            row.update(enabled=False, deleted=True, next_due_at=None, revision=expected + 1, updated_at=self._now(conn))
            self._save(row, conn)
            result = self._metadata(row, conn)
            self._event(row, "deleted", actor_id, "owner", conn, payload=result)
            return result

    @staticmethod
    def _origin(row, source, due=None):
        return validate_origin({"version": 1, "schedule_id": row["id"], "revision": row["revision"], "source": source,
                                "due_at": due.isoformat() if due else None,
                                **{key: row[key] for key in ("prompt", "expression", "timezone")}})

    def _replay_task(self, event, context, connection):
        if connection is not None:
            row = connection.execute("SELECT payload FROM core_a2a_tasks WHERE tenant=%s AND owner=%s AND task_id=%s",
                                     (event["tenant_id"], event["owner_id"], event["task_id"])).fetchone()
            if row is None:
                raise CoreError("CRON_INVALID")
            return Admission(Task.FromString(bytes(row["payload"])))
        # The synchronous admission callback already holds every TaskStore lock.
        from .auth import ScopeUser
        context.user = ScopeUser(event["owner_id"])
        task = self.admission.task_store.get_admitted(event["task_id"], context)
        if task is None:
            raise CoreError("CRON_INVALID")
        return Admission(copy.deepcopy(task))

    async def run_now(self, context, schedule_id, payload):
        if not isinstance(payload, dict) or payload.keys() != {"request_id", "expected_revision"}:
            raise CoreError("CRON_INVALID")
        request = _text(payload["request_id"], 256)
        expected = _revision(payload["expected_revision"])
        _text(schedule_id)
        message_id = "cron:manual:" + _digest(request)
        def execute(admit=None, connection=None):
            with self._transaction(connection) as conn:
                digest = _digest(["run-now", schedule_id, expected])
                self._request_lock(context.tenant, request, conn)
                self._admission_lock(context, message_id, conn)
                previous = self._receipt(context.tenant, request, digest, conn)
                if previous:
                    return self._replay_task(previous, context, conn)
                row = self._row(context.tenant, schedule_id, conn, lock=True)
                if row["revision"] != expected:
                    raise CoreError("CRON_CONFLICT")
                if not row["enabled"]:
                    raise CoreError("CRON_DISABLED")
                message = Message(message_id=message_id, context_id=row["context_id"], role=Role.ROLE_USER, parts=[{"text": row["prompt"]}])
                origin = self._origin(row, "manual")
                admitted = (admit(message, RunRequest(row["prompt"]), context, cron_origin=origin) if admit else
                            self.admission._admit_transaction(message, RunRequest(row["prompt"]), context, connection=conn, cron_origin=origin))
                self._event(row, "started", context.state["principal"].actor_id, "manual", conn,
                            request_id=request, request_digest=digest, task_id=admitted.task.id, run_id=admitted.run_id,
                            payload=origin)
                return admitted
        if self.database:
            return await asyncio.to_thread(execute)
        return await self.admission.transaction(context, execute)

    async def occur_memory(self, context, schedule_id, expected_revision, *, cutoff=None):
        return await self.admission.transaction(context, lambda admit: self.occur(
            context, schedule_id, expected_revision, cutoff=cutoff, admit=admit))

    def occur(self, context, schedule_id, expected_revision, *, connection=None, cutoff=None, admit=None):
        """Caller owns the PostgreSQL leader session and transaction; never reacquire a pool slot."""
        if self.database and connection is None:
            raise CoreError("CRON_INVALID")
        _revision(expected_revision)
        if cutoff is not None:
            cutoff = _instant(cutoff)
        with self._transaction(connection) as conn:
            candidate = self._row(context.tenant, schedule_id, conn, deleted=True)
            if candidate["deleted"] or not candidate["enabled"] or candidate["revision"] != expected_revision:
                return None
            due = candidate["next_due_at"]
            message_id = "cron:automatic:" + _digest([schedule_id, expected_revision, due.isoformat()])
            self._admission_lock(context, message_id, conn)
            row = self._row(context.tenant, schedule_id, conn, lock=True, deleted=True)
            now = self._now(conn)
            if (row["deleted"] or not row["enabled"] or row["revision"] != expected_revision
                    or row["next_due_at"] != due or due > now):
                return None
            chat = self._chat(row, conn, lock=True)
            root = self._root(row, chat, conn, lock=True)
            now = self._now(conn)
            reason, through = None, None
            if cutoff is not None and due <= cutoff:
                reason, through = "service_unavailable", cutoff
            elif now - due > timedelta(seconds=60):
                reason, through = "late", now
            elif root and root["state"] not in TERMINAL_STATES:
                reason = "context_busy"
            else:
                cleanup = getattr(self.admission, "workspace_cleanup", None)
                if cleanup:
                    try:
                        cleanup.check_ready(WorkspaceBinding(row["tenant_id"], row["owner_id"], row["context_id"]), conn)
                    except CoreError as error:
                        if error.code != "WORKSPACE_CLEANUP_PENDING":
                            raise
                        reason = "workspace_cleanup_pending"
            origin = self._origin(row, "automatic", due)
            admitted = None
            if reason:
                position = None
                if root:
                    from .history import _entries, _root
                    source = _root(self.admission, root["run_id"], row["tenant_id"], chat, conn)
                    position = list(_entries(self.admission, source, conn, before=None, limit=1)[0]["position"])
                self._event(row, "skipped", context.state["principal"].actor_id, "automatic", conn,
                            due_at=due, through=through, reason=reason, history_run_id=root["run_id"] if root else None,
                            history_position=position, payload=origin)
            else:
                message = Message(message_id=message_id, context_id=row["context_id"], role=Role.ROLE_USER, parts=[{"text": row["prompt"]}])
                admitted = (admit(message, RunRequest(row["prompt"]), context, cron_origin=origin) if admit else
                            self.admission._admit_transaction(message, RunRequest(row["prompt"]), context, connection=conn, cron_origin=origin))
                self._event(row, "started", context.state["principal"].actor_id, "automatic", conn,
                            task_id=admitted.task.id, run_id=admitted.run_id, due_at=due, payload=origin)
            row.update(next_due_at=next_due(row["expression"], row["timezone"], after_utc=through or due), updated_at=now)
            self._save(row, conn)
            return admitted

    def due(self, tenant_id, *, limit=100, after=None, connection=None):
        _text(tenant_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise CoreError("CRON_INVALID")
        with self._transaction(connection, readonly=True) as conn:
            now = self._now(conn)
            if conn is not None:
                return conn.execute("""SELECT * FROM core_cron_schedules WHERE tenant_id=%s AND enabled AND NOT deleted
                    AND next_due_at<=%s AND id>%s ORDER BY id LIMIT %s""", (tenant_id, now, after or "", limit)).fetchall()
            return copy.deepcopy(sorted((row for row in self.admission._cron_schedules.values()
                if row["tenant_id"] == tenant_id and row["enabled"] and not row["deleted"]
                and row["next_due_at"] <= now and row["id"] > (after or "")), key=lambda row: row["id"])[:limit])

    def events(self, tenant_id, *, context_id=None, owner_id=None, run_id=_UNSET, before=None, history_before=None, exact_id=None,
               schedule_id=None, kind=None, limit=100, descending=False, connection=None):
        _text(tenant_id)
        if type(limit) is not int or not 1 <= limit <= 101:
            raise CoreError("CRON_INVALID")
        if before is not None and (type(before) is not int or not 0 <= before < 2**63):
            raise CoreError("CRON_INVALID")
        if history_before is not None and (not isinstance(history_before, (list, tuple)) or len(history_before) != 6
                or any(type(value) is not int or not 0 <= value < 2**63 for value in history_before)):
            raise CoreError("CRON_INVALID")
        filters = {key: value for key, value in {"context_id": context_id, "owner_id": owner_id,
                   "id": exact_id, "schedule_id": schedule_id, "kind": kind}.items() if value is not None}
        if run_id is not _UNSET:
            filters["history_run_id"] = run_id
        with self._transaction(connection, readonly=True) as conn:
            if conn is None:
                rows = [row for row in self.admission._cron_events.values() if row["tenant_id"] == tenant_id
                        and all(row[key] == value for key, value in filters.items()) and (before is None or row["seq"] < before)]
                def position(row):
                    return (*tuple(row["history_position"] or (0, 0, 0, 0)), 1, row["seq"])
                if history_before is not None:
                    rows = [row for row in rows if position(row) < tuple(history_before)]
                rows = sorted(rows, key=position if run_id is not _UNSET else lambda row: row["seq"], reverse=descending)[:limit]
            else:
                clauses, args = ["tenant_id=%s"], [tenant_id]
                for key, value in filters.items():
                    clauses.append(key + (" IS NULL" if value is None else "=%s"))
                    if value is not None:
                        args.append(value)
                if before is not None:
                    clauses.append("seq<%s")
                    args.append(before)
                positions = [f"COALESCE((history_position->>{index})::bigint,0)" for index in range(4)]
                if history_before is not None:
                    clauses.append("(" + ",".join([*positions, "1", "seq"]) + ") < (%s,%s,%s,%s,%s,%s)")
                    args.extend(history_before)
                args.append(limit)
                direction = " DESC" if descending else " ASC"
                ordering = ",".join(value + direction for value in ([*positions, "seq"] if run_id is not _UNSET else ["seq"]))
                rows = conn.execute("SELECT * FROM core_cron_events WHERE " + " AND ".join(clauses) +
                                    " ORDER BY " + ordering + " LIMIT %s", args).fetchall()
            if any(type(row["storage_version"]) is not int or row["storage_version"] != 1 for row in rows):
                raise CoreError("CRON_INVALID")
            return copy.deepcopy(rows)
