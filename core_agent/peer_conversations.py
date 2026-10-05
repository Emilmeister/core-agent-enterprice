"""Bounded owner-only public observations of the existing remote task lifecycle."""

import copy
import hashlib
import asyncio
import io
import json
import math
import uuid
from contextlib import nullcontext

from starlette.responses import JSONResponse
from starlette.routing import Route

from .errors import CoreError
from .security import redact

MAX_MESSAGES = 200
MAX_TEXT_BYTES = 256 * 1024
OBSERVATION_TTL = 45
SAFE_REMOTE_ERRORS = frozenset({
    "REMOTE_OPERATION_TIMEOUT", "SIDE_EFFECT_UNKNOWN", "REMOTE_TASK_FAILED", "REMOTE_TASK_REJECTED",
    "REMOTE_AGENT_UNAVAILABLE", "REMOTE_AGENT_DENIED", "REMOTE_AGENT_NOT_FOUND", "REMOTE_AGENT_PROTOCOL_ERROR",
    "REMOTE_AGENT_RESPONSE_TOO_LARGE", "REMOTE_FILES_UNSUPPORTED", "REMOTE_PARTS_UNSUPPORTED",
    "ATTACHMENTS_TOO_LARGE", "ARTIFACT_INTEGRITY_FAILED", "FILE_ADMISSION_TRANSACTION_REQUIRED",
    "FILE_BATCH_NOT_FOUND", "FILE_BATCH_CONFLICT", "FILE_PUBLICATION_CONFLICT", "FILE_WRITE_FAILED",
    "INVALID_FILE_INPUT", "CONFIG_INVALID", "CHECKPOINT_INVALID", "SESSION_CONFLICT",
})


def initial_conversation():
    return {"version": 1, "messages": [], "last_checked_at": None,
            "updated_at": None, "history_truncated": False}


def public_identity(source, text):
    return hashlib.sha256((source + "\0" + text).encode("utf-8")).hexdigest()


def validate_request_provenance(value):
    if (not isinstance(value, dict) or value.keys() != {"version", "sources"}
            or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["sources"], dict) or len(value["sources"]) > 4096):
        raise CoreError("CHECKPOINT_INVALID")
    for key, source in value["sources"].items():
        if (not isinstance(key, str) or not key or len(key) > 4096
                or not isinstance(source, dict) or not {"run_id"} <= source.keys() <= {
                    "run_id", "sequence", "materials", "kind", "result_digest"}
                or not isinstance(source["run_id"], str) or not source["run_id"]
                or source.get("kind") not in {None, "terminal_result"}
                or (source.get("kind") == "terminal_result" and (not isinstance(source.get("result_digest"), str)
                    or len(source["result_digest"]) != 64 or any(char not in "0123456789abcdef" for char in source["result_digest"])))
                or ("sequence" in source and (type(source["sequence"]) is not int or not 0 <= source["sequence"] < 2**63))
                or not isinstance(source.get("materials", []), list)):
            raise CoreError("CHECKPOINT_INVALID")
        for material in source.get("materials", ()):
            if (not isinstance(material, dict) or not material or not material.keys() <= {
                    "material_kind", "material_digest", "text_digest", "review_id"}
                    or material.get("material_kind", "json") not in {"json", "file_sha256"}
                    or not {"material_digest", "text_digest", "review_id"} & material.keys()):
                raise CoreError("CHECKPOINT_INVALID")
            for name in ("material_digest", "text_digest"):
                if name in material and (not isinstance(material[name], str) or len(material[name]) != 64
                        or any(char not in "0123456789abcdef" for char in material[name])):
                    raise CoreError("CHECKPOINT_INVALID")
            if "review_id" in material and (not isinstance(material["review_id"], str) or not material["review_id"]):
                raise CoreError("CHECKPOINT_INVALID")
    try:
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > MAX_TEXT_BYTES:
            raise CoreError("CHECKPOINT_INVALID")
    except (UnicodeError, ValueError, TypeError):
        raise CoreError("CHECKPOINT_INVALID") from None


