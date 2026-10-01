"""Private material decisions, charged before detector I/O and bound to workflow waits.

Callers supply authenticated workflow scope. Neither review IDs nor detector
responses authorize access. Runtime publication/gating is a separate integration.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from contextlib import contextmanager, nullcontext
from copy import deepcopy

from psycopg.types.json import Jsonb

from .errors import CoreError
from .guardrails import Classification
from .workflow import TERMINAL_STATES


_PRIVATE = {"payload", "sealed_ref", "completed_result_ref", "attempt_token"}
_REASONS = {"clear", "suspicious", "uncertain", "invalid_response", "provider_error",
            "timeout", "budget_exhausted", "detector_busy", "incomplete_extraction", "interrupted"}


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise CoreError("MATERIAL_REVIEW_INVALID") from None


def _public(row):
    return deepcopy({key: value for key, value in row.items() if key not in _PRIVATE})


def _live(current):
    if current.cancel_requested:
        raise CoreError("CANCEL_REQUESTED")
    if current.state in TERMINAL_STATES or current.snapshot.get("terminal_intent"):
        raise CoreError("INVALID_TASK_STATE")


def _lease(token):
    if not isinstance(token, str) or not token:
        raise CoreError("LEASE_LOST")


def _identity(record):
    values = tuple(getattr(record, key) for key in ("tenant_id", "run_id", "owner_id", "context_id", "task_id"))
    if any(not isinstance(value, str) or not value for value in values):
        raise CoreError("MATERIAL_REVIEW_NOT_FOUND")
    return values


class _MaterialReviews:
    def denied_material(self, record, material_digest, *, lease_token):
        """Compatibility API for canonical JSON material identities."""
        decision = self.negative_decision(record, material_digest, lease_token=lease_token)
        return decision["state"] if decision is not None else None

    def negative_decision(self, record, material_digest, *, lease_token, material_kind="json", text_digest=None, connection=None):
        """Return the actual immutable negative decision across sources/runs.

        Raw-file hashes are distinct from JSON hashes. A fully decoded UTF-8 file may
        additionally carry exactly its canonical JSON text identity; no fuzzy,
        substring, encoding, or normalization equivalence is inferred.
        """
        _lease(lease_token)
        if material_kind not in {"json", "file_sha256"} or any(
            not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest)
            for digest in ([material_digest] + ([text_digest] if text_digest is not None else []))
        ):
            raise CoreError("MATERIAL_REVIEW_INVALID")
        with self._scope(record, lease_token=lease_token, connection=connection) as (current, conn):
            _live(current)
            return self._negative_decision(current, material_digest, conn,
                                           material_kind=material_kind, text_digest=text_digest)

    def _negative_decision(self, current, material_digest, conn, *, material_kind="json", text_digest=None):
        identities = [(material_digest, material_kind)]
        if text_digest is not None:
            identities.append((text_digest, "json"))
        for digest, kind in identities:
            for row in self._negative_candidates(current, digest, kind, conn):
                outcome = row["outcome"]
                if outcome is not None:
                    reason = outcome.get("reason")
                    if reason == "allowed":
                        continue
                    state = "timed_out" if reason == "timeout" else "rejected"
                elif row["state"] in {"rejected", "timed_out"}:
                    state = row["state"]
                elif row["deadline"] is not None and row["deadline"] <= self._now(conn):
                    state = "timed_out"
                else:
                    continue
                return {"state": state, "review_id": row["review_id"]}
        return None

    def create(self, record, *, source_id, source_kind, deadline, lease_token,
               payload=None, sealed_ref=None, content_digest=None, completed_result_ref=None,
               max_calls=32, max_input_tokens=100000, connection=None):
        _lease(lease_token)
        if (not isinstance(source_id, str) or not source_id
                or not isinstance(source_kind, str) or not 1 <= len(source_kind) <= 64
                or (payload is None) == (sealed_ref is None)
                or any(type(n) is not int or not 1 <= n < 2**63 for n in (max_calls, max_input_tokens))
                or isinstance(deadline, bool) or not isinstance(deadline, (int, float))
                or not math.isfinite(deadline)):
            raise CoreError("MATERIAL_REVIEW_INVALID")
        if payload is not None:
            encoded = _json(payload)
            payload = json.loads(encoded)
            digest = hashlib.sha256(encoded).hexdigest()
            if content_digest is not None and digest != content_digest:
                raise CoreError("MATERIAL_REVIEW_CONFLICT")
            content_digest = digest
        elif (not isinstance(sealed_ref, dict) or set(sealed_ref) != {"batch_id", "index"}
              or not isinstance(sealed_ref["batch_id"], str) or not sealed_ref["batch_id"]
              or type(sealed_ref["index"]) is not int or sealed_ref["index"] < 0):
            # Logical references only; private filesystem paths never cross this API.
            raise CoreError("MATERIAL_REVIEW_INVALID")
        if not isinstance(content_digest, str) or not re.fullmatch("[0-9a-f]{64}", content_digest):
            raise CoreError("MATERIAL_REVIEW_INVALID")
        completed_result_ref = json.loads(_json(completed_result_ref))
        with self._scope(record, lease_token=lease_token, connection=connection) as (current, conn):
            _live(current)
            existing = self._find(current, source_id, content_digest, conn)
            if existing is not None:
                if any(existing[key] != value for key, value in (
                    ("source_kind", source_kind), ("payload", payload), ("sealed_ref", sealed_ref),
                    ("completed_result_ref", completed_result_ref),
                )):
                    raise CoreError("MATERIAL_REVIEW_CONFLICT")
                return _public(self._refresh(current, existing, conn))
            row = dict(review_id=str(uuid.uuid4()), schema_version=1, tenant_id=current.tenant_id,
                       run_id=current.run_id, owner_id=current.owner_id, context_id=current.context_id,
                       source_id=source_id, source_kind=source_kind, content_digest=content_digest,
                       payload=deepcopy(payload), sealed_ref=deepcopy(sealed_ref),
                       completed_result_ref=deepcopy(completed_result_ref), state="checking",
                       deadline=deadline, max_calls=max_calls, max_input_tokens=max_input_tokens,
                       attempts_used=0, input_tokens_used=0, classification=None, wait_id=None,
                       attempt_token=None, revision=1, created_at=self._now(conn))
            self._insert(row, conn)
            return _public(row)

    def _review(self, current, review_id, conn):
        row = self._load(current, review_id, conn)
        if row is None or (row["tenant_id"], row["run_id"], row["owner_id"], row["context_id"]) != (
            current.tenant_id, current.run_id, current.owner_id, current.context_id
        ):
            raise CoreError("MATERIAL_REVIEW_NOT_FOUND")
        return row

    def _refresh(self, current, row, conn):
        if row["wait_id"]:
            wait = self._wait(row["wait_id"], current, conn)
            if (wait.kind != "guardrail" or wait.run_id != row["run_id"]
                    or wait.source_id != row["review_id"] or wait.subject != self._subject(row)):
                raise CoreError("MATERIAL_REVIEW_CONFLICT")
            if wait.outcome is None and (current.cancel_requested or current.state in TERMINAL_STATES
                                        or wait.deadline <= self._now(conn)):
                wait = self._resolve(current, wait, conn)
            if wait.outcome is not None:
                reason = wait.outcome.get("reason")
                state = {"allowed": "allowed", "timeout": "timed_out"}.get(reason, "rejected")
                if row["state"] == "pending":
                    row["state"] = state
                    self._save(row, conn)
                elif row["state"] != state:
                    raise CoreError("MATERIAL_REVIEW_CONFLICT")
        return row

    @staticmethod
    def _subject(row):
        subject = {key: row[key] for key in ("review_id", "source_id", "source_kind", "content_digest")} | {
            "reason": row["classification"]["reason"]}
        if row["source_kind"] == "file_attachment" and row["sealed_ref"] is not None:
            subject.update(affected_scope="file_batch", batch_id=row["sealed_ref"]["batch_id"])
        return subject

    def visibility_reference(self, record, review_id, *, lease_token, connection=None, source_run_id=None):
        """Digest identities and decision revision only; never return quarantined bytes."""
        _lease(lease_token)
        with self._scope(record, lease_token=lease_token, connection=connection) as (current, conn):
            _live(current)
            if source_run_id is None or source_run_id == current.run_id:
                row = self._refresh(current, self._review(current, review_id, conn), conn)
            else:
                source = self.workflow.get(source_run_id, tenant_id=current.tenant_id,
                    owner_id=current.owner_id, connection=conn)
                if (source.context_id != current.context_id or source.parent_run_id is not None
                        or source.state not in TERMINAL_STATES):
                    raise CoreError("CHECKPOINT_INVALID")
                row = self._load(source, review_id, conn, lock=False)
                if row is None or (row["tenant_id"], row["run_id"], row["owner_id"], row["context_id"]) != (
                        current.tenant_id, source.run_id, current.owner_id, current.context_id):
                    raise CoreError("CHECKPOINT_INVALID")
                if row["wait_id"]:
                    wait = self.workflow.get_wait(row["wait_id"], tenant_id=current.tenant_id,
                        owner_id=current.owner_id, **({"connection": conn} if conn is not None else {}))
                    if (wait.kind != "guardrail" or wait.run_id != source.run_id
                            or wait.source_id != review_id or wait.subject != self._subject(row)):
                        raise CoreError("CHECKPOINT_INVALID")
                    reason = (wait.outcome or {}).get("reason")
                    row["state"] = {"allowed": "allowed", "timeout": "timed_out"}.get(reason, "rejected")
            reference = row["completed_result_ref"] or {}
            return {**deepcopy({key: row[key] for key in
                ("review_id", "source_id", "source_kind", "state", "revision")}),
                "completed_result_ref": {key: reference[key] for key in
                    ("material_digest", "material_kind", "text_digest") if key in reference}}

    def get(self, record, review_id, *, connection=None):
        with self._scope(record, connection=connection) as (current, conn):
            return _public(self._refresh(current, self._review(current, review_id, conn), conn))

    def owner_read_payload(self, record, review_id, *, connection=None):
        """Trusted owner API must authorize the company role before calling this."""
        with self._scope(record, connection=connection) as (current, conn):
            row = self._review(current, review_id, conn)
            return deepcopy({key: row[key] for key in ("payload", "sealed_ref", "completed_result_ref")})

    def read_payload(self, record, review_id, *, lease_token, connection=None):
        _lease(lease_token)
        with self._scope(record, lease_token=lease_token, connection=connection) as (current, conn):
            _live(current)
            row = self._refresh(current, self._review(current, review_id, conn), conn)
            reference = row["completed_result_ref"] or {}
            material_digest = reference.get("material_digest")
            allowed = row["state"] in {"clear", "allowed"} and (material_digest is None or
                self._negative_decision(current, material_digest, conn,
                    material_kind=reference.get("material_kind", "json"), text_digest=reference.get("text_digest")) is None)
            value = deepcopy({key: row[key] for key in ("payload", "sealed_ref", "completed_result_ref")})
        if not allowed:
            raise CoreError("MATERIAL_REVIEW_REQUIRED")
        return value

    def record_attempt(self, record, review_id, *, lease_token, attempt_token, input_tokens):
        _lease(lease_token)
        if type(input_tokens) is not int or input_tokens < 0:
            raise CoreError("MATERIAL_REVIEW_INVALID")
        with self._scope(record, lease_token=lease_token) as (current, conn):
            _live(current)
            row = self._review(current, review_id, conn)
            if row["state"] != "checking" or not attempt_token or row["attempt_token"] != attempt_token:
                raise CoreError("MATERIAL_REVIEW_CONFLICT")
            if self._now(conn) >= row["deadline"]:
                raise CoreError("MATERIAL_REVIEW_DEADLINE")
            if (row["attempts_used"] >= row["max_calls"]
                    or row["input_tokens_used"] + input_tokens > row["max_input_tokens"]):
                raise CoreError("MATERIAL_REVIEW_BUDGET")
            row["attempts_used"] += 1
            row["input_tokens_used"] += input_tokens
            self._save(row, conn)

    def finish(self, record, review_id, result, *, lease_token, attempt_token,
               continuation, snapshot, owner_timeout_seconds=86400, connection=None, before_pending=None,
               _clear_fenced=False):
        _lease(lease_token)
        if (not isinstance(result, Classification) or result.verdict not in {"clear", "suspicious", "unverified"}
                or result.reason not in _REASONS
                or (result.verdict == "clear") != (result.reason == "clear")
                or any(type(n) is not int or n < 0 for n in
                       (result.calls, result.input_tokens, result.prompt_tokens, result.completion_tokens))
                or isinstance(result.elapsed_seconds, bool) or not isinstance(result.elapsed_seconds, (int, float))
                or not math.isfinite(result.elapsed_seconds) or result.elapsed_seconds < 0
                or type(owner_timeout_seconds) is not int or not 1 <= owner_timeout_seconds <= 2147483647):
            raise CoreError("MATERIAL_REVIEW_INVALID")
        if before_pending is not None:
            # Commit a clear result under the same deadline fence, or release the
            # transaction before stopping a live interpreter and creating its wait.
            with self._scope(record, lease_token=lease_token, connection=connection) as (current, conn):
                row = self._review(current, review_id, conn)
                if row["state"] != "checking" or (result.verdict == "clear" and row["attempts_used"]
                                                   and self._now(conn) < row["deadline"]):
                    return self.finish(record, review_id, result, lease_token=lease_token, attempt_token=attempt_token,
                        continuation=continuation, snapshot=snapshot, owner_timeout_seconds=owner_timeout_seconds,
                        connection=conn, _clear_fenced=True)
                if result.verdict == "clear":
                    result = Classification("unverified", "timeout", result.calls, result.input_tokens,
                                            result.prompt_tokens, result.completion_tokens, result.elapsed_seconds)
            snapshot, continuation = before_pending()
        with self._scope(record, lease_token=lease_token, connection=connection) as (current, conn):
            _live(current)
            row = self._review(current, review_id, conn)
            if row["state"] != "checking":
                return _public(self._refresh(current, row, conn))
            if not attempt_token or row["attempt_token"] != attempt_token:
                raise CoreError("MATERIAL_REVIEW_CONFLICT")
            if not _clear_fenced and result.verdict == "clear" and (not row["attempts_used"] or self._now(conn) >= row["deadline"]):
                result = Classification("unverified", "timeout", result.calls, result.input_tokens,
                                        result.prompt_tokens, result.completion_tokens, result.elapsed_seconds)
            row["classification"] = vars(result).copy()
            row["state"] = "clear" if result.verdict == "clear" else "pending"
            if row["state"] == "pending":
                wait = self.workflow.enter_wait(
                    current, kind="guardrail", source_id=row["review_id"], subject=self._subject(row),
                    continuation=continuation, deadline=self._now(conn) + owner_timeout_seconds,
                    snapshot=snapshot, lease_token=lease_token, connection=conn,
                )
                row["wait_id"] = wait.wait_id
            self._save(row, conn)
            return _public(row)

    def classify(self, record, review_id, classifier, *, lease_token, continuation, snapshot,
                 owner_timeout_seconds=86400, documents=None, complete=True, before_pending=None):
        _lease(lease_token)
        with self._scope(record, lease_token=lease_token) as (current, conn):
            _live(current)
            row = self._review(current, review_id, conn)
            if row["state"] != "checking":
                return _public(self._refresh(current, row, conn))
            interrupted = row["attempt_token"] is not None
            if not interrupted:
                row["attempt_token"] = str(uuid.uuid4())
                self._save(row, conn)
            attempt_token = row["attempt_token"]
        if interrupted:
            result = Classification("unverified", "interrupted", 0, 0, 0, 0, 0)
        else:
            if row["payload"] is not None:
                documents = [row["payload"] if isinstance(row["payload"], str) else _json(row["payload"]).decode()]
            result = classifier.classify(
                documents, source_kind=row["source_kind"], complete=complete,
                attempts_used=row["attempts_used"], input_tokens_used=row["input_tokens_used"],
                max_calls=row["max_calls"], max_input_tokens=row["max_input_tokens"],
                deadline=row["deadline"], record_attempt=lambda cost: self.record_attempt(
                    record, review_id, lease_token=lease_token, attempt_token=attempt_token, input_tokens=cost),
            )
        return self.finish(record, review_id, result, lease_token=lease_token, attempt_token=attempt_token,
                           continuation=continuation, snapshot=snapshot, owner_timeout_seconds=owner_timeout_seconds,
                           before_pending=before_pending)


class MemoryMaterialReviewStore(_MaterialReviews):
    """Test adapter: one workflow lock mirrors run -> review -> wait transactions."""
    def __init__(self, workflow_store):
        self.workflow = workflow_store
        self.rows = {}

    @contextmanager
    def _scope(self, record, *, lease_token=None, connection=None):
        identity = _identity(record)
        with self.workflow._execution_lock(record, lease_token) as (current, _):
            if _identity(current) != identity:
                raise CoreError("MATERIAL_REVIEW_NOT_FOUND")
            if lease_token == "":
                raise CoreError("LEASE_LOST")
            # Roll back the only cross-store mutation (enter_wait) if persistence fails.
            rows = deepcopy(self.rows)
            records, waits, leases = (deepcopy(getattr(self.workflow, name))
                                     for name in ("_records", "_waits", "_leases"))
            try:
                yield current, None
            except BaseException:
                self.rows = rows
                self.workflow._records, self.workflow._waits, self.workflow._leases = records, waits, leases
                raise

    def _now(self, conn):
        return self.workflow.current_time()

    def _negative_candidates(self, current, digest, kind, conn):
        for row in self.rows.values():
            reference = row.get("completed_result_ref") or {}
            matches = ((reference.get("material_kind", "json") == kind and reference.get("material_digest") == digest)
                       or (kind == "json" and reference.get("text_digest") == digest))
            if ((row["tenant_id"], row["owner_id"], row["context_id"]) !=
                    (current.tenant_id, current.owner_id, current.context_id)
                    or not matches
                    or row["state"] not in {"pending", "rejected", "timed_out"}):
                continue
            wait = self.workflow._waits.get(row["wait_id"])
            yield {"review_id": row["review_id"], "state": row["state"], "outcome": wait.outcome if wait else None,
                   "deadline": wait.deadline if wait else None}

    def _find(self, current, source_id, digest, conn):
        return next((deepcopy(row) for row in self.rows.values() if
                     (row["tenant_id"], row["run_id"], row["source_id"], row["content_digest"]) ==
                     (current.tenant_id, current.run_id, source_id, digest)), None)

    def _load(self, current, review_id, conn, *, lock=True):
        return deepcopy(self.rows.get(review_id))

    def _insert(self, row, conn):
        self.rows[row["review_id"]] = deepcopy(row)

    def _save(self, row, conn):
        row["revision"] += 1
        self.rows[row["review_id"]] = deepcopy(row)

    def _wait(self, wait_id, current, conn):
        return self.workflow.get_wait(wait_id, tenant_id=current.tenant_id, owner_id=current.owner_id)

    def _resolve(self, current, wait, conn):
        return self.workflow.resolve_wait(wait.wait_id, tenant_id=current.tenant_id, outcome={})


class PostgresMaterialReviewStore(_MaterialReviews):
    def __init__(self, database, workflow_store):
        self.database = database
        self.workflow = workflow_store

    @contextmanager
    def _scope(self, record, *, lease_token=None, connection=None):
        identity = _identity(record)
        with (self.database.transaction() if connection is None else nullcontext(connection)) as conn:
            current = self.workflow.get(record.run_id, tenant_id=record.tenant_id,
                                        owner_id=record.owner_id, connection=conn, lock=True)
            if _identity(current) != identity:
                raise CoreError("MATERIAL_REVIEW_NOT_FOUND")
            if lease_token is not None and (not lease_token or conn.execute(
                """SELECT 1 FROM core_runs WHERE run_id=%s AND lease_token=%s
                   AND lease_expires_at > EXTRACT(EPOCH FROM clock_timestamp())""",
                (current.run_id, lease_token),
            ).fetchone() is None):
                raise CoreError("LEASE_LOST")
            yield current, conn

    def _now(self, conn):
        return self.workflow._current_time(conn)

    def _negative_candidates(self, current, digest, kind, conn):
        # ponytail: existing scoped ledger scan; add a digest index if chat history
        # volume makes this predicate costly, without adding a second decision store.
        return conn.execute("""SELECT m.review_id, m.state, w.outcome, w.deadline FROM core_material_reviews m
            LEFT JOIN core_waits w ON w.wait_id=m.wait_id AND w.tenant_id=m.tenant_id
            WHERE m.tenant_id=%s AND m.owner_id=%s AND m.context_id=%s
              AND ((COALESCE(m.completed_result_ref->>'material_kind','json')=%s
                    AND m.completed_result_ref->>'material_digest'=%s)
                   OR (%s='json' AND m.completed_result_ref->>'text_digest'=%s))
              AND m.state IN ('pending','rejected','timed_out')""",
            (current.tenant_id, current.owner_id, current.context_id, kind, digest, kind, digest)).fetchall()

    def _find(self, current, source_id, digest, conn):
        return conn.execute("""SELECT * FROM core_material_reviews WHERE tenant_id=%s AND run_id=%s
                            AND source_id=%s AND content_digest=%s FOR UPDATE""",
                            (current.tenant_id, current.run_id, source_id, digest)).fetchone()

    def _load(self, current, review_id, conn, *, lock=True):
        return conn.execute("""SELECT * FROM core_material_reviews WHERE review_id=%s
                            AND tenant_id=%s AND run_id=%s""" + (" FOR UPDATE" if lock else ""),
                            (review_id, current.tenant_id, current.run_id)).fetchone()

    def _insert(self, row, conn):
        from psycopg.sql import SQL, Identifier, Placeholder
        values = [Jsonb(value) if key in {"payload", "sealed_ref", "completed_result_ref", "classification"}
                  and value is not None else value for key, value in row.items()]
        conn.execute(SQL("INSERT INTO core_material_reviews ({}) VALUES ({})").format(
            SQL(",").join(map(Identifier, row)), SQL(",").join(Placeholder() for _ in row)), values)

    def _save(self, row, conn):
        updated = conn.execute("""UPDATE core_material_reviews SET state=%s, attempts_used=%s, input_tokens_used=%s,
                     classification=%s, wait_id=%s, attempt_token=%s, revision=revision+1
                     WHERE review_id=%s AND revision=%s""",
                     (row["state"], row["attempts_used"], row["input_tokens_used"],
                      Jsonb(row["classification"]) if row["classification"] is not None else None,
                      row["wait_id"], row["attempt_token"], row["review_id"], row["revision"]))
        if updated.rowcount != 1:
            raise CoreError("MATERIAL_REVIEW_CONFLICT")
        row["revision"] += 1

    def _wait(self, wait_id, current, conn):
        return self.workflow.get_wait(wait_id, tenant_id=current.tenant_id, owner_id=current.owner_id,
                                      connection=conn, lock=True)

    def _resolve(self, current, wait, conn):
        return self.workflow._resolve_wait_locked(conn, current, wait, {})
