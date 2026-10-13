"""Explicit immutable typed output, backed by the existing scoped snapshots."""
from __future__ import annotations

import json
import mimetypes
import re
import uuid

from google.protobuf.json_format import MessageToDict, ParseDict, ParseError
from google.protobuf.struct_pb2 import Value

from .a2a import Artifact, Part
from .a2a_input import _check_depth
from .errors import CoreError


_TEXT_MEDIA = re.compile(r"text/[A-Za-z0-9!#$%&'*+.^_`|~-]+")
_MANIFEST_KEYS = {"schema_version", "artifact_id", "name", "parts"}
_PUBLIC_PART_KEYS = ("file_id", "name", "media_type", "size_bytes", "sha256")
_MARKER = {"version": 1, "complete": True, "immutable": True}


def _name(value):
    if not isinstance(value, str) or len(value) > 256 or "\0" in value:
        raise ValueError()
    value.encode("utf-8")
    return value


def _text_media(value):
    return isinstance(value, str) and len(value) <= 256 and _TEXT_MEDIA.fullmatch(value) is not None


def _json_bytes(value):
    if not isinstance(value, dict):
        raise ValueError()
    _check_depth(value)
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError()
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif item is not None and type(item) not in {str, int, float, bool}:
            raise ValueError()
    try:
        normalized = MessageToDict(ParseDict(value, Value()))
        if normalized != value:
            raise ValueError()
    except ParseError:
        raise ValueError() from None
    return json.dumps(normalized, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


class ResponseArtifactService:
    def __init__(self, response_files_service):
        self.files = response_files_service

    def _validate(self, binding, manifests, *, task_id, run_id, limit_bytes):
        self.files._scope(binding, task_id, run_id, limit_bytes)
        refs, seen = [], set()
        try:
            if not isinstance(manifests, (list, tuple)) or len(manifests) > 64:
                raise ValueError()
            for manifest in manifests:
                if (not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS
                        or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
                        or not isinstance(manifest["artifact_id"], str)
                        or uuid.UUID(manifest["artifact_id"]).hex != manifest["artifact_id"]
                        or uuid.UUID(manifest["artifact_id"]).version != 5
                        or manifest["artifact_id"] in seen
                        or not isinstance(manifest["parts"], (list, tuple)) or not manifest["parts"]):
                    raise ValueError()
                _name(manifest["name"])
                seen.add(manifest["artifact_id"])
                for part in manifest["parts"]:
                    if (not isinstance(part, dict) or set(part) != {"type", "ref"}
                            or part["type"] not in {"text", "data", "file"}
                            or not isinstance(part["ref"], dict)):
                        raise ValueError()
                    media = part["ref"].get("media_type")
                    if ((part["type"] == "text" and not _text_media(media))
                            or (part["type"] == "data" and media != "application/json")):
                        raise ValueError()
                    refs.append(part["ref"])
                    if len(refs) > 256:
                        raise ValueError()
            pinned_limit = refs[0]["limit_bytes"] if refs else limit_bytes
            validated = self.files.validate_refs(binding, refs, task_id=task_id,
                                                 run_id=run_id, limit_bytes=pinned_limit)
        except (CoreError, ValueError, TypeError, KeyError, AttributeError, UnicodeError):
            raise CoreError("ARTIFACT_INTEGRITY_FAILED") from None
        return validated, pinned_limit

    def prepare(self, binding, artifacts, *, task_id, run_id, publication_id, limit_bytes, published=()):
        scope = self.files._scope(binding, task_id, run_id, limit_bytes)
        previous, pinned_limit = self._validate(binding, published, task_id=task_id,
                                                run_id=run_id, limit_bytes=limit_bytes)
        descriptors, paths, manifests = [], [], []
        try:
            if (not isinstance(publication_id, str) or not publication_id or "\0" in publication_id
                    or not isinstance(artifacts, (list, tuple)) or not artifacts
                    or len(published) + len(artifacts) > 64):
                raise ValueError()
            for index, artifact in enumerate(artifacts):
                if (not isinstance(artifact, dict) or not {"parts"} <= set(artifact) <= {"name", "parts"}
                        or not isinstance(artifact["parts"], (list, tuple)) or not artifact["parts"]):
                    raise ValueError()
                name = _name(artifact.get("name", ""))
                identifier = uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(
                    [scope["tenant_id"], run_id, publication_id, index], separators=(",", ":"))).hex
                manifest = {"schema_version": 1, "artifact_id": identifier, "name": name, "parts": []}
                for part_index, part in enumerate(artifact["parts"]):
                    if not isinstance(part, dict):
                        raise ValueError()
                    kind = part.get("type")
                    if kind == "text" and {"type", "text"} <= set(part) <= {"type", "text", "media_type"}:
                        media = part.get("media_type", "text/plain")
                        if not isinstance(part["text"], str) or not _text_media(media):
                            raise ValueError()
                        content = part["text"].encode("utf-8")
                        filename = f"part-{part_index + 1}.{'md' if media == 'text/markdown' else 'txt'}"
                        descriptor = (filename, content, media)
                    elif kind == "data" and set(part) == {"type", "data"}:
                        descriptor = (f"part-{part_index + 1}.json", _json_bytes(part["data"]), "application/json")
                    elif kind == "file" and set(part) == {"type", "path"} and isinstance(part["path"], str):
                        paths.append(part["path"])
                        descriptor = part["path"]
                    else:
                        raise ValueError()
                    descriptors.append(descriptor)
                    manifest["parts"].append({"type": kind})
                    if len(previous) + len(descriptors) > 256:
                        raise ValueError()
                manifests.append(manifest)
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, OverflowError):
            raise CoreError("INVALID_ARTIFACT_INPUT") from None

        used = sum(ref["size_bytes"] for ref in previous)
        used += sum(len(item[1]) for item in descriptors if isinstance(item, tuple))
        captured = dict(zip(dict.fromkeys(paths), self.files._capture(
            binding, list(dict.fromkeys(paths)), pinned_limit, used_bytes=used)))
        contents = []
        for descriptor in descriptors:
            if isinstance(descriptor, str):
                name, content = captured[descriptor]
                descriptor = (name, content, mimetypes.guess_type(name)[0] or "application/octet-stream")
            contents.append(descriptor)
        self.files._check_limit(sum(ref["size_bytes"] for ref in previous)
                                + sum(len(item[1]) for item in contents), pinned_limit)
        refs = iter(self.files.prepare_contents(binding, contents, task_id=task_id,
                                                run_id=run_id, limit_bytes=pinned_limit))
        for manifest in manifests:
            for part in manifest["parts"]:
                part["ref"] = next(refs)
        return tuple(manifests)

    @staticmethod
    def receipts(manifests):
        return tuple({"artifact_id": manifest["artifact_id"], "name": manifest["name"],
                      "parts": [{"type": part["type"], **{key: part["ref"][key] for key in _PUBLIC_PART_KEYS}}
                                for part in manifest["parts"]]} for manifest in manifests)

    def load(self, binding, manifests, *, task_id, run_id, limit_bytes, connection=None):
        refs, pinned_limit = self._validate(binding, manifests, task_id=task_id,
                                            run_id=run_id, limit_bytes=limit_bytes)
        loaded = iter(self.files.load(binding, refs, task_id=task_id, run_id=run_id,
                                      limit_bytes=pinned_limit, connection=connection))
        artifacts = []
        try:
            for manifest in manifests:
                parts = []
                for part in manifest["parts"]:
                    ref, content = next(loaded)
                    if part["type"] == "text":
                        parts.append(Part("text", content.decode("utf-8"), media_type=ref["media_type"]))
                    elif part["type"] == "data":
                        data = json.loads(content)
                        if _json_bytes(data) != content:
                            raise ValueError()
                        parts.append(Part("data", data, media_type="application/json"))
                    else:
                        parts.append(Part.file(content, filename=ref["name"], media_type=ref["media_type"]))
                artifacts.append(Artifact(manifest["artifact_id"], tuple(parts), 1,
                                          {"core_agent_artifact": dict(_MARKER)}, name=manifest["name"]))
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise CoreError("ARTIFACT_INTEGRITY_FAILED") from None
        return tuple(artifacts)
