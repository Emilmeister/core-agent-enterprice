"""Bounded chat-tree manifests and immutable blobs, independent of DB transactions.

The coordinator must fence the writer, confirm process quiescence, and commit the
returned manifest under the current chat revision/lease. Capture does not prove
process termination. Restore is exclusively for a fresh, uninitialized cache.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import uuid
from contextlib import contextmanager

from .errors import CoreError
from .workspace import ChatWorkspaces

_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_HEX = re.compile(r"[0-9a-f]{64}")


def _parts(path):
    try:
        return ChatWorkspaces.path_parts(path)
    except CoreError:
        raise CoreError("WORKSPACE_SNAPSHOT_INVALID") from None


def _link(path, target):
    try:
        target_size = len(target.encode("utf-8")) if isinstance(target, str) else 0
    except UnicodeError:
        raise CoreError("WORKSPACE_SNAPSHOT_INVALID") from None
    if (not isinstance(target, str) or not target or target_size > 4096
            or target.startswith("/") or "\\" in target
            or any(ord(char) < 32 or ord(char) == 127 for char in target)):
        raise CoreError("WORKSPACE_SNAPSHOT_INVALID")
    stack = _parts(path)[:-1]
    traversed = False
    for part in target.split("/"):
        if part == "..":
            # A named component may itself be a symlink. A later '..' could
            # escape despite a seemingly contained lexical normalization.
            if traversed or not stack:
                raise CoreError("WORKSPACE_SNAPSHOT_INVALID")
            stack.pop()
        elif part not in {"", "."}:
            traversed = True
            stack.append(part)


def validate_manifest(manifest, binding=None, *, max_bytes=1_000_000_000, max_files=10_000):
    """Validate every entry before any object read or destination mutation."""
    try:
        if (not isinstance(manifest, dict) or manifest.keys() != {"version", "scope", "entries"}
                or type(manifest["version"]) is not int or manifest["version"] != 1
                or not isinstance(manifest["scope"], str) or not _HEX.fullmatch(manifest["scope"])
                or binding is not None and manifest["scope"] != ChatWorkspaces._scope(binding)
                or not isinstance(manifest["entries"], dict)):
            raise CoreError("WORKSPACE_SNAPSHOT_INVALID")
        entries = manifest["entries"]
        if len(entries) > max_files:
            raise CoreError("WORKSPACE_SNAPSHOT_LIMIT")
        total = 0
        for path, entry in entries.items():
            parts = _parts(path)
            if len(parts) > ChatWorkspaces.depth_limit:
                raise CoreError("WORKSPACE_SNAPSHOT_LIMIT")
            if not isinstance(entry, dict) or entry.get("type") not in {"file", "directory", "symlink"}:
                raise CoreError("WORKSPACE_SNAPSHOT_INVALID")
            kind = entry["type"]
            keys = {"type", "mode", "mtime_ns"}
            keys |= {"sha256", "size"} if kind == "file" else {"target"} if kind == "symlink" else set()
            if (entry.keys() != keys or type(entry["mode"]) is not int or not 0 <= entry["mode"] <= 0o1777
                    or type(entry["mtime_ns"]) is not int or not -(2**63) <= entry["mtime_ns"] < 2**63):
                raise CoreError("WORKSPACE_SNAPSHOT_INVALID")
            for index in range(1, len(parts)):
                parent = entries.get("/".join(parts[:index]))
                if not isinstance(parent, dict) or parent.get("type") != "directory":
                    raise CoreError("WORKSPACE_SNAPSHOT_INVALID")
            if kind == "file":
                if (type(entry["size"]) is not int or entry["size"] < 0
                        or not isinstance(entry["sha256"], str) or not _HEX.fullmatch(entry["sha256"])):
                    raise CoreError("WORKSPACE_SNAPSHOT_INVALID")
                total += entry["size"]
                if total > max_bytes:
                    raise CoreError("WORKSPACE_SNAPSHOT_LIMIT")
            elif kind == "symlink":
                _link(path, entry["target"])
        return entries
    except (KeyError, TypeError, ValueError, UnicodeError, OverflowError):
        raise CoreError("WORKSPACE_SNAPSHOT_INVALID") from None


def prune_manifest(manifest, deleted_paths):
    """DB-only cleanup publication: removed paths cannot return on fresh restore."""
    entries = validate_manifest(manifest)
    paths = ["/".join(_parts(path)) for path in deleted_paths]
    return {**manifest, "entries": {
        path: dict(entry) for path, entry in entries.items()
        if not any(path == removed or path.startswith(removed + "/") for removed in paths)
    }}


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class WorkspaceDurability:
    def __init__(self, workspaces, blob_store, *, max_bytes=1_000_000_000, max_files=10_000):
        if type(max_bytes) is not int or max_bytes < 1 or type(max_files) is not int or max_files < 1:
            raise CoreError("CONFIG_INVALID")
        self.workspaces, self.blob_store = workspaces, blob_store
        self.max_bytes, self.max_files = max_bytes, max_files

    def _validate(self, binding, manifest):
        return validate_manifest(manifest, binding, max_bytes=self.max_bytes, max_files=self.max_files)

    def _scan(self, root, consume=None):
        identities, total = {"": _identity(os.fstat(root))}, 0

        def visit(directory, prefix):
            nonlocal total
            names = []
            with os.scandir(directory) as entries:
                for entry in entries:
                    names.append(entry.name)
                    if len(names) > self.max_files:
                        raise CoreError("WORKSPACE_SNAPSHOT_LIMIT")
            for name in sorted(names):
                path = "/".join([*prefix, name])
                parts = _parts(path)
                if len(parts) > self.workspaces.depth_limit or len(identities) > self.max_files:
                    raise CoreError("WORKSPACE_SNAPSHOT_LIMIT")
                before = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if before.st_mode & 0o6000:
                    raise CoreError("WORKSPACE_SNAPSHOT_INVALID")
                identities[path] = _identity(before)
                metadata = {"mode": stat.S_IMODE(before.st_mode), "mtime_ns": before.st_mtime_ns}
                if stat.S_ISDIR(before.st_mode):
                    child = self.workspaces._directory(directory, [name])
                    try:
                        if consume:
                            consume(path, {"type": "directory", **metadata}, None)
                        visit(child, parts)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
                    total += before.st_size
                    if total > self.max_bytes:
                        raise CoreError("WORKSPACE_SNAPSHOT_LIMIT")
                    if consume:
                        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                        try:
                            if _identity(os.fstat(descriptor)) != identities[path]:
                                raise CoreError("WORKSPACE_SNAPSHOT_CHANGED")
                            content = bytearray()
                            while len(content) <= before.st_size:
                                chunk = os.read(descriptor, min(1024 * 1024, before.st_size - len(content) + 1))
                                if not chunk:
                                    break
                                content.extend(chunk)
                            if len(content) != before.st_size or _identity(os.fstat(descriptor)) != identities[path]:
                                raise CoreError("WORKSPACE_SNAPSHOT_CHANGED")
                            consume(path, {"type": "file", **metadata, "size": len(content)}, bytes(content))
                        finally:
                            os.close(descriptor)
                elif stat.S_ISLNK(before.st_mode):
                    target = os.readlink(name, dir_fd=directory)
                    _link(path, target)
                    identities[path] += (target,)
                    if consume:
                        consume(path, {"type": "symlink", **metadata, "target": target}, None)
                else:
                    raise CoreError("WORKSPACE_SNAPSHOT_INVALID")

        visit(root, [])
        return identities

    def prepare(self, binding, previous_manifest=None):
        previous = self._validate(binding, previous_manifest) if previous_manifest is not None else {}
        known = {entry["sha256"] for entry in previous.values() if entry["type"] == "file"}
        manifest = {"version": 1, "scope": self.workspaces._scope(binding), "entries": {}}

        def consume(path, metadata, content):
            if content is not None:
                digest = hashlib.sha256(content).hexdigest()
                if digest not in known:
                    if self.blob_store.put(content) != digest:
                        raise CoreError("ARTIFACT_INTEGRITY_FAILED")
                    known.add(digest)
                metadata["sha256"] = digest
            manifest["entries"][path] = metadata

        try:
            with self.workspaces.open_workspace(binding) as root:
                if root is None:
                    if previous:
                        raise CoreError("WORKSPACE_UNAVAILABLE")
                    return manifest
                captured = self._scan(root, consume)
                if self._scan(root) != captured:
                    raise CoreError("WORKSPACE_SNAPSHOT_CHANGED")
            self._validate(binding, manifest)
            return manifest
        except OSError:
            raise CoreError("WORKSPACE_UNAVAILABLE") from None

    @contextmanager
    def _parent(self, binding):
        descriptor = os.open(self.workspaces.root, _DIRECTORY)
        try:
            for component in self.workspaces._components(binding)[:-1]:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(component, _DIRECTORY, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield descriptor
        finally:
            os.close(descriptor)

    @staticmethod
    def _empty(parent):
        try:
            descriptor = os.open("workspace", _DIRECTORY, dir_fd=parent)
        except FileNotFoundError:
            return
        except OSError:
            raise CoreError("WORKSPACE_RESTORE_CONFLICT") from None
        try:
            with os.scandir(descriptor) as entries:
                if next(entries, None) is not None:
                    raise CoreError("WORKSPACE_RESTORE_CONFLICT")
        finally:
            os.close(descriptor)

    def restore(self, binding, manifest):
        entries = self._validate(binding, manifest)
        stage_name = ".restoring-" + uuid.uuid4().hex
        try:
            with self._parent(binding) as parent:
                self._empty(parent)
                os.mkdir(stage_name, mode=0o700, dir_fd=parent)
                stage = os.open(stage_name, _DIRECTORY, dir_fd=parent)
                try:
                    for path, entry in sorted(entries.items(), key=lambda item: (item[0].count("/"), item[0])):
                        parts = _parts(path)
                        directory = self.workspaces._directory(stage, parts[:-1])
                        try:
                            if entry["type"] == "directory":
                                os.mkdir(parts[-1], mode=0o700, dir_fd=directory)
                            elif entry["type"] == "symlink":
                                os.symlink(entry["target"], parts[-1], dir_fd=directory)
                            else:
                                content = self.blob_store.get(entry["sha256"], entry["size"])
                                if (not isinstance(content, bytes) or len(content) != entry["size"]
                                        or hashlib.sha256(content).hexdigest() != entry["sha256"]):
                                    raise CoreError("ARTIFACT_INTEGRITY_FAILED")
                                descriptor = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                                     0o600, dir_fd=directory)
                                with os.fdopen(descriptor, "wb") as stream:
                                    stream.write(content)
                                    stream.flush()
                                    os.fsync(stream.fileno())
                        finally:
                            os.close(directory)
                    # Set deepest directory metadata last, after all child writes.
                    for path, entry in sorted(entries.items(), key=lambda item: item[0].count("/"), reverse=True):
                        parts = _parts(path)
                        directory = self.workspaces._directory(stage, parts[:-1])
                        try:
                            if entry["type"] != "symlink":
                                os.chmod(parts[-1], entry["mode"], dir_fd=directory, follow_symlinks=False)
                            elif os.stat(parts[-1], dir_fd=directory, follow_symlinks=False).st_mode & 0o7777 != entry["mode"]:
                                os.chmod(parts[-1], entry["mode"], dir_fd=directory, follow_symlinks=False)
                            os.utime(parts[-1], ns=(entry["mtime_ns"], entry["mtime_ns"]),
                                     dir_fd=directory, follow_symlinks=False)
                        finally:
                            os.close(directory)
                    os.fsync(stage)
                    self._empty(parent)
                    os.rename(stage_name, "workspace", src_dir_fd=parent, dst_dir_fd=parent)
                    os.fsync(parent)
                finally:
                    os.close(stage)
                    shutil.rmtree(stage_name, dir_fd=parent, ignore_errors=True)
        except OSError:
            raise CoreError("WORKSPACE_UNAVAILABLE") from None
