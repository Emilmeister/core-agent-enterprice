"""Normalize complete A2A artifacts into the existing private file pipeline."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from pathlib import PurePosixPath
from dataclasses import replace

from .a2a_input import _check_depth, _decode_raw
from .errors import CoreError
from .remote_agents import MAX_STREAM_FRAMES, _private_material


def complete_artifact(artifact):
    metadata = artifact.get("metadata", {})
    marker = metadata.get("core_agent_artifact") if isinstance(metadata, dict) else None
    return (isinstance(marker, dict) and type(marker.get("version")) in (int, float) and marker["version"] == 1
            and marker.get("complete") is True and marker.get("immutable") is True
            and metadata.get("partial") is not True)


def new_remote_artifacts(event, checkpoint, limit_bytes):
    """Select new immutable identities; repeated snapshots never create new batches."""
    previous = checkpoint.get("received_artifacts", [])
    indexed = {receipt["artifact_id"]: receipt for receipt in previous}
    selected, receipts, seen = [], [], set()
    total = sum(receipt["size_bytes"] for receipt in previous)
    parts = sum(receipt["part_count"] for receipt in previous)
    terminal_success = event.final and (event.kind == "message" or event.state == "TASK_STATE_COMPLETED")
    for artifact in event.artifacts:
        identity = artifact.get("artifactId")
        if not isinstance(identity, str) or not identity or identity in seen:
            raise CoreError("REMOTE_AGENT_PROTOCOL_ERROR")
        seen.add(identity)
        if not complete_artifact(artifact) and identity not in indexed and not terminal_success:
            continue
        try:
            encoded = json.dumps(artifact, sort_keys=True, ensure_ascii=False,
                separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise CoreError("REMOTE_AGENT_PROTOCOL_ERROR") from None
        digest = hashlib.sha256(encoded).hexdigest()
        if identity in indexed:
            if indexed[identity]["digest"] != digest:
                raise CoreError("REMOTE_AGENT_PROTOCOL_ERROR")
            continue
        if event.final and not terminal_success:
            continue
        artifact_parts = artifact.get("parts")
        if not isinstance(artifact_parts, list) or not artifact_parts:
            raise CoreError("REMOTE_AGENT_PROTOCOL_ERROR")
        size = 0
        try:
            for part in artifact_parts:
                if "text" in part:
                    size += len(part["text"].encode("utf-8"))
                elif "data" in part and isinstance(part["data"], dict):
                    _check_depth(part["data"])
                    size += len(json.dumps(part["data"], sort_keys=True, ensure_ascii=False,
                        separators=(",", ":"), allow_nan=False).encode("utf-8"))
                elif "raw" in part:
                    size += len(_decode_raw(part["raw"]))
                else:
                    raise ValueError()
        except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError, OverflowError):
            raise CoreError("REMOTE_AGENT_PROTOCOL_ERROR") from None
        total += size
        parts += len(artifact_parts)
        if total > limit_bytes:
            raise CoreError("ATTACHMENTS_TOO_LARGE", data={"allowed_bytes": limit_bytes, "actual_bytes": total})
        if len(previous) + len(receipts) + 1 > 64 or parts > 256:
            raise CoreError("REMOTE_AGENT_RESPONSE_TOO_LARGE")
        receipts.append({"artifact_id": identity, "digest": digest,
                         "size_bytes": size, "part_count": len(artifact_parts)})
        selected.append(artifact)
    return replace(event, artifacts=tuple(selected),
                   message_parts=event.message_parts if terminal_success else ()), [*previous, *receipts]


def prepare_remote_artifacts(file_service, event, *, tenant_id, actor_id, message_id,
                             request_digest, operation_id, limit_bytes, destination_id=None):
    """Called only for new-contract results; bind/decision/publication stay fenced.

    Public Messages keep their text. Artifact text/data and Message data/files
    become files; receipts contain identity and integrity, never their contents.
    The complete public manifest is itself reviewed and counted as a file.
    """
    if (not event.final or (event.kind != "message" and event.state != "TASK_STATE_COMPLETED")) and (
            not event.artifacts or not all(complete_artifact(artifact) for artifact in event.artifacts)):
        raise CoreError("REMOTE_AGENT_PROTOCOL_ERROR")
    if (not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", operation_id)
            or type(limit_bytes) is not int or limit_bytes < 1):
        raise CoreError("INVALID_FILE_INPUT")
    if len(event.artifacts) > MAX_STREAM_FRAMES:
        raise CoreError("REMOTE_AGENT_RESPONSE_TOO_LARGE")
    files, artifacts, messages = [], [], []
    total, count = 0, 0

    def materialize(part, prefix, index, *, artifact):
        nonlocal total, count
        count += 1
        if count > MAX_STREAM_FRAMES:
            raise CoreError("REMOTE_AGENT_RESPONSE_TOO_LARGE")
        if _private_material(part) or (not artifact and "text" in part):
            return None
        media = part.get("mediaType", part.get("media_type", "application/octet-stream"))
        original = part.get("filename", "")
        if not isinstance(media, str) or not isinstance(original, str):
            raise CoreError("REMOTE_AGENT_PROTOCOL_ERROR")
        try:
            if "text" in part:
                kind, extension = "text", ".md" if media.split(";", 1)[0].strip().lower() == "text/markdown" else ".txt"
                content = part["text"].encode("utf-8")
                media = part.get("mediaType", part.get("media_type", "text/plain"))
            elif "data" in part:
                kind, extension, media = "data", ".json", "application/json"
                if not isinstance(part["data"], dict):
                    raise ValueError()
                _check_depth(part["data"])
                content = json.dumps(part["data"], sort_keys=True, ensure_ascii=False,
                    separators=(",", ":"), allow_nan=False).encode("utf-8")
            elif "raw" in part:
                kind, content = "file", _decode_raw(part["raw"])
                extension = PurePosixPath(original).suffix
                if not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", extension):
                    extension = mimetypes.guess_extension(media.split(";", 1)[0].strip().lower()) or ".bin"
            else:
                raise ValueError()  # Remote URLs are never downloaded.
        except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError, OverflowError):
            raise CoreError("REMOTE_AGENT_PROTOCOL_ERROR") from None
        total += len(content)
        if total > limit_bytes:
            raise CoreError("ATTACHMENTS_TOO_LARGE", data={"allowed_bytes": limit_bytes, "actual_bytes": total})
        name = f"{prefix}-part-{index + 1:03d}{extension}"
        receipt = {"index": index, "type": kind, "name": original or name, "media_type": media,
            "relative_path": f"a2a/{destination_id or operation_id}/{name}", "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest()}
        files.append({"name": name, "media_type": media, "raw": content,
                      "metadata": {"origin": receipt}})
        return receipt

    for index, artifact in enumerate(event.artifacts):
        if _private_material(artifact):
            continue
        identifier, name = artifact.get("artifactId", ""), artifact.get("name", "")
        if not isinstance(identifier, str) or not isinstance(name, str):
            raise CoreError("REMOTE_AGENT_PROTOCOL_ERROR")
        parts = []
        prefix = ("artifact-" + hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:24]
                  if destination_id is not None else f"artifact-{index + 1:03d}")
        for part_index, part in enumerate(artifact.get("parts", ())):
            receipt = materialize(part, prefix, part_index, artifact=True)
            if receipt is not None:
                parts.append(receipt)
        artifacts.append({"index": index, "artifact_id": identifier, "name": name, "parts": parts})
    parts = []
    for index, part in enumerate(event.message_parts):
        receipt = materialize(part, "message-001", index, artifact=False)
        if receipt is not None:
            parts.append(receipt)
    if parts:
        messages.append({"message_id": event.message_id, "parts": parts})
    if not files:
        return None
    public_manifest = {"version": 1, "operation_id": operation_id, "remote_task_id": event.task_id,
        "remote_context_id": event.context_id, "artifacts": artifacts, "messages": messages}
    try:
        manifest_bytes = json.dumps(public_manifest, sort_keys=True, ensure_ascii=False,
            separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        raise CoreError("REMOTE_AGENT_PROTOCOL_ERROR") from None
    files.append({"name": "manifest.json", "media_type": "application/json", "raw": manifest_bytes})
    metadata = {"kind": event.kind, "state": event.state, "text": "\n".join(
        message["text"] for message in event.public_messages), "final": event.final,
        "task_id": event.task_id, "context_id": event.context_id, "parts": []}
    return file_service.prepare(files, tenant_id=tenant_id, actor_id=actor_id, message_id=message_id,
        request_digest=request_digest, source="remote", request_metadata=metadata,
        limit_bytes=limit_bytes + (len(manifest_bytes) if destination_id is not None else 0),
        destination_id=destination_id or operation_id,
        artifact_manifest={"artifacts": artifacts, "messages": messages,
                           "manifest_path": f"a2a/{destination_id or operation_id}/manifest.json"})
