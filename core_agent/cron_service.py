"""One bounded schedule pass using the existing recovery thread and ASGI loop."""
from __future__ import annotations

import asyncio
import inspect
import json
import math
import threading
import time
from types import SimpleNamespace

import psycopg
from psycopg.rows import dict_row
from a2a.server.context import ServerCallContext

from .auth import ScopeUser
from .cron import _text


class CronCoordinator:
    def __init__(self, store, tenant_id, on_admitted=None, log=None):
        self.store = store
        self.tenant_id = _text(tenant_id)
        self._leader_key = json.dumps(["core-agent-cron-leader", self.tenant_id], separators=(",", ":"))
        self.on_admitted = on_admitted
        self.log = log
        self._guard = threading.Lock()
        self._pg_guard = threading.Lock()
        self._closed = False
        self._loop = None
        self._scheduled = False
        self._task = None
        self._connection = None
        self._leader = False
        self._cutoff = None
        self._last_completed = None
        self._after = None

    def bind_loop(self, loop):
        with self._guard:
            if self._closed:
                return
            if self._loop is not None and self._loop is not loop:
                raise RuntimeError("Cron coordinator is already bound to another loop")
            self._loop = loop

    def _log(self, event, error):
        if self.log:
            try:
                self.log(event, error_code=getattr(error, "code", type(error).__name__))
            except Exception:
                pass

    def _context(self, row):
        # This origin exists only in server code. It is neither an owner nor an
        # external credential, and carries exactly the canonical execution scope.
        owner = _text(row["owner_id"])
        principal = SimpleNamespace(actor_id="cron:" + _text(row["id"]), tenant=self.tenant_id,
                                    owner_id=owner, is_owner=False, is_external=False)
        return ServerCallContext(user=ScopeUser(owner), tenant=self.tenant_id, state={"principal": principal})

    def _handoff(self, admitted):
        if admitted is None or admitted.run_id is None or self.on_admitted is None:
            return
        try:
            result = self.on_admitted(admitted, self.tenant_id)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise TypeError("Cron admission handoff must be synchronous")
        except Exception as error:
            # The commit already happened. Recovery can use lease expiry if this
            # optimization fails; it must not repeat the occurrence or admission.
            self._log("cron.handoff_failed", error)

    def _begin_pass(self, connection=None):
        if self._cutoff is None or (self._last_completed is not None and time.monotonic() - self._last_completed > 60):
            self._cutoff = self.store._now(connection)

    def _candidates(self, connection=None):
        rows = self.store.due(self.tenant_id, limit=100, after=self._after, connection=connection)
        if not rows and self._after is not None:
            self._after = None
            rows = self.store.due(self.tenant_id, limit=100, connection=connection)
        return rows

    def tick(self):
        if self.store.database is not None:
            admitted = []
            with self._pg_guard:
                if not self._closed:
                    self._postgres_pass(admitted)
            for result in admitted:
                self._handoff(result)
            return
        with self._guard:
            if self._closed or self._scheduled or self._loop is None:
                return
            self._scheduled = True
            loop = self._loop
        try:
            loop.call_soon_threadsafe(self._start_memory_pass)
        except RuntimeError as error:
            with self._guard:
                self._scheduled = False
            self._log("cron.scan_failed", error)

    def _start_memory_pass(self):
        with self._guard:
            if self._closed:
                self._scheduled = False
                return
            self._task = self._loop.create_task(self._memory_pass(), name="cron-scan")

    async def _memory_pass(self):
        try:
            self._begin_pass()
            for row in self._candidates():
                if self._closed:
                    return
                self._after = row["id"]
                try:
                    admitted = await self.store.occur_memory(self._context(row), row["id"], row["revision"], cutoff=self._cutoff)
                except Exception as error:
                    self._log("cron.occurrence_failed", error)
                    continue
                self._handoff(admitted)
            self._last_completed = time.monotonic()
        except Exception as error:
            self._log("cron.scan_failed", error)
        finally:
            with self._guard:
                self._task = None
                self._scheduled = False

    def _postgres_pass(self, admitted):
        try:
            if self._connection is None:
                pool = self.store.database.pool
                self._connection = psycopg.connect(pool.conninfo, autocommit=True, row_factory=dict_row,
                                                   connect_timeout=max(1, math.ceil(pool.timeout)))
            if not self._leader:
                self._leader = self._connection.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS acquired", (self._leader_key,)).fetchone()["acquired"]
                if not self._leader:
                    return
                # A new session/leadership always establishes a fresh DB-clock
                # cutoff, even if this process was previously a healthy leader.
                self._cutoff = None
                self._last_completed = None
                self._after = None
            self._begin_pass(self._connection)
            for row in self._candidates(self._connection):
                if self._closed:
                    return
                self._after = row["id"]
                try:
                    # The session holding the advisory lock is also the only
                    # connection allowed to commit automatic admission/events.
                    with self._connection.transaction():
                        result = self.store.occur(self._context(row), row["id"], row["revision"],
                                                 connection=self._connection, cutoff=self._cutoff)
                    if result is not None:
                        admitted.append(result)
                except (psycopg.OperationalError, psycopg.InterfaceError):
                    raise
                except Exception as error:
                    self._log("cron.occurrence_failed", error)
            self._last_completed = time.monotonic()
        except Exception as error:
            # No reconnect inside a transaction and no optimistic handoff after
            # an unknown COMMIT. A fresh session gets a new recovery cutoff.
            self._log("cron.scan_failed", error)
            self._disconnect()

    def _disconnect(self):
        connection, self._connection = self._connection, None
        leader, self._leader = self._leader, False
        self._cutoff = None
        self._last_completed = None
        self._after = None
        if connection is not None:
            try:
                if leader:
                    # Wait for the server's release acknowledgement; closing the
                    # socket alone can race a successor's immediate lock attempt.
                    connection.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (self._leader_key,))
            except Exception as error:
                self._log("cron.release_failed", error)
            finally:
                try:
                    connection.close()
                except Exception as error:
                    self._log("cron.close_failed", error)

    def _stop(self):
        with self._guard:
            self._closed = True
            return self._loop, self._task

    def _close_postgres(self):
        # tick owns this lock only for synchronous DB work, never across await.
        # Closing waits for its transaction to commit/rollback before releasing
        # the session advisory lock, so a successor cannot race the old writer.
        with self._pg_guard:
            self._disconnect()

    async def aclose(self):
        _loop, task = self._stop()
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self.store.database is not None:
            await asyncio.to_thread(self._close_postgres)

    def close(self):
        loop, task = self._stop()
        if task is not None and loop is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
        self._close_postgres()
