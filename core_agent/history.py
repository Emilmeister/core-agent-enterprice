"""Bounded owner projection of existing roots, transcripts and retained inbox rows."""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from itertools import chain, islice

from .errors import CoreError
from .security import redact
from .workflow import TERMINAL_STATES
from .workspace import WorkspaceBinding


def cursor_position(value):
    try:
        parsed = json.loads(value)
        if (isinstance(parsed, list) and len(parsed) == 2 and type(parsed[0]) is int and parsed[0] == 2
                and isinstance(parsed[1], str) and str(uuid.UUID(parsed[1])) == parsed[1]):
            return 2, parsed[1], None
        version, task_id, position = parsed
        if (type(version) is not int or version != 1 or not isinstance(task_id, str) or not task_id
                or "\0" in task_id or not isinstance(position, list) or len(position) != 4
                or any(type(v) is not int or not 0 <= v < 2**63 for v in position)):
            raise ValueError()
        task_id.encode("utf-8")
        return 1, task_id, tuple(position)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise CoreError("REQUEST_INVALID") from None


def _root(admission, run_id, tenant, chat, connection):
    try:
        if not isinstance(run_id, str) or not run_id or "\0" in run_id:
            raise ValueError()
        run_id.encode("utf-8")
    except (ValueError, UnicodeError):
        raise CoreError("CHECKPOINT_INVALID") from None
    if connection is None:
        record = admission.agent.workflow_store._records.get(run_id)
        if record is None:
            raise CoreError("CHECKPOINT_INVALID")
        admitted = any(row["task_id"] == record.task_id and row["owner_id"] == chat["owner_id"]
                       for key, row in admission.messages.items() if key[0] == tenant)
        snapshot = record.snapshot
        value = {name: getattr(record, name) for name in (
            "run_id", "task_id", "tenant_id", "owner_id", "context_id", "parent_run_id", "state", "result", "error_code")}
        value.update(previous=snapshot.get("previous_root_run_id"),
            output_contract_version=snapshot.get("output_contract_version", 1),
            file_batch_id=snapshot.get("file_batch_id"),
            initial_checked=snapshot.get("initial_material_checked", False),
            start=snapshot.get("context", {}).get("sequence_range", [1, 0])[0], admitted=admitted,
            original_prompt=record.request.get("prompt"), cron_origin=snapshot.get("cron_origin"),
            display_text=next((row.get("display_text") for key, row in admission.messages.items()
                if key[0] == tenant and row["task_id"] == record.task_id and row["owner_id"] == chat["owner_id"]), None),
            input_message_id=next((key[2] for key, row in admission.messages.items()
                if key[0] == tenant and row["task_id"] == record.task_id and row["owner_id"] == chat["owner_id"]), None))
    else:
        value = connection.execute("""SELECT r.run_id,r.task_id,r.tenant_id,r.owner_id,r.context_id,
            r.parent_run_id,r.state,r.error_code,
            r.snapshot->>'file_batch_id' AS file_batch_id,
            r.snapshot->>'previous_root_run_id' AS previous,
            COALESCE(r.snapshot->'output_contract_version','1'::jsonb) AS output_contract_version,
            r.request->>'prompt' AS original_prompt,r.snapshot->'cron_origin' AS cron_origin,
            (SELECT m.display_text FROM core_root_messages m WHERE m.tenant_id=r.tenant_id
                AND m.task_id=r.task_id AND m.owner_id=r.owner_id AND m.context_id=r.context_id LIMIT 1) AS display_text,
            (SELECT m.message_id FROM core_root_messages m WHERE m.tenant_id=r.tenant_id
                AND m.task_id=r.task_id AND m.owner_id=r.owner_id AND m.context_id=r.context_id LIMIT 1) AS input_message_id,
            COALESCE((r.snapshot->>'initial_material_checked')::boolean,false) AS initial_checked,
            COALESCE((r.snapshot#>>'{context,sequence_range,0}')::bigint,1) AS start,
            EXISTS(SELECT 1 FROM core_root_messages m WHERE m.tenant_id=r.tenant_id
                AND m.task_id=r.task_id AND m.owner_id=r.owner_id AND m.context_id=r.context_id) AS admitted
            FROM core_runs r WHERE r.run_id=%s AND r.tenant_id=%s AND r.owner_id=%s AND r.context_id=%s""",
            (run_id, tenant, chat["owner_id"], chat["context_id"])).fetchone()
    if (value is None or not value["admitted"] or value["parent_run_id"] is not None
            or (value["tenant_id"], value["owner_id"], value["context_id"]) != (tenant, chat["owner_id"], chat["context_id"])
            or value["previous"] is not None and (not isinstance(value["previous"], str) or not value["previous"])):
        raise CoreError("CHECKPOINT_INVALID")
    if type(value["output_contract_version"]) is not int or value["output_contract_version"] not in {1, 2}:
        raise CoreError("CHECKPOINT_INVALID")
    return value


