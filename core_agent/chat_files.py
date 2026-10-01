"""Private, durable input batches. Transport admission remains a separate concern.

Only trusted admission/guardrail code calls bind/record_decision. PostgreSQL bind
accepts the admission connection, so acceptance is committed with Task/inbox.
There is deliberately no HTTP endpoint or default guardrail decision here.
"""
from __future__ import annotations

import base64
import binascii
import copy
import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import stat
import sys
import threading
import time
import unicodedata
import uuid
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import PurePosixPath

from psycopg.types.json import Jsonb

from .errors import CoreError
from .workflow import TERMINAL_STATES
from .interactions import DEFAULT_ATTACHMENT_LIMIT

_MANIFEST_LIMIT = 1_000_000
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_ACCEPTED = {"accepted_quarantine", "accepted_ready", "published", "excluded"}
_TRANSITIONS = {
    "staging": {"staging", "accepted_quarantine", "rejected"},
    "accepted_quarantine": {"accepted_quarantine", "accepted_ready", "excluded"},
    "accepted_ready": {"accepted_ready", "published", "excluded"},
    "published": {"published"}, "excluded": {"excluded"}, "rejected": {"rejected"},
}


def _check_update(old, new):
    if new["state"] not in _TRANSITIONS[old["state"]]:
        raise CoreError("FILE_BATCH_CONFLICT")
    fixed = ("batch_id", "tenant_id", "actor_id", "message_id", "request_digest",
             "created_at", "storage_key", "schema_version")
    if old["state"] in _ACCEPTED:
        fixed += ("manifest", "context_id", "owner_id", "task_id", "run_id", "sequence")
    if any(old[key] != new[key] for key in fixed):
        raise CoreError("FILE_BATCH_CONFLICT")
    if new["state"] in _ACCEPTED and (
        not new["manifest"] or any(not new[k] for k in ("context_id", "owner_id", "task_id", "run_id"))
    ):
        raise CoreError("FILE_BATCH_NOT_PREPARED")
    if new["state"] in {"accepted_ready", "published"} and not new["decision_ref"]:
        raise CoreError("FILE_BATCH_DECISION_REQUIRED")
    if old["decision_ref"] is not None and new["decision_ref"] != old["decision_ref"]:
        raise CoreError("FILE_BATCH_CONFLICT")


def _live(record, binding, task_id, *, accept_input=False):
    if (record.tenant_id, record.owner_id, record.context_id, record.task_id) != (
        binding.tenant_id, binding.owner_id, binding.context_id, task_id
    ):
        raise CoreError("FILE_BATCH_NOT_FOUND")
    if (record.state in TERMINAL_STATES or record.cancel_requested
            or (record.snapshot.get("terminal_intent") and not accept_input)):
        raise CoreError("FILE_BATCH_TASK_CLOSED")


class MemoryChatFileStore:
    def __init__(self, workflow_store, validate_scope):
        self.workflow = workflow_store
        self.validate_scope = validate_scope
        self.rows = {}
        self.lock = threading.RLock()

    def create(self, row):
        with self.lock:
            if row["batch_id"] in self.rows:
                raise CoreError("FILE_BATCH_CONFLICT")
            self.rows[row["batch_id"]] = copy.deepcopy(row)

    def get(self, batch_id, tenant_id, *, connection=None):
        with self.lock:
            row = self.rows.get(batch_id)
            if row is None or row["tenant_id"] != tenant_id:
                raise CoreError("FILE_BATCH_NOT_FOUND")
            return copy.deepcopy(row)

    @contextmanager
    def locked(self, batch_id, tenant_id, *, connection=None):
        with self.lock:
            row = self.get(batch_id, tenant_id)
            yield row, None
            _check_update(self.rows[batch_id], row)
            row["version"] += 1
            self.rows[batch_id] = copy.deepcopy(row)

    @contextmanager
    def chat(self, binding, run_id, task_id, *, connection=None, lease_token=None, accept_input=False):
        # Match workflow -> batch ordering used by admission and terminalization.
        with self.workflow._lock:
            self.validate_scope(binding)
            record = self.workflow.get(run_id, tenant_id=binding.tenant_id, owner_id=binding.owner_id)
            _live(record, binding, task_id, accept_input=accept_input)
            with self.workflow._execution_lock(record, lease_token):
                yield None

    def reserved(self, binding, *, connection=None):
        with self.lock:
            return {entry["actual_name"] for row in self.rows.values()
                    if (row["tenant_id"], row["owner_id"], row["context_id"]) == (
                        binding.tenant_id, binding.owner_id, binding.context_id)
                    and row["state"] in _ACCEPTED for entry in row["manifest"]["entries"]}

    def expired(self, now, cutoff, limit):
        with self.lock:
            return [(r["batch_id"], r["tenant_id"]) for r in sorted(
                self.rows.values(), key=lambda r: (r["created_at"], r["batch_id"]))
                if r["state"] in {"staging", "rejected"} and r["cleaned_at"] is None
                and r["lease_expires_at"] <= now and r["created_at"] <= cutoff][:limit]

    @contextmanager
    def orphan(self, batch_id):
        with self.workflow._lock, self.lock:
            references = [r.snapshot for r in self.workflow._records.values()]
            references += [vars(wait) for wait in self.workflow._waits.values()]
            references += list(self.workflow._inbound.values())
            yield batch_id not in self.rows and all(batch_id not in json.dumps(ref) for ref in references)


