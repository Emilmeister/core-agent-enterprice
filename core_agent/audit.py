from __future__ import annotations

import time
from dataclasses import dataclass

from .errors import CoreError


@dataclass(frozen=True)
class AuditRecord:
    run_id: str
    sequence: int
    kind: str
    data: dict
    written_at: float


class InMemoryAuditLog:
    def __init__(self):
        self._records = {}

    def append(self, run_id, kind, data, *, tenant_id="default"):
        records = self._records.setdefault(run_id, [])
        record = AuditRecord(run_id, len(records) + 1, kind, dict(data), time.time())
        records.append(record)
        return record

    def records(self, run_id):
        return tuple(self._records.get(run_id, ()))

    def replace(self, run_id, sequence, data):
        raise CoreError("AUDIT_IMMUTABLE")
