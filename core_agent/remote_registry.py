"""Company-scoped peer revisions; only trusted adapters may resolve credentials."""

import json
import os
import re
import stat
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext

from cryptography.fernet import Fernet, InvalidToken

from .errors import CoreError
from .remote_agents import _validate_endpoint


_FIELDS = frozenset({"url", "description", "enabled", "header_name"})
_METADATA = ("id", "name", "url", "description", "enabled", "header_name", "revision")
_IMPORT_MAX_BYTES = 1_048_576
_IMPORT_MAX_PEERS = 100
_RESERVED_HEADERS = frozenset({
    "host", "content-type", "content-length", "connection", "transfer-encoding",
    "upgrade", "trailer", "te", "proxy-authorization", "accept", "a2a-version",
})


def _invalid():
    return CoreError("REMOTE_AGENT_INVALID")


def read_legacy_peer_import(path):
    """Read explicit operator configuration; never infer credentials or identity."""
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise CoreError("REMOTE_IMPORT_INVALID")
            value[key] = item
        return value

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise CoreError("REMOTE_IMPORT_INVALID")
            raw = stream.read(_IMPORT_MAX_BYTES + 1)
        if len(raw) > _IMPORT_MAX_BYTES:
            raise CoreError("REMOTE_IMPORT_INVALID")
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    except (OSError, ValueError, TypeError, RecursionError):
        raise CoreError("REMOTE_IMPORT_INVALID") from None
    if (not isinstance(payload, dict) or set(payload) != {"version", "peers"}
            or type(payload["version"]) is not int or payload["version"] != 1
            or not isinstance(payload["peers"], list)
            or not 1 <= len(payload["peers"]) <= _IMPORT_MAX_PEERS):
        raise CoreError("REMOTE_IMPORT_INVALID")
    return payload["peers"]


def _text(value, maximum, *, empty=False):
    try:
        return isinstance(value, str) and (empty or bool(value)) and len(value.encode("utf-8")) <= maximum
    except UnicodeError:
        return False


def _header_value(value):
    if not isinstance(value, str) or not value or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value):
        raise _invalid()
    try:
        encoded = value.encode("latin-1")
    except UnicodeError:
        raise _invalid() from None
    if len(encoded) > 16384:
        raise _invalid()


