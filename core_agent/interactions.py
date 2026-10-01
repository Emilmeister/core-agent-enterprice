"""Company settings and owner policy; callers supply authenticated scope and origins."""

import json
import hashlib
from contextlib import contextmanager
from dataclasses import dataclass, replace

from .errors import CoreError


TIMEOUT_KEYS = frozenset({
    "hitl_timeout_seconds", "owner_answer_timeout_seconds", "guardrails_timeout_seconds",
})
SETTINGS_KEYS = TIMEOUT_KEYS | {"attachment_limit_bytes", "remote_timeout_seconds", "remote_poll_interval_seconds"}
DEFAULT_ATTACHMENT_LIMIT = 25_000_000
POLICY_MODES = ("allow", "require_hitl", "deny")


def tool_origin(canonical_name, mcp_target=None):
    """Use the trusted index, never infer an MCP identity by splitting its alias."""
    return ("mcp:" + json.dumps(mcp_target, separators=(",", ":"))
            if mcp_target else "builtin:" + canonical_name)


def interaction_digest(wait):
    immutable = {key: getattr(wait, key) for key in
                 ("wait_id", "kind", "generation", "source_id", "subject", "continuation")}
    return "sha256:" + hashlib.sha256(json.dumps(
        immutable, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


@dataclass(frozen=True)
class OwnerSettings:
    tenant_id: str
    revision: int = 0
    hitl_timeout_seconds: int = 86400
    owner_answer_timeout_seconds: int = 86400
    guardrails_timeout_seconds: int = 86400
    attachment_limit_bytes: int = DEFAULT_ATTACHMENT_LIMIT
    remote_timeout_seconds: int = 86400
    remote_poll_interval_seconds: int = 300


@dataclass(frozen=True)
class ToolPolicy:
    tenant_id: str
    canonical_name: str
    origin: str
    mode: str = "require_hitl"
    guardrails_exempt: bool = False
    revision: int = 0


def _validate_revision(expected_revision):
    if type(expected_revision) is not int or not 0 <= expected_revision < 2**63 - 1:
        raise CoreError("SETTINGS_INVALID", "expected_revision must be a nonnegative integer")


def _validate_settings(values, expected_revision):
    _validate_revision(expected_revision)
    if not isinstance(values, dict) or not TIMEOUT_KEYS <= values.keys() <= SETTINGS_KEYS or any(
        type(value) is not int or not 1 <= value <= 2147483647 for value in values.values()
    ):
        raise CoreError("SETTINGS_INVALID", "provide all required timeouts and positive bounded integer settings")


def _validate_policy(mode, guardrails_exempt, expected_revision):
    _validate_revision(expected_revision)
    if mode not in POLICY_MODES or type(guardrails_exempt) is not bool:
        raise CoreError("SETTINGS_INVALID", "invalid tool policy")


def _check_revision(current, expected_revision):
    if current.revision != expected_revision:
        raise CoreError("SETTINGS_CONFLICT", data={"revision": current.revision})


class InMemoryInteractionStore:
    """Test adapter; the workflow lock protects company-wide wait resolution."""

    def __init__(self, workflow_store):
        self.workflow_store = workflow_store
        self._settings = {}
        self._policies = {}
        # Settings reads also happen inside admission's workflow guard. Sharing
        # it keeps policy + wait changes atomic without opposing lock orders.
        self._lock = workflow_store._lock

    def get_settings(self, tenant_id):
        with self._lock:
            return self._settings.get(tenant_id, OwnerSettings(tenant_id))

    def update_settings(self, tenant_id, values, expected_revision):
        _validate_settings(values, expected_revision)
        with self._lock:
            current = self.get_settings(tenant_id)
            _check_revision(current, expected_revision)
            updated = replace(current, revision=current.revision + 1, **values)
            self._settings[tenant_id] = updated
            return updated

    def get_policy(self, tenant_id, canonical_name, origin):
        with self._lock:
            key = (tenant_id, canonical_name, origin)
            return self._policies.get(key, ToolPolicy(*key))

    @contextmanager
    def policy_scope(self, tenant_id, canonical_name, origin):
        """Hold this scope through dispatch intent or approval admission."""
        with self._lock:
            yield self.get_policy(tenant_id, canonical_name, origin), None

    def update_policy(
        self, tenant_id, canonical_name, origin, *, mode, guardrails_exempt,
        expected_revision, actor_id,
    ):
        _validate_policy(mode, guardrails_exempt, expected_revision)
        with self.policy_scope(tenant_id, canonical_name, origin) as (current, _):
            _check_revision(current, expected_revision)
            updated = replace(current, mode=mode, guardrails_exempt=guardrails_exempt,
                              revision=current.revision + 1)
            with self.workflow_store._lock:
                if mode == "deny":
                    for wait in tuple(self.workflow_store._waits.values()):
                        if (wait.tenant_id == tenant_id and wait.kind == "tool_approval"
                                and wait.outcome is None
                                and wait.subject.get("tool_name") == canonical_name
                                and wait.subject.get("origin") == origin):
                            self.workflow_store.resolve_wait(
                                wait.wait_id, tenant_id=tenant_id,
                                outcome={"reason": "policy_denied", "code": "POLICY_DENIED"},
                                actor_id=actor_id,
                            )
                self._policies[(tenant_id, canonical_name, origin)] = updated
            return updated


class PostgresInteractionStore:
    """Policy lock precedes sorted run locks and wait locks in every transaction."""

    def __init__(self, database, workflow_store):
        self.database = database
        self.workflow_store = workflow_store

    def get_settings(self, tenant_id):
        with self.database.pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM core_owner_settings WHERE tenant_id = %s", (tenant_id,),
            ).fetchone()
        return OwnerSettings(**row) if row is not None else OwnerSettings(tenant_id)

    def update_settings(self, tenant_id, values, expected_revision):
        _validate_settings(values, expected_revision)
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO core_owner_settings (tenant_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (tenant_id,),
            )
            row = connection.execute(
                """UPDATE core_owner_settings SET hitl_timeout_seconds = %s,
                   owner_answer_timeout_seconds = %s, guardrails_timeout_seconds = %s,
                   attachment_limit_bytes = COALESCE(%s, attachment_limit_bytes),
                   remote_timeout_seconds = COALESCE(%s, remote_timeout_seconds),
                   remote_poll_interval_seconds = COALESCE(%s, remote_poll_interval_seconds),
                   revision = revision + 1 WHERE tenant_id = %s AND revision = %s
                   RETURNING *""",
                (values["hitl_timeout_seconds"], values["owner_answer_timeout_seconds"],
                 values["guardrails_timeout_seconds"], values.get("attachment_limit_bytes"),
                 values.get("remote_timeout_seconds"), values.get("remote_poll_interval_seconds"), tenant_id, expected_revision),
            ).fetchone()
            if row is None:
                raise CoreError("SETTINGS_CONFLICT")
            return OwnerSettings(**row)

    def get_policy(self, tenant_id, canonical_name, origin):
        key = (tenant_id, canonical_name, origin)
        with self.database.pool.connection() as connection:
            row = connection.execute(
                """SELECT * FROM core_tool_policies
                   WHERE tenant_id = %s AND canonical_name = %s AND origin = %s""", key,
            ).fetchone()
        return ToolPolicy(**row) if row is not None else ToolPolicy(*key)

    @contextmanager
    def policy_scope(self, tenant_id, canonical_name, origin):
        """Share this transaction with dispatch intent or approval admission."""
        key = (tenant_id, canonical_name, origin)
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO core_tool_policies (tenant_id, canonical_name, origin)
                   VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""", key,
            )
            row = connection.execute(
                """SELECT * FROM core_tool_policies
                   WHERE tenant_id = %s AND canonical_name = %s AND origin = %s FOR UPDATE""", key,
            ).fetchone()
            yield ToolPolicy(**row), connection

    def update_policy(
        self, tenant_id, canonical_name, origin, *, mode, guardrails_exempt,
        expected_revision, actor_id,
    ):
        _validate_policy(mode, guardrails_exempt, expected_revision)
        with self.policy_scope(tenant_id, canonical_name, origin) as (current, connection):
            _check_revision(current, expected_revision)
            row = connection.execute(
                """UPDATE core_tool_policies SET mode = %s, guardrails_exempt = %s,
                   revision = revision + 1
                   WHERE tenant_id = %s AND canonical_name = %s AND origin = %s RETURNING *""",
                (mode, guardrails_exempt, tenant_id, canonical_name, origin),
            ).fetchone()
            if mode == "deny":
                runs = connection.execute(
                    """SELECT run_id FROM core_runs WHERE tenant_id = %s AND run_id IN (
                       SELECT run_id FROM core_waits WHERE tenant_id = %s
                       AND kind = 'tool_approval' AND resolved_at IS NULL
                       AND subject->>'tool_name' = %s AND subject->>'origin' = %s)
                       ORDER BY run_id FOR UPDATE""",
                    (tenant_id, tenant_id, canonical_name, origin),
                ).fetchall()
                for run in runs:
                    current_run = self.workflow_store.get(
                        run["run_id"], tenant_id=tenant_id, connection=connection,
                    )
                    waits = connection.execute(
                        """SELECT wait_id FROM core_waits WHERE run_id = %s AND tenant_id = %s
                           AND kind = 'tool_approval' AND resolved_at IS NULL
                           AND subject->>'tool_name' = %s AND subject->>'origin' = %s
                           ORDER BY wait_id FOR UPDATE""",
                        (run["run_id"], tenant_id, canonical_name, origin),
                    ).fetchall()
                    for wait in waits:
                        self.workflow_store._resolve_wait_locked(
                            connection, current_run, self.workflow_store.get_wait(
                                wait["wait_id"], tenant_id=tenant_id, connection=connection,
                            ), {"reason": "policy_denied", "code": "POLICY_DENIED"}, actor_id,
                        )
            return ToolPolicy(**row)