def freeze_request_provenance(snapshot):
    sources = {}
    for item in snapshot.get("context", {}).get("active", ()):
        sources.update(copy.deepcopy((item.get("provenance") or {}).get("sources", {})))
    value = {"version": 1, "sources": sources}
    validate_request_provenance(value)
    return value


def merge_observation(saved, observation, now):
    saved = copy.deepcopy(saved or initial_conversation())
    if observation is None:
        return saved
    if (not isinstance(observation, dict) or observation.keys() != {"messages", "history_truncated"}
            or not isinstance(observation["messages"], list)
            or type(observation["history_truncated"]) is not bool):
        raise CoreError("CHECKPOINT_INVALID")
    messages = saved["messages"]
    identities = {message["id"] for message in messages}
    used = sum(len(message["text"].encode("utf-8")) for message in messages)
    changed = False
    for message in observation["messages"]:
        if (not isinstance(message, dict) or message.keys() != {"id", "text"}
                or not isinstance(message["id"], str) or len(message["id"]) != 64
                or any(char not in "0123456789abcdef" for char in message["id"])
                or not isinstance(message["text"], str)):
            raise CoreError("CHECKPOINT_INVALID")
        if message["id"] in identities or not message["text"]:
            continue
        try:
            size = len(message["text"].encode("utf-8"))
        except UnicodeError:
            raise CoreError("CHECKPOINT_INVALID") from None
        if len(messages) >= MAX_MESSAGES or used + size > MAX_TEXT_BYTES:
            saved["history_truncated"] = True
            continue
        messages.append({**message, "direction": "incoming", "created_at": now})
        identities.add(message["id"])
        used += size
        changed = True
    saved["last_checked_at"] = now
    if changed:
        saved["updated_at"] = now
    saved["history_truncated"] |= observation["history_truncated"]
    return saved


def _authorized_root(admission, binding, row, connection):
    from .chat_files import _caller_records
    from .history import _root
    from .tasks import _remote_contract

    contract = row["contract"]
    try:
        _remote_contract(contract, binding.tenant_id, row["owner_run_id"])
        source_task = contract.get("caller_scope", {}).get("task_id")
        records = _caller_records(admission.agent.workflow_store, binding, row["owner_run_id"],
                                   source_task, connection=connection)
        if contract["version"] == 2 and contract["caller_scope"] != {
                key: getattr(records[-1], key) for key in ("owner_id", "context_id", "task_id", "run_id")}:
            raise CoreError("TASK_NOT_FOUND")
        _root(admission, records[0].run_id, binding.tenant_id,
              {"owner_id": binding.owner_id, "context_id": binding.context_id}, connection)
        return records[0], records[-1]
    except CoreError as error:
        if error.code in {"FILE_BATCH_NOT_FOUND", "CHECKPOINT_INVALID"}:
            raise CoreError("TASK_NOT_FOUND") from None
        raise


def _rows(agent, admission, binding, limit, after, operation_id, connection):
    if connection is not None:
        values = [binding.tenant_id, binding.owner_id, binding.context_id]
        predicate = ""
        if after is not None:
            predicate += " AND (b.created_at,b.id)<(%s,%s)"
            values.extend(after)
        if operation_id is not None:
            predicate += " AND b.id=%s"
            values.append(operation_id)
        values.append(limit)
        return connection.execute("""SELECT b.* FROM core_background_tasks b JOIN core_runs r
            ON r.run_id=b.owner_run_id AND r.tenant_id=b.tenant_id
            WHERE b.tenant_id=%s AND r.owner_id=%s AND r.context_id=%s
              AND b.kind='remote_a2a'""" + predicate +
            " ORDER BY b.created_at DESC,b.id DESC LIMIT %s", values).fetchall()
    scheduler = agent.task_scheduler
    rows = []
    for identifier, row in scheduler._remote.items():
        if row["contract"]["tenant_id"] != binding.tenant_id or operation_id is not None and identifier != operation_id:
            continue
        source = agent.workflow_store._records.get(row["contract"]["owner_id"])
        if source is None or (source.tenant_id, source.owner_id, source.context_id) != (
                binding.tenant_id, binding.owner_id, binding.context_id):
            continue
        task = scheduler._tasks[identifier]
        value = {**copy.deepcopy(row), "id": identifier, "owner_run_id": task.owner_id,
                 "state": task.state, "result": copy.deepcopy(task.result), "revision": task.revision,
                 "error_code": getattr(task.error, "code", None),
                 "remote_conversation": copy.deepcopy(row.get("conversation")),
                 "remote_observed_until": row.get("observed_until")}
        if after is None or (value["created_at"], identifier) < after:
            rows.append(value)
    return sorted(rows, key=lambda row: (row["created_at"], row["id"]), reverse=True)[:limit]


