from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import OrderedDict
from contextlib import nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from threading import BoundedSemaphore, RLock

from psycopg.types.json import Jsonb

from .errors import CoreError


@dataclass(frozen=True)
class StoredArtifact:
    id: str
    tenant_id: str
    media_type: str
    digest: str
    size: int
    provenance: dict


class _StorageNotPrefetched(CoreError):
    def __init__(self, tenant_id, artifact_id):
        super().__init__("STORAGE_NOT_PREFETCHED", retryable=True)
        self.tenant_id = tenant_id
        self.artifact_id = artifact_id


class InMemoryArtifactStore:
    def __init__(self):
        self._metadata = {}
        self._content = {}

    def put(self, tenant_id, content, *, media_type, provenance):
        content = bytes(content)
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        artifact_id = digest
        key = (tenant_id, artifact_id)
        artifact = StoredArtifact(
            artifact_id, tenant_id, media_type, digest, len(content), dict(provenance)
        )
        self._metadata[key] = artifact
        self._content[key] = content
        return artifact

    def get(self, tenant_id, artifact_id, *, connection=None):
        try:
            return self._metadata[(tenant_id, artifact_id)], self._content[
                (tenant_id, artifact_id)
            ]
        except KeyError:
            raise CoreError("NOT_FOUND") from None

    def delete(self, tenant_id, artifact_id):
        key = (tenant_id, artifact_id)
        if key not in self._metadata:
            raise CoreError("NOT_FOUND")
        self._metadata.pop(key)
        self._content.pop(key)


