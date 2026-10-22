"""PostgreSQL staging leases and fenced collection of immutable S3 generations."""

from __future__ import annotations

import hashlib
import logging
import math
import time
import uuid

from .errors import CoreError

_LOG = logging.getLogger(__name__)


class PostgresS3BlobStore:
    def __init__(self, database, transport, *, retention_seconds=86400, stage_seconds=300, clock=time.time):
        if (not math.isfinite(retention_seconds) or retention_seconds < 86400
                or not math.isfinite(stage_seconds) or not 120 <= stage_seconds <= 3600):
            raise CoreError("CONFIG_INVALID", "Invalid S3 retention or staging lease")
        self.database, self.transport = database, transport
        self.tenant_id, self.max_bytes = transport.tenant_id, transport.max_bytes
        self.retention_seconds, self.stage_seconds, self.clock = retention_seconds, stage_seconds, clock
        self.last_sweep_scanned = 0

    def _lock(self, connection, digest):
        key = hashlib.sha256(("s3:" + self.tenant_id + ":" + digest).encode()).digest()[:8]
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (int.from_bytes(key, signed=True),))

    def put(self, content):
        if not isinstance(content, bytes) or len(content) > self.max_bytes:
            raise CoreError("ARTIFACT_TOO_LARGE")
        digest, now = hashlib.sha256(content).hexdigest(), self.clock()
        with self.database.transaction() as connection:
            self._lock(connection, digest)
            row = connection.execute("""SELECT generation, size FROM core_s3_blobs
                WHERE tenant_id = %s AND digest = %s AND state IN ('staging','live') FOR UPDATE""",
                (self.tenant_id, digest)).fetchone()
            if row is None:
                generation = uuid.uuid4().hex
                connection.execute("""INSERT INTO core_s3_blobs
                    (tenant_id,digest,size,state,generation,stage_until,unreferenced_at,updated_at)
                    VALUES (%s,%s,%s,'staging',%s,%s,%s,%s)""",
                    (self.tenant_id, digest, len(content), generation, now + self.stage_seconds, now, now))
            else:
                if row["size"] != len(content):
                    raise CoreError("ARTIFACT_INTEGRITY_FAILED")
                generation = row["generation"]
                connection.execute("""UPDATE core_s3_blobs SET stage_until = GREATEST(stage_until,%s), updated_at = %s
                    WHERE tenant_id = %s AND digest = %s AND generation = %s""",
                    (now + self.stage_seconds, now, self.tenant_id, digest, generation))
        if self.transport.put(content, object_id=generation) != digest:
            raise CoreError("ARTIFACT_INTEGRITY_FAILED")
        now = self.clock()
        with self.database.transaction() as connection:
            row = connection.execute("""UPDATE core_s3_blobs SET state = 'live', updated_at = %s
                WHERE tenant_id = %s AND digest = %s AND generation = %s
                  AND state IN ('staging','live') AND stage_until > %s RETURNING digest""",
                (now, self.tenant_id, digest, generation, now)).fetchone()
            if row is None:
                raise CoreError("STORAGE_UNAVAILABLE", retryable=True)
        return digest

    def get(self, digest, size):
        self.transport.key(digest)
        with self.database.pool.connection() as connection:
            row = connection.execute("""SELECT generation, size FROM core_s3_blobs
                WHERE tenant_id = %s AND digest = %s AND state = 'live'""",
                (self.tenant_id, digest)).fetchone()
        if row is None:
            raise CoreError("NOT_FOUND")
        if type(size) is not int or row["size"] != size:
            raise CoreError("ARTIFACT_INTEGRITY_FAILED")
        return self.transport.get(digest, size, object_id=row["generation"])

    def pin(self, connection, digests):
        """Call in the same transaction that publishes the authorized reference."""
        digests = sorted(set(digests))
        if len(digests) > 10_000:
            raise CoreError("STORAGE_LIMIT_EXCEEDED")
        for digest in digests:
            self.transport.key(digest)
        if not digests:
            return
        rows = connection.execute("""SELECT digest FROM core_s3_blobs
            WHERE tenant_id = %s AND digest = ANY(%s) AND state = 'live'
            ORDER BY digest FOR UPDATE""", (self.tenant_id, digests)).fetchall()
        if {row["digest"] for row in rows} != set(digests):
            raise CoreError("STORAGE_UNAVAILABLE", retryable=True)
        connection.execute("""UPDATE core_s3_blobs SET unreferenced_at = NULL, updated_at = %s
            WHERE tenant_id = %s AND digest = ANY(%s) AND state = 'live'""",
            (self.clock(), self.tenant_id, digests))

    def delete(self, digest):
        """Queue an unreferenced candidate; only sweep may issue a remote DELETE."""
        self.transport.key(digest)
        with self.database.transaction() as connection:
            connection.execute("""UPDATE core_s3_blobs SET unreferenced_at = COALESCE(unreferenced_at,%s)
                WHERE tenant_id = %s AND digest = %s AND state IN ('staging','live')""",
                (self.clock(), self.tenant_id, digest))

    def _claim(self, digest, generation, now):
        with self.database.transaction() as connection:
            row = connection.execute("""SELECT state,stage_until,unreferenced_at,updated_at FROM core_s3_blobs
                WHERE tenant_id = %s AND digest = %s AND generation = %s FOR UPDATE SKIP LOCKED""",
                (self.tenant_id, digest, generation)).fetchone()
            if row is None or row["stage_until"] > now:
                return False
            if row["state"] == "deleted" and row["updated_at"] > now - self.retention_seconds:
                return False
            if row["state"] in {"staging", "live"}:
                referenced = connection.execute("""SELECT
                    EXISTS(SELECT 1 FROM core_artifacts WHERE tenant_id = %s
                           AND digest = %s AND state = 'active') OR
                    EXISTS(SELECT 1 FROM core_workspace_manifests,
                           LATERAL jsonb_each(manifest->'entries') AS entry
                           WHERE tenant_id = %s AND entry.value->>'type' = 'file'
                             AND entry.value->>'sha256' = %s) OR
                    EXISTS(SELECT 1 FROM core_chat_file_batches,
                           LATERAL jsonb_array_elements(manifest->'entries') AS entry
                           WHERE tenant_id = %s AND manifest->'storage' = '{"version":1,"backend":"s3"}'::jsonb
                             AND entry.value->>'sha256' = %s
                             AND (state IN ('accepted_quarantine','accepted_ready','published','excluded')
                               OR (cleaned_at IS NULL AND (state = 'rejected'
                                 OR (state = 'staging' AND lease_expires_at > %s))))) AS present""",
                    (self.tenant_id, "sha256:" + digest, self.tenant_id, digest,
                     self.tenant_id, digest, now)).fetchone()["present"]
                if referenced:
                    connection.execute("""UPDATE core_s3_blobs SET unreferenced_at = NULL, updated_at = %s
                        WHERE tenant_id = %s AND digest = %s AND generation = %s""",
                        (now, self.tenant_id, digest, generation))
                    return False
                since = row["unreferenced_at"]
                if since is None or since > now - self.retention_seconds:
                    connection.execute("""UPDATE core_s3_blobs
                        SET unreferenced_at = COALESCE(unreferenced_at,%s), updated_at = %s
                        WHERE tenant_id = %s AND digest = %s AND generation = %s""",
                        (now, now, self.tenant_id, digest, generation))
                    return False
            connection.execute("""UPDATE core_s3_blobs SET state = 'deleting', updated_at = %s
                WHERE tenant_id = %s AND digest = %s AND generation = %s""",
                (now, self.tenant_id, digest, generation))
            return True

    def sweep(self, *, limit=100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise CoreError("CONFIG_INVALID", "S3 GC limit must be between 1 and 100")
        now = self.clock()
        with self.database.pool.connection() as connection:
            rows = connection.execute("""SELECT digest,generation FROM core_s3_blobs
                WHERE tenant_id = %s AND stage_until <= %s
                  AND (state != 'deleted' OR updated_at <= %s)
                ORDER BY updated_at,digest,generation LIMIT %s""",
                (self.tenant_id, now, now - self.retention_seconds, limit)).fetchall()
        self.last_sweep_scanned = len(rows)
        deleted = 0
        for row in rows:
            if not self._claim(row["digest"], row["generation"], now):
                continue
            try:
                self.transport.delete(row["digest"], object_id=row["generation"])
            except CoreError as error:
                _LOG.warning("storage.s3", extra={"operation": "gc", "outcome": "retry", "code": error.code})
                continue
            with self.database.transaction() as connection:
                connection.execute("""UPDATE core_s3_blobs SET state = 'deleted', updated_at = %s
                    WHERE tenant_id = %s AND digest = %s AND generation = %s AND state = 'deleting'""",
                    (self.clock(), self.tenant_id, row["digest"], row["generation"]))
            deleted += 1
        # Tombstones remain bounded-work candidates so a delayed old PUT is collected
        # without allowing an old DELETE to affect the next immutable generation.
        return deleted

    def close(self):
        self.transport.close()

    def check(self):
        self.transport.check()
