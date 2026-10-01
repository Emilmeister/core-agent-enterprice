"""Owner-only control plane; all scope and tool identities come from the server."""

import asyncio
import base64
import binascii
import hashlib
import json
import math
import re
from dataclasses import asdict
from decimal import Decimal
from urllib.parse import quote

from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .auth import AuthContextBuilder, Principal
from .config import compile_effective_config
from .errors import CoreError
from .interactions import SETTINGS_KEYS, TIMEOUT_KEYS, interaction_digest, tool_origin
from .mcp import mcp_tool_index
from .skills import SKILL_TOOLS
from .workspace import ChatWorkspaces, WorkspaceBinding


def require_owner(actor):
    if not isinstance(actor, Principal) or not actor.is_owner or actor.is_external:
        raise CoreError("ACCESS_DENIED")
    return actor


def policy_catalog(agent):
    # MCP allowlists are exact remote names. Configuring policy does not assert
    # discovery success or add an absent tool to a model's catalog.
    declarations = agent.platform_mcp
    allowed = agent.agent_config.tools["mcp"].get("allow_tools", {})
    configured = {server: dict.fromkeys(names, {}) for server, names in allowed.items()}
    effective = compile_effective_config(
        agent.platform_config, agent.agent_config, declarations, configured,
    )
    index = mcp_tool_index(effective.mcp_tools)
    names = set(effective.model_tool_catalog)
    if effective.skills:
        names.update(SKILL_TOOLS)
    return {name: tool_origin(name, index.get(name)) for name in sorted(names)}


def public_settings(value):
    return {key: item for key, item in asdict(value).items() if key != "tenant_id"}


async def read_payload(request, fields, *, optional_fields=frozenset()):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 512 * 1024:
            raise CoreError("REQUEST_TOO_LARGE")
    def unique_object(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate JSON member")
        return result

    try:
        payload = json.loads(body, object_pairs_hook=unique_object)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise CoreError("REQUEST_INVALID") from None
    if not isinstance(payload, dict) or not fields <= payload.keys() <= fields | optional_fields:
        raise CoreError("REQUEST_INVALID")
    return payload


def public_interaction(wait):
    value = {key: getattr(wait, key) for key in (
        "wait_id", "context_id", "generation", "kind", "source_id", "subject",
        "deadline", "outcome", "created_at", "resolved_at", "applied_at",
    )}
    return {**value, "subject_digest": interaction_digest(wait)}


def page_query(actor, query, namespace):
    if set(query) - {"limit", "cursor"} or len(query.multi_items()) != len(query):
        raise CoreError("REQUEST_INVALID")
    size = query.get("limit", "50")
    if not size.isascii() or not size.isdigit() or len(size) > 3 or not 1 <= int(size) <= 100:
        raise CoreError("REQUEST_INVALID")
    limit, after = int(size), None
    if "cursor" in query:
        try:
            encoded = query["cursor"]
            if not 0 < len(encoded) <= 4096:
                raise ValueError()
            value = json.loads(base64.b64decode(encoded, altchars=b"-_", validate=True))
            if (not isinstance(value, list) or len(value) != 3 or value[:2] != [namespace, actor.tenant]
                    or not isinstance(value[2], str) or not value[2] or "\0" in value[2]):
                raise ValueError()
            value[2].encode("utf-8")
            after = value[2]
        except (ValueError, UnicodeError, binascii.Error, RecursionError):
            raise CoreError("REQUEST_INVALID") from None
    return limit, after


def page_cursor(actor, namespace, identifier):
    return base64.urlsafe_b64encode(json.dumps(
        [namespace, actor.tenant, identifier], separators=(",", ":"),
    ).encode()).decode()


async def list_chats(admission, actor, query):
    require_owner(actor)
    limit, after = page_query(actor, query, "chats")
    rows = await admission.list_chats(actor.tenant, limit=limit + 1, after=after)
    cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        identifier = json.dumps([2, last["context_id"]], ensure_ascii=False, separators=(",", ":"))
        cursor = page_cursor(actor, "chats", identifier)
        if len(cursor) > 4096:
            # Existing long contexts retain their already-supported task cursor.
            cursor = page_cursor(actor, "chats", last["latest_task_id"])
    return {"chats": rows[:limit], "next_cursor": cursor}


async def chat_history(admission, actor, context_id, query):
    require_owner(actor)
    try:
        if not isinstance(context_id, str) or not context_id or "\0" in context_id:
            raise ValueError()
        namespace = "history:" + hashlib.sha256(context_id.encode("utf-8")).hexdigest()
    except (ValueError, UnicodeError):
        raise CoreError("REQUEST_INVALID") from None
    limit, after = page_query(actor, query, namespace)
    rows = await admission.history(actor.tenant, context_id, limit=limit + 1, after=after)
    cursor = page_cursor(actor, namespace, rows[limit - 1][1]) if len(rows) > limit else None
    return {"items": [item for item, _position in rows[:limit]], "next_cursor": cursor}


class WorkspaceDownload(StreamingResponse):
    """Starlette's background callback alone does not cover send/disconnect errors."""
    def __init__(self, stream, size, name):
        self.stream = stream
        def chunks():
            remaining = size
            try:
                while remaining:
                    value = stream.read(min(64 * 1024, remaining))
                    if not value:
                        break
                    remaining -= len(value)
                    yield value
            finally:
                stream.close()
        super().__init__(chunks(), media_type="application/octet-stream", background=BackgroundTask(stream.close),
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                     "Content-Security-Policy": "sandbox; default-src 'none'",
                     "Content-Disposition": "attachment; filename*=UTF-8''" + quote(name, safe="")})

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.stream.close()


