from __future__ import annotations

import hashlib
from dataclasses import dataclass

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