def _validate(values, *, create=False):
    required = _FIELDS | ({"name"} if create else set())
    if not isinstance(values, dict) or not required <= values.keys() <= required | {"header_value"}:
        raise _invalid()
    if create and (not isinstance(values["name"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", values["name"])):
        raise _invalid()
    url = values["url"]
    if not _text(url, 4096) or any(char.isspace() or ord(char) < 32 or 127 <= ord(char) < 160 for char in url):
        raise _invalid()
    try:
        parsed = _validate_endpoint(url)
        port = parsed.port
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                or "?" in url or "#" in url or (port is not None and not 1 <= port <= 65535)
                or "\\" in parsed.netloc or parsed.netloc.endswith(":")):
            raise _invalid()
    except (CoreError, ValueError):
        raise _invalid() from None
    if (not _text(values["description"], 4096, empty=True)
            or "\x00" in values["description"] or type(values["enabled"]) is not bool):
        raise _invalid()
    header = values["header_name"]
    if (not isinstance(header, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}", header)
            or header.lower() in _RESERVED_HEADERS):
        raise _invalid()
    if values.get("header_value") is not None:
        _header_value(values["header_value"])


def _revision(value):
    if type(value) is not int or not 1 <= value < 2**63 - 1:
        raise _invalid()


def _identifier(value, *, code="REMOTE_AGENT_INVALID"):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise CoreError(code)
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise CoreError(code) from None


def _cipher(key):
    if key is None:
        return None
    try:
        return Fernet(key.encode("ascii") if isinstance(key, str) else key)
    except (ValueError, TypeError, UnicodeError):
        raise CoreError("REMOTE_AGENT_CREDENTIAL_UNAVAILABLE") from None


def _binding(row):
    return {key: row[key] for key in ("tenant_id", "id", "revision", "header_name")}


def _decrypt(cipher, row):
    if row["storage_version"] != 1:
        raise CoreError("CHECKPOINT_INVALID")
    if row["encrypted_payload"] is None:
        return None
    if cipher is None:
        raise CoreError("REMOTE_AGENT_CREDENTIAL_UNAVAILABLE")
    try:
        value = json.loads(cipher.decrypt(bytes(row["encrypted_payload"])))
        if value != {"version": 1, **_binding(row), "header_value": value.get("header_value")}:
            raise ValueError()
        _header_value(value["header_value"])
        return value["header_value"]
    except (InvalidToken, ValueError, TypeError, KeyError, AttributeError, CoreError):
        raise CoreError("REMOTE_AGENT_CREDENTIAL_UNAVAILABLE") from None


def _new_row(cipher, tenant_id, peer_id, values, *, actor_id, now, previous=None):
    row = {key: values[key] for key in _FIELDS}
    row.update(tenant_id=tenant_id, id=peer_id,
               name=previous["name"] if previous else values["name"],
               revision=previous["revision"] + 1 if previous else 1,
               storage_version=1, actor_id=actor_id, created_at=now, encrypted_payload=None)
    secret = values.get("header_value")
    if "header_value" not in values and previous is not None:
        secret = _decrypt(cipher, previous)
    if secret is not None:
        if cipher is None:
            raise CoreError("REMOTE_AGENT_CREDENTIAL_UNAVAILABLE")
        row["encrypted_payload"] = cipher.encrypt(json.dumps(
            {"version": 1, **_binding(row), "header_value": secret},
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8"))
    return row


def _metadata(row):
    if row["storage_version"] != 1:
        raise CoreError("CHECKPOINT_INVALID")
    return {**{key: row[key] for key in _METADATA}, "has_header_value": row["encrypted_payload"] is not None}


def _page(limit, after_id):
    if type(limit) is not int or not 1 <= limit <= 101:
        raise CoreError("REQUEST_INVALID")
    if after_id is not None:
        _identifier(after_id, code="REQUEST_INVALID")


class InMemoryRemoteRegistry:
    def __init__(self, encryption_key=None, *, clock=time.time):
        self._cipher = _cipher(Fernet.generate_key() if encryption_key is None else encryption_key)
        self._clock = clock
        self._lock = threading.RLock()
        self._current = {}
        self._revisions = {}
        self._deleted = set()

    def create(self, tenant_id, values, *, actor_id):
        _validate(values, create=True)
        with self._lock:
            if any(scope == tenant_id and row["name"] == values["name"] and (scope, peer_id) not in self._deleted
                   for (scope, peer_id), row in self._current.items()):
                raise CoreError("REMOTE_AGENT_CONFLICT")
            row = _new_row(self._cipher, tenant_id, str(uuid.uuid4()), values, actor_id=actor_id, now=self._clock())
            self._save(row)
            return _metadata(row)

    def _save(self, row):
        self._current[(row["tenant_id"], row["id"])] = row
        self._revisions[(row["tenant_id"], row["id"], row["revision"])] = row

    def _get(self, tenant_id, peer_id, revision=None):
        _identifier(peer_id)
        if revision is not None:
            _revision(revision)
        row = (self._current.get((tenant_id, peer_id)) if revision is None
               else self._revisions.get((tenant_id, peer_id, revision)))
        if row is None or (revision is None and (tenant_id, peer_id) in self._deleted):
            raise CoreError("REMOTE_AGENT_NOT_FOUND")
        _metadata(row)
        return row

    def update(self, tenant_id, peer_id, values, *, expected_revision, actor_id):
        _validate(values)
        _revision(expected_revision)
        with self._lock:
            previous = self._get(tenant_id, peer_id)
            if previous["revision"] != expected_revision:
                raise CoreError("REMOTE_AGENT_CONFLICT")
            row = _new_row(self._cipher, tenant_id, peer_id, values, actor_id=actor_id,
                           now=self._clock(), previous=previous)
            self._save(row)
            return _metadata(row)

    def disable(self, tenant_id, peer_id, *, expected_revision, actor_id):
        _revision(expected_revision)
        with self._lock:
            previous = self._get(tenant_id, peer_id)
            values = {key: previous[key] for key in _FIELDS}
            return self.update(tenant_id, peer_id, {**values, "enabled": False},
                               expected_revision=expected_revision, actor_id=actor_id)

    def list(self, tenant_id, *, limit=50, after_id=None):
        _page(limit, after_id)
        with self._lock:
            if after_id is not None and (tenant_id, after_id) not in self._current:
                raise CoreError("REQUEST_INVALID")
            return [_metadata(row) for (tenant, peer_id), row in sorted(self._current.items())
                    if tenant == tenant_id and (tenant, peer_id) not in self._deleted
                    and (after_id is None or peer_id > after_id)][:limit]

    def delete(self, tenant_id, peer_id, *, expected_revision, actor_id):
        _revision(expected_revision)
        with self._lock:
            previous = self._get(tenant_id, peer_id)
            values = {key: previous[key] for key in _FIELDS}
            removed = self.update(tenant_id, peer_id, {**values, "enabled": False, "header_value": None},
                                  expected_revision=expected_revision, actor_id=actor_id)
            self._deleted.add((tenant_id, peer_id))
            return removed

    def get_revision(self, tenant_id, peer_id, revision=None, *, connection=None):
        with self._lock:
            return _metadata(self._get(tenant_id, peer_id, revision))

    @contextmanager
    def pin_current(self, tenant_id, peer_id, revision):
        _revision(revision)
        with self._lock:
            current = self._get(tenant_id, peer_id)
            if not current["enabled"] or current["revision"] != revision:
                raise CoreError("REMOTE_AGENT_CONFLICT")
            yield None

    def resolve_headers(self, tenant_id, peer_id, revision, *, connection=None):
        with self._lock:
            row = self._get(tenant_id, peer_id, revision)
            secret = _decrypt(self._cipher, row)
            return {row["header_name"]: secret} if secret is not None else {}


class PostgresRemoteRegistry:
    def __init__(self, database, encryption_key=None):
        self.database = database
        self._cipher = _cipher(encryption_key)

    @staticmethod
    def _insert_revision(connection, row):
        connection.execute(
            """INSERT INTO core_remote_agent_revisions
               (tenant_id, id, revision, storage_version, url, description, enabled,
                header_name, encrypted_payload, actor_id, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            tuple(row[key] for key in ("tenant_id", "id", "revision", "storage_version", "url",
                                      "description", "enabled", "header_name", "encrypted_payload", "actor_id", "created_at")),
        )

    def create(self, tenant_id, values, *, actor_id, connection=None):
        _validate(values, create=True)
        with self.database.pool.connection() if connection is None else nullcontext(connection) as connection:
            with connection.transaction():
                row = _new_row(self._cipher, tenant_id, str(uuid.uuid4()), values, actor_id=actor_id,
                               now=self._now(connection))
                inserted = connection.execute(
                    """INSERT INTO core_remote_agents (tenant_id, id, name, revision)
                       VALUES (%s, %s, %s, 1) ON CONFLICT DO NOTHING RETURNING id""",
                    (tenant_id, row["id"], row["name"]),
                ).fetchone()
                if inserted is None:
                    raise CoreError("REMOTE_AGENT_CONFLICT")
                self._insert_revision(connection, row)
                return _metadata(row)

    def import_legacy(self, tenant_id, entries):
        """One explicit migration into an empty company registry, all or none."""
        if not isinstance(entries, list) or not 1 <= len(entries) <= _IMPORT_MAX_PEERS:
            raise CoreError("REMOTE_IMPORT_INVALID")
        for values in entries:
            _validate(values, create=True)
        with self.database.transaction() as connection:
            # ponytail: a bounded, one-time import locks all registry writes; run before workers.
            connection.execute("LOCK TABLE core_remote_agents IN SHARE ROW EXCLUSIVE MODE")
            if connection.execute("SELECT id FROM core_remote_agents WHERE tenant_id = %s LIMIT 1",
                                  (tenant_id,)).fetchone() is not None:
                raise CoreError("REMOTE_IMPORT_NOT_EMPTY")
            actor = "migration:" + connection.execute("SELECT current_user AS actor").fetchone()["actor"]
            return [self.create(tenant_id, values, actor_id=actor, connection=connection) for values in entries]

    @staticmethod
    def _now(connection):
        return connection.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision AS now").fetchone()["now"]

    @staticmethod
    def _get(connection, tenant_id, peer_id, revision=None, *, lock=False):
        _identifier(peer_id)
        if revision is not None:
            _revision(revision)
        if lock:
            current = connection.execute(
                "SELECT revision FROM core_remote_agents WHERE tenant_id = %s AND id = %s AND NOT deleted FOR UPDATE",
                (tenant_id, peer_id),
            ).fetchone()
            if current is None:
                raise CoreError("REMOTE_AGENT_NOT_FOUND")
            revision = current["revision"]
        # Exact revisions also verify restored backups before their schema upgrade.
        visibility = " AND NOT a.deleted" if revision is None else ""
        row = connection.execute(
            """SELECT r.*, a.name FROM core_remote_agents a
               JOIN core_remote_agent_revisions r ON r.tenant_id = a.tenant_id AND r.id = a.id
               AND r.revision = COALESCE(%s, a.revision)
               WHERE a.tenant_id = %s AND a.id = %s""" + visibility,
            (revision, tenant_id, peer_id),
        ).fetchone()
        if row is None:
            raise CoreError("REMOTE_AGENT_NOT_FOUND")
        _metadata(row)
        return row

    def _update(self, connection, tenant_id, peer_id, values, expected_revision, actor_id, *, deleted=False):
        previous = self._get(connection, tenant_id, peer_id, lock=True)
        if previous["revision"] != expected_revision:
            raise CoreError("REMOTE_AGENT_CONFLICT")
        if values is None:
            values = {**{key: previous[key] for key in _FIELDS}, "enabled": False}
        if deleted:
            values = {**values, "header_value": None}
        row = _new_row(self._cipher, tenant_id, peer_id, values, actor_id=actor_id,
                       now=self._now(connection), previous=previous)
        self._insert_revision(connection, row)
        connection.execute("UPDATE core_remote_agents SET revision = %s, deleted = %s WHERE tenant_id = %s AND id = %s",
                           (row["revision"], deleted, tenant_id, peer_id))
        return _metadata(row)

    def update(self, tenant_id, peer_id, values, *, expected_revision, actor_id):
        _validate(values)
        _revision(expected_revision)
        with self.database.transaction() as connection:
            return self._update(connection, tenant_id, peer_id, values, expected_revision, actor_id)

    def disable(self, tenant_id, peer_id, *, expected_revision, actor_id):
        _revision(expected_revision)
        with self.database.transaction() as connection:
            return self._update(connection, tenant_id, peer_id, None, expected_revision, actor_id)

    def delete(self, tenant_id, peer_id, *, expected_revision, actor_id):
        _revision(expected_revision)
        with self.database.transaction() as connection:
            return self._update(connection, tenant_id, peer_id, None, expected_revision, actor_id, deleted=True)

    def list(self, tenant_id, *, limit=50, after_id=None):
        _page(limit, after_id)
        with self.database.pool.connection() as connection:
            if after_id is not None and connection.execute(
                "SELECT 1 FROM core_remote_agents WHERE tenant_id = %s AND id = %s", (tenant_id, after_id),
            ).fetchone() is None:
                raise CoreError("REQUEST_INVALID")
            rows = connection.execute(
                """SELECT r.*, a.name FROM core_remote_agents a
                   JOIN core_remote_agent_revisions r ON r.tenant_id = a.tenant_id
                     AND r.id = a.id AND r.revision = a.revision
                   WHERE a.tenant_id = %s AND NOT a.deleted AND (%s::text IS NULL OR a.id > %s)
                   ORDER BY a.id LIMIT %s""", (tenant_id, after_id, after_id, limit),
            ).fetchall()
            return [_metadata(row) for row in rows]

    def get_revision(self, tenant_id, peer_id, revision=None, *, connection=None):
        with self.database.pool.connection() if connection is None else nullcontext(connection) as connection:
            return _metadata(self._get(connection, tenant_id, peer_id, revision))

    @contextmanager
    def pin_current(self, tenant_id, peer_id, revision):
        _revision(revision)
        with self.database.transaction() as connection:
            current = self._get(connection, tenant_id, peer_id, lock=True)
            if not current["enabled"] or current["revision"] != revision:
                raise CoreError("REMOTE_AGENT_CONFLICT")
            yield connection

    def resolve_headers(self, tenant_id, peer_id, revision, *, connection=None):
        with self.database.pool.connection() if connection is None else nullcontext(connection) as connection:
            row = self._get(connection, tenant_id, peer_id, revision)
            secret = _decrypt(self._cipher, row)
            return {row["header_name"]: secret} if secret is not None else {}