async def workspace_files(agent, admission, actor, request):
    require_owner(actor)
    context_id, query = request.path_params["context_id"], request.query_params
    try:
        if not context_id or "\0" in context_id:
            raise ValueError()
        context_id.encode("utf-8")
    except (ValueError, UnicodeError):
        raise CoreError("REQUEST_INVALID") from None
    content = request.url.path.endswith("/files/content")
    allowed = {"path"} if content else {"limit", "cursor", "directory", "older_than_days"}
    if set(query) - allowed or len(query.multi_items()) != len(query):
        raise CoreError("REQUEST_INVALID")
    if content:
        if set(query) != {"path"}:
            raise CoreError("REQUEST_INVALID")
        parts = ChatWorkspaces.path_parts(query["path"])
    else:
        size = query.get("limit", "50")
        if not re.fullmatch(r"[0-9]{1,3}", size) or not 1 <= int(size) <= 100:
            raise CoreError("REQUEST_INVALID")
        age = None
        if "older_than_days" in query:
            value = query["older_than_days"]
            if len(value) > 64 or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
                raise CoreError("REQUEST_INVALID")
            age = Decimal(value)
            if age > 100000:
                raise CoreError("REQUEST_INVALID")
        ChatWorkspaces.path_parts(query.get("directory", ""), directory=True)
    binding, active = await admission.workspace_scope(actor.tenant, context_id)
    manager = agent.tool_runtime.environment_manager.backend.chats
    if manager is None:
        raise CoreError("WORKSPACE_UNAVAILABLE")
    if content:
        opened = asyncio.create_task(asyncio.to_thread(manager.open_file, binding, query["path"]))
        try:
            stream, length = await asyncio.shield(opened)
        except asyncio.CancelledError:
            # The filesystem thread continues after client cancellation; close its eventual result.
            def close_late(completed):
                if not completed.cancelled() and completed.exception() is None:
                    completed.result()[0].close()
            opened.add_done_callback(close_late)
            raise
        try:
            return WorkspaceDownload(stream, length, parts[-1])
        except BaseException:
            stream.close()
            raise
    state = await asyncio.to_thread(agent.workspace_cleanup.preview_state, binding)
    result = await asyncio.to_thread(manager.preview, binding, limit=int(size), directory=query.get("directory", ""),
        older_than_days=age, cursor=query.get("cursor"), workspace_revision=state["workspace_revision"])
    block = "CONTEXT_BUSY" if active else "WORKSPACE_CLEANUP_PENDING" if state["cleanup_pending"] else None
    return {**result, "active": active, "cleanup_block_reason": block, "workspace_revision": state["workspace_revision"]}


async def workspace_cleanup(agent, actor, request):
    require_owner(actor)
    context = request.path_params["context_id"]
    try:
        if not context or "\0" in context:
            raise ValueError()
        context.encode("utf-8")
    except (ValueError, UnicodeError):
        raise CoreError("REQUEST_INVALID") from None
    query = request.query_params
    if request.method == "POST":
        if query:
            raise CoreError("REQUEST_INVALID")
        payload = await read_payload(request, {"request_id", "files"})
        return await agent.workspace_cleanup.delete(actor.tenant, context, actor.actor_id, payload)
    if set(query) - {"request_id"} or len(query.multi_items()) != len(query):
        raise CoreError("REQUEST_INVALID")
    return await agent.workspace_cleanup.get(actor.tenant, context, query.get("request_id"))