def _file_identity(entry, content=None):
    from .history import _json_identity
    identity = {"material_kind": "file_sha256", "material_digest": entry["sha256"]}
    if content is not None:
        try:
            identity["text_digest"] = _json_identity(content.decode("utf-8"))["material_digest"]
        except UnicodeError:
            pass
    return identity


def _files(agent, binding, row, source, connection, materials=None):
    service = agent.chat_file_service
    batch_id = (row["result"] or {}).get("file_batch_id")
    if service is None or not batch_id:
        return []
    try:
        batch = service.store.get(batch_id, binding.tenant_id, connection=connection)
        if batch["state"] != "published":
            return []
        with service._accepted_files(batch_id, binding, run_id=source.run_id,
                                     task_id=source.task_id, connection=connection) as (batch, directory):
            if materials is not None:
                materials.extend(_file_identity(entry, service._read_entry(directory, entry))
                                 for entry in batch["manifest"]["entries"])
            return [{key: entry[key] for key in (
                "index", "actual_name", "relative_path", "size_bytes", "sha256")}
                for entry in batch["manifest"]["entries"]]
    except CoreError as error:
        if error.code in {"FILE_BATCH_NOT_FOUND", "FILE_NOT_FOUND", "TASK_NOT_FOUND"}:
            return []
        raise


def _scope_record(record):
    return {key: getattr(record, key) for key in ("run_id", "task_id", "tenant_id", "owner_id", "context_id", "state", "result")} | {
        "previous": record.snapshot.get("previous_root_run_id")}


def _visibility(agent, admission, binding, row, source, connection, outgoing_files, loaded, incoming_materials):
    from .chat_files import _caller_records
    from .history import _final_dependency_reviews, _json_identity, _reviews, _review_status, _source_materials
    from .tasks import remote_result_projection

    if agent.material_review_store is None:
        return None, None
    records = _caller_records(agent.workflow_store, binding, source.run_id, source.task_id, connection=connection)
    scope, chat = _scope_record(source), {"owner_id": binding.owner_id, "context_id": binding.context_id}
    request_reviews = []
    for record in records:
        request_reviews.extend(_reviews(admission, _scope_record(record), ["initial"], [],
            [_json_identity(record.request.get("prompt"))],
            [record.snapshot["file_batch_id"]] if record.snapshot.get("file_batch_id") else [], connection))
    sender, provenance = None, None
    for key, entry in source.snapshot.get("remote_calls", {}).items():
        pinned = agent._remote_entry_contract(entry)
        if {k: v for k, v in row["contract"].items() if k != "_trace_parent"} != pinned:
            continue
        attempt, call_id = key.split(":", 1)
        if str(uuid.uuid5(uuid.NAMESPACE_URL, f"remote:{source.run_id}:{attempt}:{call_id}")) == row["id"]:
            sender, provenance = call_id, entry.get("request_provenance")
            break
    latest = source.snapshot.get("remote_admission", {})
    if sender is None and latest.get("task_id") == row["id"]:
        sender = latest.get("source_id")
    materials = [_json_identity(row["contract"]["task"])]
    if loaded is not None:
        materials.extend(_file_identity(entry, content) for entry, content in loaded)
    else:
        materials.extend(_file_identity(entry) for entry in outgoing_files)
    if provenance is not None:
        validate_request_provenance(provenance)
        lineage = {record.run_id: record for record in records}
        for key, value in provenance["sources"].items():
            dependency_scope = _scope_record(lineage.get(value["run_id"], records[0]))
            materials.extend(_source_materials(admission, dependency_scope,
                {"version": 1, "sources": {key: value}}, connection, chat))
    incoming_sources = ["result:" + sender] if sender else []
    # Legacy jobs use only a surviving causal marker; later unrelated reviews grant no new dependency.
    cutoff = None
    for item in source.snapshot.get("context", {}).get("transcript", ()):
        if item.get("kind") not in {"assistant_tool_calls", "tool_result"}:
            continue
        value = json.loads(item["content"])
        if item["kind"] == "assistant_tool_calls":
            calls = value if isinstance(value, list) else value.get("tool_calls", ())
            if sender and any(call.get("id") == sender for call in calls):
                cutoff = sender
            continue
        outputs = value.get("output")
        outputs = outputs if isinstance(outputs, list) else [outputs]
        if any(isinstance(output, dict) and output.get("task_id") == row["id"] for output in outputs):
            incoming_sources.extend(["result:" + value["tool_call_id"], "arguments:" + value["tool_call_id"]])
            if value.get("tool_name") == "core_agent_send_message":
                sender = sender or value["tool_call_id"]
                cutoff = cutoff or sender
    if provenance is None and cutoff:
        request_reviews.extend(_final_dependency_reviews(admission, scope, connection, chat, before_call_id=cutoff))
    request_reviews.extend(_reviews(admission, scope, ["arguments:" + sender] if sender else [],
        [m["review_id"] for m in materials if m.get("review_id")], materials, [], connection))
    result = row["result"] or {}
    incoming_materials = list(incoming_materials)
    incoming_materials.extend(_json_identity(message["text"]) for message in
                              (row.get("remote_conversation") or initial_conversation())["messages"])
    incoming_materials.append(_json_identity(remote_result_projection(result)))
    if isinstance(result.get("text"), str):
        incoming_materials.append(_json_identity(result["text"]))
    incoming = _review_status(_reviews(admission, scope, incoming_sources, [], incoming_materials,
        [result["file_batch_id"]] if result.get("file_batch_id") else [], connection))
    return _review_status(request_reviews), incoming


