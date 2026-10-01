"""Immutable response snapshots; a trusted scoped manifest grants each read."""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import stat
import uuid
from contextlib import ExitStack
from urllib.parse import urlsplit

from .errors import CoreError
from .workspace import WorkspaceBinding


_PUBLIC_KEYS = ("file_id", "name", "media_type", "size_bytes", "sha256")
_SCOPE_KEYS = ("tenant_id", "owner_id", "context_id", "task_id", "run_id")
_REF_KEYS = frozenset(("schema_version", "limit_bytes", "blob_id", *_SCOPE_KEYS, *_PUBLIC_KEYS))


def _identity(stream):
    value = os.fstat(stream.fileno())
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise CoreError("FILE_CHANGED")
    return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


class ResponseFileService:
    def __init__(self, workspaces, artifact_store):
        self.workspaces = workspaces
        self.artifact_store = artifact_store

    @staticmethod
    def _scope(binding, task_id, run_id, limit_bytes):
        if (not isinstance(binding, WorkspaceBinding) or type(limit_bytes) is not int or not 1 <= limit_bytes <= 2147483647
                or any(not isinstance(value, str) or not value or "\0" in value for value in (task_id, run_id))):
            raise CoreError("CONFIG_INVALID")
        return dict(zip(_SCOPE_KEYS, (binding.tenant_id, binding.owner_id, binding.context_id, task_id, run_id)))

    @staticmethod
    def _check_limit(total, limit_bytes):
        if total > limit_bytes:
            raise CoreError("ATTACHMENTS_TOO_LARGE", data={"allowed_bytes": limit_bytes, "actual_bytes": total})

    def _parts(self, path):
        try:
            parts = self.workspaces.path_parts(path)
            if urlsplit(path).scheme:
                raise ValueError()
            return parts
        except (CoreError, ValueError):
            raise CoreError("INVALID_FILE_PATH") from None

    def prepare(self, binding, paths, *, task_id, run_id, limit_bytes):
        scope = self._scope(binding, task_id, run_id, limit_bytes)
        if not isinstance(paths, (list, tuple)):
            raise CoreError("INVALID_FILE_PATH")
        parts = [self._parts(path) for path in paths]
        if len(set(paths)) != len(paths):
            raise CoreError("INVALID_FILE_PATH")
        captured = []
        try:
            with ExitStack() as stack:
                sources = []
                for path, components in zip(paths, parts):
                    try:
                        stream, size = self.workspaces.open_file(binding, path)
                    except CoreError as error:
                        if error.code == "WORKSPACE_UNAVAILABLE":
                            raise CoreError("FILE_READ_FAILED") from None
                        raise
                    stack.enter_context(stream)
                    identity = _identity(stream)
                    if identity[4] != size:
                        raise CoreError("FILE_CHANGED")
                    sources.append((path, components[-1], stream, identity))
                self._check_limit(sum(item[3][4] for item in sources), limit_bytes)
                for path, name, stream, identity in sources:
                    content = stream.read(identity[4] + 1)
                    if len(content) != identity[4] or _identity(stream) != identity:
                        raise CoreError("FILE_CHANGED")
                    captured.append((name, content))
                # Later reads must not hide edits/replacements to an earlier file.
                for path, _, stream, identity in sources:
                    if _identity(stream) != identity:
                        raise CoreError("FILE_CHANGED")
                    try:
                        current, _ = self.workspaces.open_file(binding, path)
                        with current:
                            if _identity(current) != identity:
                                raise CoreError("FILE_CHANGED")
                    except CoreError:
                        raise CoreError("FILE_CHANGED") from None
        except OSError:
            raise CoreError("FILE_READ_FAILED") from None

        refs = []
        for name, content in captured:
            file_id = uuid.uuid4().hex
            media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            try:
                blob = self.artifact_store.put(binding.tenant_id, content, media_type=media_type,
                    provenance={**scope, "kind": "response_file", "file_id": file_id})
            except OSError:
                raise CoreError("FILE_READ_FAILED") from None
            refs.append({"schema_version": 1, "limit_bytes": limit_bytes, **scope, "file_id": file_id, "blob_id": blob.id,
                         "name": name, "media_type": media_type, "size_bytes": len(content),
                         "sha256": hashlib.sha256(content).hexdigest()})
        return tuple(refs)

    @staticmethod
    def receipts(refs):
        return tuple({key: ref[key] for key in _PUBLIC_KEYS} for ref in refs)

    def validate_refs(self, binding, refs, *, task_id, run_id, limit_bytes):
        scope = self._scope(binding, task_id, run_id, limit_bytes)
        if not isinstance(refs, (tuple, list)):
            raise CoreError("ARTIFACT_INTEGRITY_FAILED")
        validated = []
        seen = set()
        pinned_limit = None
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) != _REF_KEYS:
                raise CoreError("ARTIFACT_INTEGRITY_FAILED")
            ref = dict(ref)
            if any(ref[key] != value for key, value in scope.items()):
                raise CoreError("FILE_NOT_FOUND")
            try:
                if (type(ref["schema_version"]) is not int or ref["schema_version"] != 1
                        or type(ref["limit_bytes"]) is not int or not 1 <= ref["limit_bytes"] <= 2147483647
                        or limit_bytes > ref["limit_bytes"]
                        or (pinned_limit is not None and pinned_limit != ref["limit_bytes"])
                        or type(ref["size_bytes"]) is not int or ref["size_bytes"] < 0
                        or not isinstance(ref["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", ref["sha256"]) is None
                        or not isinstance(ref["blob_id"], str) or not ref["blob_id"] or "\0" in ref["blob_id"]
                        or not isinstance(ref["media_type"], str) or not ref["media_type"]
                        or not isinstance(ref["file_id"], str) or uuid.UUID(ref["file_id"]).hex != ref["file_id"]
                        or ref["file_id"] in seen or len(self.workspaces.path_parts(ref["name"])) != 1):
                    raise ValueError()
            except (CoreError, ValueError, AttributeError):
                raise CoreError("ARTIFACT_INTEGRITY_FAILED") from None
            validated.append(ref)
            seen.add(ref["file_id"])
            pinned_limit = ref["limit_bytes"]
        self._check_limit(sum(ref["size_bytes"] for ref in validated), limit_bytes)
        return tuple(validated)

    def load(self, binding, refs, *, task_id, run_id, limit_bytes, connection=None):
        validated = self.validate_refs(binding, refs, task_id=task_id, run_id=run_id, limit_bytes=limit_bytes)
        result = []
        for ref in validated:
            try:
                metadata, content = self.artifact_store.get(binding.tenant_id, ref["blob_id"],
                    **({"connection": connection} if connection is not None else {}))
            except (CoreError, OSError):
                raise CoreError("ARTIFACT_INTEGRITY_FAILED") from None
            if (metadata.id != ref["blob_id"] or metadata.tenant_id != binding.tenant_id
                    or metadata.size != ref["size_bytes"] or len(content) != ref["size_bytes"]
                    or metadata.digest != "sha256:" + ref["sha256"]
                    or hashlib.sha256(content).hexdigest() != ref["sha256"]):
                raise CoreError("ARTIFACT_INTEGRITY_FAILED")
            # Store IDs can deduplicate bytes and overwrite MIME/provenance. The
            # persisted scoped reference, never blob metadata, authorizes delivery.
            result.append((ref, bytes(content)))
        return tuple(result)
