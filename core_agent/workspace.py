"""Persistent chat paths derived only from trusted workflow ownership."""
from __future__ import annotations

import hashlib
import base64
import binascii
import hmac
import json
import os
import secrets
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, localcontext
from pathlib import Path

from .errors import CoreError


@dataclass(frozen=True)
class WorkspaceBinding:
    tenant_id: str
    owner_id: str
    context_id: str

    def __post_init__(self):
        if any(
            not isinstance(value, str) or not value or "\0" in value
            for value in (self.tenant_id, self.owner_id, self.context_id)
        ):
            raise CoreError("WORKSPACE_SCOPE_REQUIRED")


class ChatWorkspaces:
    scan_limit = 10_000
    depth_limit = 64

    def __init__(self, root):
        # Resolve the deployment-selected root once, including macOS /var aliases.
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._cursor_key = secrets.token_bytes(32)

    @staticmethod
    def _components(binding):
        if not isinstance(binding, WorkspaceBinding):
            raise CoreError("WORKSPACE_SCOPE_REQUIRED")
        return ["chats", *(
            hashlib.sha256(value.encode("utf-8")).hexdigest()
            for value in (binding.tenant_id, binding.owner_id, binding.context_id)
        ), "workspace"]

    @contextmanager
    def open_workspace(self, binding, *, create=False):
        """Hold the scoped directory itself, including when its pathname is replaced."""
        components = self._components(binding)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = None
        try:
            descriptor = os.open(self.root, flags)
            for component in components:
                if create:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                child = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
        except FileNotFoundError as error:
            if descriptor is not None:
                os.close(descriptor)
                descriptor = None
            if create:
                raise CoreError("WORKSPACE_UNAVAILABLE") from error
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            raise CoreError("WORKSPACE_UNAVAILABLE") from error
        try:
            yield descriptor
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def workspace(self, binding):
        with self.open_workspace(binding, create=True):
            return self.root.joinpath(*self._components(binding))

    @staticmethod
    def path_parts(path, *, directory=False):
        try:
            if not isinstance(path, str) or len(path.encode("utf-8")) > 4096:
                raise ValueError()
            if directory and path == "":
                return []
            if (not path or "\\" in path or any(ord(char) < 32 or ord(char) == 127 for char in path)
                    or any(part in {"", ".", ".."} for part in path.split("/"))):
                raise ValueError()
            return path.split("/")
        except (ValueError, UnicodeError):
            raise CoreError("REQUEST_INVALID") from None

    @staticmethod
    def _control(parts):
        if len(parts) != 3 or parts[0] != "attachments":
            return False
        try:
            identifier = uuid.UUID(parts[1])
            batch = parts[1] in {identifier.hex, str(identifier)}
        except ValueError:
            return False
        return batch and (parts[2] == ".manifest.json" or parts[2].startswith(".manifest-"))

    @staticmethod
    def _directory(descriptor, parts):
        current = os.dup(descriptor)
        try:
            for part in parts:
                before = os.stat(part, dir_fd=current, follow_symlinks=False)
                if not stat.S_ISDIR(before.st_mode):
                    raise CoreError("FILE_NOT_FOUND")
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                actual = os.fstat(child)
                if (before.st_dev, before.st_ino) != (actual.st_dev, actual.st_ino):
                    os.close(child)
                    raise CoreError("WORKSPACE_UNAVAILABLE")
                os.close(current)
                current = child
            return current
        except BaseException:
            os.close(current)
            raise

    @staticmethod
    def _scope(binding):
        return hashlib.sha256(json.dumps([binding.tenant_id, binding.owner_id, binding.context_id],
            ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()

    def _cursor(self, payload):
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.digest(self._cursor_key, encoded, "sha256")
        return base64.urlsafe_b64encode(encoded).decode() + "." + base64.urlsafe_b64encode(signature).decode()

    def _decode_cursor(self, cursor, *, scope, directory, age, workspace_revision):
        try:
            if not isinstance(cursor, str) or not 0 < len(cursor) <= 16384:
                raise ValueError()
            encoded, signature = (base64.b64decode(part, altchars=b"-_", validate=True) for part in cursor.split("."))
            if not hmac.compare_digest(signature, hmac.digest(self._cursor_key, encoded, "sha256")):
                raise ValueError()
            value = json.loads(encoded)
            if (not isinstance(value, dict) or value.keys() != {"version", "scope", "directory", "age", "at", "after", "revision"}
                    or type(value["version"]) is not int or value["version"] != 2 or value["scope"] != scope
                    or type(value["revision"]) is not int or value["revision"] != workspace_revision
                    or value["directory"] != directory or value["age"] != age
                    or type(value["at"]) is not int or value["at"] < 0):
                raise ValueError()
            self.path_parts(value["after"])
            if directory and not value["after"].startswith(directory + "/"):
                raise ValueError()
            return value
        except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError):
            raise CoreError("REQUEST_INVALID") from None

    def identity(self, binding, path, info, workspace_revision=0):
        value = ["workspace-file-v2", self._scope(binding), workspace_revision, path, info.st_dev, info.st_ino,
                 info.st_ctime_ns, info.st_mtime_ns, info.st_size]
        return "v2:" + hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()

    def preview(self, binding, *, limit=50, directory="", older_than_days=None, cursor=None, workspace_revision=0):
        if type(workspace_revision) is not int or workspace_revision < 0:
            raise CoreError("REQUEST_INVALID")
        parts = self.path_parts(directory, directory=True)
        scope = self._scope(binding)
        with localcontext() as precision:
            # The API admits at most 64 decimal digits; retain sub-nanosecond boundaries.
            precision.prec = 100
            age = str(older_than_days.normalize()) if older_than_days is not None else None
        position = self._decode_cursor(cursor, scope=scope, directory=directory, age=age,
            workspace_revision=workspace_revision) if cursor is not None else {
            "version": 2, "scope": scope, "directory": directory, "age": age, "at": time.time_ns(), "after": None,
            "revision": workspace_revision}
        with localcontext() as precision:
            precision.prec = 100
            threshold = Decimal(position["at"]) - older_than_days * 86400 * 10**9 if older_than_days is not None else None
        values, visited = [], 0

        def visit(descriptor, prefix, depth):
            nonlocal visited
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    visited += 1
                    if visited > self.scan_limit:
                        raise CoreError("WORKSPACE_SCAN_LIMIT")
                    path = "/".join([*prefix, entry.name])
                    try:
                        names = self.path_parts(path)
                    except CoreError:
                        raise CoreError("WORKSPACE_UNAVAILABLE") from None
                    if self._control(names):
                        continue
                    info = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        if depth >= self.depth_limit:
                            raise CoreError("WORKSPACE_SCAN_LIMIT")
                        try:
                            child = self._directory(descriptor, [entry.name])
                        except CoreError:
                            raise CoreError("WORKSPACE_UNAVAILABLE") from None
                        try:
                            visit(child, names, depth + 1)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                        if (position["after"] is not None and path <= position["after"]
                                or threshold is not None and info.st_mtime_ns >= threshold):
                            continue
                        values.append({"name": entry.name, "path": path, "size": info.st_size, "mtime_ns": info.st_mtime_ns,
                            "identity_token": self.identity(binding, path, info, workspace_revision)})
        try:
            with self.open_workspace(binding) as root:
                if root is None:
                    if parts:
                        raise CoreError("FILE_NOT_FOUND")
                else:
                    try:
                        folder = self._directory(root, parts)
                    except FileNotFoundError:
                        raise CoreError("FILE_NOT_FOUND") from None
                    try:
                        visit(folder, parts, 0)
                    finally:
                        os.close(folder)
        except OSError:
            raise CoreError("WORKSPACE_UNAVAILABLE") from None
        values.sort(key=lambda item: item["path"])
        next_cursor = self._cursor({**position, "after": values[limit-1]["path"]}) if len(values) > limit else None
        return {"files": values[:limit], "next_cursor": next_cursor, "listed_at": position["at"] / 10**9}

    def open_file(self, binding, path):
        parts = self.path_parts(path)
        if self._control(parts):
            raise CoreError("FILE_NOT_FOUND")
        descriptor = None
        try:
            with self.open_workspace(binding) as root:
                if root is None:
                    raise CoreError("FILE_NOT_FOUND")
                folder = self._directory(root, parts[:-1])
                try:
                    before = os.stat(parts[-1], dir_fd=folder, follow_symlinks=False)
                    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                        raise CoreError("FILE_NOT_FOUND")
                    try:
                        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
                    except OSError:
                        # The entry existed at stat; its disappearance is a concurrent change.
                        raise CoreError("WORKSPACE_UNAVAILABLE") from None
                    actual = os.fstat(descriptor)
                    if not stat.S_ISREG(actual.st_mode) or actual.st_nlink != 1:
                        raise CoreError("FILE_NOT_FOUND")
                    if (before.st_dev, before.st_ino) != (actual.st_dev, actual.st_ino):
                        raise CoreError("WORKSPACE_UNAVAILABLE")
                    stream = os.fdopen(descriptor, "rb")
                    descriptor = None
                    return stream, actual.st_size
                finally:
                    os.close(folder)
        except FileNotFoundError:
            raise CoreError("FILE_NOT_FOUND") from None
        except OSError:
            raise CoreError("WORKSPACE_UNAVAILABLE") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