class PostgresArtifactStore:
    def __init__(self, database, root, *, max_bytes=50_000_000, blob_store=None, cache_max_bytes=None):
        self.database = database
        self.root = Path(root).absolute() / "artifacts" / "blobs" / "sha256"
        self.blob_store = blob_store
        if blob_store is None:
            self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = int(max_bytes)
        if self.max_bytes <= 0:
            raise CoreError("CONFIG_INVALID")
        self.cache_max_bytes = self.max_bytes * 4 if cache_max_bytes is None else cache_max_bytes
        if type(self.cache_max_bytes) is not int or self.cache_max_bytes < self.max_bytes:
            raise CoreError("CONFIG_INVALID")
        self._cache = OrderedDict()
        self._cache_bytes = 0
        self._cache_lock = RLock()
        self._prefetched = ContextVar("artifact_prefetch", default=None)
        self._prefetch_slots = BoundedSemaphore(1)

    def _blob(self, digest):
        return self.root / digest.removeprefix("sha256:")

    def _cache_put(self, digest, content):
        with self._cache_lock:
            previous = self._cache.pop(digest, None)
            if previous is not None:
                self._cache_bytes -= len(previous)
            while self._cache and (self._cache_bytes + len(content) > self.cache_max_bytes or len(self._cache) >= 1024):
                self._cache_bytes -= len(self._cache.popitem(last=False)[1])
            self._cache[digest] = content
            self._cache_bytes += len(content)

    def _cache_get(self, digest):
        pinned = self._prefetched.get()
        if pinned is not None and digest in pinned:
            return pinned[digest]
        with self._cache_lock:
            content = self._cache.get(digest)
            if content is not None:
                self._cache.move_to_end(digest)
                self._pin_prefetched(digest, content)
            return content

    def _pin_prefetched(self, digest, content):
        pinned = self._prefetched.get()
        if pinned is not None and digest not in pinned:
            if len(pinned) >= 256 or sum(map(len, pinned.values())) + len(content) > self.cache_max_bytes:
                raise CoreError("STORAGE_LIMIT_EXCEEDED")
            pinned[digest] = content

    def run_prefetched(self, operation):
        """Retry a rolled-back projection with bounded, request-local verified bytes."""
        if self.blob_store is None or self._prefetched.get() is not None:
            return operation()
        if not self._prefetch_slots.acquire(timeout=30):
            raise CoreError("STORAGE_UNAVAILABLE", retryable=True)
        token = self._prefetched.set({})
        try:
            for _ in range(257):
                try:
                    return operation()
                except _StorageNotPrefetched as error:
                    metadata, content = self.get(error.tenant_id, error.artifact_id)
                    self._pin_prefetched(metadata.digest, content)
            raise CoreError("STORAGE_LIMIT_EXCEEDED")
        finally:
            self._prefetched.reset(token)
            self._prefetch_slots.release()

    def _write_blob(self, digest, content):
        if self.blob_store is not None:
            if self.blob_store.put(content) != digest[7:]:
                raise CoreError("ARTIFACT_INTEGRITY_FAILED")
            self._cache_put(digest, content)
            return
        blob = self._blob(digest)
        try:
            with blob.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            with blob.open("rb") as stream:
                existing = stream.read(len(content) + 1)
                if len(existing) != len(content) or hashlib.sha256(existing).hexdigest() != digest[7:]:
                    raise CoreError("ARTIFACT_INTEGRITY_FAILED")
                os.fsync(stream.fileno())
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def put(self, tenant_id, content, *, media_type, provenance):
        if self.blob_store is not None and tenant_id != self.blob_store.tenant_id:
            raise CoreError("NOT_FOUND")
        content = bytes(content)
        if len(content) > self.max_bytes:
            raise CoreError("ARTIFACT_TOO_LARGE")
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        provenance_bytes = json.dumps(
            dict(provenance), sort_keys=True, separators=(",", ":")
        ).encode()
        artifact_id = "sha256:" + hashlib.sha256(
            content + b"\0" + provenance_bytes
        ).hexdigest()
        self._write_blob(digest, content)
        artifact = StoredArtifact(
            artifact_id,
            tenant_id,
            media_type,
            digest,
            len(content),
            dict(provenance),
        )
        with self.database.transaction() as connection:
            if self.blob_store is not None and hasattr(self.blob_store, "pin"):
                self.blob_store.pin(connection, [digest[7:]])
            row = connection.execute(
                """INSERT INTO core_artifacts
                   (id, tenant_id, media_type, digest, size, provenance, state, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, 'active', %s)
                   ON CONFLICT (id, tenant_id) DO UPDATE SET
                     state = 'active', deleted_at = NULL
                   RETURNING media_type, digest, size, provenance""",
                (
                    artifact_id,
                    tenant_id,
                    media_type,
                    digest,
                    len(content),
                    Jsonb(dict(provenance)),
                    time.time(),
                ),
            ).fetchone()
        if (
            row["media_type"] != media_type
            or row["digest"] != digest
            or row["size"] != len(content)
            or json.dumps(row["provenance"], sort_keys=True)
            != json.dumps(dict(provenance), sort_keys=True)
        ):
            raise CoreError("ARTIFACT_CONFLICT")
        return artifact

    def get(self, tenant_id, artifact_id, *, connection=None):
        if self.blob_store is not None and tenant_id != self.blob_store.tenant_id:
            raise CoreError("NOT_FOUND")
        with (nullcontext(connection) if connection is not None else self.database.pool.connection()) as conn:
            row = conn.execute(
                """SELECT id, tenant_id, media_type, digest, size, provenance
                   FROM core_artifacts
                   WHERE id = %s AND tenant_id = %s AND state = 'active'""",
                (artifact_id, tenant_id),
            ).fetchone()
        if not row:
            raise CoreError("NOT_FOUND")
        if (type(row["size"]) is not int or not 0 <= row["size"] <= self.max_bytes
                or not isinstance(row["digest"], str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", row["digest"]) is None):
            raise CoreError("ARTIFACT_INTEGRITY_FAILED")
        if self.blob_store is not None:
            content = self._cache_get(row["digest"])
            if content is None:
                if connection is not None:
                    raise _StorageNotPrefetched(tenant_id, artifact_id)
                content = self.blob_store.get(row["digest"][7:], row["size"])
                self._cache_put(row["digest"], content)
        else:
            try:
                with self._blob(row["digest"]).open("rb") as stream:
                    content = stream.read(row["size"] + 1)
            except OSError:
                raise CoreError("ARTIFACT_INTEGRITY_FAILED") from None
        if (
            len(content) != row["size"]
            or "sha256:" + hashlib.sha256(content).hexdigest() != row["digest"]
        ):
            raise CoreError("ARTIFACT_INTEGRITY_FAILED")
        return StoredArtifact(**row), content

    def prefetch(self, tenant_id, artifact_ids):
        """Hydrate authorized immutable bytes before borrowing a DB connection."""
        return tuple(self.get(tenant_id, artifact_id) for artifact_id in artifact_ids)

    def delete(self, tenant_id, artifact_id):
        if self.blob_store is not None and tenant_id != self.blob_store.tenant_id:
            raise CoreError("NOT_FOUND")
        with self.database.transaction() as connection:
            row = connection.execute(
                """UPDATE core_artifacts SET state = 'deleted', deleted_at = %s
                   WHERE id = %s AND tenant_id = %s AND state = 'active'
                   RETURNING digest""",
                (time.time(), artifact_id, tenant_id),
            ).fetchone()
            if not row:
                raise CoreError("NOT_FOUND")
            remaining = connection.execute(
                """SELECT 1 FROM core_artifacts
                   WHERE digest = %s AND state = 'active' LIMIT 1""",
                (row["digest"],),
            ).fetchone()
        if not remaining and self.blob_store is None:
            self._blob(row["digest"]).unlink(missing_ok=True)

    def purge_unreferenced(self, digests):
        if self.blob_store is not None:
            # PostgreSQL staging/reference GC owns S3 deletion and backup retention.
            return
        for digest in set(digests):
            with self.database.pool.connection() as connection:
                active = connection.execute(
                    """SELECT 1 FROM core_artifacts
                       WHERE digest = %s AND state = 'active' LIMIT 1""",
                    (digest,),
                ).fetchone()
            if not active:
                self._blob(digest).unlink(missing_ok=True)