async def registry_request(registry, actor, request):
    require_owner(actor)
    if request.method == "GET":
        limit, after = page_query(actor, request.query_params, "remote-agents")
        rows = await asyncio.to_thread(registry.list, actor.tenant, limit=limit + 1, after_id=after)
        cursor = page_cursor(actor, "remote-agents", rows[limit - 1]["id"]) if len(rows) > limit else None
        return {"agents": rows[:limit], "next_cursor": cursor}
    if request.query_params:
        raise CoreError("REQUEST_INVALID")
    fields = {"url", "description", "enabled", "header_name"}
    if request.method == "POST":
        payload = await read_payload(request, fields | {"name"}, optional_fields={"header_value"})
        return await asyncio.to_thread(registry.create, actor.tenant, payload, actor_id=actor.actor_id)
    if request.method == "DELETE":
        payload = await read_payload(request, {"expected_revision"})
        return await asyncio.to_thread(registry.disable, actor.tenant, request.path_params["peer_id"],
                                       expected_revision=payload["expected_revision"], actor_id=actor.actor_id)
    payload = await read_payload(request, fields | {"expected_revision"}, optional_fields={"header_value"})
    revision = payload.pop("expected_revision")
    return await asyncio.to_thread(registry.update, actor.tenant, request.path_params["peer_id"], payload,
                                   expected_revision=revision, actor_id=actor.actor_id)


def list_interactions(workflow, actor, query):
    require_owner(actor)
    if set(query) - {"task_id", "status", "limit", "cursor"} or len(query.multi_items()) != len(query):
        raise CoreError("REQUEST_INVALID")
    task_id, status = query.get("task_id"), query.get("status", "pending")
    limit_value = query.get("limit", "50")
    if not task_id or status not in {"pending", "all"} or not limit_value.isascii() or not limit_value.isdigit():
        raise CoreError("REQUEST_INVALID")
    if len(limit_value) > 3 or not 1 <= (limit := int(limit_value)) <= 100:
        raise CoreError("REQUEST_INVALID")
    after = None
    if "cursor" in query:
        try:
            cursor = query["cursor"]
            if not 0 < len(cursor) <= 1024:
                raise ValueError()
            cursor = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if (not isinstance(cursor, list) or len(cursor) != 4 or cursor[:2] != [task_id, status]
                    or type(cursor[2]) not in (float, int) or not math.isfinite(cursor[2])
                    or not isinstance(cursor[3], str) or not cursor[3]):
                raise ValueError()
            after = (cursor[2], cursor[3])
        except (ValueError, UnicodeDecodeError, binascii.Error, OverflowError):
            raise CoreError("REQUEST_INVALID") from None
    waits = workflow.list_interactions(task_id, tenant_id=actor.tenant, status=status, limit=limit + 1, after=after)
    cursor = None
    if len(waits) > limit:
        last = waits[limit - 1]
        cursor = base64.urlsafe_b64encode(json.dumps(
            [task_id, status, last.created_at, last.wait_id], separators=(",", ":"),
        ).encode()).decode()
    return {"interactions": [public_interaction(wait) for wait in waits[:limit]], "next_cursor": cursor}


def decide_interaction(workflow, actor, wait_id, kind, payload):
    require_owner(actor)
    wait = workflow.get_wait(wait_id, tenant_id=actor.tenant)
    if wait.kind != kind:
        raise CoreError("REQUEST_INVALID")
    if not isinstance(payload["subject_digest"], str) or not payload["subject_digest"]:
        raise CoreError("REQUEST_INVALID")
    if payload["subject_digest"] != interaction_digest(wait):
        raise CoreError("INTERACTION_VERSION_CONFLICT")
    if kind == "owner_question":
        answer = payload["answer"]
        try:
            valid = isinstance(answer, str) and bool(answer.strip()) and len(answer.encode("utf-8")) <= 65536
        except UnicodeEncodeError:
            valid = False
        if not valid:
            raise CoreError("REQUEST_INVALID")
        outcome = {"reason": "answer", "answer": answer}
    else:
        decision = payload["decision"]
        if decision not in ("allow", "reject"):
            raise CoreError("REQUEST_INVALID")
        outcome = {"reason": "allowed" if decision == "allow" else "rejected"}
    # Resolve commits timeout/cancel before a late-decision conflict is returned.
    # The original wait's immutable subject makes this safe across competing owners.
    resolved = workflow.resolve_wait(wait_id, tenant_id=actor.tenant, outcome=outcome, actor_id=actor.actor_id)
    if any(resolved.outcome.get(key) != value for key, value in outcome.items()):
        raise CoreError("INTERACTION_CLOSED", data={"outcome": resolved.outcome})
    return public_interaction(resolved)