def _entries(admission, root, connection, *, before, limit, exact=False, inclusive=False, source_sequence=-1):
    """Only selected JSON elements leave PostgreSQL; no full snapshot is loaded."""
    if connection is None:
        store = admission.agent.workflow_store
        transcript = store._records[root["run_id"]].snapshot.get("context", {}).get("transcript", [])
        matched = {}
        values = []
        for offset, item in enumerate(transcript, root["start"]):
            inbound = (item.get("provenance") or {}).get("inbound_sequence")
            if inbound is not None:
                if type(inbound) is not int or inbound < 1 or inbound in matched:
                    raise CoreError("CHECKPOINT_INVALID")
                matched[inbound] = item
            else:
                values.append({"position": (0, offset, 0, 0), "item": item, "input": None})
        for message in store._inbound.get(root["run_id"], ()):
            item = matched.pop(message["sequence"], None)
            if message["consumed"] and item is None:
                continue  # Legacy consumed input already lives in the full transcript.
            anchor = message["provenance"].get("history_after_sequence")
            if anchor is not None and (type(anchor) is not int or anchor < 0):
                raise CoreError("CHECKPOINT_INVALID")
            position = (1, 0, 1, message["sequence"]) if anchor is None else (0, max(1, anchor), 1, message["sequence"])
            values.append({"position": position, "item": item, "input": message})
        if matched:
            raise CoreError("CHECKPOINT_INVALID")
        if not transcript:
            values.append({"position": (0, 1, 0, 0), "item": {"kind": "prompt", "content": ""}, "input": None})
        if root["state"] in TERMINAL_STATES:
            values.append({"position": (2, 0, 0, 0), "item": None, "input": None})
        if source_sequence != -1:
            if source_sequence is None:
                values = [row for row in values if (row["item"] or {}).get("kind") == "prompt" and row["input"] is None]
            else:
                values = [row for row in values if row["input"] and row["input"]["sequence"] == source_sequence]
        return sorted((row for row in values if before is None or
                       (row["position"] == before if exact else
                        row["position"] < before or inclusive and row["position"] == before)),
                      key=lambda row: row["position"], reverse=True)[:limit]
    compare = "=" if exact else "<=" if inclusive else "<"
    predicate = f"WHERE (lane,anchor,part,seq) {compare} (%s,%s,%s,%s)" if before is not None else ""
    source_values = ()
    if source_sequence != -1:
        predicate += " AND " if predicate else "WHERE "
        if source_sequence is None:
            predicate += "item->>'kind'='prompt' AND input IS NULL"
        else:
            predicate += "(input->>'sequence')::bigint=%s"
            source_values = (source_sequence,)
    rows = connection.execute(f"""WITH transcript AS (
        SELECT item, ordinal-1+%s AS position FROM core_runs r,
            jsonb_array_elements(COALESCE(r.snapshot#>'{{context,transcript}}','[]'::jsonb))
            WITH ORDINALITY AS t(item,ordinal) WHERE r.run_id=%s AND r.tenant_id=%s
    ), entries AS (
        SELECT 0::bigint AS lane,position AS anchor,0::bigint AS part,0::bigint AS seq,item,NULL::jsonb AS input
        FROM transcript WHERE NOT (COALESCE(item->'provenance','{{}}'::jsonb) ? 'inbound_sequence')
        UNION ALL
        SELECT CASE WHEN i.provenance ? 'history_after_sequence' THEN 0 ELSE 1 END,
            CASE WHEN i.provenance ? 'history_after_sequence'
                THEN GREATEST(1,(i.provenance->>'history_after_sequence')::bigint) ELSE 0 END,1,i.sequence,t.item,
            jsonb_build_object('sequence',i.sequence,'message_id',i.message_id,'consumed',i.consumed_at IS NOT NULL,
                'provenance',i.provenance)
        FROM core_inbound_messages i LEFT JOIN transcript t
          ON t.item#>>'{{provenance,inbound_sequence}}'=i.sequence::text
        WHERE i.run_id=%s AND (i.consumed_at IS NULL OR t.item IS NOT NULL)
        UNION ALL SELECT 0,1,0,0,'{{"kind":"prompt","content":""}}'::jsonb,NULL::jsonb
            WHERE NOT EXISTS(SELECT 1 FROM transcript)
        UNION ALL SELECT 2,0,0,0,NULL::jsonb,NULL::jsonb WHERE %s
    ) SELECT lane,anchor,part,seq,item,input FROM entries {predicate}
      ORDER BY lane DESC,anchor DESC,part DESC,seq DESC LIMIT %s""",
        (root["start"], root["run_id"], root["tenant_id"], root["run_id"],
         root["state"] in TERMINAL_STATES, *(before or ()), *source_values, limit)).fetchall()
    return [{"position": (row["lane"], row["anchor"], row["part"], row["seq"]),
             "item": row["item"], "input": row["input"]} for row in rows]


_PRIVATE_KEYS = frozenset({"provider_replay", "reasoning_replay", "reasoning", "thinking", "signature",
    "thought_signature", "authorization", "headers", "api_key", "apikey", "password", "secret", "token",
    "access_token", "refresh_token", "credentials", "sealed_ref", "snapshot", "trace", "traceparent", "tracestate"})


def _safe_value(value):
    if isinstance(value, dict):
        return {key: _safe_value(item) for key, item in value.items()
                if key.lower().replace("-", "_") not in _PRIVATE_KEYS
                and not any(marker in key.lower() for marker in ("password", "secret", "authorization", "api_key", "token",
                                                                "reasoning", "thinking", "signature", "trace", "sealed"))}
    if isinstance(value, list):
        return [_safe_value(item) for item in value]
    return redact(value)