def _outgoing_files(agent, binding, row, source, connection):
    from .response_files import ResponseFileService

    contract = row["contract"]
    if not row["checkpoint"]["send_started"] or contract["version"] != 2 or not contract["outgoing_files"]:
        return [], ()
    refs = ResponseFileService.validate_refs(binding, contract["outgoing_files"],
        task_id=source.task_id, run_id=source.run_id, limit_bytes=contract["attachment_limit_bytes"])
    receipts = list(ResponseFileService.receipts(refs))
    if agent.response_files_service is None:
        return receipts, None
    try:
        loaded = agent.response_files_service.load(binding, refs, task_id=source.task_id, run_id=source.run_id,
            limit_bytes=contract["attachment_limit_bytes"], connection=connection)
        return receipts, loaded
    except CoreError as error:
        if error.code == "ARTIFACT_INTEGRITY_FAILED":
            return receipts, None
        raise


def read_outgoing_file(agent, admission, binding, operation_id, file_id):
    database = getattr(admission, "database", None)
    with database.transaction() if database is not None else agent.workflow_store._lock as connection:
        connection = connection if database is not None else None
        with nullcontext() if database is not None else agent.task_scheduler._lock:
            if connection is not None:
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            rows = _rows(agent, admission, binding, 1, None, operation_id, connection)
            if not rows:
                raise CoreError("FILE_NOT_FOUND")
            row = rows[0]
            _root, source = _authorized_root(admission, binding, row, connection)
            receipts, loaded = _outgoing_files(agent, binding, row, source, connection)
            request_status, _incoming = _visibility(agent, admission, binding, row, source, connection, receipts, loaded, [])
            if request_status:
                raise CoreError("FILE_NOT_FOUND")
            if not any(entry["file_id"] == file_id for entry in receipts):
                raise CoreError("FILE_NOT_FOUND")
            if loaded is None:
                raise CoreError("ARTIFACT_INTEGRITY_FAILED")
            return next((entry, content) for entry, content in loaded if entry["file_id"] == file_id)


