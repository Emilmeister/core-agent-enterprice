"""Cron records and root admission share one durable commit, without model calls."""
import copy
import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from a2a.types import TaskState

from core_agent.errors import CoreError
from tests.test_auth import AuthAppTestCase, TEST_DATABASE_URL
from tests import test_admission as admission_tests


class CronStoreTests(AuthAppTestCase):
    context = admission_tests.AuthAdmissionTests.context

    async def asyncSetUp(self):
        await super().asyncSetUp()
        from core_agent.cron import CronStore
        self.agent = self.app.state.core_agent
        self.admission = self.agent.tool_runtime.environment_manager.validate_workspace_scope.__self__
        self.store = CronStore(self.admission)
        self.tenant = self.context("owner-a").tenant

    async def create(self, **values):
        return await self.store.create(self.context("owner-a"), {
            "request_id": "create", "prompt": "Summarize the day", "expression": "0 18 * * *", **values})

    def update(self, row, **changes):
        values = {key: row[key] for key in ("prompt", "expression", "timezone", "enabled")}
        return self.store.update(self.tenant, row["id"], {**values, "expected_revision": row["revision"], **changes}, actor_id="editor")

    async def run_schedule(self, row, request="run", **changes):
        return await self.store.run_now(self.context("owner-a"), row["id"], {
            "request_id": request, "expected_revision": row["revision"], **changes})

    def due_at(self, row, instant):
        if self.store.database:
            with self.store.database.transaction() as connection:
                connection.execute("UPDATE core_cron_schedules SET next_due_at=%s WHERE tenant_id=%s AND id=%s",
                                   (instant, self.tenant, row["id"]))
        else:
            self.admission._cron_schedules[(self.tenant, row["id"])]["next_due_at"] = instant

    async def occur(self, row, *, cutoff=None):
        context = self.context("owner-a")
        if self.store.database:
            with self.store.database.transaction() as connection:
                return self.store.occur(context, row["id"], row["revision"], connection=connection, cutoff=cutoff)
        return await self.store.occur_memory(context, row["id"], row["revision"], cutoff=cutoff)

    async def test_archive_racing_schedule_creation_never_leaves_enabled_schedule(self):
        for archive_first in (False, True):
            original = await self.create(request_id="base-" + str(archive_first))
            context_id = original["context_id"]
            context = self.context("owner-a")
            calls = [self.admission.archive_chat(self.tenant, context_id, actor_id=context.state["principal"].actor_id),
                     self.create(request_id="race-" + str(archive_first), context_id=context_id)]
            if not archive_first:
                calls.reverse()
            results = await asyncio.wait_for(asyncio.gather(*calls, return_exceptions=True), 10)
            if not archive_first:
                results.reverse()
            archived, created = results
            self.assertEqual(archived, {"context_id": context_id, "archived": True})
            if isinstance(created, CoreError):
                self.assertEqual(created.code, "CRON_NOT_FOUND")
            else:
                self.assertFalse(self.store.get(self.tenant, created["id"])["enabled"])
            self.assertFalse(self.store.get(self.tenant, original["id"])["enabled"])
            self.assertFalse(any(row["context_id"] == context_id and row["enabled"] for row in self.store.list(self.tenant)))

    async def test_archive_racing_manual_and_automatic_run_share_context_gate(self):
        for automatic in (False, True):
            row = await self.create(request_id="base-" + str(automatic), expression="* * * * *")
            if automatic:
                self.due_at(row, datetime.now(UTC) - timedelta(seconds=1))
            context = self.context("owner-a")
            async def automatic_run():
                if self.store.database:
                    def run():
                        with self.store.database.transaction() as connection:
                            return self.store.occur(context, row["id"], row["revision"], connection=connection)
                    return await asyncio.to_thread(run)
                return await self.store.occur_memory(context, row["id"], row["revision"])
            run = automatic_run() if automatic else self.run_schedule(row, request="race-run")
            archived, admitted = await asyncio.wait_for(asyncio.gather(
                self.admission.archive_chat(self.tenant, row["context_id"], actor_id=context.state["principal"].actor_id),
                run, return_exceptions=True), 10)
            if isinstance(archived, dict):
                self.assertEqual(archived, {"context_id": row["context_id"], "archived": True})
                self.assertFalse(self.store.get(self.tenant, row["id"])["enabled"])
                if automatic:
                    self.assertIsNone(admitted)
                else:
                    self.assertIsInstance(admitted, CoreError)
                    self.assertIn(admitted.code, {"CRON_CONFLICT", "CRON_DISABLED"})
            else:
                self.assertIsInstance(archived, CoreError)
                self.assertEqual(archived.code, "CONTEXT_BUSY")
                self.assertIsNotNone(admitted.run_id)
                self.assertEqual(self.store.get(self.tenant, row["id"])["active_task_id"], admitted.task.id)

    async def test_create_empty_chat_shared_company_metadata_and_idempotency(self):
        calls = len(self.model.calls)
        row = await self.create()
        self.assertEqual(set(row), {"id", "revision", "context_id", "prompt", "expression", "timezone",
                                   "enabled", "next_due_at", "active_task_id"})
        self.assertEqual(row["timezone"], "Europe/Moscow")
        self.assertEqual(row["revision"], 1)
        self.assertIsNone(row["active_task_id"])
        self.assertEqual(await self.create(), row)
        self.assertEqual(self.store.get(self.tenant, row["id"]), row)
        self.assertEqual(len(self.model.calls), calls)
        self.assertEqual(len(self.store.events(self.tenant, context_id=row["context_id"])), 1)
        with self.assertRaises(CoreError) as error:
            await self.create(prompt="Changed request")
        self.assertEqual(error.exception.code, "CRON_CONFLICT")

    async def test_edit_disable_delete_cas_and_create_receipt_survives_tombstone(self):
        row = await self.create()
        changed = self.update(row, prompt="New prompt", enabled=False)
        self.assertEqual(changed["revision"], 2)
        self.assertIsNone(changed["next_due_at"])
        with self.assertRaises(CoreError) as error:
            self.update(row)
        self.assertEqual(error.exception.code, "CRON_CONFLICT")
        with self.assertRaises(CoreError) as error:
            await self.run_schedule(changed)
        self.assertEqual(error.exception.code, "CRON_DISABLED")
        deleted = self.store.delete(self.tenant, row["id"], expected_revision=2, actor_id="owner")
        self.assertEqual(self.store.delete(self.tenant, row["id"], expected_revision=2, actor_id="owner"), deleted)
        self.assertEqual(self.store.list(self.tenant), [])
        with self.assertRaises(CoreError) as error:
            self.store.get(self.tenant, row["id"])
        self.assertEqual(error.exception.code, "CRON_NOT_FOUND")
        self.assertEqual(await self.create(), row)

    async def test_bound_chat_uses_original_owner_and_manual_snapshot(self):
        task = await self.submit("external-a", "initial", "existing")
        original = self.agent.workflow_store.lookup_task(task["id"])
        row = await self.create(context_id="existing")
        admitted = await self.run_schedule(row)
        self.assertIsNotNone(admitted.run_id)
        record = self.agent.workflow_store.lookup_task(admitted.task.id)
        self.assertEqual(record.owner_id, original.owner_id)
        self.assertEqual(record.context_id, "existing")
        self.assertEqual(record.snapshot["previous_root_run_id"], original.run_id)
        self.assertEqual(record.snapshot["cron_origin"], {
            "version": 1, "schedule_id": row["id"], "revision": 1, "source": "manual", "due_at": None,
            "prompt": row["prompt"], "expression": row["expression"], "timezone": row["timezone"]})
        self.assertEqual(self.store.get(self.tenant, row["id"])["next_due_at"], row["next_due_at"])
        self.assertEqual(len(self.model.calls), 1)

    async def test_manual_retry_after_delete_returns_same_task_and_changed_body_conflicts(self):
        row = await self.create()
        admitted = await self.run_schedule(row)
        self.store.delete(self.tenant, row["id"], expected_revision=1, actor_id="owner")
        replay = await self.run_schedule(row)
        self.assertEqual(replay.task.id, admitted.task.id)
        self.assertIsNone(replay.run_id)
        with self.assertRaises(CoreError) as error:
            await self.run_schedule(row, expected_revision=2)
        self.assertEqual(error.exception.code, "CRON_CONFLICT")

    async def test_manual_busy_is_durable_failed_task_automatic_busy_only_notice(self):
        row = await self.create()
        first = await self.run_schedule(row)
        busy = await self.run_schedule(row, "second")
        self.assertIsNone(busy.run_id)
        self.assertEqual(busy.task.status.state, TaskState.TASK_STATE_FAILED)
        self.due_at(row, datetime.now(UTC) - timedelta(seconds=2))
        self.assertIsNone(await self.occur(row))
        events = self.store.events(self.tenant, context_id=row["context_id"])
        self.assertEqual(events[-1]["reason"], "context_busy")
        self.assertEqual(events[-1]["history_run_id"], first.run_id)
        self.assertEqual(events[-1]["kind"], "skipped")
        self.assertIsNone(events[-1]["task_id"])

    async def test_automatic_atomic_admission_then_duplicate_noop(self):
        row = await self.create()
        self.due_at(row, datetime.now(UTC) - timedelta(seconds=2))
        admitted = await self.occur(row)
        self.assertIsNotNone(admitted.run_id)
        self.assertIsNone(await self.occur(row))
        record = self.agent.workflow_store.lookup_task(admitted.task.id)
        self.assertEqual(record.snapshot["cron_origin"]["source"], "automatic")
        events = self.store.events(self.tenant, context_id=row["context_id"])
        self.assertEqual([event["kind"] for event in events], ["created", "started"])
        self.assertGreater(datetime.fromisoformat(self.store.get(self.tenant, row["id"])["next_due_at"]), datetime.now(UTC))
        self.assertFalse(self.model.calls)

    async def test_downtime_and_late_coalesce_without_tasks_or_minute_enumeration(self):
        row = await self.create()
        before = datetime.now(UTC) - timedelta(days=3650)
        cutoff = datetime.now(UTC)
        self.due_at(row, before)
        self.assertIsNone(await self.occur(row, cutoff=cutoff))
        event = self.store.events(self.tenant, context_id=row["context_id"])[-1]
        self.assertEqual(event["reason"], "service_unavailable")
        self.assertEqual(event["due_at"], before)
        self.assertEqual(event["through"], cutoff)
        self.assertIsNone(event["history_run_id"])
        self.due_at(row, datetime.now(UTC) - timedelta(seconds=61))
        self.assertIsNone(await self.occur(row))
        self.assertEqual(self.store.events(self.tenant, context_id=row["context_id"])[-1]["reason"], "late")
        self.assertFalse(self.model.calls)

    async def test_event_failure_rolls_back_schedule_root_and_creation_receipt(self):
        row = await self.create()
        self.due_at(row, datetime.now(UTC) - timedelta(seconds=2))
        before = self.store.get(self.tenant, row["id"])
        with patch.object(self.store, "_event", side_effect=RuntimeError("storage failure")):
            with self.assertRaises(RuntimeError):
                await self.occur(row)
        self.assertEqual(self.store.get(self.tenant, row["id"]), before)
        self.assertIsNotNone((await self.occur(row)).run_id)
        self.assertEqual(len(self.store.events(self.tenant, context_id=row["context_id"])), 2)

    async def test_invalid_scope_and_input_have_no_effects(self):
        for change in ({"prompt": " "}, {"expression": "@daily"}, {"timezone": "Invalid/Zone"},
                       {"request_id": "\0"}, {"context_id": "missing"}, {"extra": True}):
            with self.subTest(change=change), self.assertRaises(CoreError):
                await self.create(**change)
        self.assertEqual(self.store.list(self.tenant), [])
        row = await self.create()
        with self.assertRaises(CoreError) as error:
            self.store.get("foreign", row["id"])
        self.assertEqual(error.exception.code, "CRON_NOT_FOUND")
        for revision in (True, 0, "1"):
            with self.assertRaises(CoreError):
                await self.run_schedule(row, expected_revision=revision)

    async def test_pagination_and_detached_metadata(self):
        rows = [await self.create(request_id=str(index)) for index in range(3)]
        expected = sorted(rows, key=lambda row: row["id"])
        self.assertEqual(self.store.list(self.tenant, limit=2), expected[:2])
        self.assertEqual(self.store.list(self.tenant, after=expected[1]["id"]), expected[2:])
        changed = copy.deepcopy(expected[0])
        changed["prompt"] = "mutated"
        self.assertNotEqual(self.store.get(self.tenant, changed["id"])["prompt"], changed["prompt"])

    async def test_cleanup_skip_and_manual_retry_leave_no_admission_receipt(self):
        row = await self.create()
        self.due_at(row, datetime.now(UTC) - timedelta(seconds=2))
        with patch.object(self.admission.workspace_cleanup, "check_ready", side_effect=CoreError("WORKSPACE_CLEANUP_PENDING")):
            with self.assertRaises(CoreError) as error:
                await self.run_schedule(row)
            self.assertEqual(error.exception.code, "WORKSPACE_CLEANUP_PENDING")
            self.assertIsNone(await self.occur(row))
        events = self.store.events(self.tenant, context_id=row["context_id"])
        self.assertEqual([event["kind"] for event in events], ["created", "skipped"])
        self.assertEqual(events[-1]["reason"], "workspace_cleanup_pending")
        self.assertIsNotNone((await self.run_schedule(row)).run_id)

    async def test_create_failure_rolls_back_allocated_chat_and_receipt(self):
        before = await self.admission.list_chats(self.tenant, limit=100)
        with patch.object(self.store, "_event", side_effect=RuntimeError("commit failed")):
            with self.assertRaises(RuntimeError):
                await self.create()
        self.assertEqual(await self.admission.list_chats(self.tenant, limit=100), before)
        self.assertEqual(self.store.list(self.tenant), [])
        self.assertEqual((await self.create())["revision"], 1)

    async def test_edit_stale_occurrence_and_disabled_schedule_never_admit(self):
        row = await self.create()
        self.due_at(row, datetime.now(UTC) - timedelta(seconds=2))
        edited = self.update(row, prompt="Edited", enabled=False)
        self.assertIsNone(await self.occur(row))
        self.assertIsNone(await self.occur(edited))
        enabled = self.update(edited, enabled=True)
        self.assertGreater(datetime.fromisoformat(enabled["next_due_at"]), datetime.now(UTC))
        self.assertEqual(self.store.due(self.tenant), [])
        self.assertFalse(self.model.calls)

    async def test_history_events_have_bounded_anchor_seek_and_no_cross_scope(self):
        row = await self.create()
        admitted = await self.run_schedule(row)
        for _ in range(2):
            self.due_at(row, datetime.now(UTC) - timedelta(seconds=2))
            await self.occur(row)
        events = self.store.events(self.tenant, context_id=row["context_id"], run_id=admitted.run_id,
                                  kind="skipped", descending=True, limit=1)
        anchor = (*events[0]["history_position"], 1, events[0]["seq"])
        previous = self.store.events(self.tenant, context_id=row["context_id"], run_id=admitted.run_id,
                                    kind="skipped", descending=True, history_before=anchor)
        self.assertEqual(len(previous), 1)
        self.assertLess(previous[0]["seq"], events[0]["seq"])
        self.assertEqual(self.store.events("foreign", exact_id=events[0]["id"]), [])

    async def test_grace_inclusive_and_origin_validation(self):
        from core_agent.cron import validate_origin
        row = await self.create()
        now = datetime.now(UTC)
        self.due_at(row, now - timedelta(seconds=60))
        with patch.object(self.store, "_now", return_value=now):
            admitted = await self.occur(row)
        self.assertIsNotNone(admitted.run_id)
        origin = self.agent.workflow_store.lookup_task(admitted.task.id).snapshot["cron_origin"]
        for changes in ({"version": True}, {"revision": True}, {"due_at": "2026-01-01T00:00:00"},
                        {"source": "owner"}, {"schedule_id": "\0"}, {"unexpected": "value"}):
            with self.subTest(changes=changes), self.assertRaises(CoreError):
                validate_origin({**origin, **changes})
        with self.assertRaises(CoreError):
            validate_origin(origin, prompt="different")

    async def test_grace_boundary_does_not_silently_drop_second_due_tick(self):
        row = await self.create(expression="* * * * *")
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        self.due_at(row, now - timedelta(seconds=60))
        with patch.object(self.store, "_now", return_value=now):
            first = await self.occur(row)
            self.assertIsNotNone(first.run_id)
            self.assertEqual(datetime.fromisoformat(self.store.get(self.tenant, row["id"])["next_due_at"]), now)
            self.assertIsNone(await self.occur(row))
        events = self.store.events(self.tenant, context_id=row["context_id"])
        self.assertEqual([event["kind"] for event in events], ["created", "started", "skipped"])
        self.assertEqual(events[-1]["reason"], "context_busy")
        self.assertEqual(events[-1]["due_at"], now)

    async def test_concurrent_company_replay_is_one_root_and_one_event(self):
        row = await self.create()
        first, second = await asyncio.gather(
            self.run_schedule(row),
            self.store.run_now(self.context("owner-b"), row["id"], {"request_id": "run", "expected_revision": 1}))
        self.assertEqual(first.task.id, second.task.id)
        self.assertEqual(sum(item.run_id is not None for item in (first, second)), 1)
        events = self.store.events(self.tenant, context_id=row["context_id"])
        self.assertEqual([event["kind"] for event in events], ["created", "started"])

    async def test_request_receipts_are_company_scoped_across_operation_kinds(self):
        row = await self.create()
        with self.assertRaises(CoreError) as error:
            await self.run_schedule(row, request="create")
        self.assertEqual(error.exception.code, "CRON_CONFLICT")
        self.assertIsNone(self.store.get(self.tenant, row["id"])["active_task_id"])

    async def test_empty_chat_notice_has_no_root_and_exact_event_lookup_is_scoped(self):
        row = await self.create()
        self.due_at(row, datetime.now(UTC) - timedelta(days=100))
        await self.occur(row, cutoff=datetime.now(UTC))
        events = self.store.events(self.tenant, context_id=row["context_id"], run_id=None, kind="skipped")
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0]["history_position"])
        exact = self.store.events(self.tenant, context_id=row["context_id"], run_id=None, exact_id=events[0]["id"])
        self.assertEqual(exact, events)
        self.assertEqual(self.store.events(self.tenant, context_id="other", exact_id=events[0]["id"]), [])


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL not set")
class PostgresCronStoreTests(CronStoreTests):
    use_postgres = True

    async def test_schema_immutable_events_and_chat_keys(self):
        import psycopg
        row = await self.create()
        event = self.store.events(self.tenant, context_id=row["context_id"])[0]
        for sql, args in (
            ("UPDATE core_cron_events SET actor_id='changed' WHERE id=%s", (event["id"],)),
            ("DELETE FROM core_cron_events WHERE id=%s", (event["id"],)),
            ("UPDATE core_chats SET owner_id='changed' WHERE tenant_id=%s AND context_id=%s", (self.tenant, row["context_id"])),
            ("UPDATE core_cron_schedules SET storage_version=2 WHERE tenant_id=%s AND id=%s", (self.tenant, row["id"]))):
            with self.assertRaises(psycopg.Error):
                with self.store.database.transaction() as connection:
                    connection.execute(sql, args)
