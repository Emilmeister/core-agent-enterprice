import os
import threading
import time
import unittest
import uuid
from dataclasses import asdict

from core_agent.database import PostgresDatabase
from core_agent.errors import CoreError
from core_agent.interactions import InMemoryInteractionStore, PostgresInteractionStore
from core_agent.workflow import InMemoryWorkflowStore, PostgresWorkflowStore, WorkflowRecord


class InteractionStoreContract:
    def settings(self, seconds=60):
        return dict.fromkeys(
            ("hitl_timeout_seconds", "owner_answer_timeout_seconds", "guardrails_timeout_seconds"),
            seconds,
        )

    def policy(self, *, tenant=None, name="tool", origin="builtin:tool", mode="deny", revision=0):
        return self.store.update_policy(
            tenant or self.tenant, name, origin, mode=mode,
            guardrails_exempt=False, expected_revision=revision, actor_id="owner-actor",
        )

    def wait(self, *, tenant=None, origin="builtin:tool", kind="tool_approval"):
        tenant = tenant or self.tenant
        record = self.workflow.create(WorkflowRecord(
            str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()), tenant,
            "external-owner", None, "RUNNING", 1, {"prompt": "wait"}, {"turns": 1},
        ))
        token = self.workflow.acquire_lease(
            record.run_id, tenant_id=tenant, owner_id=record.owner_id,
            worker_id="worker", ttl=100,
        )
        with self.store.policy_scope(tenant, "tool", origin) as (_, connection):
            options = {"connection": connection} if connection is not None else {}
            return self.workflow.enter_wait(
                record, kind=kind, source_id="call", subject={"tool_name": "tool", "origin": origin},
                continuation={"version": 1, "phase": "tool_gate", "call_id": "call"},
                deadline=self.workflow.current_time() + 100, snapshot=record.snapshot,
                lease_token=token, **options,
            )

    def test_defaults_settings_cas_and_company_scope(self):
        initial = self.store.get_settings(self.tenant)
        self.assertEqual(asdict(initial), {"tenant_id": self.tenant, "revision": 0,
                                          "remote_timeout_seconds": 86400, "remote_poll_interval_seconds": 300,
                                          "attachment_limit_bytes": 25_000_000, **self.settings(86400)})
        changed = self.store.update_settings(self.tenant, self.settings(), 0)
        self.assertEqual(changed.revision, 1)
        self.assertEqual(changed.hitl_timeout_seconds, 60)
        self.assertEqual(self.store.get_settings(self.tenant + "-other").revision, 0)
        with self.assertRaises(CoreError) as error:
            self.store.update_settings(self.tenant, self.settings(90), 0)
        self.assertEqual(error.exception.code, "SETTINGS_CONFLICT")
        self.assertEqual(self.store.get_settings(self.tenant), changed)

    def test_strict_settings_and_policy_validation(self):
        for value in (True, False, 0, -1, 1.5, "60", 2147483648):
            with self.subTest(timeout=value), self.assertRaises(CoreError) as error:
                self.store.update_settings(self.tenant, self.settings(value), 0)
            self.assertEqual(error.exception.code, "SETTINGS_INVALID")
        for values in ({}, {**self.settings(), "unknown": 2}):
            with self.assertRaises(CoreError):
                self.store.update_settings(self.tenant, values, 0)

        for revision in (True, -1, 1.5, "0", 2**63):
            with self.assertRaises(CoreError):
                self.store.update_settings(self.tenant, self.settings(), revision)
            with self.assertRaises(CoreError):
                self.policy(revision=revision)
        for mode, exempt in (("unknown", False), (False, False), ("allow", 1), ("allow", "false")):
            with self.assertRaises(CoreError):
                self.store.update_policy(
                    self.tenant, "tool", "builtin:tool", mode=mode,
                    guardrails_exempt=exempt, expected_revision=0, actor_id="owner",
                )
        self.assertEqual(self.store.get_settings(self.tenant).revision, 0)
        self.assertEqual(self.store.update_settings(self.tenant, self.settings(2147483647), 0).revision, 1)


    def test_attachment_limit_is_shared_versioned_and_legacy_updates_preserve_it(self):
        self.assertEqual(self.store.get_settings(self.tenant).attachment_limit_bytes, 25_000_000)
        changed = self.store.update_settings(self.tenant,
            {**self.settings(), "attachment_limit_bytes": 8_000_000}, 0)
        self.assertEqual(changed.attachment_limit_bytes, 8_000_000)
        self.assertEqual(self.store.get_settings(self.tenant + "-other").attachment_limit_bytes, 25_000_000)
        updated = self.store.update_settings(self.tenant, self.settings(90), changed.revision)
        self.assertEqual(updated.attachment_limit_bytes, 8_000_000)
        for value in (True, None, 0, -1, 1.5, "25", 2147483648):
            with self.subTest(limit=value), self.assertRaises(CoreError) as caught:
                self.store.update_settings(self.tenant,
                    {**self.settings(), "attachment_limit_bytes": value}, updated.revision)
            self.assertEqual(caught.exception.code, "SETTINGS_INVALID")
        self.assertEqual(self.store.get_settings(self.tenant), updated)

    def test_remote_settings_preserve_omitted_values_and_share_cas(self):
        updated = self.store.update_settings(self.tenant, {**self.settings(),
            "remote_timeout_seconds": 600, "remote_poll_interval_seconds": 5}, 0)
        self.assertEqual((updated.remote_timeout_seconds, updated.remote_poll_interval_seconds), (600, 5))
        legacy = self.store.update_settings(self.tenant, self.settings(90), 1)
        self.assertEqual((legacy.remote_timeout_seconds, legacy.remote_poll_interval_seconds), (600, 5))
        other = self.store.get_settings(self.tenant + "other")
        self.assertEqual((other.remote_timeout_seconds, other.remote_poll_interval_seconds), (86400, 300))
        for key in ("remote_timeout_seconds", "remote_poll_interval_seconds"):
            for value in (True, None, 0, -1, "60", 1.5, 2147483648):
                with self.subTest(key=key, value=value), self.assertRaises(CoreError) as error:
                    self.store.update_settings(self.tenant, {**self.settings(), key: value}, 2)
                self.assertEqual(error.exception.code, "SETTINGS_INVALID")
        self.assertEqual(self.store.get_settings(self.tenant), legacy)

    def test_policy_origin_replacement_and_company_do_not_inherit_allow(self):
        original = self.store.get_policy(self.tenant, "tool", "server:original")
        self.assertEqual((original.mode, original.guardrails_exempt, original.revision), ("require_hitl", False, 0))
        allowed = self.policy(origin="server:original", mode="allow")
        self.assertEqual(allowed.revision, 1)
        for tenant, origin in ((self.tenant, "server:replacement"), (self.tenant + "-other", "server:original")):
            policy = self.store.get_policy(tenant, "tool", origin)
            self.assertEqual((policy.mode, policy.guardrails_exempt, policy.revision), ("require_hitl", False, 0))
        with self.assertRaises(CoreError) as error:
            self.policy(origin="server:original", revision=0)
        self.assertEqual(error.exception.code, "SETTINGS_CONFLICT")

    def test_competing_settings_and_policy_revisions_have_one_winner(self):
        for mutation in (
            lambda: self.store.update_settings(self.tenant, self.settings(), 0),
            lambda: self.policy(mode="allow"),
        ):
            barrier = threading.Barrier(3)
            results = []
            errors = []

            def update():
                try:
                    barrier.wait(timeout=5)
                    results.append(mutation())
                except Exception as error:
                    errors.append(error)

            threads = [threading.Thread(target=update) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
            self.assertEqual(len(results), 1)
            self.assertEqual([getattr(error, "code", None) for error in errors], ["SETTINGS_CONFLICT"])

    def test_deny_closes_all_matching_chats_but_keeps_other_origins_tenants_and_kinds(self):
        matching = [self.wait(), self.wait()]
        others = [self.wait(origin="server:replacement"), self.wait(tenant=self.tenant + "-other"), self.wait(kind="guardrail")]
        self.policy()
        for wait in matching:
            resolved = self.workflow.get_wait(wait.wait_id, tenant_id=wait.tenant_id)
            self.assertEqual(resolved.outcome["reason"], "policy_denied")
            self.assertEqual(resolved.outcome["code"], "POLICY_DENIED")
            current = self.workflow.get(wait.run_id, tenant_id=wait.tenant_id)
            self.assertTrue(current.snapshot["wait_ready"])
            self.assertEqual(current.owner_id, "external-owner")
        for wait in others:
            self.assertEqual(self.workflow.get_wait(wait.wait_id, tenant_id=wait.tenant_id), wait)

    def test_allow_and_settings_changes_leave_pending_approval_unchanged(self):
        wait = self.wait()
        self.policy(mode="allow")
        self.store.update_settings(self.tenant, self.settings(1), 0)
        self.assertEqual(self.workflow.get_wait(wait.wait_id, tenant_id=self.tenant), wait)

    def test_deadline_and_cancel_take_precedence_over_policy_denial(self):
        expired = self.wait()
        cancelled = self.wait()
        self.expire(expired)
        self.workflow.request_cancel(cancelled.run_id, tenant_id=self.tenant, owner_id=cancelled.owner_id)
        self.policy()
        self.assertEqual(self.workflow.get_wait(expired.wait_id, tenant_id=self.tenant).outcome["reason"], "timeout")
        self.assertEqual(self.workflow.get_wait(cancelled.wait_id, tenant_id=self.tenant).outcome["reason"], "cancelled")

    def test_policy_scope_serializes_last_dispatch_check_against_denial(self):
        attempted = threading.Event()
        finished = threading.Event()
        errors = []

        def deny():
            try:
                attempted.set()
                self.policy()
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()

        with self.store.policy_scope(self.tenant, "tool", "builtin:tool") as (policy, connection):
            self.assertEqual(policy.mode, "require_hitl")
            self.assertEqual(connection is not None, self.workflow.atomic)
            thread = threading.Thread(target=deny)
            thread.start()
            self.assertTrue(attempted.wait(timeout=5))
            self.assertFalse(finished.wait(timeout=0.05))
        thread.join(timeout=5)
        self.assertTrue(finished.is_set())
        self.assertFalse(errors)
        self.assertEqual(self.store.get_policy(self.tenant, "tool", "builtin:tool").mode, "deny")


class MemoryInteractionStoreTests(InteractionStoreContract, unittest.TestCase):
    def setUp(self):
        self.tenant = str(uuid.uuid4())
        self.now = time.time()
        self.workflow = InMemoryWorkflowStore(clock=lambda: self.now)
        self.store = InMemoryInteractionStore(self.workflow)

    def expire(self, wait):
        self.now = wait.deadline + 1


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "set TEST_DATABASE_URL to run PostgreSQL interaction tests")
class PostgresInteractionStoreTests(InteractionStoreContract, unittest.TestCase):
    def setUp(self):
        self.tenant = str(uuid.uuid4())
        self.database = PostgresDatabase(os.environ["TEST_DATABASE_URL"], min_size=0, max_size=5)
        self.addCleanup(self.database.close)
        self.database.migrate()
        self.workflow = PostgresWorkflowStore(self.database)
        self.store = PostgresInteractionStore(self.database, self.workflow)

    def expire(self, wait):
        with self.database.transaction() as connection:
            connection.execute("UPDATE core_waits SET deadline = 1 WHERE wait_id = %s", (wait.wait_id,))

    def test_scope_transaction_rolls_back_wait_and_policy_materialization(self):
        record = self.workflow.create(WorkflowRecord(
            str(uuid.uuid4()), str(uuid.uuid4()), "chat", self.tenant, "owner", None,
            "RUNNING", 1, {"prompt": "wait"}, {"turns": 1},
        ))
        token = self.workflow.acquire_lease(record.run_id, tenant_id=self.tenant, owner_id="owner", worker_id="worker", ttl=100)
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.store.policy_scope(self.tenant, "tool", "builtin:tool") as (_, connection):
                wait = self.workflow.enter_wait(
                    record, kind="tool_approval", source_id="call", subject={"tool_name": "tool", "origin": "builtin:tool"},
                    continuation={"version": 1, "phase": "tool_gate", "call_id": "call"},
                    deadline=time.time() + 100, snapshot=record.snapshot, lease_token=token, connection=connection,
                )
                raise RuntimeError("rollback")
        with self.assertRaises(CoreError):
            self.workflow.get_wait(wait.wait_id, tenant_id=self.tenant)
        self.assertEqual(self.workflow.get(record.run_id, tenant_id=self.tenant).version, 1)
        with self.database.pool.connection() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM core_tool_policies WHERE tenant_id = %s", (self.tenant,)).fetchone())

    def test_deny_audits_actor_and_rolls_back_all_waits_on_resolution_failure(self):
        from unittest.mock import patch

        waits = [self.wait(), self.wait()]
        original = self.workflow._resolve_wait_locked
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("rollback")
            return original(*args, **kwargs)

        with patch.object(self.workflow, "_resolve_wait_locked", side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, "rollback"):
                self.policy()
        self.assertEqual(self.store.get_policy(self.tenant, "tool", "builtin:tool").revision, 0)
        for wait in waits:
            self.assertIsNone(self.workflow.get_wait(wait.wait_id, tenant_id=self.tenant).outcome)
        self.policy()
        with self.database.pool.connection() as connection:
            records = connection.execute(
                "SELECT data FROM core_audit_records WHERE tenant_id = %s AND kind = 'wait.resolved'", (self.tenant,),
            ).fetchall()
        self.assertEqual([record["data"]["actor_id"] for record in records], ["owner-actor", "owner-actor"])
