"""Fenced PostgreSQL workspace references; object I/O precedes DB mutation locks."""
from contextlib import contextmanager
import os
import stat
import threading

from psycopg.types.json import Jsonb

from .errors import CoreError
from .workspace import WorkspaceBinding
from .workspace_durability import SNAPSHOT_INVALID_REASONS, prune_manifest


class PostgresWorkspaceDurability:
    def __init__(self, database, codec, environment_manager):
        self.database, self.codec, self.manager = database, codec, environment_manager
        self._initialized = {}
        self._resident = {}
        self._publication_recovery = {}
        self.log = None
        # ponytail: one cache gate per Pod; split by chat if upload contention matters.
        self._lock = threading.RLock()

    def _report(self, operation, manifest=None, error=None):
        if self.log is None:
            return
        files = [entry for entry in (manifest or {}).get("entries", {}).values() if entry["type"] == "file"]
        reason = error.data.get("reason") if error is not None and error.code == "WORKSPACE_SNAPSHOT_INVALID" else None
        self.log("workspace.storage", operation=operation, outcome="failed" if error else "succeeded",
                 file_count=len(files), bytes=sum(entry["size"] for entry in files),
                 **({"reason": reason} if type(reason) is str and reason in SNAPSHOT_INVALID_REASONS else {}),
                 **({"error_code": error.code} if error else {}))

    def _capacity(self, binding, manifest):
        entries = manifest["entries"]
        size = sum(entry["size"] for entry in entries.values() if entry["type"] == "file")
        return self._check_capacity(binding, size, len(entries))

    def _check_capacity(self, binding, size, count):
        if size > self.codec.max_bytes or count > self.codec.max_files:
            raise CoreError("WORKSPACE_SNAPSHOT_LIMIT")
        others = [value for scope, value in self._resident.items() if scope != binding]
        if (size + sum(value[0] for value in others) > 2 * self.codec.max_bytes
                or count + sum(value[1] for value in others) > 2 * self.codec.max_files):
            raise CoreError("WORKSPACE_CACHE_LIMIT")
        return size, count

    def reserve_publication(self, binding, destination, directory):
        """Bound an incoming directory before rename, including uncommitted copies."""
        with self.local_lock(binding):
            self.manager.require_workspace_quiescent(binding)
            parts = self.codec.workspaces.path_parts(destination)
            with self.codec.workspaces.open_workspace(binding) as root:
                current = self.codec._scan(root) if root is not None else {}
            size = sum(identity[4] for identity in current.values() if stat.S_ISREG(identity[2]))
            count = len(current) - int("" in current)
            for index in range(1, len(parts) + 1):
                parent = "/".join(parts[:index])
                if parent in current and not stat.S_ISDIR(current[parent][2]):
                    raise CoreError("WORKSPACE_SNAPSHOT_INVALID", data={"reason": "manifest_parent"})
            if destination not in current:
                added = self.codec._scan(directory)
                if any(len(parts) + (len(path.split("/")) if path else 0) > self.codec.workspaces.depth_limit
                       for path in added):
                    raise CoreError("WORKSPACE_SNAPSHOT_LIMIT")
                size += sum(identity[4] for identity in added.values() if stat.S_ISREG(identity[2]))
                count += len(added) + sum("/".join(parts[:index]) not in current for index in range(1, len(parts)))
            # A failed post-rename upload still consumes resident capacity. A
            # retry/checkpoint/cleanup recomputes this conservative reservation.
            self._resident[binding] = self._check_capacity(binding, size, count)

    def _remember(self, binding, revision, manifest):
        self._resident[binding] = self._capacity(binding, manifest)
        self._initialized[binding] = revision

    def cache_empty(self, binding):
        with self.local_lock(binding):
            self.manager.require_workspace_quiescent(binding)
            with self.codec.workspaces.open_workspace(binding) as root:
                if root is None:
                    return True
                with os.scandir(root) as entries:
                    return next(entries, None) is None

    @contextmanager
    def local_lock(self, binding):
        if not isinstance(binding, WorkspaceBinding):
            raise CoreError("WORKSPACE_SCOPE_REQUIRED")
        if getattr(self.codec.blob_store, "tenant_id", binding.tenant_id) != binding.tenant_id:
            raise CoreError("WORKSPACE_SCOPE_CONFLICT")
        with self._lock:
            self._resolve_publication(binding)
            yield

    def reconcile_publication(self, binding, resolve):
        """Fence this local cache until DB-only publication readback succeeds."""
        with self._lock:
            self._publication_recovery[binding] = resolve
            return self._resolve_publication(binding)

    def _resolve_publication(self, binding):
        resolve = self._publication_recovery.get(binding)
        if resolve is None:
            return None
        try:
            result = resolve()
        except Exception as error:
            raise CoreError("FILE_PUBLICATION_PENDING", retryable=True) from error
        del self._publication_recovery[binding]
        return result

    @staticmethod
    def binding(record):
        return WorkspaceBinding(record.tenant_id, record.owner_id, record.context_id)

    def _state(self, connection, binding, *, lock=False, cleanup=False):
        row = connection.execute("""SELECT owner_id,workspace_revision FROM core_chats
            WHERE tenant_id=%s AND context_id=%s""" + (" FOR NO KEY UPDATE" if lock else ""),
            (binding.tenant_id, binding.context_id)).fetchone()
        if row is None or row["owner_id"] != binding.owner_id:
            raise CoreError("WORKSPACE_SCOPE_CONFLICT")
        if not cleanup and connection.execute("""SELECT 1 FROM core_workspace_cleanups
                WHERE tenant_id=%s AND owner_id=%s AND context_id=%s AND state<>'completed' LIMIT 1""",
                (binding.tenant_id, binding.owner_id, binding.context_id)).fetchone():
            raise CoreError("WORKSPACE_CLEANUP_PENDING")
        pointer = connection.execute("""SELECT revision,manifest FROM core_workspace_manifests
            WHERE tenant_id=%s AND owner_id=%s AND context_id=%s""" + (" FOR UPDATE" if lock else ""),
            (binding.tenant_id, binding.owner_id, binding.context_id)).fetchone()
        manifest = pointer["manifest"] if pointer else {
            "version": 1, "scope": self.codec.workspaces._scope(binding), "entries": {}}
        self.codec._validate(binding, manifest)
        return {"revision": pointer["revision"] if pointer else 0, "manifest": manifest,
                "cleanup_revision": row["workspace_revision"]}

    def ensure_restored(self, binding):
        try:
            return self._ensure_restored(binding)
        except CoreError as error:
            self._report("restore", error=error)
            raise

    def _ensure_restored(self, binding):
        with self.local_lock(binding):
            with self.database.transaction() as connection:
                state = self._state(connection, binding, cleanup=binding in self._initialized)
            if binding in self._initialized:
                if self._initialized[binding] != state["revision"]:
                    raise CoreError("WORKSPACE_RESTORE_CONFLICT")
                return
            self.manager.require_workspace_quiescent(binding)
            self._capacity(binding, state["manifest"])
            self.codec.restore(binding, state["manifest"])
            with self.database.transaction() as connection:
                current = self._state(connection, binding)
                if current != state:
                    raise CoreError("WORKSPACE_RESTORE_CONFLICT")
            self._remember(binding, state["revision"], state["manifest"])
            self._report("restore", state["manifest"])

    @staticmethod
    def _run(connection, record, token, *, lock=False):
        current = connection.execute("""SELECT version,snapshot,lease_token,
            lease_expires_at>EXTRACT(EPOCH FROM clock_timestamp()) AS lease_live
            FROM core_runs WHERE run_id=%s AND tenant_id=%s AND owner_id=%s AND context_id=%s"""
            + (" FOR UPDATE" if lock else ""),
            (record.run_id, record.tenant_id, record.owner_id, record.context_id)).fetchone()
        if current is None:
            raise CoreError("TASK_NOT_FOUND")
        if current["version"] != record.version:
            raise CoreError("SESSION_CONFLICT")
        if token is not None and (current["lease_token"] != token or not current["lease_live"]):
            raise CoreError("LEASE_LOST")
        owner = record.snapshot.get("execution_owner")
        actual = current["snapshot"].get("execution_owner")
        expected = owner
        # Stop confirmation is an operational receipt without a version bump.
        # The same live generation may still checkpoint its known tool result;
        # the canonical workflow transition preserves the confirmed receipt.
        if (isinstance(owner, dict) and isinstance(actual, dict)
                and owner.get("cleanup_confirmed") is False
                and actual.get("cleanup_confirmed") is True
                and actual.get("local_execution_pending") is False):
            expected = {**owner, "cleanup_confirmed": True, "local_execution_pending": False}
            if "cleanup_proof" not in owner and "cleanup_proof" in actual:
                expected["cleanup_proof"] = actual["cleanup_proof"]
        # A retried terminal commit acquires a new lease without creating a new
        # process generation. Its persisted intent already retired this owner.
        retired_terminal = (owner is not None and owner.get("cleanup_confirmed") is True
                            and bool(record.snapshot.get("terminal_intent")))
        if expected != actual or (owner is not None and token is not None
                and owner["generation"] != token and not retired_terminal):
            raise CoreError("LEASE_LOST")

    def _publish(self, connection, binding, previous, manifest):
        if previous["manifest"] == manifest:
            return previous["revision"]
        digests = {entry["sha256"] for entry in manifest["entries"].values() if entry["type"] == "file"}
        self.codec.blob_store.pin(connection, digests)
        revision = previous["revision"] + 1
        connection.execute("""INSERT INTO core_workspace_manifests(tenant_id,owner_id,context_id,revision,manifest)
            VALUES (%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,owner_id,context_id)
            DO UPDATE SET revision=EXCLUDED.revision,manifest=EXCLUDED.manifest""",
            (binding.tenant_id, binding.owner_id, binding.context_id, revision, Jsonb(manifest)))
        return revision

    def transition(self, record, lease_token, operation):
        """Commit captured bytes and the supplied workflow-only operation together."""
        try:
            return self._transition(record, lease_token, operation)
        except CoreError as error:
            self._report("checkpoint", error=error)
            raise

    def _transition(self, record, lease_token, operation):
        binding = self.binding(record)
        with self.local_lock(binding):
            self.ensure_restored(binding)
            self.manager.require_workspace_quiescent(binding)
            with self.database.transaction() as connection:
                before = self._state(connection, binding)
                self._run(connection, record, lease_token)
            manifest = self.codec.prepare(binding, before["manifest"])
            self._capacity(binding, manifest)
            with self.database.transaction() as connection:
                current = self._state(connection, binding, lock=True)
                self._run(connection, record, lease_token, lock=True)
                if current != before:
                    raise CoreError("WORKSPACE_RESTORE_CONFLICT")
                revision = self._publish(connection, binding, before, manifest)
                result = operation(connection)
            self._remember(binding, revision, manifest)
            self._report("checkpoint", manifest)
            return result

    def prune(self, connection, binding, deleted_paths):
        previous = self._state(connection, binding, lock=True, cleanup=True)
        return self._publish(connection, binding, previous, prune_manifest(previous["manifest"], deleted_paths))

    def note_cleanup(self, binding, exact_revision):
        """Called while holding local_lock, strictly after the cleanup commit."""
        with self.local_lock(binding):
            with self.database.transaction() as connection:
                current = self._state(connection, binding, cleanup=True)
                if current["revision"] != exact_revision:
                    raise CoreError("WORKSPACE_RESTORE_CONFLICT")
            if binding in self._initialized:
                self._remember(binding, exact_revision, current["manifest"])