def read_conversations(agent, admission, binding, *, limit=50, after=None, operation_id=None):
    database = getattr(admission, "database", None)
    locks = database.transaction() if database is not None else agent.workflow_store._lock
    with locks as connection:
        connection = connection if database is not None else None
        with nullcontext() if database is not None else agent.task_scheduler._lock:
            if connection is not None:
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            values = []
            scanned, scan_after, exhausted = 0, after, False
            while len(values) < limit and scanned < 300:
                batch_size = min(limit - len(values), 300 - scanned)
                rows = _rows(agent, admission, binding, batch_size, scan_after, operation_id, connection)
                if not rows:
                    exhausted = True
                    break
                scanned += len(rows)
                scan_after = rows[-1]["created_at"], rows[-1]["id"]
                for row in rows:
                    try:
                        root, source = _authorized_root(admission, binding, row, connection)
                    except CoreError as error:
                        if error.code == "TASK_NOT_FOUND" and operation_id is None:
                            # Corrupt/foreign lineage fails closed without hiding other authorized rows.
                            continue
                        raise
                    conversation = row.get("remote_conversation") or initial_conversation()
                    messages = conversation["messages"]
                    checkpoint, contract = row["checkpoint"], row["contract"]
                    incoming_materials = []
                    files = _files(agent, binding, row, source, connection, incoming_materials)
                    outgoing_files, loaded = _outgoing_files(agent, binding, row, source, connection)
                    request_status, incoming_status = _visibility(agent, admission, binding, row, source,
                        connection, outgoing_files, loaded, incoming_materials)
                    if request_status:
                        outgoing_files, loaded = [], ()
                    if request_status or incoming_status:
                        messages, files = [], []
                    statuses = [status for status in (request_status, incoming_status) if status]
                    status = next((status for status in statuses if status[0] in {"rejected", "timed_out"}),
                                  statuses[0] if statuses else None)
                    delivery = "not_sent"
                    if checkpoint["send_started"]:
                        delivery = "confirmed" if (checkpoint["remote_task_id"] or
                            row["state"] == "completed" and not row.get("error_code") and
                            (row["result"] or {}).get("remote_outcome") != "unknown") else "unconfirmed"
                    value = {"operation_id": row["id"], "peer_id": contract["peer_id"], "peer_name": contract["peer_name"],
                        "root_task_id": root.task_id, "state": row["state"],
                        "request_delivery": delivery,
                        "material_status": (status[0] if status[0] in {"rejected", "timed_out"} else "pending_guardrail") if status else "available",
                        "error_code": row.get("error_code") if row.get("error_code") in SAFE_REMOTE_ERRORS else None,
                        "outcome_unknown": (row["result"] or {}).get("remote_outcome") == "unknown",
                        "remote_state": (row["result"] or {}).get("remote_state"),
                        "created_at": row["created_at"], "updated_at": row["updated_at"],
                        "revision": row["revision"], "message_count": len(messages) + bool(checkpoint["send_started"] and not request_status),
                        "last_message": messages[-1]["text"][:240] if messages else "",
                        "files_available": bool(files),
                        "last_checked_at": conversation["last_checked_at"],
                        "next_check_at": checkpoint["next_poll_at"] if row["state"] not in {"completed", "failed", "canceled"} else None}
                    if operation_id:
                        outgoing = []
                        request = redact(contract["task"])
                        encoded = request.encode("utf-8")
                        truncated_request = len(encoded) > MAX_TEXT_BYTES
                        if truncated_request:
                            request = encoded[:MAX_TEXT_BYTES].decode("utf-8", "ignore")
                        if checkpoint["send_started"] and not request_status:
                            outgoing = [{"id": public_identity("outgoing:" + contract["message_id"], request),
                                         "direction": "outgoing", "text": request, "created_at": row["created_at"]}]
                        detail_messages = outgoing + copy.deepcopy(messages)
                        used, kept, truncated = 0, [], conversation["history_truncated"] or truncated_request
                        for message in detail_messages:
                            size = len(message["text"].encode("utf-8"))
                            if len(kept) >= MAX_MESSAGES or used + size > MAX_TEXT_BYTES:
                                truncated = True
                                continue
                            used += size
                            kept.append(message)
                        value.update(messages=kept, files=files, history_truncated=truncated,
                                     outgoing_files=outgoing_files,
                                     outgoing_files_status="unavailable" if loaded is None else "available" if loaded else "none",
                                     observation_expires_at=row.get("remote_observed_until"))
                    values.append(value)
                if len(rows) < batch_size:
                    exhausted = True
                    break
            if operation_id and not values:
                raise CoreError("TASK_NOT_FOUND")
            continuation = scan_after if not exhausted and len(values) < limit else None
            return values, continuation