def read_guardrail_material(agent, actor, wait_id, *, include_file_metadata=False):
    require_owner(actor)
    workflow = agent.workflow_store
    wait = workflow.get_wait(wait_id, tenant_id=actor.tenant)
    reviews = getattr(agent, "material_review_store", None)
    review_id = wait.subject.get("review_id") if isinstance(wait.subject, dict) else None
    if wait.kind != "guardrail" or reviews is None or not isinstance(review_id, str):
        raise CoreError("MATERIAL_REVIEW_NOT_FOUND")
    record = workflow.get(wait.run_id, tenant_id=actor.tenant, owner_id=wait.owner_id)
    review = reviews.get(record, review_id)
    if review["wait_id"] != wait.wait_id:
        raise CoreError("MATERIAL_REVIEW_NOT_FOUND")
    material = reviews.owner_read_payload(record, review_id)
    if include_file_metadata and material["sealed_ref"] is not None:
        service = getattr(agent, "chat_file_service", None)
        if service is None:
            raise CoreError("MATERIAL_REVIEW_NOT_FOUND")
        reference = material["sealed_ref"]
        inspected = service.owner_download(reference["batch_id"],
            WorkspaceBinding(actor.tenant, record.owner_id, record.context_id), reference["index"],
            run_id=record.run_id, task_id=record.task_id)
        material["file"] = {key: inspected[key] for key in ("entry", "manifest")}
    wait = workflow.get_wait(wait_id, tenant_id=actor.tenant, owner_id=record.owner_id)
    return {"interaction": public_interaction(wait), "review": review, "material": material}


def read_guardrail_file(agent, actor, wait_id):
    value = read_guardrail_material(agent, actor, wait_id)
    reference = value["material"]["sealed_ref"]
    service = getattr(agent, "chat_file_service", None)
    if service is None or not isinstance(reference, dict) or set(reference) != {"batch_id", "index"}:
        raise CoreError("MATERIAL_REVIEW_NOT_FOUND")
    review = value["review"]
    record = agent.workflow_store.get(review["run_id"], tenant_id=actor.tenant, owner_id=review["owner_id"])
    binding = WorkspaceBinding(actor.tenant, record.owner_id, record.context_id)
    return service.owner_download(reference["batch_id"], binding, reference["index"],
                                  run_id=record.run_id, task_id=record.task_id)


async def schedule_request(cron_store, actor, request, on_admitted):
    """Only the authenticated owner chooses a schedule; execution scope is canonical."""
    from google.protobuf.json_format import MessageToDict

    schedule_id = request.path_params.get("schedule_id")
    try:
        if request.method == "GET" and schedule_id is None:
            limit, after = page_query(actor, request.query_params, "schedules")
            rows = await asyncio.to_thread(cron_store.list, actor.tenant, limit=limit + 1, after=after)
            cursor = page_cursor(actor, "schedules", rows[limit - 1]["id"]) if len(rows) > limit else None
            return {"schedules": rows[:limit], "next_cursor": cursor}, 200
        if request.query_params:
            raise CoreError("CRON_INVALID")
        if request.method == "GET":
            return {"schedule": await asyncio.to_thread(cron_store.get, actor.tenant, schedule_id)}, 200
        if request.method == "POST" and schedule_id is None:
            payload = await read_payload(request, {"prompt", "expression", "request_id"},
                                         optional_fields={"timezone", "context_id"})
            result = await cron_store.create(AuthContextBuilder().build(request), payload)
            return {"schedule": result}, 201
        if request.method == "POST":
            payload = await read_payload(request, {"expected_revision", "request_id"})
            context = AuthContextBuilder().build(request)
            accepted = await cron_store.run_now(context, schedule_id, payload)
            if on_admitted is not None:
                await asyncio.to_thread(on_admitted, accepted, actor.tenant)
            return {"task": MessageToDict(accepted.task)}, 200
        if request.method == "PUT":
            payload = await read_payload(request, {"prompt", "expression", "timezone", "enabled", "expected_revision"})
            result = await asyncio.to_thread(cron_store.update, actor.tenant, schedule_id, payload,
                                            actor_id=actor.actor_id)
            return {"schedule": result}, 200
        payload = await read_payload(request, {"expected_revision"})
        result = await asyncio.to_thread(cron_store.delete, actor.tenant, schedule_id,
                                        expected_revision=payload["expected_revision"], actor_id=actor.actor_id)
        return {"schedule": result, "deleted": True}, 200
    except CoreError as error:
        if error.code == "REQUEST_INVALID":
            raise CoreError("CRON_INVALID") from None
        raise


