"""Exact-selection cleanup: committed chat intent, private capture, durable receipt.

Hashing reads each selected file in bounded chunks twice. Admission quiesces model
writers; this is not isolation from hostile host processes holding open file fds.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager, nullcontext

from psycopg.types.json import Jsonb

from .chat_files import _mkdir, _rename, _write
from .errors import CoreError
from .workflow import TERMINAL_STATES
from .workspace import WorkspaceBinding


def _stat(info):
    return [info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _text(value, maximum=None):
    try:
        return (isinstance(value, str) and bool(value) and "\0" not in value
                and len(value.encode("utf-8")) <= (maximum if maximum is not None else 2**31 - 1))
    except UnicodeError:
        return False


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


class WorkspaceCleanupService:
    def __init__(self, admission, workspaces):
        self.admission, self.workspaces = admission, workspaces
        self.database = getattr(admission, "database", None)
        self._recovery_after = ""
        if not self.database and not hasattr(admission, "_workspace_cleanups"):
            admission._workspace_cleanups = {}

    @contextmanager
    def _locked(self, tenant, context):
        if not _text(tenant) or not _text(context):
            raise CoreError("TASK_NOT_FOUND")
        if self.database:
            with self.database.transaction() as connection:
                chat = connection.execute("SELECT * FROM core_chats WHERE tenant_id=%s AND context_id=%s FOR NO KEY UPDATE",
                                          (tenant, context)).fetchone()
                record = connection.execute("""SELECT r.state FROM core_runs r WHERE r.run_id=%s
                    AND r.tenant_id=%s AND r.context_id=%s AND r.owner_id=%s AND r.parent_run_id IS NULL
                    AND EXISTS(SELECT 1 FROM core_root_messages m WHERE m.tenant_id=r.tenant_id
                        AND m.owner_id=r.owner_id AND m.context_id=r.context_id AND m.task_id=r.task_id)""",
                    (chat["latest_root_run_id"], tenant, context, chat["owner_id"])).fetchone() if chat else None
                if chat is None or (chat["latest_root_run_id"] is not None and record is None):
                    raise CoreError("TASK_NOT_FOUND")
                yield WorkspaceBinding(tenant, chat["owner_id"], context), chat, record["state"] if record else None, connection
        else:
            with self.admission.agent.workflow_store._lock:
                chat = self.admission.chats.get((tenant, context))
                record = self.admission.agent.workflow_store._records.get(chat.get("latest_root_run_id")) if chat else None
                if (chat is None or chat.get("latest_root_run_id") is not None and (record is None or record.parent_run_id is not None
                        or (record.tenant_id, record.owner_id, record.context_id) != (tenant, chat.get("owner_id"), context)
                        or not any(key[0] == tenant and row["task_id"] == record.task_id and row["owner_id"] == record.owner_id
                                   for key, row in self.admission.messages.items()))):
                    raise CoreError("TASK_NOT_FOUND")
                yield WorkspaceBinding(tenant, chat["owner_id"], context), chat, record.state if record else None, None

    def _load(self, binding, request_id, connection):
        if connection is not None:
            query = "SELECT * FROM core_workspace_cleanups WHERE tenant_id=%s AND context_id=%s AND owner_id=%s"
            args = [binding.tenant_id, binding.context_id, binding.owner_id]
            if request_id is not None:
                query += " AND request_id=%s"
                args.append(request_id)
            return connection.execute(query + " ORDER BY created_at DESC,operation_id DESC LIMIT 1", args).fetchone()
        rows = [value for (tenant, context, request), value in self.admission._workspace_cleanups.items()
                if (tenant, context, value["owner_id"]) == (binding.tenant_id, binding.context_id, binding.owner_id)
                and (request_id is None or request == request_id)]
        return copy.deepcopy(rows[-1]) if rows else None

    def _save(self, operation, connection, *, insert=False):
        if connection is None:
            self.admission._workspace_cleanups[(operation["tenant_id"], operation["context_id"], operation["request_id"])] = copy.deepcopy(operation)
        elif insert:
            keys = ("tenant_id", "context_id", "owner_id", "request_id", "operation_id", "actor_id", "storage_version",
                    "request_digest", "selection", "base_revision", "workspace_revision", "state", "results")
            connection.execute("INSERT INTO core_workspace_cleanups (" + ",".join(keys) + ") VALUES (" + ",".join(["%s"] * len(keys)) + ")",
                [Jsonb(operation[key]) if key in {"selection", "results"} else operation[key] for key in keys])
        else:
            connection.execute("""UPDATE core_workspace_cleanups SET state=%s,results=%s,workspace_revision=%s,updated_at=now()
                WHERE tenant_id=%s AND context_id=%s AND owner_id=%s AND request_id=%s""",
                (operation["state"], Jsonb(operation["results"]), operation["workspace_revision"], operation["tenant_id"],
                 operation["context_id"], operation["owner_id"], operation["request_id"]))

    def check_ready(self, binding, connection=None):
        if self.database:
            if connection is None:
                with self.database.transaction() as borrowed:
                    return self.check_ready(binding, borrowed)
            pending = connection.execute("""SELECT 1 FROM core_workspace_cleanups
                WHERE tenant_id=%s AND context_id=%s AND owner_id=%s AND state<>'completed' LIMIT 1""",
                (binding.tenant_id, binding.context_id, binding.owner_id)).fetchone()
            latest = self._load(binding, None, connection) if not pending else None
        else:
            with self.admission.agent.workflow_store._lock:
                pending = any((tenant, context, row["owner_id"]) == (binding.tenant_id, binding.context_id, binding.owner_id)
                              and row["state"] != "completed"
                              for (tenant, context, _), row in self.admission._workspace_cleanups.items())
                latest = self._load(binding, None, None) if not pending else None
        if pending:
            raise CoreError("WORKSPACE_CLEANUP_PENDING")
        if latest is not None:
            try:
                self._validate(latest)
            except CoreError:
                raise CoreError("WORKSPACE_CLEANUP_PENDING") from None

    def preview_state(self, binding):
        with self._locked(binding.tenant_id, binding.context_id) as (actual, chat, _, connection):
            if actual != binding:
                raise CoreError("TASK_NOT_FOUND")
            pending = False
            try:
                self.check_ready(binding, connection)
            except CoreError as error:
                if error.code != "WORKSPACE_CLEANUP_PENDING":
                    raise
                pending = True
            return {"workspace_revision": chat.get("workspace_revision", 0), "cleanup_pending": pending}

    def _request(self, payload):
        if (not isinstance(payload, dict) or payload.keys() != {"request_id", "files"}
                or not _text(payload["request_id"], 256) or not isinstance(payload["files"], list) or len(payload["files"]) > 1000):
            raise CoreError("REQUEST_INVALID")
        seen = set()
        for item in payload["files"]:
            if (not isinstance(item, dict) or item.keys() != {"path", "identity_token"}
                    or not isinstance(item["identity_token"], str) or not re.fullmatch(r"v[12]:[0-9a-f]{64}", item["identity_token"])):
                raise CoreError("REQUEST_INVALID")
            self.workspaces.path_parts(item["path"])
            if item["path"] in seen:
                raise CoreError("REQUEST_INVALID")
            seen.add(item["path"])
        return copy.deepcopy(payload)

    def _validate(self, operation):
        try:
            if (operation["storage_version"] != 1 or type(operation["storage_version"]) is not int
                    or operation["state"] not in {"pending", "completed", "reconciliation"}
                    or type(operation["base_revision"]) is not int or operation["base_revision"] < 0
                    or type(operation["workspace_revision"]) is not int
                    or operation["workspace_revision"] not in (operation["base_revision"], operation["base_revision"] + 1)
                    or str(uuid.UUID(operation["operation_id"])) != operation["operation_id"]):
                raise ValueError()
            files = [{key: item[key] for key in ("path", "identity_token")} for item in operation["selection"]]
            self._request({"request_id": operation["request_id"], "files": files})
            if operation["request_digest"] != _digest([operation["tenant_id"], operation["context_id"], files]):
                raise ValueError()
            if len(operation["results"]) != len(files):
                raise ValueError()
            for selected, result in zip(operation["selection"], operation["results"]):
                if (selected.keys() != {"path", "identity_token", "proof"} or result["path"] != selected["path"]
                        or result["status"] not in {"pending", "deleted", "skipped", "error"}):
                    raise ValueError()
                proof = selected["proof"]
                if proof is not None and (not isinstance(proof, dict) or proof.keys() != {"stat", "sha256", "parents"}
                        or not isinstance(proof["stat"], list) or len(proof["stat"]) != 7
                        or any(type(value) is not int for value in proof["stat"])
                        or not stat.S_ISREG(proof["stat"][2]) or proof["stat"][3] != 1 or proof["stat"][4] < 0
                        or not isinstance(proof["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", proof["sha256"])
                        or not isinstance(proof["parents"], list) or len(proof["parents"]) != len(selected["path"].split("/"))
                        or any(not isinstance(pair, list) or len(pair) != 2
                               or any(type(value) is not int for value in pair) for pair in proof["parents"])):
                    raise ValueError()
                expected_keys = {"path", "status"} | ({"size"} if result["status"] == "deleted" else
                                                      {"reason"} if result["status"] in {"skipped", "error"} else set())
                if (result.keys() != expected_keys
                        or result.get("reason") not in {None, "missing", "identity_changed", "unsafe_file", "protected_file",
                                                       "filesystem_error", "reconciliation_required"}
                        or result["status"] == "skipped" and result.get("reason") not in {"missing", "identity_changed", "unsafe_file", "protected_file"}
                        or result["status"] == "error" and result.get("reason") not in {"filesystem_error", "reconciliation_required"}
                        or (result["status"] in {"pending", "deleted"} or result.get("reason") == "reconciliation_required") and proof is None
                        or result["status"] == "deleted" and (type(result.get("size")) is not int or result["size"] != proof["stat"][4])):
                    raise ValueError()
            deleted = any(result["status"] == "deleted" for result in operation["results"])
            if operation["state"] == "completed":
                if (any(result["status"] == "pending" or result.get("reason") == "reconciliation_required"
                        for result in operation["results"])
                        or operation["workspace_revision"] != operation["base_revision"] + int(deleted)):
                    raise ValueError()
            elif operation["workspace_revision"] != operation["base_revision"]:
                raise ValueError()
        except (KeyError, IndexError, TypeError, ValueError, AttributeError, CoreError):
            raise CoreError("WORKSPACE_CLEANUP_INVALID") from None

    def _receipt(self, operation):
        self._validate(operation)
        results = copy.deepcopy(operation["results"])
        return {"request_id": operation["request_id"], "operation_id": operation["operation_id"],
                "state": operation["state"], "workspace_revision": operation["workspace_revision"],
                "files": [{key: item[key] for key in ("path", "identity_token")} for item in operation["selection"]],
                "results": results, "totals": {
                    "deleted": sum(item["status"] == "deleted" for item in results),
                    "skipped": sum(item["status"] == "skipped" for item in results),
                    "errors": sum(item["status"] == "error" for item in results),
                    "deleted_bytes": sum(item["size"] for item in results if item["status"] == "deleted")}}

    @contextmanager
    def _source(self, binding, path):
        parts = self.workspaces.path_parts(path)
        with self.workspaces.open_workspace(binding) as root:
            if root is None:
                raise FileNotFoundError()
            descriptor = os.dup(root)
            parents = []
            try:
                info = os.fstat(descriptor)
                parents.append([info.st_dev, info.st_ino])
                for part in parts[:-1]:
                    child = self.workspaces._directory(descriptor, [part])
                    os.close(descriptor)
                    descriptor = child
                    info = os.fstat(descriptor)
                    parents.append([info.st_dev, info.st_ino])
                yield descriptor, parts[-1], parents
            finally:
                os.close(descriptor)

    @staticmethod
    def _hash(parent, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise CoreError("CLEANUP_UNSAFE_FILE")
            remaining, digest = before.st_size, hashlib.sha256()
            while remaining:
                data = stream.read(min(1024 * 1024, remaining))
                if not data:
                    raise CoreError("CLEANUP_IDENTITY_CHANGED")
                digest.update(data)
                remaining -= len(data)
            if _stat(os.fstat(stream.fileno())) != _stat(before):
                raise CoreError("CLEANUP_IDENTITY_CHANGED")
            return _stat(before), digest.hexdigest()

    def _selected(self, binding, item, revision):
        selected = {**item, "proof": None}
        result = {"path": item["path"], "status": "skipped"}
        if self.workspaces._control(self.workspaces.path_parts(item["path"])):
            return selected, {**result, "reason": "protected_file"}
        try:
            with self._source(binding, item["path"]) as (parent, name, parents):
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    return selected, {**result, "reason": "unsafe_file"}
                if self.workspaces.identity(binding, item["path"], info, revision) != item["identity_token"]:
                    return selected, {**result, "reason": "identity_changed"}
                captured, digest = self._hash(parent, name)
                if captured != _stat(info):
                    return selected, {**result, "reason": "identity_changed"}
                selected["proof"] = {"stat": captured, "sha256": digest, "parents": parents}
                return selected, {"path": item["path"], "status": "pending"}
        except FileNotFoundError:
            return selected, {**result, "reason": "missing"}
        except CoreError as error:
            reason = {"FILE_NOT_FOUND": "unsafe_file", "CLEANUP_UNSAFE_FILE": "unsafe_file",
                      "CLEANUP_IDENTITY_CHANGED": "identity_changed"}.get(error.code)
            return selected, {**result, "reason": reason} if reason else {**result, "status": "error", "reason": "filesystem_error"}
        except OSError:
            return selected, {**result, "status": "error", "reason": "filesystem_error"}

    def _prepare(self, tenant, context, actor, payload):
        with self._locked(tenant, context) as (binding, chat, state, connection):
            digest = _digest([tenant, context, payload["files"]])
            existing = self._load(binding, payload["request_id"], connection)
            if existing:
                self._validate(existing)
                if digest != existing["request_digest"]:
                    raise CoreError("CLEANUP_REQUEST_CONFLICT")
                return existing
            if state is not None and state not in TERMINAL_STATES:
                raise CoreError("CONTEXT_BUSY")
            self.check_ready(binding, connection)
            revision = chat.get("workspace_revision", 0)
            selected = [self._selected(binding, item, revision) for item in payload["files"]]
            operation = {"tenant_id": tenant, "context_id": context, "owner_id": binding.owner_id,
                "request_id": payload["request_id"], "operation_id": str(uuid.uuid4()), "actor_id": actor,
                "storage_version": 1, "request_digest": digest, "selection": [item[0] for item in selected],
                "base_revision": revision, "workspace_revision": revision, "state": "pending", "results": [item[1] for item in selected]}
            self._save(operation, connection, insert=True)
            return operation

    async def delete(self, tenant_id, context_id, actor_id, payload):
        payload = self._request(payload)
        if not _text(actor_id):
            raise CoreError("REQUEST_INVALID")
        if self.database:
            operation = await asyncio.to_thread(self._prepare, tenant_id, context_id, actor_id, payload)
        else:
            async with self.admission.lock:
                operation = await asyncio.to_thread(self._prepare, tenant_id, context_id, actor_id, payload)
        return await asyncio.to_thread(self._execute, tenant_id, context_id, operation["request_id"])

    async def get(self, tenant_id, context_id, request_id=None):
        if request_id is not None and not _text(request_id, 256):
            raise CoreError("REQUEST_INVALID")
        def read():
            with self._locked(tenant_id, context_id) as (binding, _, _, connection):
                operation = self._load(binding, request_id, connection)
                if operation is None:
                    raise CoreError("FILE_CLEANUP_NOT_FOUND")
                return self._receipt(operation)
        try:
            return await asyncio.to_thread(read)
        except CoreError as error:
            if error.code == "TASK_NOT_FOUND":
                raise CoreError("FILE_CLEANUP_NOT_FOUND") from None
            raise

    @contextmanager
    def _stage(self, operation):
        root = os.open(self.workspaces.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name in ("private", "cleanup", operation["operation_id"]):
                child = _mkdir(root, name)
                os.close(root)
                root = child
            yield root
        finally:
            os.close(root)

    @staticmethod
    def _marker(stage, name, value=None):
        if value is not None:
            temporary = ".pending-" + uuid.uuid4().hex
            _write(stage, temporary, json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
            os.replace(temporary, name, src_dir_fd=stage, dst_dir_fd=stage)
            os.fsync(stage)
            return value
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=stage)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536:
                raise CoreError("WORKSPACE_CLEANUP_INVALID")
            try:
                value = json.loads(stream.read(65537))
                if not isinstance(value, dict):
                    raise ValueError()
                return value
            except (ValueError, RecursionError):
                raise CoreError("WORKSPACE_CLEANUP_INVALID") from None

    def _item(self, binding, operation, selected, index, stage):
        name, marker_name = str(index) + ".file", str(index) + ".json"
        proof = selected["proof"]
        scope = _digest([operation["operation_id"], binding.tenant_id, binding.owner_id, binding.context_id, index, selected])
        marker = self._marker(stage, marker_name)
        if marker is None:
            marker = self._marker(stage, marker_name, {"version": 1, "scope": scope, "phase": "intent", "captured": None})
        if (not isinstance(marker, dict) or marker.keys() != {"version", "scope", "phase", "captured"}
                or type(marker["version"]) is not int or marker["version"] != 1 or marker["scope"] != scope
                or not isinstance(marker["phase"], str)
                or marker["phase"] not in {"intent", "delete_ready", "restore_intent", "restored"}):
            raise CoreError("WORKSPACE_CLEANUP_INVALID")
        captured = marker["captured"]
        if ((marker["phase"] == "intent" and captured is not None)
                or (marker["phase"] != "intent" and (not isinstance(captured, list) or len(captured) != 7
                    or any(type(value) is not int for value in captured)))
                or marker["phase"] == "delete_ready" and captured[:6] != proof["stat"][:6]):
            raise CoreError("WORKSPACE_CLEANUP_INVALID")
        result = {"path": selected["path"], "status": "skipped"}
        try:
            staged = os.stat(name, dir_fd=stage, follow_symlinks=False)
        except FileNotFoundError:
            staged = None
        if staged is None and marker["phase"] == "delete_ready":
            os.fsync(stage)
            return {"path": selected["path"], "status": "deleted", "size": proof["stat"][4]}
        if marker["phase"] == "restored":
            if staged is not None:
                raise CoreError("WORKSPACE_CLEANUP_INVALID")
            return {**result, "reason": "identity_changed"}
        with self._source(binding, selected["path"]) as (parent, source, parents):
            if parents != proof["parents"]:
                if staged is not None or marker["phase"] != "intent":
                    raise CoreError("WORKSPACE_CLEANUP_RECONCILIATION")
                return {**result, "reason": "identity_changed"}
            if marker["phase"] == "restore_intent" and staged is None:
                current = _stat(os.stat(source, dir_fd=parent, follow_symlinks=False))
                if current[:6] != marker["captured"][:6]:
                    raise CoreError("WORKSPACE_CLEANUP_RECONCILIATION")
                os.fsync(parent)
                os.fsync(stage)
                self._marker(stage, marker_name, {**marker, "phase": "restored"})
                return {**result, "reason": "identity_changed"}
            if staged is None:
                current = os.stat(source, dir_fd=parent, follow_symlinks=False)
                if _stat(current) != proof["stat"]:
                    return {**result, "reason": "identity_changed"}
                _rename(parent, source, stage, name)
                staged = os.stat(name, dir_fd=stage, follow_symlinks=False)
            # Recovery may observe the rename before either parent was fsynced.
            # Pin/verify the source parent above, then make both directory entries
            # durable before the deletion proof or unlink, on every capture path.
            os.fsync(parent)
            os.fsync(stage)
            try:
                captured, digest = self._hash(stage, name)
                valid = captured[:6] == proof["stat"][:6] and digest == proof["sha256"]
                if marker["phase"] == "delete_ready":
                    valid = valid and captured == marker["captured"]
            except (OSError, CoreError):
                valid, captured = False, _stat(staged)
            try:
                with self._source(binding, selected["path"]) as (_, _, current_parents):
                    valid = valid and current_parents == proof["parents"]
            except (OSError, CoreError):
                valid = False
            if marker["phase"] == "restore_intent" or not valid:
                if marker["phase"] != "restore_intent":
                    marker = self._marker(stage, marker_name, {**marker, "phase": "restore_intent", "captured": captured})
                elif _stat(staged) != marker["captured"]:
                    raise CoreError("WORKSPACE_CLEANUP_RECONCILIATION")
                try:
                    _rename(stage, name, parent, source)
                    os.fsync(stage)
                    os.fsync(parent)
                except OSError:
                    raise CoreError("WORKSPACE_CLEANUP_RECONCILIATION") from None
                self._marker(stage, marker_name, {**marker, "phase": "restored"})
                return {**result, "reason": "identity_changed"}
            self._marker(stage, marker_name, {**marker, "phase": "delete_ready", "captured": captured})
            # Capture no longer addresses the user-controlled pathname.
            os.unlink(name, dir_fd=stage)
            os.fsync(stage)
            return {"path": selected["path"], "status": "deleted", "size": proof["stat"][4]}

    def _execute(self, tenant, context, request):
        with self._locked(tenant, context) as (binding, chat, state, connection):
            operation = self._load(binding, request, connection)
            if operation is None:
                raise CoreError("FILE_CLEANUP_NOT_FOUND")
            self._validate(operation)
            if operation["state"] == "completed":
                return self._receipt(operation)
            if (state is not None and state not in TERMINAL_STATES) or chat.get("workspace_revision", 0) != operation["base_revision"]:
                raise CoreError("WORKSPACE_CLEANUP_INVALID")
            reconciliation = False
            try:
                needs_stage = any(item["status"] == "pending" or item.get("reason") == "reconciliation_required"
                                  for item in operation["results"])
                with self._stage(operation) if needs_stage else nullcontext(None) as stage:
                    for index, selected in enumerate(operation["selection"]):
                        previous = operation["results"][index]
                        if previous["status"] != "pending" and previous.get("reason") != "reconciliation_required":
                            continue
                        try:
                            result = self._item(binding, operation, selected, index, stage)
                        except (OSError, CoreError) as error:
                            if isinstance(error, CoreError) and error.code == "WORKSPACE_CLEANUP_INVALID":
                                raise
                            marker = self._marker(stage, str(index) + ".json")
                            try:
                                os.stat(str(index) + ".file", dir_fd=stage, follow_symlinks=False)
                                staged = True
                            except FileNotFoundError:
                                staged = False
                            uncertain = staged or (marker is not None and marker.get("phase") != "intent")
                            reconciliation |= bool(uncertain)
                            if uncertain:
                                result = {"path": selected["path"], "status": "error", "reason": "reconciliation_required"}
                            elif isinstance(error, FileNotFoundError):
                                result = {"path": selected["path"], "status": "skipped", "reason": "missing"}
                            else:
                                result = {"path": selected["path"], "status": "error", "reason": "filesystem_error"}
                        operation["results"][index] = result
            except OSError:
                # A private storage failure leaves the committed operation retryable.
                operation["state"] = "reconciliation"
                self._save(operation, connection)
                return self._receipt(operation)
            operation["state"] = "reconciliation" if reconciliation else "completed"
            if not reconciliation and any(item["status"] == "deleted" for item in operation["results"]):
                operation["workspace_revision"] = operation["base_revision"] + 1
                if connection is not None:
                    connection.execute("UPDATE core_chats SET workspace_revision=%s WHERE tenant_id=%s AND context_id=%s",
                        (operation["workspace_revision"], tenant, context))
            self._save(operation, connection)
            if connection is None:
                # One locked in-memory publication; a failed save cannot advance the revision.
                chat["workspace_revision"] = operation["workspace_revision"]
            return self._receipt(operation)

    def recover(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise CoreError("REQUEST_INVALID")
        if self.database:
            with self.database.transaction() as connection:
                def page(after):
                    return connection.execute("""SELECT tenant_id,context_id,request_id,operation_id
                        FROM core_workspace_cleanups WHERE state<>'completed' AND operation_id>%s
                        ORDER BY operation_id LIMIT %s""", (after, limit)).fetchall()
                rows = page(self._recovery_after)
                if not rows and self._recovery_after:
                    rows = page("")
        else:
            with self.admission.agent.workflow_store._lock:
                pending = sorted((value for value in self.admission._workspace_cleanups.values() if value["state"] != "completed"),
                                 key=lambda row: row["operation_id"])
                rows = [row for row in pending if row["operation_id"] > self._recovery_after][:limit] or pending[:limit]
                rows = [{key: row[key] for key in ("tenant_id", "context_id", "request_id", "operation_id")} for row in rows]
        # Advance even when a selected operation needs human repair. This process-local
        # scheduling hint changes neither immutable intent nor the admission barrier.
        self._recovery_after = rows[-1]["operation_id"] if rows else ""
        for row in rows:
            try:
                self._execute(row["tenant_id"], row["context_id"], row["request_id"])
            except CoreError:
                # Unknown/corrupt protocols retain their admission barrier for repair.
                continue
        return len(rows)