def peer_conversation_routes(agent, admission):
    from .owner_api import WorkspaceDownload, page_query, page_cursor, read_payload, require_owner

    async def endpoint(request):
        try:
            actor = require_owner(request.scope.get("principal"))
            context_id = request.path_params["context_id"]
            namespace = "peer-conversations:" + hashlib.sha256(context_id.encode("utf-8")).hexdigest()
            operation_id = request.path_params.get("operation_id")
            binding, _ = await admission.workspace_scope(actor.tenant, context_id)
            if "file_id" in request.path_params:
                if request.query_params:
                    raise CoreError("REQUEST_INVALID")
                entry, content = await asyncio.to_thread(read_outgoing_file, agent, admission, binding,
                    operation_id, request.path_params["file_id"])
                return WorkspaceDownload(io.BytesIO(content), len(content), entry["name"])
            if operation_id is None:
                limit, cursor = page_query(actor, request.query_params, namespace)
                after = None
                if cursor is not None:
                    try:
                        created, identifier = json.loads(cursor)
                        if (type(created) not in {int, float} or not math.isfinite(created)
                                or not isinstance(identifier, str) or not identifier):
                            raise ValueError()
                        after = created, identifier
                    except (ValueError, TypeError):
                        raise CoreError("REQUEST_INVALID") from None
                rows, continuation = await asyncio.to_thread(read_conversations, agent, admission, binding,
                                                             limit=limit + 1, after=after)
                if len(rows) > limit:
                    continuation = rows[limit-1]["created_at"], rows[limit-1]["operation_id"]
                cursor = page_cursor(actor, namespace, json.dumps(continuation)) if continuation else None
                result = {"conversations": rows[:limit], "next_cursor": cursor}
            else:
                if request.query_params:
                    raise CoreError("REQUEST_INVALID")
                values, _ = await asyncio.to_thread(read_conversations, agent, admission, binding,
                                                    operation_id=operation_id)
                result = values[0]
                if request.method == "POST":
                    payload = await read_payload(request, {"visible"})
                    if type(payload["visible"]) is not bool:
                        raise CoreError("REQUEST_INVALID")
                    owner = await asyncio.to_thread(_operation_owner, agent, admission, binding, operation_id)
                    expires = await asyncio.to_thread(agent.task_scheduler.observe_remote,
                        operation_id, tenant_id=actor.tenant, owner_id=owner, visible=payload["visible"])
                    result = {"expires_at": expires}
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except CoreError as error:
            status = {"ACCESS_DENIED": 403, "TASK_NOT_FOUND": 404, "FILE_NOT_FOUND": 404,
                      "ARTIFACT_INTEGRITY_FAILED": 409, "REQUEST_TOO_LARGE": 413}.get(error.code, 400)
            return JSONResponse({"error": {"code": error.code}}, status_code=status,
                                headers={"Cache-Control": "no-store"})

    prefix = "/api/chats/{context_id:path}/peer-conversations"
    return [Route(prefix, endpoint), Route(prefix + "/{operation_id}/observation", endpoint, methods=["POST"]),
            Route(prefix + "/{operation_id}/outgoing-files/{file_id}", endpoint),
            Route(prefix + "/{operation_id}", endpoint)]


def _operation_owner(agent, admission, binding, operation_id):
    database = getattr(admission, "database", None)
    with database.transaction() if database is not None else agent.task_scheduler._lock as connection:
        rows = _rows(agent, admission, binding, 1, None, operation_id, connection if database is not None else None)
        if not rows:
            raise CoreError("TASK_NOT_FOUND")
        return rows[0]["owner_run_id"]