def owner_routes(agent, store, *, admission=None, remote_registry=None, cron_store=None, on_cron_admitted=None):
    def settings(actor, payload):
        require_owner(actor)
        if payload is None:
            return public_settings(store.get_settings(actor.tenant))
        return public_settings(store.update_settings(
            actor.tenant, {key: payload[key] for key in SETTINGS_KEYS if key in payload}, payload["expected_revision"],
        ))

    def policies(actor, name, payload):
        require_owner(actor)
        catalog = policy_catalog(agent)
        if payload is None:
            return {"tools": [public_settings(store.get_policy(actor.tenant, name, origin))
                              for name, origin in catalog.items()]}
        if name not in catalog:
            raise CoreError("TOOL_NOT_FOUND")
        if not isinstance(payload["expected_origin"], str):
            raise CoreError("REQUEST_INVALID")
        if payload["expected_origin"] != catalog[name]:
            raise CoreError("TOOL_IDENTITY_CONFLICT")
        return public_settings(store.update_policy(
            actor.tenant, name, catalog[name], actor_id=actor.actor_id,
            **{key: value for key, value in payload.items() if key != "expected_origin"},
        ))

    async def endpoint(request):
        try:
            actor = require_owner(request.scope.get("principal"))
            is_settings = request.url.path == "/api/settings"
            payload = None
            if cron_store is not None and (request.url.path == "/api/schedules" or "schedule_id" in request.path_params):
                result, status = await schedule_request(cron_store, actor, request, on_cron_admitted)
                return JSONResponse(result, status_code=status, headers={"Cache-Control": "no-store"})
            if remote_registry is not None and (request.url.path == "/api/remote-agents"
                                                or "peer_id" in request.path_params):
                result = await registry_request(remote_registry, actor, request)
            elif "context_id" in request.path_params and request.url.path.endswith("/files/delete"):
                result = await workspace_cleanup(agent, actor, request)
                return JSONResponse(result, status_code=200 if result["state"] == "completed" else 202,
                                    headers={"Cache-Control": "no-store"})
            elif request.method == "POST":
                kind = request.path_params["kind"]
                fields = {"subject_digest", "answer" if kind == "owner_question" else "decision"}
                payload = await read_payload(request, fields)
                result = await asyncio.to_thread(
                    decide_interaction, agent.workflow_store, actor, request.path_params["wait_id"], kind, payload,
                )
            elif request.url.path == "/api/chats":
                result = await list_chats(admission, actor, request.query_params)
            elif "context_id" in request.path_params:
                if request.url.path.endswith("/files") or request.url.path.endswith("/files/content"):
                    result = await workspace_files(agent, admission, actor, request)
                    if isinstance(result, Response):
                        return result
                else:
                    result = await chat_history(admission, actor, request.path_params["context_id"], request.query_params)
            elif request.url.path == "/api/interactions":
                result = await asyncio.to_thread(list_interactions, agent.workflow_store, actor, request.query_params)
            elif request.url.path.endswith("/material"):
                if request.query_params:
                    raise CoreError("REQUEST_INVALID")
                result = await asyncio.to_thread(read_guardrail_material, agent, actor, request.path_params["wait_id"],
                                                 include_file_metadata=True)
            elif request.url.path.endswith("/file"):
                if request.query_params:
                    raise CoreError("REQUEST_INVALID")
                result = await asyncio.to_thread(read_guardrail_file, agent, actor, request.path_params["wait_id"])
                return Response(result["content"], media_type="application/octet-stream", headers={
                    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                    "Content-Security-Policy": "sandbox; default-src 'none'",
                    "Content-Disposition": "attachment; filename*=UTF-8''" + quote(result["entry"]["actual_name"], safe=""),
                })
            else:
                if request.method == "PUT":
                    fields = (TIMEOUT_KEYS | {"expected_revision"} if is_settings
                              else {"mode", "guardrails_exempt", "expected_revision", "expected_origin"})
                    payload = await read_payload(request, fields,
                        optional_fields=SETTINGS_KEYS - TIMEOUT_KEYS if is_settings else frozenset())
                result = await asyncio.to_thread(
                    settings, actor, payload,
                ) if is_settings else await asyncio.to_thread(
                    policies, actor, request.path_params.get("canonical_name"), payload,
                )
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except CoreError as error:
            status = {"ACCESS_DENIED": 403, "TOOL_NOT_FOUND": 404,
                      "FILE_NOT_FOUND": 404, "WORKSPACE_UNAVAILABLE": 409, "WORKSPACE_SCAN_LIMIT": 409,
                      "FILE_CLEANUP_NOT_FOUND": 404, "CONTEXT_BUSY": 409, "CLEANUP_REQUEST_CONFLICT": 409,
                      "WORKSPACE_CLEANUP_INVALID": 409, "WORKSPACE_CLEANUP_PENDING": 503,
                      "TASK_NOT_FOUND": 404, "INTERACTION_VERSION_CONFLICT": 409,
                      "TOOL_IDENTITY_CONFLICT": 409,
                      "MATERIAL_REVIEW_NOT_FOUND": 404, "MATERIAL_REVIEW_CONFLICT": 409,
                      "FILE_BATCH_NOT_FOUND": 404, "ARTIFACT_INTEGRITY_FAILED": 409,
                      "INTERACTION_CLOSED": 409, "SETTINGS_CONFLICT": 409,
                      "REMOTE_AGENT_NOT_FOUND": 404, "REMOTE_AGENT_CONFLICT": 409,
                      "REMOTE_AGENT_CREDENTIAL_UNAVAILABLE": 503,
                      "CRON_NOT_FOUND": 404, "CRON_CONFLICT": 409, "CRON_DISABLED": 409,
                      "REQUEST_TOO_LARGE": 413}.get(error.code, 400)
            return JSONResponse({"error": {"code": error.code, **error.data}}, status_code=status,
                                headers={"Cache-Control": "no-store"})

    routes = [
        Route("/api/settings", endpoint, methods=["GET", "PUT"]),
        Route("/api/tool-policies", endpoint),
        Route("/api/tool-policies/{canonical_name}", endpoint, methods=["PUT"]),
        Route("/api/interactions", endpoint),
        Route("/api/guardrails/{wait_id}/material", endpoint),
        Route("/api/guardrails/{wait_id}/file", endpoint),
    ]
    if admission is not None:
        routes.append(Route("/api/chats", endpoint))
        routes.append(Route("/api/chats/{context_id:path}/history", endpoint))
        routes.append(Route("/api/chats/{context_id:path}/files/delete", endpoint, methods=["GET", "POST"]))
        routes.append(Route("/api/chats/{context_id:path}/files", endpoint))
        routes.append(Route("/api/chats/{context_id:path}/files/content", endpoint))
    if remote_registry is not None:
        routes.extend([
            Route("/api/remote-agents", endpoint, methods=["GET", "POST"]),
            Route("/api/remote-agents/{peer_id}", endpoint, methods=["PUT", "DELETE"]),
        ])
    if cron_store is not None:
        routes.extend([
            Route("/api/schedules", endpoint, methods=["GET", "POST"]),
            Route("/api/schedules/{schedule_id}", endpoint, methods=["GET", "PUT", "DELETE"]),
            Route("/api/schedules/{schedule_id}/run-now", endpoint, methods=["POST"]),
        ])
    for path, kind in (("hitl/{wait_id}/decision", "tool_approval"),
                       ("questions/{wait_id}/answer", "owner_question"),
                       ("guardrails/{wait_id}/decision", "guardrail")):
        async def decision(request, kind=kind):
            request.path_params["kind"] = kind
            return await endpoint(request)
        routes.append(Route("/api/" + path, decision, methods=["POST"]))
    return routes