def _text(item):
    kind, content = item.get("kind"), item.get("content", "")
    if not isinstance(content, str):
        raise CoreError("CHECKPOINT_INVALID")
    if kind in {"prompt", "user_message", "unprocessed_due_to_failure", "unprocessed_due_to_cancel"}:
        return "user_message", content
    if kind in {"agent_message", "assistant_message"}:
        return "agent_message", content
    try:
        payload = json.loads(content)
    except (ValueError, TypeError):
        payload = None
    if kind == "assistant_tool_calls":
        calls = payload if isinstance(payload, list) else payload.get("tool_calls") if isinstance(payload, dict) else None
        if not isinstance(calls, list):
            raise CoreError("CHECKPOINT_INVALID")
        values = []
        for call in calls:
            if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                raise CoreError("CHECKPOINT_INVALID")
            function = call.get("function", {})
            if not isinstance(call.get("id"), str) or not isinstance(function.get("name"), str):
                raise CoreError("CHECKPOINT_INVALID")
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = "[Unstructured arguments unavailable]"
            values.append({"id": call.get("id"), "name": function.get("name"), "arguments": _safe_value(arguments)})
        return "tool_call", json.dumps(values, ensure_ascii=False, sort_keys=True)
    if kind == "tool_result":
        if not isinstance(payload, dict):
            return "tool_result", "[Unstructured tool result unavailable]"
        value = {key: _safe_value(payload[key]) for key in ("tool_call_id", "tool_name", "status", "output") if key in payload}
        if payload.get("status") == "failed":
            value.pop("output", None)  # Exception/provider text is not a public error contract.
            output = payload.get("output")
            if (isinstance(payload.get("tool_name"), str) and payload["tool_name"] in {"core_terminal_exec", "core_python_exec"}
                    and isinstance(output, dict) and type(output.get("exit_code")) is int
                    and -(2**31) <= output["exit_code"] < 2**31
                    and type(output.get("timed_out")) is bool and type(output.get("truncated")) is bool
                    and isinstance(output.get("status"), str) and output["status"] in {"failed", "timed_out"}
                    and (type(output.get("duration")) is int and 0 <= output["duration"] < 2**63
                         or type(output.get("duration")) is float and math.isfinite(output["duration"]) and output["duration"] >= 0)):
                value["output"] = {key: output[key] for key in ("exit_code", "timed_out", "truncated", "duration", "status")}
        if isinstance(payload.get("error_code"), str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", payload["error_code"]):
            value["error_code"] = payload["error_code"]
        return "tool_result", json.dumps(value, ensure_ascii=False, sort_keys=True)
    return "placeholder", "[Stored runtime event]"


def _json_identity(value):
    return {"material_kind": "json", "material_digest": hashlib.sha256(json.dumps(value,
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()}


def _reviews(admission, root, sources, review_ids, materials, batches, connection):
    """Metadata-only reads; never refresh reviews, resolve waits, or retrieve payloads."""
    if admission.agent.material_review_store is None:
        return []
    if connection is None:
        snapshot = admission.agent.workflow_store._records[root["run_id"]].snapshot
        identities = [snapshot.get("context_materials", {}).get(source, {}).get("identity", {}) for source in sources]
        review_ids.extend(snapshot["material_denials"][source] for source in sources if source in snapshot.get("material_denials", {}))
    else:
        metadata = connection.execute("""SELECT
            ARRAY(SELECT value FROM jsonb_each_text(COALESCE(snapshot->'material_denials','{}'::jsonb)) WHERE key=ANY(%s)) AS denied,
            ARRAY(SELECT value->'identity' FROM jsonb_each(COALESCE(snapshot->'context_materials','{}'::jsonb)) WHERE key=ANY(%s)) AS identities
            FROM core_runs WHERE run_id=%s AND tenant_id=%s""", (sources, sources, root["run_id"], root["tenant_id"])).fetchone()
        identities = metadata["identities"]
        review_ids.extend(metadata["denied"])
    for identity in identities:
        if not isinstance(identity, dict):
            raise CoreError("CHECKPOINT_INVALID")
        review_ids.extend([identity["review_id"]] if identity.get("review_id") else [])
        materials.append(identity)
    pairs = set()
    for material in materials:
        if material.get("material_digest"):
            pairs.add((material["material_digest"], material.get("material_kind", "json")))
        if material.get("text_digest"):
            pairs.add((material["text_digest"], "json"))
    digests, kinds = [pair[0] for pair in pairs], [pair[1] for pair in pairs]
    if connection is not None:
        return connection.execute("""WITH candidates AS (SELECT r.review_id,r.wait_id,r.state,w.outcome,w.deadline,
                ((r.run_id=%s AND (r.source_id=ANY(%s) OR
                    r.source_kind='file_attachment' AND split_part(r.source_id,':',2)=ANY(%s)))
                    OR r.review_id=ANY(%s)) AS direct,
                (r.state IN ('pending','rejected','timed_out') AND EXISTS (
                    SELECT 1 FROM unnest(%s::text[],%s::text[]) AS identity(digest,kind)
                    WHERE (COALESCE(r.completed_result_ref->>'material_kind','json')=identity.kind
                        AND r.completed_result_ref->>'material_digest'=identity.digest)
                      OR (identity.kind='json' AND r.completed_result_ref->>'text_digest'=identity.digest))) AS negative,
                extract(epoch FROM clock_timestamp()) AS now
            FROM core_material_reviews r LEFT JOIN core_waits w ON w.wait_id=r.wait_id
                AND w.tenant_id=r.tenant_id AND w.owner_id=r.owner_id AND w.run_id=r.run_id
            WHERE r.tenant_id=%s AND r.owner_id=%s AND r.context_id=%s)
            SELECT * FROM candidates WHERE direct OR negative ORDER BY review_id""",
            (root["run_id"], sources, batches, review_ids, digests, kinds,
             root["tenant_id"], root["owner_id"], root["context_id"])).fetchall()
    store = admission.agent.workflow_store
    rows = []
    for row in admission.agent.material_review_store.rows.values():
        reference = row.get("completed_result_ref") or {}
        if ((row["tenant_id"], row["owner_id"], row["context_id"]) !=
                (root["tenant_id"], root["owner_id"], root["context_id"])):
            continue
        direct = ((row["run_id"] == root["run_id"] and (row["source_id"] in sources or
                row["source_kind"] == "file_attachment" and row["source_id"].split(":")[1] in batches))
                or row["review_id"] in review_ids)
        negative = row["state"] in {"pending", "rejected", "timed_out"} and (
            (reference.get("material_digest"), reference.get("material_kind", "json")) in pairs
            or (reference.get("text_digest"), "json") in pairs)
        if not (direct or negative):
            continue
        wait = store._waits.get(row["wait_id"])
        rows.append({key: row[key] for key in ("review_id", "wait_id", "state")} |
                    {"outcome": wait.outcome if wait else None, "deadline": wait.deadline if wait else None,
                     "now": store.current_time(), "direct": direct})
    return rows


def _source_materials(admission, root, provenance, connection, chat):
    if (not isinstance(provenance, dict) or type(provenance.get("version")) is not int
            or provenance["version"] != 1 or type(provenance.get("summary_version", 1)) is not int
            or provenance.get("summary_version", 1) != 1 or not isinstance(provenance.get("sources"), dict)):
        raise CoreError("CHECKPOINT_INVALID")
    materials = []
    for source in provenance["sources"].values():
        if not isinstance(source, dict):
            raise CoreError("CHECKPOINT_INVALID")
        ancestor, seen = root, set()
        while source.get("run_id") != ancestor["run_id"]:
            previous = ancestor["previous"]
            if previous is None or previous in seen:
                raise CoreError("CHECKPOINT_INVALID")
            seen.add(previous)
            ancestor = _root(admission, previous, root["tenant_id"], chat, connection)
            if ancestor["state"] not in TERMINAL_STATES:
                raise CoreError("CHECKPOINT_INVALID")
        materials.extend(source.get("materials", ()))
        if source.get("kind") == "terminal_result":
            if connection is None:
                message = (ancestor["result"] or {}).get("message")
            else:
                message = connection.execute("SELECT result->'message' AS message FROM core_runs WHERE run_id=%s AND tenant_id=%s",
                    (ancestor["run_id"], root["tenant_id"])).fetchone()["message"]
            if message is not None:
                materials.append(_json_identity(message))
    return materials


def _review_status(reviews):
    """The passive current visibility verdict, shared by owner material readers."""
    statuses = []
    for review in reviews:
        state = review["state"]
        if review["outcome"] is not None:
            state = {"allowed": "allowed", "timeout": "timed_out"}.get(review["outcome"].get("reason"), "rejected")
        elif review["deadline"] is not None and review["deadline"] <= review["now"]:
            state = "timed_out"
        if not review["direct"] and state not in {"rejected", "timed_out"}:
            continue
        if state not in {"clear", "allowed"}:
            statuses.append((state, review["wait_id"]))
    return next((value for value in statuses if value[0] in {"rejected", "timed_out"}),
                statuses[0] if statuses else None)


def _final_dependency_reviews(admission, root, connection, chat, *, before_call_id=None):
    """Scan provenance in bounded pages, not full outputs or an additional transcript copy."""
    offset = 0
    while True:
        if connection is None:
            snapshot = admission.agent.workflow_store._records[root["run_id"]].snapshot
            imported = snapshot.get("context_import")
            if imported is not None and (type(imported.get("version")) is not int or imported["version"] != 1):
                raise CoreError("CHECKPOINT_INVALID")
            def originals():
                if before_call_id:
                    for key, source in (imported or {}).get("sources", {}).items():
                        yield {"provenance": {"version": 1, "sources": {key: source}}}
                for item in snapshot.get("context", {}).get("transcript", ()):
                    if not item["kind"].startswith("unprocessed_due_to_"):
                        yield item
                if not before_call_id:
                    for key, source in (imported or {}).get("sources", {}).items():
                        yield {"provenance": {"version": 1, "sources": {key: source}}}
            rows = list(islice(originals(), offset, offset + 100))
        else:
            rows = [row["item"] for row in connection.execute("""WITH r AS (
                SELECT snapshot FROM core_runs WHERE run_id=%s AND tenant_id=%s
            ), dependencies AS (
                SELECT 0 AS phase,ordinal AS position,
                    CASE WHEN item->'provenance' IS NULL OR item->'provenance'='null'::jsonb THEN
                        jsonb_build_object('kind',item->'kind','content',item->'content')
                    ELSE jsonb_build_object('kind',item->'kind','provenance',item->'provenance') ||
                        CASE WHEN %s::text IS NOT NULL AND item->>'kind' IN ('assistant_tool_calls','tool_result')
                            THEN jsonb_build_object('content',item->'content') ELSE '{}'::jsonb END END AS item
                FROM r,jsonb_array_elements(COALESCE(snapshot#>'{context,transcript}','[]'::jsonb))
                    WITH ORDINALITY AS t(item,ordinal)
                WHERE item->>'kind' NOT LIKE 'unprocessed_due_to_%%'
                UNION ALL
                SELECT 1,ordinal,jsonb_build_object('provenance',jsonb_build_object(
                    'version',snapshot#>'{context_import,version}','sources',jsonb_build_object(key,value)))
                FROM r,jsonb_each(COALESCE(snapshot#>'{context_import,sources}','{}'::jsonb))
                    WITH ORDINALITY AS s(key,value,ordinal)
            ) SELECT item FROM dependencies
                ORDER BY CASE WHEN %s::text IS NOT NULL THEN 1-phase ELSE phase END,position LIMIT 100 OFFSET %s""",
                (root["run_id"], root["tenant_id"], before_call_id, before_call_id, offset)).fetchall()]
        if not rows:
            return
        materials, sources = [], []
        stopped = False
        for item in rows:
            if before_call_id and item.get("kind") in {"assistant_tool_calls", "tool_result"}:
                value = json.loads(item["content"])
                calls = value if isinstance(value, list) else value.get("tool_calls", ())
                if (isinstance(value, dict) and value.get("tool_call_id") == before_call_id
                        or any(call.get("id") == before_call_id for call in calls)):
                    stopped = True
                    break
            if item.get("provenance") is not None:
                materials.extend(_source_materials(admission, root, item["provenance"], connection, chat))
            elif item.get("kind") in {"prompt", "user_message", "tool_result"}:
                payload = item.get("content")
                if item["kind"] == "prompt":
                    sources.append("initial")
                elif item["kind"] == "tool_result":
                    value = json.loads(payload)
                    sources.append("result:" + value["tool_call_id"])
                    payload = value.get("output")
                    if isinstance(payload, dict):
                        if value.get("tool_name") == "core_ask_owner":
                            payload = payload.get("answer", payload)
                        elif value.get("tool_name") in {"core_task_get", "core_task_wait", "core_delegate"}:
                            payload = payload.get("result", payload)
                materials.append(_json_identity(payload))
        yield from _reviews(admission, root, sources, [m["review_id"] for m in materials if m.get("review_id")],
                            materials, [], connection)
        if stopped:
            return
        offset += len(rows)


def published_artifacts(admission, root, connection=None):
    """Validate every scoped pointer before projecting metadata or granting a read."""
    from .response_artifacts import ResponseArtifactService

    if connection is None:
        manifests = admission.agent.workflow_store._records[root["run_id"]].snapshot.get("published_artifacts", [])
    else:
        manifests = connection.execute("""SELECT COALESCE(snapshot->'published_artifacts','[]'::jsonb) AS artifacts
            FROM core_runs WHERE run_id=%s AND tenant_id=%s""", (root["run_id"], root["tenant_id"])).fetchone()["artifacts"]
    try:
        if not manifests:
            if manifests != [] and manifests != ():
                raise ValueError()
            return ()
        service = ResponseArtifactService(admission.agent.response_files_service)
        binding = WorkspaceBinding(root["tenant_id"], root["owner_id"], root["context_id"])
        service._validate(binding, manifests, task_id=root["task_id"], run_id=root["run_id"],
                          limit_bytes=manifests[0]["parts"][0]["ref"]["limit_bytes"])
    except (CoreError, ValueError, TypeError, KeyError, IndexError, AttributeError):
        raise CoreError("ARTIFACT_INTEGRITY_FAILED") from None
    return manifests


def _published_materials(admission, root, connection):
    """Hash exact typed snapshots once per history read; retain only identities."""
    from .peer_conversations import _file_identity
    from .response_artifacts import ResponseArtifactService, _json_bytes

    if "_published_materials" in root:
        return root["_published_materials"]
    manifests = published_artifacts(admission, root, connection)
    identities = {}
    if manifests:
        service = ResponseArtifactService(admission.agent.response_files_service)
        binding = WorkspaceBinding(root["tenant_id"], root["owner_id"], root["context_id"])
        artifacts = service.load(binding, manifests, task_id=root["task_id"], run_id=root["run_id"],
            limit_bytes=manifests[0]["parts"][0]["ref"]["limit_bytes"], connection=connection)
        for manifest, artifact in zip(manifests, artifacts):
            materials = []
            for entry, part in zip(manifest["parts"], artifact.parts):
                content = (part.data.encode("utf-8") if part.kind == "text" else
                           _json_bytes(part.data) if part.kind == "data" else part.data["bytes"])
                materials.append(_file_identity(entry["ref"], content))
                if part.kind == "data":
                    materials.append(_json_identity(part.data))
            identities[manifest["artifact_id"]] = materials
    root["_published_materials"] = identities
    return identities


def _public_message(admission, root, message_id, connection):
    if not isinstance(message_id, str):
        return None
    if connection is None:
        entries = admission.agent.workflow_store._records[root["run_id"]].snapshot.get("public_messages", [])
        if not isinstance(entries, list):
            raise CoreError("CHECKPOINT_INVALID")
        matches = [entry for entry in entries if isinstance(entry, dict) and entry.get("message_id") == message_id]
    else:
        matches = [row["entry"] for row in connection.execute("""SELECT entry FROM core_runs r,
            jsonb_array_elements(COALESCE(r.snapshot->'public_messages','[]'::jsonb)) AS entry
            WHERE r.run_id=%s AND r.tenant_id=%s AND entry->>'message_id'=%s LIMIT 2""",
            (root["run_id"], root["tenant_id"], message_id)).fetchall()]
    if not matches:
        return None
    if (len(matches) != 1 or set(matches[0]) != {"message_id", "text"}
            or not isinstance(matches[0].get("text"), str)):
        raise CoreError("CHECKPOINT_INVALID")
    return {"message_id": message_id, "text": matches[0]["text"]}


def _publication_receipts(admission, root, value, connection):
    from .response_artifacts import ResponseArtifactService

    output = value.get("output")
    receipts = output.get("artifacts") if isinstance(output, dict) else None
    if (value.get("status") != "succeeded" or value.get("tool_name") not in {"core_response_artifacts", "core_response_files"}
            or not isinstance(receipts, (list, tuple)) or not receipts):
        return ()
    ids = [item.get("artifact_id") if isinstance(item, dict) else None for item in receipts]
    if any(not isinstance(identifier, str) for identifier in ids):
        return ()
    manifests = published_artifacts(admission, root, connection)
    selected = [manifest for manifest in manifests if manifest["artifact_id"] in ids]
    if len(selected) != len(ids) or len(set(ids)) != len(ids):
        return ()  # A tool payload cannot publish a pointer absent from the committed snapshot.
    return ResponseArtifactService.receipts(selected)


def published_file_source(admission, root, file_id, connection, chat):
    """The current decision for the accepted publisher also gates explicit downloads."""
    before = None
    while True:
        rows = _entries(admission, root, connection, before=before, limit=100)
        if not rows:
            return None
        for entry in rows:
            item = entry["item"] or {}
            if item.get("kind") != "tool_result":
                continue
            try:
                value = json.loads(item["content"])
            except (ValueError, KeyError, TypeError):
                continue
            if not isinstance(value, dict):
                continue
            receipts = _publication_receipts(admission, root, value, connection)
            if any(part["file_id"] == file_id for artifact in receipts for part in artifact["parts"]):
                return _project(admission, root, entry, connection, chat)
        before = rows[-1]["position"]


def _project(admission, root, entry, connection, chat):
    position, item, message = entry["position"], entry["item"], entry["input"]
    identity = root["task_id"] + ("/result" if position[0] == 2 else
        "/input/" + str(message["sequence"]) if message else "/transcript/" + str(position[1]))
    result = {"id": identity, "task_id": root["task_id"], "kind": "placeholder", "text": "", "status": "available"}
    input_id = message["message_id"] if message else root.get("input_message_id") if (item or {}).get("kind") == "prompt" else None
    if isinstance(input_id, str):
        result["input_message_id"] = input_id
    terminal_result = position[0] == 2
    public_message, artifact_receipts, publisher_call = None, (), None
    sources, review_ids, materials, batches = [], [], [], []
    if terminal_result:
        if connection is None:
            final = root["result"] or {}
        else:
            final = connection.execute("""SELECT jsonb_strip_nulls(jsonb_build_object(
                'message',result->'message','complete',result->'complete','completion_reason',result->'completion_reason'))
                || CASE WHEN result ? 'outgoing_files'
                    THEN jsonb_build_object('outgoing_files',result->'outgoing_files') ELSE '{}'::jsonb END AS result
                FROM core_runs WHERE run_id=%s AND tenant_id=%s""", (root["run_id"], root["tenant_id"])).fetchone()["result"]
        if (not isinstance(final, dict) or ("complete" in final and type(final["complete"]) is not bool)
                or ("completion_reason" in final and not isinstance(final["completion_reason"], str))):
            raise CoreError("CHECKPOINT_INVALID")
        result.update(kind="result", text=final.get("message", "") if isinstance(final.get("message", ""), str) else "",
            outcome={"state": root["state"], **{key: final[key] for key in ("complete", "completion_reason") if key in final}})
        if root["error_code"] and re.fullmatch(r"[A-Z][A-Z0-9_]*", root["error_code"]):
            result["outcome"]["error_code"] = root["error_code"]
        if final.get("message") is not None:
            materials.append(_json_identity(final["message"]))
        if root["output_contract_version"] == 2:
            suffix = {"COMPLETED": "final", "FAILED": "failed", "ABORTED": "aborted", "REJECTED": "rejected"}.get(root["state"])
            if suffix:
                result["public_message_id"] = root["task_id"] + ":" + suffix
    if message:
        sources.append("input:" + message["message_id"])
        if message["provenance"].get("file_batch_id"):
            batches.append(message["provenance"]["file_batch_id"])
    if item and item.get("kind") == "prompt":
        sources.append("initial")
        if root.get("file_batch_id"):
            batches.append(root["file_batch_id"])
    provenance = (item or {}).get("provenance")
    if provenance is not None:
        materials.extend(_source_materials(admission, root, provenance, connection, chat))
        if message and (provenance.get("inbound_sequence"), provenance.get("message_id")) != (message["sequence"], message["message_id"]):
            raise CoreError("CHECKPOINT_INVALID")
    review_ids.extend(material["review_id"] for material in materials if material.get("review_id"))
    kind = (item or {}).get("kind")
    if kind == "tool_result":
        try:
            value = json.loads(item["content"])
            sources.extend(["result:" + value["tool_call_id"], "arguments:" + value["tool_call_id"]])
            if root["output_contract_version"] == 2 and value.get("status") == "succeeded":
                output = value.get("output")
                if value.get("tool_name") == "core_message_send" and isinstance(output, dict):
                    message_id = output.get("message_id")
                    if message_id == root["task_id"] + ":message:" + value["tool_call_id"]:
                        public_message = _public_message(admission, root, message_id, connection)
                        if public_message:
                            result["public_message_id"] = public_message["message_id"]
                            publisher_call = value["tool_call_id"]
                            materials.append(_json_identity(public_message["text"]))
                artifact_receipts = _publication_receipts(admission, root, value, connection)
                if artifact_receipts:
                    publisher_call = value["tool_call_id"]
        except (ValueError, KeyError, TypeError):
            pass
    elif kind == "assistant_tool_calls":
        try:
            value = json.loads(item["content"])
            for call in value if isinstance(value, list) else value["tool_calls"]:
                sources.append("arguments:" + call["id"])
        except (ValueError, KeyError, TypeError):
            raise CoreError("CHECKPOINT_INVALID") from None
    reviews = _reviews(admission, root, sources, review_ids, materials, batches, connection)
    if terminal_result:
        reviews = chain(reviews, _final_dependency_reviews(admission, root, connection, chat))
    elif publisher_call:
        reviews = chain(reviews, _final_dependency_reviews(admission, root, connection, chat, before_call_id=publisher_call))
    blocked = _review_status(reviews)
    if not blocked and root["output_contract_version"] == 2 and (artifact_receipts or terminal_result):
        selected = {receipt["artifact_id"] for receipt in artifact_receipts} if not terminal_result else None
        published_materials = [material for identifier, identities in _published_materials(admission, root, connection).items()
            if selected is None or identifier in selected for material in identities]
        blocked = _review_status(_reviews(admission, root, [], [], published_materials, [], connection))
    if blocked:
        state, wait_id = blocked
        result.update(kind="placeholder", status=state if state in {"rejected", "timed_out"} else "pending_guardrail",
                      text="[Material is unavailable pending owner review]" if state not in {"rejected", "timed_out"} else "[Material is unavailable]")
        if wait_id:
            result["review"] = {"wait_id": wait_id}
    elif message and not message["consumed"]:
        result.update(status="queued", text="[Accepted message awaiting processing]")
    elif kind == "prompt" and admission.agent.material_review_store is not None and not root["initial_checked"]:
        result.update(status="pending_guardrail", text="[Initial message awaiting material review]")
    elif not terminal_result:
        result["kind"], result["text"] = _text(item or {})
        if kind in {"unprocessed_due_to_failure", "unprocessed_due_to_cancel"}:
            result.update(kind="placeholder", status=kind, text="[Accepted message was not processed]")
    if result["status"] == "available" and result["kind"] == "user_message":
        display = message["provenance"].get("display_text") if message else root.get("display_text") if kind == "prompt" else None
        if display is not None:
            if not isinstance(display, str):
                raise CoreError("CHECKPOINT_INVALID")
            result["display_text"] = display
    if result["status"] == "available" and batches:
        service = admission.agent.chat_file_service
        for batch_id in batches:
            batch = service.store.get(batch_id, root["tenant_id"], connection=connection)
            if (batch["owner_id"], batch["context_id"], batch["task_id"], batch["run_id"], batch["sequence"]) != (
                    root["owner_id"], root["context_id"], root["task_id"], root["run_id"],
                    message["sequence"] if message else None):
                raise CoreError("CHECKPOINT_INVALID")
            if batch["state"] == "published":
                from .chat_files import attachment_entries

                result.setdefault("attachments", []).extend(attachment_entries(batch))
    if result["status"] == "available":
        if public_message:
            result["public_message"] = public_message
        if artifact_receipts:
            result["response_artifacts"] = list(artifact_receipts)
        if terminal_result and root["output_contract_version"] == 2:
            from .response_artifacts import ResponseArtifactService

            manifests = published_artifacts(admission, root, connection)
            if manifests:
                result["response_artifacts"] = list(ResponseArtifactService.receipts(manifests))
    if (terminal_result and root["output_contract_version"] == 1
            and root["state"] == "COMPLETED" and result["status"] == "available"):
        files = final.get("outgoing_files", ())
        if not isinstance(files, (tuple, list)) or not all(isinstance(ref, dict) for ref in files):
            raise CoreError("ARTIFACT_INTEGRITY_FAILED")
        if files:
            service = admission.agent.response_files_service
            if service is None:
                raise CoreError("ARTIFACT_INTEGRITY_FAILED")
            binding = WorkspaceBinding(root["tenant_id"], root["owner_id"], root["context_id"])
            validated = service.validate_refs(binding, files, task_id=root["task_id"], run_id=root["run_id"],
                                              limit_bytes=files[0].get("limit_bytes"))
            result["response_files"] = list(service.receipts(validated))
    return result


def title_source(admission, tenant, chat, source, connection=None):
    if (not isinstance(source, dict) or set(source) != {"run_id", "sequence"}
            or source["sequence"] is not None and (type(source["sequence"]) is not int or source["sequence"] < 1)):
        raise CoreError("CHECKPOINT_INVALID")
    root = _root(admission, source["run_id"], tenant, chat, connection)
    entries = _entries(admission, root, connection, before=None, limit=1, source_sequence=source["sequence"])
    return _project(admission, root, entries[0], connection, chat) if entries else None


def title_text(admission, tenant, chat, connection=None):
    """Automatic names follow the same current material decisions as the source."""
    source = chat.get("title_source")
    if source is not None:
        item = title_source(admission, tenant, chat, source, connection)
        return chat["title"] if item and item["status"] == "available" else ""
    if chat.get("title_revision", 0):
        return chat.get("title", "")  # Explicit owner rename is independent metadata.
    # ponytail: legacy names derive from existing history; no rewrite of old inputs.
    roots, seen, run_id = [], set(), chat["latest_root_run_id"]
    while run_id:
        if run_id in seen:
            raise CoreError("CHECKPOINT_INVALID")
        seen.add(run_id)
        root = _root(admission, run_id, tenant, chat, connection)
        roots.append(root)
        run_id = root["previous"]
    from .a2a import ATTACHMENTS_ONLY_PROMPT
    from .auth import OWNER_SCOPE

    for root in reversed(roots):
        if root["owner_id"] != OWNER_SCOPE or root.get("cron_origin") is not None:
            continue
        entries = _entries(admission, root, connection, before=None, limit=1, source_sequence=None)
        if not entries:
            continue
        item = _project(admission, root, entries[0], connection, chat)
        if item["status"] != "available":
            continue
        text = item.get("display_text", root.get("original_prompt") or "")
        if text == ATTACHMENTS_ONLY_PROMPT and root.get("file_batch_id"):
            text = ""
        name = " ".join(text.split()) or next((entry["actual_name"] for entry in item.get("attachments", ())), "")
        if name:
            return name[:120]
    return ""


def _notices(admission, tenant, chat, connection, **query):
    store = getattr(admission, "cron_store", None)
    if store is None:
        return []
    return store.events(tenant, context_id=chat["context_id"], owner_id=chat["owner_id"],
        kind="skipped", descending=True, connection=connection, **query)


def _notice_position(event):
    position = event["history_position"]
    if ((event["history_run_id"] is None) != (position is None)
            or position is not None and (not isinstance(position, list) or len(position) != 4
                or any(type(value) is not int or not 0 <= value < 2**63 for value in position))
            or type(event["seq"]) is not int or not 1 <= event["seq"] < 2**63):
        raise CoreError("CHECKPOINT_INVALID")
    return (*(position or (0, 0, 0, 0)), 1, event["seq"])


def _project_notice(event, task_id):
    # The saved cron origin contains the prompt; only these metadata leave history.
    if event["reason"] not in {"context_busy", "late", "service_unavailable", "workspace_cleanup_pending"}:
        raise CoreError("CHECKPOINT_INVALID")
    result = {"id": "schedule-notice:" + event["id"], "task_id": task_id,
        "kind": "schedule_notice", "status": "available", "text": "[Scheduled run skipped]",
        **{key: event[key] for key in ("schedule_id", "schedule_revision", "reason")},
        "timezone": event["payload"]["timezone"],
        **{key: event[key].isoformat() if event[key] is not None else None
           for key in ("due_at", "through", "created_at")}}
    return result, json.dumps([2, event["id"]], separators=(",", ":"))


def _history_page(admission, tenant, chat, root, connection, *, before, limit):
    ordinary = _entries(admission, root, connection, before=before[:4] if before else None,
        inclusive=before is not None and before[4] == 1, limit=limit) if root else []
    notices = _notices(admission, tenant, chat, connection, run_id=root["run_id"] if root else None,
        history_before=before, limit=limit)
    selected = sorted(chain((((*entry["position"], 0, 0), entry, None) for entry in ordinary),
                            ((_notice_position(event), None, event) for event in notices)),
                      key=lambda row: row[0], reverse=True)[:limit]
    return [(_project(admission, root, entry, connection, chat),
             json.dumps([1, root["task_id"], entry["position"]], separators=(",", ":")))
            if event is None else _project_notice(event, root["task_id"] if root else None)
            for _position, entry, event in selected]


def read_history(admission, tenant, context_id, *, limit, after=None, connection=None):
    if connection is None:
        chat = admission.chats.get((tenant, context_id))
    else:
        chat = connection.execute("SELECT owner_id,latest_root_run_id FROM core_chats WHERE tenant_id=%s AND context_id=%s",
                                  (tenant, context_id)).fetchone()
    if chat is None:
        raise CoreError("TASK_NOT_FOUND")
    chat = {**chat, "context_id": context_id}
    version, target, before = cursor_position(after) if after is not None else (1, None, None)
    if version == 2:
        events = _notices(admission, tenant, chat, connection, exact_id=target, limit=1)
        if not events:
            raise CoreError("REQUEST_INVALID")
        target, before = events[0]["history_run_id"], _notice_position(events[0])
        if target is None:
            return _history_page(admission, tenant, chat, None, connection, before=before, limit=limit)
    elif before is not None:
        before = (*before, 0, 0)
    run_id, seen, rows = chat["latest_root_run_id"], set(), []
    found = target is None
    while run_id:
        if run_id in seen:
            raise CoreError("CHECKPOINT_INVALID")
        seen.add(run_id)
        root = _root(admission, run_id, tenant, chat, connection)
        if run_id != chat["latest_root_run_id"] and root["state"] not in TERMINAL_STATES:
            raise CoreError("CHECKPOINT_INVALID")
        if not found and root["task_id" if version == 1 else "run_id"] == target:
            found = True
            if version == 1 and not _entries(admission, root, connection, before=before[:4], exact=True, limit=1):
                raise CoreError("REQUEST_INVALID")
        if found:
            rows.extend(_history_page(admission, tenant, chat, root, connection, before=before, limit=limit-len(rows)))
            before = None
            if len(rows) == limit:
                return rows
        run_id = root["previous"]
    if not found:
        raise CoreError("REQUEST_INVALID")
    rows.extend(_history_page(admission, tenant, chat, None, connection, before=None, limit=limit-len(rows)))
    return rows
