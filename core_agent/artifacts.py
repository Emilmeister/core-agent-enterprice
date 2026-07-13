from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

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

    def get(self, tenant_id, artifact_id):
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
    def __init__(self, database, root, *, max_bytes=50_000_000):
        self.database = database
        self.root = Path(root).absolute() / "artifacts" / "blobs" / "sha256"
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = int(max_bytes)
        if self.max_bytes <= 0:
            raise CoreError("CONFIG_INVALID")

    def _blob(self, digest):
        return self.root / digest.removeprefix("sha256:")

    def put(self, tenant_id, content, *, media_type, provenance):
        content = bytes(content)
        if not content or len(content) > self.max_bytes:
            raise CoreError("ARTIFACT_TOO_LARGE")
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        provenance_bytes = json.dumps(
            dict(provenance), sort_keys=True, separators=(",", ":")
        ).encode()
        artifact_id = "sha256:" + hashlib.sha256(
            content + b"\0" + provenance_bytes
        ).hexdigest()
        blob = self._blob(digest)
        try:
            with blob.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            if hashlib.sha256(blob.read_bytes()).hexdigest() != digest.removeprefix(
                "sha256:"
            ):
                raise CoreError("ARTIFACT_INTEGRITY_FAILED") from None
        artifact = StoredArtifact(
            artifact_id,
            tenant_id,
            media_type,
            digest,
            len(content),
            dict(provenance),
        )
        with self.database.transaction() as connection:
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

    def get(self, tenant_id, artifact_id):
        with self.database.pool.connection() as connection:
            row = connection.execute(
                """SELECT id, tenant_id, media_type, digest, size, provenance
                   FROM core_artifacts
                   WHERE id = %s AND tenant_id = %s AND state = 'active'""",
                (artifact_id, tenant_id),
            ).fetchone()
        if not row:
            raise CoreError("NOT_FOUND")
        try:
            content = self._blob(row["digest"]).read_bytes()
        except OSError:
            raise CoreError("ARTIFACT_INTEGRITY_FAILED") from None
        if (
            len(content) != row["size"]
            or "sha256:" + hashlib.sha256(content).hexdigest() != row["digest"]
        ):
            raise CoreError("ARTIFACT_INTEGRITY_FAILED")
        return StoredArtifact(**row), content

    def delete(self, tenant_id, artifact_id):
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
        if not remaining:
            self._blob(row["digest"]).unlink(missing_ok=True)
