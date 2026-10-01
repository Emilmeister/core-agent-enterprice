"""Owner history projects canonical roots and retained input without executing them."""
import base64
import copy
import hashlib
import json
import unittest
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from urllib.parse import quote

from psycopg.types.json import Jsonb

from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
from tests import test_admission as admission_tests
from tests.test_auth import AuthAppTestCase, TEST_DATABASE_URL


class OwnerHistoryTests(AuthAppTestCase):
    async def asyncSetUp(self):
        # History fixtures own their continuations; startup recovery must not
        # consume this fixture's model script for retained synthetic workflows.
        with patch("core_agent.runtime.CoreAgent.recover_workflows"):
            await super().asyncSetUp()

    @staticmethod
    def digest(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()).hexdigest()

    def review_material(self, source, identity, *, reason="rejected", source_kind="follow_up"):
        """Create a real scoped review/wait in another run, using both store implementations."""
        from core_agent.guardrails import GuardrailClassifier
        agent = self.app.state.core_agent
        record = agent._new_workflow({"prompt": "review source"}, task_id=str(uuid.uuid4()), identity=source.owner_id,
            session_id=source.context_id, tenant_id=source.tenant_id, defer_initialization=True)[0]
        token = agent.workflow_store.acquire_lease(record.run_id, tenant_id=record.tenant_id,
            owner_id=record.owner_id, worker_id="history-test", ttl=60)
        review = agent.material_review_store.create(record, source_id="fixture", source_kind=source_kind,
            payload="review source", completed_result_ref=identity, deadline=agent.workflow_store.current_time()+60,
            max_calls=2, max_input_tokens=10000, lease_token=token)
        detector = ScriptedModel([ModelResponse(message='{"verdict":"suspicious"}', finish_reason="stop")])
        classifier = GuardrailClassifier(detector, token_counter=lambda text: max(1, len(text)//4),
            clock=agent.workflow_store.current_time)
        review = agent.material_review_store.classify(record, review["review_id"], classifier, lease_token=token,
            continuation={"version": 1, "phase": "input", "sequence": 0}, snapshot=record.snapshot)
        if reason is not None:
            agent.workflow_store.resolve_wait(review["wait_id"], tenant_id=record.tenant_id, outcome={"reason": reason})
        return review

    def negative(self, record, digest, *, material_kind="json"):
        agent = self.app.state.core_agent
        if self.use_postgres:
            with agent.workflow_store.database.transaction() as connection:
                return agent.material_review_store._negative_decision(record, digest, connection, material_kind=material_kind)
        return agent.material_review_store._negative_decision(record, digest, None, material_kind=material_kind)

    async def history(self, chat="history", *, token="owner-b", **params):
        return await self.http.get("/api/chats/" + quote(chat, safe="") + "/history",
            headers=self.headers(token), params=params)

    def record(self, task):
        return self.app.state.core_agent.workflow_store.lookup_task(task["id"])

    def snapshot(self, record, snapshot):
        store = self.app.state.core_agent.workflow_store
        if self.use_postgres:
            with store.database.transaction() as connection:
                connection.execute("UPDATE core_runs SET snapshot=%s WHERE run_id=%s", (Jsonb(snapshot), record.run_id))
        else:
            store._records[record.run_id] = replace(record, snapshot=snapshot)

    async def schedule(self, context_id=None):
        from core_agent.cron import CronStore
        agent = self.app.state.core_agent
        admission = agent.tool_runtime.environment_manager.validate_workspace_scope.__self__
        if not hasattr(admission, "cron_store"):
            admission.cron_store = CronStore(admission)
        self.cron = admission.cron_store
        self.cron_context = admission_tests.AuthAdmissionTests.context(self, "owner-a")
        return await self.cron.create(self.cron_context, {
            "request_id": str(uuid.uuid4()), "prompt": "PRIVATE_SCHEDULE_PROMPT", "expression": "0 18 * * *",
            **({"context_id": context_id} if context_id is not None else {}),
        })

    async def skip_schedule(self, schedule, *, seconds=61):
        due = datetime.now(UTC) - timedelta(seconds=seconds)
        if self.use_postgres:
            with self.cron.database.transaction() as connection:
                connection.execute("UPDATE core_cron_schedules SET next_due_at=%s WHERE tenant_id=%s AND id=%s",
                    (due, self.cron_context.tenant, schedule["id"]))
                self.cron.occur(self.cron_context, schedule["id"], schedule["revision"], connection=connection)
        else:
            self.cron.admission._cron_schedules[(self.cron_context.tenant, schedule["id"])]["next_due_at"] = due
            await self.cron.occur_memory(self.cron_context, schedule["id"], schedule["revision"])
        return self.cron.events(self.cron_context.tenant, context_id=schedule["context_id"],
            kind="skipped", descending=True, limit=1)[0]

    async def test_cron_notices_merge_with_terminal_history_and_keep_v1_cursor(self):
        task = await self.submit("external-a", "first", "history")
        record = self.record(task)
        original = (await self.history()).json()["items"]
        old_page = (await self.history(limit="1")).json()
        schedule = await self.schedule("history")
        first, second = await self.skip_schedule(schedule), await self.skip_schedule(schedule)
        with patch.object(self.model, "generate", side_effect=AssertionError("history cannot execute")):
            response = await self.history()
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()["items"]
        self.assertEqual([item["kind"] for item in items], ["schedule_notice", "schedule_notice", "result", "user_message"])
        self.assertEqual(items[2:], original)
        self.assertEqual([item["id"] for item in items[:2]], ["schedule-notice:" + second["id"], "schedule-notice:" + first["id"]])
        self.assertEqual(items[0], {
            "id": "schedule-notice:" + second["id"], "task_id": task["id"], "kind": "schedule_notice",
            "status": "available", "text": "[Scheduled run skipped]", "schedule_id": schedule["id"],
            "schedule_revision": 1, "timezone": "Europe/Moscow", "reason": "late",
            "due_at": second["due_at"].isoformat(), "through": second["through"].isoformat(),
            "created_at": second["created_at"].isoformat(),
        })
        self.assertNotIn("PRIVATE_SCHEDULE_PROMPT", response.text)
        self.assertEqual(self.record(task), record)
        old_tail = await self.history(cursor=old_page["next_cursor"])
        self.assertEqual(old_tail.json()["items"], original[1:])
        page = (await self.history(limit="1")).json()
        cursor = page["next_cursor"]
        self.assertEqual(json.loads(json.loads(base64.urlsafe_b64decode(cursor))[2]), [2, second["id"]])
        values = {key: schedule[key] for key in ("prompt", "expression", "timezone", "enabled")}
        self.cron.update(self.cron_context.tenant, schedule["id"],
            {**values, "prompt": "PRIVATE_EDITED_PROMPT", "timezone": "UTC", "expected_revision": 1}, actor_id="PRIVATE_EDITOR")
        self.cron.delete(self.cron_context.tenant, schedule["id"], expected_revision=2, actor_id="PRIVATE_EDITOR")
        snapshot = copy.deepcopy(record.snapshot)
        snapshot["context"]["active"] = [{"kind": "summary", "content": "PRIVATE_SUMMARY", "tokens": 1}]
        self.snapshot(record, snapshot)
        await self.submit("owner-b", "new-root", "history")
        tail = (await self.history(cursor=cursor)).json()["items"]
        self.assertEqual(tail, items[1:])

    async def test_cron_empty_chat_notices_survive_first_root_and_bounded_pagination(self):
        schedule = await self.schedule()
        self.assertEqual((await self.history(schedule["context_id"])).json()["items"], [])
        events = [await self.skip_schedule(schedule) for _ in range(4)]
        with patch.object(self.cron, "events", wraps=self.cron.events) as reads:
            page = await self.history(schedule["context_id"], limit="1")
        self.assertEqual(page.status_code, 200, page.text)
        item = page.json()["items"][0]
        self.assertEqual(item["id"], "schedule-notice:" + events[-1]["id"])
        self.assertIsNone(item["task_id"])
        for call in reads.call_args_list:
            self.assertLessEqual(call.kwargs["limit"], 2)
            self.assertEqual(call.kwargs["context_id"], schedule["context_id"])
            self.assertEqual(call.kwargs["owner_id"], self.cron_context.state["principal"].owner_id)
        await self.submit("owner-b", "first-root", schedule["context_id"])
        await self.skip_schedule(schedule)
        cursor, collected = page.json()["next_cursor"], [item]
        while cursor:
            page = await self.history(schedule["context_id"], limit="1", cursor=cursor)
            self.assertEqual(page.status_code, 200, page.text)
            collected.extend(page.json()["items"])
            cursor = page.json()["next_cursor"]
        self.assertEqual([item["id"] for item in collected], ["schedule-notice:" + event["id"] for event in reversed(events)])

    async def test_cron_notice_anchor_survives_appended_transcript_and_guards_stay_private(self):
        from core_agent.guardrails import GuardrailClassifier
        agent = self.app.state.core_agent
        detector = ScriptedModel([ModelResponse(message='{"verdict":"suspicious"}', finish_reason="stop")])
        agent.guardrail_classifier = GuardrailClassifier(detector, token_counter=lambda text: max(1, len(text)//4))
        task = await self.submit("external-a", "guarded", "history")
        record = self.record(task)
        schedule = await self.schedule("history")
        event = await self.skip_schedule(schedule, seconds=2)
        page = await self.history(limit="1")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual(page.json()["items"][0]["reason"], "context_busy")
        guarded = await self.history()
        self.assertEqual([item["status"] for item in guarded.json()["items"]], ["available", "pending_guardrail"])
        self.assertNotIn("Answer briefly", guarded.text)
        agent.workflow_store.resolve_wait(record.snapshot["wait_id"], tenant_id=record.tenant_id, outcome={"reason": "allowed"})
        agent.resume_task(task["id"])
        await self.skip_schedule(schedule)
        response = await self.history(cursor=page.json()["next_cursor"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([item["text"] for item in response.json()["items"]], ["Answer briefly"])
        self.assertEqual(response.json()["items"][0]["id"], guarded.json()["items"][1]["id"])
        self.assertEqual(self.cron.events(self.cron_context.tenant, exact_id=event["id"])[0]["history_position"], event["history_position"])

    async def test_cron_notice_cursor_rejects_foreign_scope_and_unmapped_anchor(self):
        task = await self.submit("owner-a", "first", "history")
        schedule = await self.schedule("history")
        event = await self.skip_schedule(schedule)
        foreign = await self.schedule()
        foreign_event = await self.skip_schedule(foreign)
        page = (await self.history(limit="1")).json()
        outer = json.loads(base64.urlsafe_b64decode(page["next_cursor"]))
        created = self.cron.events(self.cron_context.tenant, context_id="history", kind="created")[0]
        for identifier in (foreign_event["id"], created["id"], str(uuid.uuid4()), "\0", "\ud800", True):
            outer[2] = json.dumps([2, identifier])
            forged = base64.urlsafe_b64encode(json.dumps(outer).encode()).decode()
            response = await self.history(cursor=forged)
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(response.json()["error"]["code"], "REQUEST_INVALID")
        with patch.object(self.cron, "events", wraps=self.cron.events) as reads:
            self.assertEqual((await self.history(cursor=page["next_cursor"])).status_code, 200)
        exact = next(call for call in reads.call_args_list if "exact_id" in call.kwargs)
        self.assertEqual(exact.args, (self.record(task).tenant_id,))
        self.assertEqual({key: exact.kwargs[key] for key in ("context_id", "owner_id", "kind", "exact_id")},
            {"context_id": "history", "owner_id": self.record(task).owner_id, "kind": "skipped", "exact_id": event["id"]})
        # An admitted root in the same scope is insufficient if outside the canonical chain.
        record = self.record(task)
        if self.use_postgres:
            with self.cron.database.transaction() as connection:
                connection.execute("UPDATE core_chats SET latest_root_run_id=NULL WHERE tenant_id=%s AND context_id=%s",
                    (record.tenant_id, record.context_id))
        else:
            self.cron.admission.chats[(record.tenant_id, record.context_id)]["latest_root_run_id"] = None
        outer[2] = json.dumps([2, event["id"]])
        forged = base64.urlsafe_b64encode(json.dumps(outer).encode()).decode()
        self.assertEqual((await self.history(cursor=forged)).status_code, 400)

    async def test_all_roots_once_and_stable_cursor_after_new_root_and_compaction(self):
        first = await self.submit("external-a", "first", "history")
        second = await self.submit("owner-a", "second", "history")
        calls = len(self.model.calls)
        page = await self.history(limit="1")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertEqual(page.json()["items"][0]["task_id"], second["id"])
        self.assertEqual(page.json()["items"][0]["kind"], "result")
        self.assertEqual(page.json()["items"][0]["text"], "verified")
        cursor = page.json()["next_cursor"]
        old = self.record(first)
        snapshot = copy.deepcopy(old.snapshot)
        snapshot["context"]["active"] = [{"kind": "summary", "content": "DO NOT DISPLAY SUMMARY", "tokens": 4}]
        self.snapshot(old, snapshot)
        await self.submit("owner-b", "third", "history")
        items = list(page.json()["items"])
        while cursor:
            page = await self.history(limit="1", cursor=cursor)
            self.assertEqual(page.status_code, 200, page.text)
            items.extend(page.json()["items"])
            cursor = page.json()["next_cursor"]
        self.assertEqual([item["kind"] for item in items], ["result", "user_message", "result", "user_message"])
        self.assertEqual(len({item["id"] for item in items}), 4)
        self.assertEqual({item["task_id"] for item in items}, {first["id"], second["id"]})
        self.assertNotIn("DO NOT DISPLAY SUMMARY", json.dumps(items))
        self.assertEqual(len(self.model.calls), calls + 1)

    async def test_owner_only_missing_chat_and_invalid_query(self):
        await self.submit("external-a", "first", "history")
        self.tokens["dual"] = {**self.tokens["owner-a"], "realm_access": {"roles": ["agent-owner", "agent-external"]}}
        for token in ("external-a", "external-b", "dual"):
            self.assertEqual((await self.history(token=token)).status_code, 403)
        self.assertEqual((await self.history("missing")).status_code, 404)
        for params in ({"limit": "0"}, {"limit": "101"}, {"cursor": "bad"}, {"tenant_id": "other"}):
            self.assertEqual((await self.history(**params)).status_code, 400)
        auth = self.app.state.authenticator
        with patch.object(auth, "settings", replace(auth.settings, tenant="other")):
            self.assertEqual((await self.history()).status_code, 404)

    async def test_queued_input_identity_survives_delivery_without_duplicate(self):
        agent = self.app.state.core_agent
        agent.model = ScriptedModel([ModelResponse(tool_requests=(ToolRequest("ask", "core_ask_owner", {"question": "Confirm?"}),)),
            ModelResponse(message="Done after correction")])
        task = await self.submit("external-a", "first", "history")
        record = self.record(task)
        agent.enqueue_message({"prompt": "CORRECTION"}, task_id=task["id"], message_id="correction",
            identity=record.owner_id, session_id=record.context_id, tenant_id=record.tenant_id)
        before = agent.workflow_store.get(record.run_id, tenant_id=record.tenant_id, owner_id=record.owner_id)
        queued = await self.history()
        self.assertEqual(queued.status_code, 200, queued.text)
        item = next(item for item in queued.json()["items"] if item["status"] == "queued")
        self.assertNotIn("CORRECTION", queued.text)
        self.assertEqual(agent.workflow_store.get(record.run_id, tenant_id=record.tenant_id, owner_id=record.owner_id), before)
        wait = agent.workflow_store.get_wait(before.snapshot["wait_id"], tenant_id=record.tenant_id, owner_id=record.owner_id)
        agent.workflow_store.resolve_wait(wait.wait_id, tenant_id=record.tenant_id, outcome={"reason": "answer", "answer": "yes"})
        agent.resume_task(task["id"])
        delivered = (await self.history()).json()["items"]
        matching = [value for value in delivered if value["id"] == item["id"]]
        self.assertEqual(len(matching), 1)
        self.assertEqual((matching[0]["status"], matching[0]["text"]), ("available", "CORRECTION"))
        self.assertEqual(sum(value["text"] == "CORRECTION" for value in delivered), 1)

    async def test_guard_pending_and_rejected_are_metadata_only_without_refresh(self):
        from core_agent.guardrails import GuardrailClassifier
        agent = self.app.state.core_agent
        detector = ScriptedModel([ModelResponse(message='{"verdict":"suspicious"}', finish_reason="stop")])
        agent.guardrail_classifier = GuardrailClassifier(detector, token_counter=lambda text: max(1, len(text) // 4))
        task = await self.submit("external-a", "guarded", "history")
        record = self.record(task)
        wait = agent.workflow_store.get_wait(record.snapshot["wait_id"], tenant_id=record.tenant_id, owner_id=record.owner_id)
        for expected in ("pending_guardrail", "rejected"):
            if expected == "rejected":
                agent.workflow_store.resolve_wait(wait.wait_id, tenant_id=record.tenant_id, outcome={"reason": "rejected"})
            before = agent.workflow_store.get(record.run_id, tenant_id=record.tenant_id, owner_id=record.owner_id)
            with patch.object(detector, "generate", side_effect=AssertionError("history must not classify")), \
                 patch.object(agent.model, "generate", side_effect=AssertionError("history must not model")):
                response = await self.history()
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(response.json()["items"]), 1)
            entry = response.json()["items"][0]
            self.assertEqual((entry["kind"], entry["status"], entry["review"]), ("placeholder", expected, {"wait_id": wait.wait_id}))
            self.assertNotIn("Answer briefly", response.text)
            self.assertEqual(agent.workflow_store.get(record.run_id, tenant_id=record.tenant_id, owner_id=record.owner_id), before)

    async def test_followup_before_initial_guard_completion_keeps_initial_first(self):
        from core_agent.guardrails import GuardrailClassifier
        agent = self.app.state.core_agent
        detector = ScriptedModel([ModelResponse(message=json.dumps({"verdict": value}), finish_reason="stop")
                                  for value in ("suspicious", "clear")])
        agent.guardrail_classifier = GuardrailClassifier(detector, token_counter=lambda text: max(1, len(text) // 4))
        task = await self.submit("external-a", "guarded", "history")
        record = self.record(task)
        message = agent.enqueue_message({"prompt": "early correction"}, task_id=task["id"], message_id="early",
            identity=record.owner_id, tenant_id=record.tenant_id, session_id=record.context_id)
        self.assertEqual(message["provenance"]["history_after_sequence"], 0)
        before = (await self.history()).json()["items"]
        self.assertEqual([item["status"] for item in before], ["queued", "pending_guardrail"])
        page = (await self.history(limit="1")).json()
        wait = agent.workflow_store.get_wait(record.snapshot["wait_id"], tenant_id=record.tenant_id, owner_id=record.owner_id)
        agent.workflow_store.resolve_wait(wait.wait_id, tenant_id=record.tenant_id, outcome={"reason": "allowed"})
        agent.resume_task(task["id"])
        after = (await self.history()).json()["items"]
        self.assertEqual(after[-1]["id"], before[-1]["id"])
        self.assertEqual(after[-1]["text"], "Answer briefly")
        self.assertEqual(after[-2]["id"], before[0]["id"])
        older = await self.history(cursor=page["next_cursor"])
        self.assertEqual(older.status_code, 200, older.text)
        self.assertEqual([item["id"] for item in older.json()["items"]], [before[-1]["id"]])

    async def test_queued_followup_keeps_identity_through_guard_rejection(self):
        from core_agent.guardrails import GuardrailClassifier
        agent = self.app.state.core_agent
        detector = ScriptedModel([ModelResponse(message='{"verdict":"suspicious"}', finish_reason="stop") for _ in range(2)])
        agent.guardrail_classifier = GuardrailClassifier(detector, token_counter=lambda text: max(1, len(text) // 4))
        task = await self.submit("external-a", "guarded", "history")
        record = self.record(task)
        agent.enqueue_message({"prompt": "PRIVATE_REJECTED_CORRECTION"}, task_id=task["id"], message_id="rejected-followup",
            identity=record.owner_id, tenant_id=record.tenant_id, session_id=record.context_id)
        entry = (await self.history()).json()["items"][0]
        self.assertEqual(entry["status"], "queued")
        initial_wait = agent.workflow_store.get_wait(record.snapshot["wait_id"], tenant_id=record.tenant_id, owner_id=record.owner_id)
        agent.workflow_store.resolve_wait(initial_wait.wait_id, tenant_id=record.tenant_id, outcome={"reason": "allowed"})
        agent.resume_task(task["id"])
        pending = await self.history()
        pending_entry = next(item for item in pending.json()["items"] if item["id"] == entry["id"])
        self.assertEqual(pending_entry["status"], "pending_guardrail")
        self.assertNotIn("PRIVATE_REJECTED_CORRECTION", pending.text)
        agent.workflow_store.resolve_wait(pending_entry["review"]["wait_id"], tenant_id=record.tenant_id, outcome={"reason": "rejected"})
        agent.resume_task(task["id"])
        response = await self.history()
        matched = [item for item in response.json()["items"] if item["id"] == entry["id"]]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["status"], "rejected")
        self.assertEqual(matched[0]["review"], pending_entry["review"])
        self.assertNotIn("PRIVATE_REJECTED_CORRECTION", response.text)

    async def test_partial_and_failed_results_are_once_with_safe_outcome_fields(self):
        task = await self.submit("owner-a", "first", "history")
        agent = self.app.state.core_agent
        record = self.record(task)
        for state, result, error in (("COMPLETED", {"message": "Half done", "complete": False,
                "completion_reason": "budget_exhausted", "provider_replay": "PRIVATE_RESULT"}, None),
                ("FAILED", None, "MODEL_UNAVAILABLE")):
            if self.use_postgres:
                with agent.workflow_store.database.transaction() as connection:
                    connection.execute("UPDATE core_runs SET state=%s,result=%s,error_code=%s WHERE run_id=%s",
                        (state, Jsonb(result) if result is not None else None, error, record.run_id))
            else:
                agent.workflow_store._records[record.run_id] = replace(record, state=state, result=result, error_code=error)
            response = await self.history()
            self.assertEqual(response.status_code, 200, response.text)
            finals = [item for item in response.json()["items"] if item["kind"] == "result"]
            self.assertEqual(len(finals), 1)
            expected = {"state": state, **({"complete": False, "completion_reason": "budget_exhausted"} if result else {"error_code": error})}
            self.assertEqual(finals[0]["outcome"], expected)
            self.assertNotIn("PRIVATE_RESULT", response.text)

    async def test_file_digest_namespace_does_not_reject_json_and_pending_alias_is_not_negative(self):
        task = await self.submit("owner-a", "first", "history")
        record = self.record(task)
        digest = self.digest("Answer briefly")
        self.review_material(record, {"material_kind": "file_sha256", "material_digest": digest,
            "text_digest": self.digest(json.dumps("Answer briefly"))}, source_kind="file_attachment")
        self.assertIsNone(self.negative(record, digest))
        self.review_material(record, {"material_kind": "file_sha256", "material_digest": self.digest("different file"),
            "text_digest": digest}, reason=None, source_kind="file_attachment")
        self.assertIsNone(self.negative(record, digest))
        response = await self.history()
        self.assertEqual(response.status_code, 200, response.text)
        initial = next(item for item in response.json()["items"] if item["id"].endswith("/transcript/1"))
        self.assertEqual((initial["status"], initial["text"]), ("available", "Answer briefly"))

    async def test_final_exact_negative_is_placeholder_with_outcome_and_no_read_mutations(self):
        task = await self.submit("owner-a", "first", "history")
        record = self.record(task)
        review = self.review_material(record, {"material_kind": "json", "material_digest": self.digest("verified")})
        self.assertEqual(self.negative(record, self.digest("verified"))["review_id"], review["review_id"])
        agent = self.app.state.core_agent
        with patch.object(agent.material_review_store, "get", side_effect=AssertionError("history cannot refresh review")), \
             patch.object(agent.model, "generate", side_effect=AssertionError("history cannot model")):
            response = await self.history()
        self.assertEqual(response.status_code, 200, response.text)
        final = next(item for item in response.json()["items"] if item["id"].endswith("/result"))
        self.assertEqual((final["kind"], final["status"], final["review"]), ("placeholder", "rejected", {"wait_id": review["wait_id"]}))
        self.assertNotIn("verified", final["text"])
        self.assertEqual(final["outcome"], {"state": "COMPLETED", "complete": True, "completion_reason": "completed"})
        self.assertEqual(agent.workflow_store.get(record.run_id, tenant_id=record.tenant_id, owner_id=record.owner_id), record)

    async def test_final_dependency_text_alias_rejection_hides_derived_text(self):
        task = await self.submit("owner-a", "first", "history")
        record = self.record(task)
        review = self.review_material(record, {"material_kind": "file_sha256", "material_digest": self.digest("different file"),
            "text_digest": self.digest("Answer briefly")}, source_kind="file_attachment")
        self.assertIsNone(self.negative(record, self.digest("verified")))
        self.assertEqual(self.negative(record, self.digest("Answer briefly"))["review_id"], review["review_id"])
        response = await self.history()
        self.assertEqual(response.status_code, 200, response.text)
        final = next(item for item in response.json()["items"] if item["id"].endswith("/result"))
        self.assertEqual(final["status"], "rejected")
        self.assertNotIn("verified", final["text"])

    async def test_whitelisted_legacy_tools_partial_result_and_unknown_provenance(self):
        task = await self.submit("owner-a", "first", "history")
        record = self.record(task)
        snapshot = copy.deepcopy(record.snapshot)
        calls = {"tool_calls": [{"id": "call", "function": {"name": "example", "arguments": {
            "query": "visible", "Authorization": "PRIVATE_AUTH", "nested": {"provider_replay": "PRIVATE_REPLAY"}}}}],
            "thinking": "PRIVATE_THOUGHT", "reasoning_replay": {"secret": "PRIVATE_SIGNATURE"}}
        snapshot["context"]["transcript"].extend([
            {"kind": "assistant_tool_calls", "content": json.dumps(calls), "tokens": 1, "provider_replay": {"secret": "PRIVATE_REPLAY"}},
            {"kind": "tool_result", "content": json.dumps({"tool_call_id": "call", "status": "succeeded", "output": {
                "answer": "visible output", "sealed_ref": "PRIVATE_FILE", "traceparent": "PRIVATE_TRACE"}, "snapshot": "PRIVATE_SNAPSHOT"}), "tokens": 1},
        ])
        snapshot["context"]["sequence_range"][1] = 3
        self.snapshot(record, snapshot)
        response = await self.history()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("visible output", response.text)
        self.assertNotIn("PRIVATE_", response.text)
        self.assertEqual([item["kind"] for item in response.json()["items"]], ["result", "tool_result", "tool_call", "user_message"])
        snapshot["context"]["transcript"][1]["provenance"] = {"version": 99, "sources": {}}
        self.snapshot(record, snapshot)
        failed = await self.history()
        self.assertEqual(failed.status_code, 400)
        self.assertEqual(failed.json()["error"]["code"], "CHECKPOINT_INVALID")
        self.assertNotIn("PRIVATE_", failed.text)

    async def test_forged_cursor_cannot_select_foreign_child_or_unmapped_root(self):
        first = await self.submit("owner-a", "first", "history")
        other = await self.submit("owner-a", "other", "other")
        response = await self.history(limit="1")
        outer = json.loads(base64.urlsafe_b64decode(response.json()["next_cursor"]))
        for task_id in (other["id"], "unmapped", "child"):
            outer[2] = json.dumps([1, task_id, [0, 1, 0, 0]])
            forged = base64.urlsafe_b64encode(json.dumps(outer).encode()).decode()
            self.assertEqual((await self.history(cursor=forged)).status_code, 400)
        record = self.record(first)
        snapshot = copy.deepcopy(record.snapshot)
        snapshot["previous_root_run_id"] = self.record(other).run_id
        self.snapshot(record, snapshot)
        failed = await self.history()
        self.assertEqual(failed.status_code, 400)
        self.assertEqual(failed.json()["error"]["code"], "CHECKPOINT_INVALID")

    async def test_legacy_unread_anchor_keeps_identity_and_cursor_after_delivery(self):
        agent = self.app.state.core_agent
        agent.model = ScriptedModel([ModelResponse(tool_requests=(ToolRequest("ask", "core_ask_owner", {"question": "Confirm?"}),)),
            ModelResponse(message="done")])
        task = await self.submit("external-a", "first", "history")
        record = self.record(task)
        message = agent.enqueue_message({"prompt": "legacy correction"}, task_id=task["id"], message_id="legacy",
            identity=record.owner_id, tenant_id=record.tenant_id, session_id=record.context_id)
        if self.use_postgres:
            with agent.workflow_store.database.transaction() as connection:
                connection.execute("UPDATE core_inbound_messages SET provenance=provenance-'history_after_sequence' WHERE run_id=%s", (record.run_id,))
        else:
            agent.workflow_store._inbound[record.run_id][0]["provenance"].pop("history_after_sequence")
        page = await self.history(limit="1")
        self.assertEqual(page.status_code, 200, page.text)
        pending = page.json()["items"][0]
        self.assertEqual(pending["status"], "queued")
        cursor = page.json()["next_cursor"]
        wait = agent.workflow_store.get_wait(record.snapshot["wait_id"], tenant_id=record.tenant_id, owner_id=record.owner_id)
        agent.workflow_store.resolve_wait(wait.wait_id, tenant_id=record.tenant_id, outcome={"reason": "answer", "answer": "yes"})
        agent.resume_task(task["id"])
        all_items = (await self.history()).json()["items"]
        self.assertEqual(sum(item["id"] == pending["id"] for item in all_items), 1)
        page = await self.history(cursor=cursor)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertNotIn(pending["id"], [item["id"] for item in page.json()["items"]])
        # A pre-upgrade consumed input stays represented by its original transcript.
        current = self.record(task)
        snapshot = copy.deepcopy(current.snapshot)
        delivered = next(item for item in snapshot["context"]["transcript"]
                         if (item.get("provenance") or {}).get("inbound_sequence") == message["sequence"])
        delivered["provenance"].pop("inbound_sequence")
        delivered["provenance"].pop("message_id")
        self.snapshot(current, snapshot)
        items = (await self.history()).json()["items"]
        self.assertEqual(sum(item["text"] == "legacy correction" for item in items), 1)


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL not set")
class PostgresOwnerHistoryTests(OwnerHistoryTests):
    use_postgres = True