class PostgresChatFileStore:
    def __init__(self, database, workflow_store):
        self.database = database
        self.workflow = workflow_store

    @staticmethod
    def _decode(row):
        manifest = row["manifest"]
        if isinstance(manifest, dict) and "manifest_json" in manifest:
            row["manifest"] = json.loads(manifest["manifest_json"])
        return row

    def create(self, row):
        with self.database.transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("file-upload:" + row["batch_id"],))
            connection.execute(
                """INSERT INTO core_chat_file_batches
                   (batch_id, tenant_id, actor_id, message_id, request_digest, created_at,
                    storage_key, lease_owner, lease_token, lease_expires_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                tuple(row[k] for k in ("batch_id", "tenant_id", "actor_id", "message_id",
                    "request_digest", "created_at", "storage_key", "lease_owner", "lease_token", "lease_expires_at")),
            )

    def get(self, batch_id, tenant_id, *, connection=None):
        with (nullcontext(connection) if connection is not None else self.database.transaction()) as conn:
            row = conn.execute(
                "SELECT * FROM core_chat_file_batches WHERE batch_id=%s AND tenant_id=%s",
                (batch_id, tenant_id),
            ).fetchone()
        if row is None:
            raise CoreError("FILE_BATCH_NOT_FOUND")
        return self._decode(row)

    @contextmanager
    def locked(self, batch_id, tenant_id, *, connection=None):
        with (nullcontext(connection) if connection is not None else self.database.transaction()) as conn:
            old = conn.execute(
                "SELECT * FROM core_chat_file_batches WHERE batch_id=%s AND tenant_id=%s FOR UPDATE",
                (batch_id, tenant_id),
            ).fetchone()
            if old is None:
                raise CoreError("FILE_BATCH_NOT_FOUND")
            stored_manifest = old["manifest"]
            old = self._decode(old)
            row = copy.deepcopy(old)
            yield row, conn
            _check_update(old, row)
            row["version"] = old["version"] + 1
            manifest = stored_manifest
            if row["manifest"] != old["manifest"]:
                manifest = row["manifest"]
                if manifest is not None:
                    # JSONB cannot hold NUL or lone surrogates. Preserve exact
                    # metadata as JSON text, keeping constraint/index fields native.
                    manifest = {"schema_version": manifest["schema_version"],
                        "entries": [{"actual_name": entry["actual_name"]} for entry in manifest["entries"]],
                        "manifest_json": json.dumps(manifest, ensure_ascii=True, sort_keys=True)}
            conn.execute(
                """UPDATE core_chat_file_batches SET state=%s, manifest=%s, context_id=%s,
                   owner_id=%s, task_id=%s, run_id=%s, sequence=%s, lease_expires_at=%s,
                   decision_ref=%s, published_at=%s, error_code=%s, cleaned_at=%s,
                   version=version+1 WHERE batch_id=%s AND version=%s""",
                (row["state"], Jsonb(manifest), row["context_id"], row["owner_id"],
                 row["task_id"], row["run_id"], row["sequence"], row["lease_expires_at"],
                 row["decision_ref"], row["published_at"], row["error_code"], row["cleaned_at"],
                 batch_id, old["version"]),
            )

    @contextmanager
    def chat(self, binding, run_id, task_id, *, connection=None, lease_token=None, accept_input=False):
        with (nullcontext(connection) if connection is not None else self.database.transaction()) as conn:
            row = conn.execute(
                "SELECT owner_id FROM core_chats WHERE tenant_id=%s AND context_id=%s FOR NO KEY UPDATE",
                (binding.tenant_id, binding.context_id),
            ).fetchone()
            if row is None or row["owner_id"] != binding.owner_id:
                raise CoreError("FILE_BATCH_NOT_FOUND")
            record = self.workflow.get(run_id, tenant_id=binding.tenant_id,
                                       owner_id=binding.owner_id, connection=conn, lock=True)
            _live(record, binding, task_id, accept_input=accept_input)
            if lease_token is not None and (not lease_token or conn.execute(
                "SELECT 1 FROM core_runs WHERE run_id=%s AND lease_token=%s AND lease_expires_at>EXTRACT(EPOCH FROM clock_timestamp())",
                (run_id, lease_token),
            ).fetchone() is None):
                raise CoreError("LEASE_LOST")
            yield conn

    def reserved(self, binding, *, connection):
        rows = connection.execute(
            """SELECT manifest FROM core_chat_file_batches WHERE tenant_id=%s AND owner_id=%s
               AND context_id=%s AND state IN ('accepted_quarantine','accepted_ready','published','excluded')""",
            (binding.tenant_id, binding.owner_id, binding.context_id),
        ).fetchall()
        return {entry["actual_name"] for row in rows for entry in row["manifest"]["entries"]}

    def expired(self, now, cutoff, limit):
        with self.database.transaction() as connection:
            rows = connection.execute(
                """SELECT batch_id, tenant_id FROM core_chat_file_batches
                   WHERE state IN ('staging','rejected') AND cleaned_at IS NULL
                   AND lease_expires_at<=%s AND created_at<=%s
                   ORDER BY created_at,batch_id LIMIT %s""", (now, cutoff, limit),
            ).fetchall()
        return [(r["batch_id"], r["tenant_id"]) for r in rows]

    @contextmanager
    def orphan(self, batch_id):
        with self.database.transaction() as connection:
            # Same fence as create: no stage row can appear between check/unlink.
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("file-upload:" + batch_id,))
            referenced = connection.execute(
                """SELECT 1 FROM core_chat_file_batches WHERE batch_id=%s OR storage_key=%s
                   UNION ALL SELECT 1 FROM core_runs WHERE position(%s in snapshot::text)>0
                   UNION ALL SELECT 1 FROM core_waits
                       WHERE position(%s in subject::text)>0 OR position(%s in continuation::text)>0
                   UNION ALL SELECT 1 FROM core_inbound_messages WHERE position(%s in provenance::text)>0 LIMIT 1""",
                (batch_id,) * 6,
            ).fetchone()
            yield referenced is None


def _mkdir(parent, name):
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
        os.fsync(parent)
    except FileExistsError:
        pass
    return os.open(name, _DIRECTORY, dir_fd=parent)


def _rename(source_fd, source, target_fd, target):
    """Atomic directory publication that cannot replace even an empty directory."""
    libc = ctypes.CDLL(None, use_errno=True)
    name, flag = ("renameatx_np", 4) if sys.platform == "darwin" else ("renameat2", 1)
    operation = getattr(libc, name, None)
    if operation is None:
        raise CoreError("FILE_ATOMIC_RENAME_UNAVAILABLE")
    operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    operation.restype = ctypes.c_int
    if operation(source_fd, os.fsencode(source), target_fd, os.fsencode(target), flag):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _safe_name(original, index, occupied):
    name = unicodedata.normalize("NFC", original or "")
    # Preserve Unicode display names; strip separators and every control/format char.
    name = "".join("_" if c in '/\\:' or unicodedata.category(c).startswith("C") else c for c in name)
    name = name.strip(" .")
    if not name or name.startswith("."):
        name = f"attachment-{index + 1}"
    while len(name.encode("utf-8")) > 180:
        name = name[:-1]
    suffix = PurePosixPath(name).suffix
    stem = name[:-len(suffix)] if suffix else name
    candidate, counter = name, 2
    while candidate in occupied:
        candidate = f"{stem}_{counter}{suffix}"
        counter += 1
    occupied.add(candidate)
    return candidate


def _write(fd, name, content, heartbeat=None):
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    with os.fdopen(descriptor, "wb") as stream:
        for offset in range(0, len(content), 1024 * 1024):
            if heartbeat:
                heartbeat()
            stream.write(memoryview(content)[offset:offset + 1024 * 1024])
        stream.flush()
        os.fsync(stream.fileno())


def _manifest(fd, manifest):
    temporary = ".manifest-" + uuid.uuid4().hex
    encoded = json.dumps(manifest, sort_keys=True, ensure_ascii=True).encode()
    if len(encoded) > _MANIFEST_LIMIT:
        raise CoreError("FILE_METADATA_TOO_LARGE")
    _write(fd, temporary, encoded)
    os.replace(temporary, ".manifest.json", src_dir_fd=fd, dst_dir_fd=fd)
    os.fsync(fd)


def _verify(fd, manifest):
    try:
        expected_names = {".manifest.json", *(entry["actual_name"] for entry in manifest["entries"])}
        with os.scandir(fd) as entries:
            for entry in entries:
                if entry.name not in expected_names:
                    raise CoreError("ARTIFACT_INTEGRITY_FAILED")
                expected_names.remove(entry.name)
        if expected_names:
            raise CoreError("ARTIFACT_INTEGRITY_FAILED")
        descriptor = os.open(".manifest.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(descriptor, "rb") as stream:
            expected = json.dumps(manifest, sort_keys=True, ensure_ascii=True).encode()
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or stream.read(len(expected) + 1) != expected:
                raise CoreError("ARTIFACT_INTEGRITY_FAILED")
        for entry in manifest["entries"]:
            descriptor = os.open(entry["actual_name"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size != entry["size_bytes"]:
                    raise CoreError("ARTIFACT_INTEGRITY_FAILED")
                digest = hashlib.sha256()
                remaining = entry["size_bytes"]
                while remaining:
                    chunk = stream.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        raise CoreError("ARTIFACT_INTEGRITY_FAILED")
                    digest.update(chunk)
                    remaining -= len(chunk)
                after = os.fstat(stream.fileno())
                if (stream.read(1) or digest.hexdigest() != entry["sha256"]
                        or (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                        != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                    raise CoreError("ARTIFACT_INTEGRITY_FAILED")
    except (OSError, ValueError) as error:
        raise CoreError("ARTIFACT_INTEGRITY_FAILED") from error


class ChatFileService:
    def __init__(self, store, workspaces, *, limit_bytes=DEFAULT_ATTACHMENT_LIMIT,
                 clock=time.time, lease_seconds=300):
        if (type(limit_bytes) is not int or limit_bytes < 1
                or type(lease_seconds) not in (int, float)
                or not 0 < lease_seconds <= sys.float_info.max):
            raise CoreError("CONFIG_INVALID")
        self.store, self.workspaces = store, workspaces
        self.limit_bytes, self.clock, self.lease_seconds = limit_bytes, clock, lease_seconds
        self._orphan_scan = None
        with ExitStack() as descriptors:
            self.root = os.open(workspaces.root, _DIRECTORY)
            descriptors.callback(os.close, self.root)
            private = _mkdir(self.root, "private")
            try:
                self.uploads = _mkdir(private, "uploads")
                descriptors.callback(os.close, self.uploads)
                self.quarantine = _mkdir(private, "quarantine")
                descriptors.callback(os.close, self.quarantine)
            finally:
                os.close(private)
            self._descriptors = descriptors.pop_all()

    def close(self):
        if self._orphan_scan is not None:
            self._orphan_scan.close()
            self._orphan_scan = None
        self._descriptors.close()

    @contextmanager
    def _attachments(self, binding):
        path = self.workspaces.workspace(binding)
        descriptor = os.dup(self.root)
        try:
            for component in path.relative_to(self.workspaces.root).parts:
                os.fsync(descriptor)
                child = os.open(component, _DIRECTORY, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            child = _mkdir(descriptor, "attachments")
            os.close(descriptor)
            descriptor = child
            yield descriptor
        finally:
            os.close(descriptor)

    def renew(self, batch_id, tenant_id, lease_token):
        with self.store.locked(batch_id, tenant_id) as (row, _):
            self._lease(row, lease_token)
            row["lease_expires_at"] = self.clock() + self.lease_seconds

    def _lease(self, row, token):
        if row["state"] != "staging" or row["lease_token"] != token or row["lease_expires_at"] <= self.clock():
            raise CoreError("FILE_UPLOAD_LEASE_LOST")

    def prepare(self, files, *, tenant_id, actor_id, message_id, request_digest, source,
                request_metadata=None, limit_bytes=None):
        """Accept transport data containing exactly raw or base64 file content.

        Names, source, and metadata remain untrusted and are included in the
        eventual published manifest. Callers must provide only original public
        transport metadata (never auth/secrets/private host paths), and review
        that metadata together with file contents before recording guardrail allow.
        """
        if not files or any(not isinstance(v, str) or not v for v in (
            tenant_id, actor_id, message_id, request_digest, source
        )):
            raise CoreError("INVALID_FILE_INPUT")
        limit_bytes = self.limit_bytes if limit_bytes is None else limit_bytes
        if type(limit_bytes) is not int or limit_bytes < 1:
            raise CoreError("CONFIG_INVALID")
        batch_id, token, now = uuid.uuid4().hex, uuid.uuid4().hex, self.clock()
        row = dict(batch_id=batch_id, schema_version=1, tenant_id=tenant_id, actor_id=actor_id,
                   message_id=message_id, request_digest=request_digest, created_at=now,
                   storage_key=batch_id, lease_owner=actor_id, lease_token=token,
                   lease_expires_at=now + self.lease_seconds, state="staging", manifest=None,
                   context_id=None, owner_id=None, task_id=None, run_id=None, sequence=None,
                   decision_ref=None, published_at=None, error_code=None, cleaned_at=None, version=1)
        self.store.create(row)  # Durable original age precedes any filesystem creation.
        directory = None
        try:
            manifest = dict(schema_version=1, batch_id=batch_id, created_at=now,
                            source=source, metadata=copy.deepcopy(request_metadata or {}), entries=[], total_bytes=0)
            with self.store.locked(batch_id, tenant_id) as (current, _):
                self._lease(current, token)
                directory = _mkdir(self.uploads, batch_id)
                _manifest(directory, manifest)
            occupied = {".manifest.json"}
            for index, file in enumerate(files):
                self.renew(batch_id, tenant_id, token)
                if not isinstance(file, dict) or ("raw" in file) == ("base64" in file) or any(
                    key not in {"raw", "base64", "name", "media_type", "metadata"} for key in file
                ):
                    raise CoreError("CONTENT_TYPE_NOT_SUPPORTED")
                original, media = file.get("name", ""), file.get("media_type", "application/octet-stream")
                if not isinstance(original, str) or not isinstance(media, str):
                    raise CoreError("INVALID_FILE_INPUT")
                if "raw" in file:
                    content = file["raw"]
                    if not isinstance(content, bytes):
                        raise CoreError("INVALID_FILE_INPUT")
                else:
                    encoded = file["base64"]
                    if not isinstance(encoded, str):
                        raise CoreError("INVALID_FILE_INPUT")
                    # Validate/count first: an oversized base64 string must never
                    # allocate another unbounded decoded copy.
                    if len(encoded) % 4 or not re.fullmatch(r"[A-Za-z0-9+/]*={0,2}", encoded):
                        raise CoreError("INVALID_FILE_ENCODING")
                    decoded_size = len(encoded) // 4 * 3 - (len(encoded) - len(encoded.rstrip("=")))
                    if manifest["total_bytes"] + decoded_size > limit_bytes:
                        raise CoreError("ATTACHMENTS_TOO_LARGE", data={
                            "allowed_bytes": limit_bytes,
                            "actual_bytes": manifest["total_bytes"] + decoded_size})
                    try:
                        content = base64.b64decode(encoded, validate=True)
                    except (ValueError, binascii.Error) as error:
                        raise CoreError("INVALID_FILE_ENCODING") from error
                    if base64.b64encode(content).decode("ascii") != encoded:
                        raise CoreError("INVALID_FILE_ENCODING")
                manifest["total_bytes"] += len(content)
                if manifest["total_bytes"] > limit_bytes:
                    raise CoreError("ATTACHMENTS_TOO_LARGE", data={
                        "allowed_bytes": limit_bytes, "actual_bytes": manifest["total_bytes"]})
                actual = _safe_name(original, index, occupied)
                _write(directory, actual, content, lambda: self.renew(batch_id, tenant_id, token))
                manifest["entries"].append(dict(index=index, original_name=original, actual_name=actual,
                    relative_path=f"attachments/{batch_id}/{actual}", media_type=media,
                    size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest(),
                    metadata=copy.deepcopy(file.get("metadata", {}))))
            _manifest(directory, manifest)
            os.fsync(self.uploads)
            with self.store.locked(batch_id, tenant_id) as (current, _):
                self._lease(current, token)
                current["manifest"] = manifest
            return self.store.get(batch_id, tenant_id)
        except (CoreError, OSError, TypeError, ValueError) as error:
            self.reject(batch_id, tenant_id, token, getattr(error, "code", "FILE_WRITE_FAILED"))
            if isinstance(error, CoreError):
                raise
            raise CoreError("FILE_WRITE_FAILED") from error
        finally:
            if directory is not None:
                os.close(directory)

    def _remove(self, batch_id):
        for parent in (self.uploads, self.quarantine):
            try:
                shutil.rmtree(batch_id, dir_fd=parent)
                os.fsync(parent)
            except FileNotFoundError:
                pass

    def reject(self, batch_id, tenant_id, token, code="FILE_UPLOAD_REJECTED"):
        with self.store.locked(batch_id, tenant_id) as (row, _):
            if row["state"] not in {"staging", "rejected"} or row["lease_token"] != token:
                raise CoreError("FILE_BATCH_CONFLICT")
            row.update(state="rejected", error_code=code, lease_expires_at=self.clock())
            self._remove(batch_id)
            row["cleaned_at"] = self.clock()

    @contextmanager
    def _private(self, batch_id):
        # An interrupted quarantine move remains discoverable on either side.
        try:
            descriptor = os.open(batch_id, _DIRECTORY, dir_fd=self.quarantine)
            parent = self.quarantine
        except FileNotFoundError:
            try:
                descriptor = os.open(batch_id, _DIRECTORY, dir_fd=self.uploads)
            except FileNotFoundError as error:
                raise CoreError("ARTIFACT_INTEGRITY_FAILED") from error
            parent = self.uploads
        try:
            yield parent, descriptor
        finally:
            os.close(descriptor)

    def bind(self, batch_id, binding, *, task_id, run_id, actor_id, message_id,
             request_digest, lease_token, sequence=None, connection=None):
        """Trusted caller already holds root dedup/chat or chat/inbox locks.

        PostgreSQL callers MUST pass their admission transaction. This helper does
        not create Task/inbox/ledger rows, nor authorize binary transport intake.
        On any admission failure, the caller MUST exit/rollback its transaction,
        then call reject with the original lease token. This also cleans a stage
        whose final names were prepared before a later admission error. A process
        crash instead leaves the unaccepted row for the original-age sweeper.
        """
        if isinstance(self.store, PostgresChatFileStore) and connection is None:
            raise CoreError("FILE_ADMISSION_TRANSACTION_REQUIRED")
        if sequence is not None and (type(sequence) is not int or sequence < 1):
            raise CoreError("INVALID_FILE_INPUT")
        with self.store.chat(binding, run_id, task_id, connection=connection, accept_input=sequence is not None) as conn:
            occupied = self.store.reserved(binding, connection=conn)
            with self._attachments(binding) as attachments:
                occupied.update(os.listdir(attachments))
            with self.store.locked(batch_id, binding.tenant_id, connection=conn) as (row, _):
                self._lease(row, lease_token)
                if (row["actor_id"], row["message_id"], row["request_digest"]) != (actor_id, message_id, request_digest):
                    raise CoreError("FILE_BATCH_NOT_FOUND")
                if not row["manifest"]:
                    raise CoreError("FILE_BATCH_NOT_PREPARED")
                try:
                    with self._private(batch_id) as (_, directory):
                        _verify(directory, row["manifest"])
                        # Two passes avoid overwriting a sibling matching a suffix.
                        entries = row["manifest"]["entries"]
                        for entry in entries:
                            os.rename(entry["actual_name"], f".input-{entry['index']}", src_dir_fd=directory, dst_dir_fd=directory)
                        for entry in entries:
                            actual = _safe_name(entry["original_name"], entry["index"], occupied)
                            os.rename(f".input-{entry['index']}", actual, src_dir_fd=directory, dst_dir_fd=directory)
                            entry.update(actual_name=actual, relative_path=f"attachments/{batch_id}/{actual}")
                        _manifest(directory, row["manifest"])
                except (CoreError, OSError) as error:
                    # The row is still unaccepted and locked. The admission caller
                    # records rejection after its outer transaction has rolled back.
                    self._remove(batch_id)
                    if isinstance(error, CoreError):
                        raise
                    raise CoreError("FILE_WRITE_FAILED") from error
                row.update(state="accepted_quarantine", context_id=binding.context_id,
                           owner_id=binding.owner_id, task_id=task_id, run_id=run_id, sequence=sequence)
        return copy.deepcopy(row)

    @contextmanager
    def _accepted_files(self, batch_id, binding, *, run_id, task_id):
        row = self.store.get(batch_id, binding.tenant_id)
        self._scope(row, binding)
        if row["state"] not in _ACCEPTED or (row["run_id"], row["task_id"]) != (run_id, task_id):
            raise CoreError("FILE_BATCH_NOT_FOUND")
        try:
            with ExitStack() as stack:
                if row["state"] == "published":
                    attachments = stack.enter_context(self._attachments(binding))
                    directory = os.open(batch_id, _DIRECTORY, dir_fd=attachments)
                    stack.callback(os.close, directory)
                else:
                    try:
                        _, directory = stack.enter_context(self._private(batch_id))
                    except CoreError as error:
                        # Rename may have committed to disk before the database
                        # transaction. Read the exact target; never copy it back.
                        if row["state"] != "accepted_ready" or error.code != "ARTIFACT_INTEGRITY_FAILED":
                            raise
                        attachments = stack.enter_context(self._attachments(binding))
                        directory = os.open(batch_id, _DIRECTORY, dir_fd=attachments)
                        stack.callback(os.close, directory)
                _verify(directory, row["manifest"])
                yield row, directory
        except OSError as error:
            raise CoreError("ARTIFACT_INTEGRITY_FAILED") from error

    @staticmethod
    def _read_entry(directory, entry):
        descriptor = os.open(entry["actual_name"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != entry["size_bytes"]:
                raise CoreError("ARTIFACT_INTEGRITY_FAILED")
            content = stream.read(entry["size_bytes"] + 1)
            after = os.fstat(stream.fileno())
            if (len(content) != entry["size_bytes"] or hashlib.sha256(content).hexdigest() != entry["sha256"]
                    or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise CoreError("ARTIFACT_INTEGRITY_FAILED")
            return content

    def owner_download(self, batch_id, binding, index, *, run_id, task_id):
        """Role-authorized owner API only; immutable logical scope, no host paths.

        History remains readable after a Task closes. The API must derive batch
        and index from its authorized persisted review, never caller overrides.
        """
        with self._accepted_files(batch_id, binding, run_id=run_id, task_id=task_id) as (row, directory):
            if type(index) is not int or not 0 <= index < len(row["manifest"]["entries"]):
                raise CoreError("FILE_BATCH_NOT_FOUND")
            entry = row["manifest"]["entries"][index]
            return {"entry": copy.deepcopy(entry), "manifest": copy.deepcopy(row["manifest"]),
                    "content": self._read_entry(directory, entry)}

    def review_material(self, batch_id, binding, *, run_id, task_id):
        """Extract only complete UTF-8 text; unsupported formats require owner review.

        No MIME sniffing/parser framework, external links, or execution. Metadata
        is part of every document's review and stays private until whole-batch allow.
        Accepted sizes come from the sealed manifest, not a mutable intake limit.
        """
        with self._accepted_files(batch_id, binding, run_id=run_id, task_id=task_id) as (row, directory):
            documents = []
            for entry in row["manifest"]["entries"]:
                content = self._read_entry(directory, entry)
                media = entry["media_type"].split(";", 1)[0].strip().lower()
                supported = media.startswith("text/") or media in {"application/json", "application/xml"}
                try:
                    text = content.decode("utf-8")
                except UnicodeDecodeError:
                    text = None
                # Exact identity is independent of extraction support. A rejected
                # UTF-8 byte sequence cannot reopen as inline text by changing MIME.
                text_digest = (hashlib.sha256(json.dumps(text, ensure_ascii=False,
                    separators=(",", ":"), allow_nan=False).encode()).hexdigest() if text is not None else None)
                complete = supported and text is not None and bool(text.strip()) and not any(
                    unicodedata.category(c) == "Cc" and c not in "\t\r\n" for c in text)
                documents.append({"text": text if complete else "", "complete": complete, "text_digest": text_digest})
            return {"manifest": copy.deepcopy(row["manifest"]), "documents": documents,
                    "state": row["state"], "decision_ref": row["decision_ref"]}

    def record_decision(self, batch_id, binding, *, decision_ref, allow, connection=None, lease_token=None):
        """Only the trusted guardrail transaction may record its durable decision."""
        if not isinstance(decision_ref, str) or not decision_ref or type(allow) is not bool:
            raise CoreError("FILE_BATCH_DECISION_REQUIRED")
        batch = self.store.get(batch_id, binding.tenant_id, connection=connection)
        self._scope(batch, binding)
        with self.store.chat(binding, batch["run_id"], batch["task_id"], connection=connection, lease_token=lease_token) as conn:
            with self.store.locked(batch_id, binding.tenant_id, connection=conn) as (row, _):
                if row["state"] != "accepted_quarantine":
                    raise CoreError("FILE_BATCH_CONFLICT")
                with self._private(batch_id) as (parent, directory):
                    _verify(directory, row["manifest"])
                    if parent == self.uploads:
                        _rename(parent, batch_id, self.quarantine, batch_id)
                        os.fsync(parent)
                        os.fsync(self.quarantine)
                row.update(state="accepted_ready" if allow else "excluded", decision_ref=decision_ref)

    @staticmethod
    def _scope(row, binding):
        if (row["owner_id"], row["context_id"]) != (binding.owner_id, binding.context_id):
            raise CoreError("FILE_BATCH_NOT_FOUND")

    def publish(self, batch_id, binding, *, lease_token=None):
        # Read committed decision before entering publication transaction. A caller
        # cannot publish using its own uncommitted allow on the same connection.
        batch = self.store.get(batch_id, binding.tenant_id)
        self._scope(batch, binding)
        if batch["state"] not in {"accepted_ready", "published"}:
            raise CoreError("FILE_BATCH_NOT_READY")
        try:
            with self.store.chat(binding, batch["run_id"], batch["task_id"], lease_token=lease_token) as conn:
                with self.store.locked(batch_id, binding.tenant_id, connection=conn) as (row, _):
                    if row["state"] not in {"accepted_ready", "published"}:
                        raise CoreError("FILE_BATCH_NOT_READY")
                    with self._attachments(binding) as attachments:
                        try:
                            target = os.open(batch_id, _DIRECTORY, dir_fd=attachments)
                        except FileNotFoundError:
                            with self._private(batch_id) as (parent, directory):
                                _verify(directory, row["manifest"])
                                _rename(parent, batch_id, attachments, batch_id)
                                os.fsync(parent)
                            target = os.open(batch_id, _DIRECTORY, dir_fd=attachments)
                        try:
                            # Recovery target is authoritative only when the private
                            # source vanished through the single atomic rename.
                            for parent in (self.uploads, self.quarantine):
                                try:
                                    os.stat(batch_id, dir_fd=parent, follow_symlinks=False)
                                except FileNotFoundError:
                                    continue
                                raise CoreError("FILE_PUBLICATION_CONFLICT")
                            _verify(target, row["manifest"])
                        finally:
                            os.close(target)
                        os.fsync(attachments)
                    row.update(state="published", published_at=row["published_at"] or self.clock(), error_code=None)
            return copy.deepcopy(row["manifest"])
        except (OSError, CoreError) as error:
            # Retain accepted bytes; a retry is publication, never a new admission.
            with self.store.locked(batch_id, binding.tenant_id) as (row, _):
                row["error_code"] = getattr(error, "code", "FILE_PUBLICATION_PENDING")
            if isinstance(error, CoreError):
                raise
            raise CoreError("FILE_PUBLICATION_PENDING", retryable=True) from error

    def sweep(self, *, startup=False, limit=100):
        """Bounded lifecycle hook; call again immediately when has_more is true.

        Rowless uploads require intact original-age metadata and a crosscheck of
        all authoritative references. Unknown/corrupt metadata is retained for
        reconciliation. Neither mtime nor restart time authorizes deletion.
        """
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise CoreError("CONFIG_INVALID")
        now = self.clock()
        cutoff = now if startup else now - 23 * 3600
        candidates = self.store.expired(now, cutoff, limit + 1)
        cleaned = 0
        for batch_id, tenant_id in candidates[:limit]:
            with self.store.locked(batch_id, tenant_id) as (row, _):
                if (row["state"] not in {"staging", "rejected"} or row["cleaned_at"] is not None
                        or row["lease_expires_at"] > now or row["created_at"] > cutoff
                        or any(row[k] is not None for k in ("task_id", "run_id", "decision_ref"))):
                    continue
                self._remove(batch_id)
                row.update(state="rejected", error_code=row["error_code"] or "FILE_UPLOAD_EXPIRED", cleaned_at=now)
                cleaned += 1
        orphan_cleaned, more = self._sweep_rowless(now - 23 * 3600, limit)
        return {"cleaned": cleaned + orphan_cleaned, "has_more": len(candidates) > limit or more}

    def _sweep_rowless(self, cutoff, limit):
        if self._orphan_scan is None:
            descriptor = os.open(".", _DIRECTORY, dir_fd=self.uploads)
            try:
                self._orphan_scan = os.scandir(descriptor)
            finally:
                os.close(descriptor)
        cleaned = 0
        for _ in range(limit):
            entry = next(self._orphan_scan, None)
            if entry is None:
                self._orphan_scan.close()
                self._orphan_scan = None
                return cleaned, False
            if not re.fullmatch(r"[0-9a-f]{32}", entry.name) or not entry.is_dir(follow_symlinks=False):
                continue
            with self.store.orphan(entry.name) as unreferenced:
                if not unreferenced:
                    continue
                directory = None
                try:
                    directory = os.open(entry.name, _DIRECTORY, dir_fd=self.uploads)
                    descriptor = os.open(".manifest.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                    with os.fdopen(descriptor, "rb") as stream:
                        info = os.fstat(stream.fileno())
                        if not stat.S_ISREG(info.st_mode) or info.st_size > _MANIFEST_LIMIT:
                            continue
                        manifest = json.loads(stream.read(_MANIFEST_LIMIT + 1))
                    age = manifest.get("created_at")
                    if (manifest.get("schema_version") != 1 or manifest.get("batch_id") != entry.name
                            or type(age) not in (int, float) or not math.isfinite(age) or age > cutoff):
                        continue
                    # Do not remove a replaced directory after opening metadata.
                    current = os.stat(entry.name, dir_fd=self.uploads, follow_symlinks=False)
                    opened = os.fstat(directory)
                    if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                        continue
                    shutil.rmtree(entry.name, dir_fd=self.uploads)
                    os.fsync(self.uploads)
                    cleaned += 1
                except (OSError, ValueError, AttributeError):
                    # Missing/corrupt age or inaccessible files require reconciliation.
                    continue
                finally:
                    if directory is not None:
                        os.close(directory)
        return cleaned, True
