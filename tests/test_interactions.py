import asyncio
import json
import threading
import uuid
import unittest
from dataclasses import replace
from unittest.mock import patch

import httpx

from core_agent.workflow import WorkflowRecord
from core_agent.workflow import InMemoryWorkflowStore
from core_agent.interactions import InMemoryInteractionStore
from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
from tests.app_support import create_app
from core_agent.database import PostgresDatabase
from core_agent.errors import CoreError
from core_agent.config import AgentConfig
from tests.test_auth import AuthAppTestCase, TEST_DATABASE_URL


class MemoryInteractionLockTests(unittest.TestCase):
    def test_settings_read_under_workflow_guard_cannot_deadlock_policy_update(self):
        from core_agent.interactions import _check_revision

        workflow = InMemoryWorkflowStore()
        store = InMemoryInteractionStore(workflow)
        started, policy_entered = threading.Event(), threading.Event()
        failures = []

        def checked(current, expected):
            policy_entered.set()
            return _check_revision(current, expected)

        def update():
            started.set()
            try:
                store.update_policy("tenant", "tool", "builtin:tool", mode="deny",
                    guardrails_exempt=False, expected_revision=0, actor_id="owner")
            except BaseException as error:
                failures.append(error)

        worker = threading.Thread(target=update, daemon=True)
        with patch("core_agent.interactions._check_revision", side_effect=checked):
            try:
                with workflow._lock:
                    worker.start()
                    self.assertTrue(started.wait(1))
                    policy_entered.wait(0.1)
                    # Probe the settings guard first so the regression fails without
                    # permanently deadlocking the test's owning workflow thread.
                    readable = store._lock.acquire(timeout=0.2)
                    if readable:
                        try:
                            self.assertEqual(store.get_settings("tenant").attachment_limit_bytes, 25_000_000)
                            self.assertEqual(store.get_policy("tenant", "tool", "builtin:tool").mode, "require_hitl")
                        finally:
                            store._lock.release()
            finally:
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(readable, "policy update held settings lock while waiting for the workflow guard")
        self.assertEqual(store.get_policy("tenant", "tool", "builtin:tool").mode, "deny")


class OwnerSettingsAPITests(AuthAppTestCase):
    automatic_tools = False

    async def test_startup_recovery_cannot_bind_an_unowned_chat(self):
        probes = []

        def recover(agent):
            self.assertIsNotNone(agent.interaction_store)
            with self.assertRaises(CoreError) as caught:
                agent._bind_workspace("legacy-run", self.app.state.authenticator.settings.tenant,
                                      "external-legacy", "missing-chat")
            self.assertEqual(caught.exception.code, "WORKSPACE_SCOPE_REQUIRED")
            probes.append("checked")

        with patch("core_agent.runtime.CoreAgent.recover_durable_tasks", autospec=True, side_effect=recover), \
                patch("core_agent.runtime.CoreAgent.recover_workflows"):
            app = create_app(model=self.model, auth_transport=httpx.MockTransport(self.introspect),
                             database=PostgresDatabase(TEST_DATABASE_URL) if self.use_postgres else None)
        self.addCleanup(app.state.close)
        self.assertEqual(probes, ["checked"])

    async def test_settings_are_owner_only_and_shared_with_cas(self):
        denied = await self.http.get("/api/settings", headers=self.headers("external-a"))
        self.assertEqual(denied.status_code, 403)
        initial = await self.http.get("/api/settings", headers=self.headers("owner-a"))
        self.assertEqual(initial.status_code, 200, initial.text)
        self.assertEqual(initial.headers["cache-control"], "no-store")
        self.assertEqual(initial.json(), {
            "revision": 0, "hitl_timeout_seconds": 86400,
            "owner_answer_timeout_seconds": 86400, "guardrails_timeout_seconds": 86400,
            "attachment_limit_bytes": 25_000_000,
            "remote_timeout_seconds": 86400, "remote_poll_interval_seconds": 300,
        })
        values = {"hitl_timeout_seconds": 3600, "owner_answer_timeout_seconds": 7200,
                  "guardrails_timeout_seconds": 1800, "expected_revision": 0}
        saved = await self.http.put("/api/settings", headers=self.headers("owner-a"), json=values)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["revision"], 1)
        shared = await self.http.get("/api/settings", headers=self.headers("owner-b"))
        self.assertEqual(shared.json(), saved.json())
        stale = await self.http.put("/api/settings", headers=self.headers("owner-b"), json=values)
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["error"]["code"], "SETTINGS_CONFLICT")
        self.assertFalse(self.model.calls)

    async def test_settings_reject_unknown_fields_booleans_and_invalid_bounds(self):
        base = {"hitl_timeout_seconds": 3600, "owner_answer_timeout_seconds": 7200,
                "guardrails_timeout_seconds": 1800, "expected_revision": 0}
        for change in ({"tenant_id": "other"}, {"hitl_timeout_seconds": True},
                       {"owner_answer_timeout_seconds": 0}, {"guardrails_timeout_seconds": 2147483648},
                       {"expected_revision": False}):
            with self.subTest(change=change):
                response = await self.http.put("/api/settings", headers=self.headers("owner-a"), json={**base, **change})
                self.assertEqual(response.status_code, 400, response.text)
        current = await self.http.get("/api/settings", headers=self.headers("owner-a"))
        self.assertEqual(current.json()["revision"], 0)
        duplicate = json.dumps(base)[:-1] + ',"expected_revision":1}'
        response = await self.http.put("/api/settings", headers=self.headers("owner-a"), content=duplicate)
        self.assertEqual(response.status_code, 400)

    async def test_attachment_limit_update_is_owner_only_and_survives_legacy_timeout_put(self):
        values = {"hitl_timeout_seconds": 3600, "owner_answer_timeout_seconds": 7200,
                  "guardrails_timeout_seconds": 1800, "attachment_limit_bytes": 4_000_000, "expected_revision": 0}
        denied = await self.http.put("/api/settings", headers=self.headers("external-a"), json=values)
        self.assertEqual(denied.status_code, 403)
        saved = await self.http.put("/api/settings", headers=self.headers("owner-b"), json=values)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["attachment_limit_bytes"], 4_000_000)
        values.pop("attachment_limit_bytes")
        values["expected_revision"] = 1
        legacy = await self.http.put("/api/settings", headers=self.headers("owner-a"), json=values)
        self.assertEqual(legacy.status_code, 200, legacy.text)
        self.assertEqual(legacy.json()["attachment_limit_bytes"], 4_000_000)
        invalid = await self.http.put("/api/settings", headers=self.headers("owner-a"),
                                     json={**values, "attachment_limit_bytes": True, "expected_revision": 2})
        self.assertEqual(invalid.status_code, 400)
        shared = await self.http.get("/api/settings", headers=self.headers("owner-b"))
        self.assertEqual(shared.json(), legacy.json())

    async def test_policy_settings_derive_tool_identity_and_do_not_create_unknown_tools(self):
        response = await self.http.get("/api/tool-policies", headers=self.headers("owner-a"))
        self.assertEqual(response.status_code, 200, response.text)
        policies = {item["canonical_name"]: item for item in response.json()["tools"]}
        tool = policies["core_python_exec"]
        self.assertEqual(tool["mode"], "require_hitl")
        self.assertFalse(tool["guardrails_exempt"])
        approved = await self.http.put("/api/tool-policies/core_python_exec", headers=self.headers("owner-a"), json={
            "mode": "allow", "guardrails_exempt": True, "expected_revision": 0, "expected_origin": tool["origin"],
        })
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["origin"], tool["origin"])
        forged = await self.http.put("/api/tool-policies/core_python_exec", headers=self.headers("owner-a"), json={
            "mode": "allow", "guardrails_exempt": True, "expected_revision": 1,
            "expected_origin": tool["origin"], "origin": "forged",
        })
        self.assertEqual(forged.status_code, 400)
        missing = await self.http.put("/api/tool-policies/not_a_tool", headers=self.headers("owner-a"), json={
            "mode": "allow", "guardrails_exempt": True, "expected_revision": 0, "expected_origin": "builtin:not_a_tool",
        })
        self.assertEqual(missing.status_code, 404)

    async def test_owner_deny_refreshes_both_agent_cards_without_changing_other_company(self):
        policies = await self.http.get("/api/tool-policies", headers=self.headers("owner-a"))
        tool = next(item for item in policies.json()["tools"] if item["canonical_name"] == "core_python_exec")
        for mode, revision in (("deny", 0), ("allow", 1), ("require_hitl", 2)):
            changed = await self.http.put("/api/tool-policies/core_python_exec", headers=self.headers("owner-a"), json={
                "mode": mode, "guardrails_exempt": False, "expected_revision": revision,
                "expected_origin": tool["origin"],
            })
            self.assertEqual(changed.status_code, 200, changed.text)
            for kind, token in (("owner", "owner-b"), ("external", "external-a")):
                for filename in ("agent-card.json", "agent.json"):
                    with self.subTest(mode=mode, kind=kind, filename=filename):
                        card = await self.http.get(f"/a2a/{kind}/.well-known/{filename}", headers=self.headers(token))
                        self.assertEqual(card.status_code, 200, card.text)
                        skills = {item["id"] for item in card.json()["skills"]}
                        self.assertEqual("core_python_exec" in skills, mode != "deny")
                        self.assertIn("core_task_list", skills)
            if mode == "deny":
                authenticator = self.app.state.authenticator
                with patch.object(authenticator, "settings", replace(authenticator.settings, tenant="other-company")):
                    card = await self.http.get("/a2a/owner/.well-known/agent-card.json", headers=self.headers("owner-a"))
                    self.assertEqual(card.status_code, 200, card.text)
                    self.assertIn("core_python_exec", {item["id"] for item in card.json()["skills"]})
        self.assertFalse(self.model.calls)

    async def test_stale_ui_cannot_allow_a_different_mcp_origin_with_same_alias(self):
        agent = self.app.state.core_agent
        agent.platform_config = replace(agent.platform_config, allowed_mcp_servers={"docs_a", "docs"})
        agent.platform_mcp = tuple({"name": name, "transport": {"url": "https://trusted.example.test/" + name}}
                                   for name in ("docs_a", "docs"))

        def configure(server, name):
            raw = agent.agent_config.to_dict()
            raw["tools"]["mcp"] = {"default": "deny", "allow_servers": [server], "allow_tools": {server: [name]}}
            agent.agent_config = AgentConfig.from_dict(raw)

        configure("docs_a", "b")
        response = await self.http.get("/api/tool-policies", headers=self.headers("owner-a"))
        original = next(tool for tool in response.json()["tools"] if tool["canonical_name"] == "docs_a_b")
        configure("docs", "a_b")
        rejected = await self.http.put("/api/tool-policies/docs_a_b", headers=self.headers("owner-a"), json={
            "mode": "allow", "guardrails_exempt": False, "expected_revision": original["revision"],
            "expected_origin": original["origin"],
        })
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertEqual(rejected.json()["error"]["code"], "TOOL_IDENTITY_CONFLICT")
        response = await self.http.get("/api/tool-policies", headers=self.headers("owner-a"))
        current = next(tool for tool in response.json()["tools"] if tool["canonical_name"] == "docs_a_b")
        self.assertNotEqual(current["origin"], original["origin"])
        self.assertEqual((current["mode"], current["revision"]), ("require_hitl", 0))


class OwnerDecisionAPITests(AuthAppTestCase):
    async def asyncSetUp(self):
        # This suite exercises owner HTTP decisions over real wait-store records;
        # it does not execute their synthetic continuations in the model loop.
        with patch("core_agent.runtime.CoreAgent.recover_workflows"):
            await super().asyncSetUp()
        self.workflow = self.app.state.core_agent.workflow_store
        self.tenant = self.app.state.authenticator.settings.tenant

    async def test_guardrail_material_is_private_scoped_and_owner_only(self):
        from core_agent.guardrails import GuardrailClassifier
        agent = self.app.state.core_agent
        reviews = agent.material_review_store
        record = self.workflow.create(WorkflowRecord(
            str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()), self.tenant,
            "external-service", None, "RUNNING", 1, {"prompt": "private review"}, {},
        ))
        lease = self.workflow.acquire_lease(record.run_id, tenant_id=self.tenant,
                                            owner_id=record.owner_id, worker_id="test", ttl=60)
        review = reviews.create(record, source_id="input", source_kind="message",
                                payload="private suspicious material", deadline=self.workflow.current_time() + 60,
                                lease_token=lease)
        detector = GuardrailClassifier(ScriptedModel([
            ModelResponse(message='{"verdict":"suspicious"}', finish_reason="stop"),
        ]), token_counter=len)
        review = reviews.classify(record, review["review_id"], detector, lease_token=lease,
                                  continuation={"version": 1, "phase": "input"}, snapshot=record.snapshot)
        url = f"/api/guardrails/{review['wait_id']}/material"
        denied = await self.http.get(url, headers=self.headers("external-a"))
        self.assertEqual(denied.status_code, 403)
        response = await self.http.get(url, headers=self.headers("owner-a"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["material"]["payload"], "private suspicious material")
        self.assertNotIn("payload", response.json()["review"])
        self.assertNotIn("attempt_token", response.json()["review"])
        overridden = await self.http.get(url + "?tenant=other", headers=self.headers("owner-a"))
        self.assertEqual(overridden.status_code, 400)
        _, foreign = self.wait(tenant="other-company", kind="guardrail")
        missing = await self.http.get(f"/api/guardrails/{foreign.wait_id}/material", headers=self.headers("owner-a"))
        self.assertEqual(missing.status_code, 404, missing.text)

    def wait(self, *, tenant=None, kind="tool_approval", parent=None, seconds=86400):
        tenant = tenant or self.tenant
        record = self.workflow.create(WorkflowRecord(
            str(uuid.uuid4()), str(uuid.uuid4()), parent.context_id if parent else str(uuid.uuid4()),
            tenant, "external-service", parent.run_id if parent else None,
            "RUNNING", 1, {"prompt": "owner decision"}, {},
        ))
        lease = self.workflow.acquire_lease(record.run_id, tenant_id=tenant, owner_id=record.owner_id,
                                            worker_id="test", ttl=60)
        wait = self.workflow.enter_wait(
            record, kind=kind, source_id="call-1",
            subject={"tool_name": "core_python_exec", "origin": "builtin:core_python_exec",
                     "arguments": {"code": "private-code"}, "schema_digest": "sha256:schema"},
            continuation={"version": 1, "phase": "tool_gate", "call_id": "call-1"},
            deadline=self.workflow.current_time() + seconds, snapshot=record.snapshot, lease_token=lease,
        )
        return record, wait

    async def test_guardrail_file_route_uses_saved_reference_and_private_attachment_headers(self):
        from core_agent.guardrails import GuardrailClassifier
        from core_agent.workspace import WorkspaceBinding

        agent = self.app.state.core_agent
        record = self.workflow.create(WorkflowRecord(
            str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()), self.tenant,
            "external-service", None, "RUNNING", 1, {"prompt": "private file review"}, {},
        ))
        lease = self.workflow.acquire_lease(record.run_id, tenant_id=self.tenant,
                                            owner_id=record.owner_id, worker_id="test", ttl=60)
        batch_id = str(uuid.uuid4())
        review = agent.material_review_store.create(record, source_id="file", source_kind="file_attachment",
            sealed_ref={"batch_id": batch_id, "index": 2}, content_digest="a" * 64,
            deadline=self.workflow.current_time() + 60, lease_token=lease)
        detector = GuardrailClassifier(ScriptedModel([
            ModelResponse(message='{"verdict":"suspicious"}', finish_reason="stop"),
        ]), token_counter=len)
        review = agent.material_review_store.classify(record, review["review_id"], detector, lease_token=lease,
            continuation={"version": 1, "phase": "input"}, snapshot=record.snapshot, documents=["private file"])
        url = f"/api/guardrails/{review['wait_id']}/file"
        # Actual byte integrity is exercised by ChatFileService tests; this spy
        # proves HTTP role/scope projection never trusts a client file reference.
        with patch.object(agent.chat_file_service, "owner_download", return_value={
            "entry": {"actual_name": "проверка.txt"}, "manifest": {"original_name": "original-name.txt"},
            "content": b"private file",
        }) as download:
            denied = await self.http.get(url, headers=self.headers("external-a"))
            self.assertEqual(denied.status_code, 403)
            download.assert_not_called()
            response = await self.http.get(url, headers=self.headers("owner-b"))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.content, b"private file")
            self.assertEqual(response.headers["content-type"], "application/octet-stream")
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")
            self.assertTrue(response.headers["content-disposition"].startswith("attachment; filename*=UTF-8''%"))
            download.assert_called_once_with(batch_id,
                WorkspaceBinding(self.tenant, record.owner_id, record.context_id), 2,
                run_id=record.run_id, task_id=record.task_id)
            overridden = await self.http.get(url + "?index=0", headers=self.headers("owner-a"))
            self.assertEqual(overridden.status_code, 400)
            _, foreign = self.wait(tenant="other-company", kind="guardrail")
            missing = await self.http.get(f"/api/guardrails/{foreign.wait_id}/file", headers=self.headers("owner-a"))
            self.assertEqual(missing.status_code, 404)
            self.assertEqual(download.call_count, 1)
            metadata = await self.http.get(url.removesuffix("/file") + "/material", headers=self.headers("owner-a"))
            self.assertEqual(metadata.status_code, 200, metadata.text)
            self.assertEqual(metadata.json()["material"]["file"]["manifest"], {"original_name": "original-name.txt"})
            self.assertNotIn("content", metadata.json()["material"]["file"])

    async def listed(self, record, **query):
        response = await self.http.get("/api/interactions", headers=self.headers("owner-a"),
                                       params={"task_id": record.task_id, **query})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()

    async def decide(self, wait, digest, *, token="owner-a", decision="allow", route="hitl"):
        return await self.http.post(f"/api/{route}/{wait.wait_id}/decision", headers=self.headers(token),
                                    json={"decision": decision, "subject_digest": digest})

    async def test_owner_decision_is_immutable_idempotent_and_external_cannot_decide(self):
        record, wait = self.wait()
        entry = (await self.listed(record))["interactions"][0]
        digest = entry["subject_digest"]
        self.assertEqual(entry["subject"]["arguments"]["code"], "private-code")
        self.assertEqual((await self.decide(wait, digest, token="external-a")).status_code, 403)
        self.assertEqual((await self.decide(wait, "sha256:stale")).status_code, 409)
        self.assertEqual((await self.decide(wait, False)).status_code, 400)
        wrong_kind = await self.decide(wait, digest, route="guardrails")
        self.assertEqual(wrong_kind.status_code, 400)
        self.assertIsNone(self.workflow.get_wait(wait.wait_id, tenant_id=self.tenant).outcome)
        accepted = await self.decide(wait, digest)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["outcome"]["reason"], "allowed")
        repeated = await self.decide(wait, digest, token="owner-b")
        self.assertEqual(repeated.json(), accepted.json())
        rejected = await self.decide(wait, digest, decision="reject")
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(rejected.json()["error"]["code"], "INTERACTION_CLOSED")
        self.assertEqual((await self.listed(record))["interactions"], [])
        self.assertEqual((await self.listed(record, status="all"))["interactions"][0]["subject_digest"], digest)
        self.assertFalse(self.model.calls)

    async def test_chat_family_listing_is_bounded_scoped_and_excludes_timer_waits(self):
        root, root_wait = self.wait()
        _, child_wait = self.wait(parent=root, kind="owner_question")
        self.wait(parent=root, kind="timer")
        other, other_wait = self.wait()
        foreign, foreign_wait = self.wait(tenant=self.tenant + "-foreign")
        first = await self.listed(root, limit=1)
        second = await self.listed(root, limit=1, cursor=first["next_cursor"])
        entries = first["interactions"] + second["interactions"]
        self.assertEqual({item["wait_id"] for item in entries}, {root_wait.wait_id, child_wait.wait_id})
        self.assertIsNone(second["next_cursor"])
        for query in ({"task_id": foreign.task_id}, {"task_id": "unknown"}):
            response = await self.http.get("/api/interactions", headers=self.headers("owner-a"), params=query)
            self.assertEqual(response.status_code, 404)
        self.assertEqual((await self.decide(foreign_wait, "any")).status_code, 404)
        for change in ({"limit": 0}, {"limit": 101}, {"status": "bad"}, {"cursor": "bad"},
                       {"tenant_id": "forged"}, {"task_id": other.task_id, "cursor": first["next_cursor"]}):
            response = await self.http.get("/api/interactions", headers=self.headers("owner-a"),
                                           params={"task_id": root.task_id, **change})
            self.assertEqual(response.status_code, 400, response.text)
        self.assertIsNone(self.workflow.get_wait(other_wait.wait_id, tenant_id=self.tenant).outcome)

    async def test_expired_decision_persists_timeout_before_conflict_response(self):
        record, wait = self.wait(seconds=10)
        digest = (await self.listed(record))["interactions"][0]["subject_digest"]
        if self.use_postgres:
            # PostgreSQL deadline enforcement uses the database clock.
            with self.workflow.database.transaction() as connection:
                connection.execute("UPDATE core_waits SET deadline=EXTRACT(EPOCH FROM clock_timestamp())-1 "
                                   "WHERE wait_id=%s AND tenant_id=%s", (wait.wait_id, self.tenant))
            response = await self.decide(wait, digest)
        else:
            with patch.object(self.workflow, "clock", return_value=wait.deadline + 1):
                response = await self.decide(wait, digest)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.workflow.get_wait(wait.wait_id, tenant_id=self.tenant).outcome["reason"], "timeout")
        self.assertTrue(self.workflow.get(record.run_id, tenant_id=self.tenant).snapshot["wait_ready"])

    async def test_competing_owners_have_one_durable_decision(self):
        record, wait = self.wait()
        digest = (await self.listed(record))["interactions"][0]["subject_digest"]
        responses = await asyncio.gather(
            self.decide(wait, digest, token="owner-a", decision="allow"),
            self.decide(wait, digest, token="owner-b", decision="reject"),
        )
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        accepted = next(response.json() for response in responses if response.status_code == 200)
        stored = self.workflow.get_wait(wait.wait_id, tenant_id=self.tenant)
        self.assertEqual(accepted["outcome"], stored.outcome)

    async def test_owner_answer_is_strict_bounded_and_private(self):
        record, wait = self.wait(kind="owner_question")
        digest = (await self.listed(record))["interactions"][0]["subject_digest"]
        path = f"/api/questions/{wait.wait_id}/answer"
        for value in ({"answer": " "}, {"answer": "я" * 40000}, {"answer": False},
                      {"answer": "yes", "arguments": {}}, {"answer": "yes", "tenant_id": "other"}):
            response = await self.http.post(path, headers=self.headers("owner-a"),
                                            json={"subject_digest": digest, **value})
            self.assertEqual(response.status_code, 400, response.text)
        payload = {"answer": "private-owner-answer", "subject_digest": digest}
        self.assertEqual((await self.http.post(path, headers=self.headers("external-a"), json=payload)).status_code, 403)
        response = await self.http.post(path, headers=self.headers("owner-b"), json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["outcome"]["answer"], "private-owner-answer")
        self.assertEqual((await self.http.post(path, headers=self.headers("owner-a"), json=payload)).json(), response.json())


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL is required")
class PostgresOwnerSettingsAPITests(OwnerSettingsAPITests):
    use_postgres = True


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL is required")
class PostgresOwnerDecisionAPITests(OwnerDecisionAPITests):
    use_postgres = True


class OwnerRuntimeAPITests(AuthAppTestCase):
    automatic_tools = False

    async def asyncSetUp(self):
        with patch("core_agent.runtime.CoreAgent.recover_workflows") as recovery:
            await super().asyncSetUp()
        self.reconcile_workflows = recovery.call_args.kwargs["on_settled"]
        self.agent = self.app.state.core_agent

    async def test_owner_stream_does_not_publish_material_awaiting_review(self):
        from core_agent.guardrails import GuardrailClassifier

        self.agent.model = ScriptedModel([
            ModelResponse(reasoning="private-reasoning-251", tool_requests=(ToolRequest("call-private", "core_task_list", {}),)),
            ModelResponse(message="continued without material"),
        ])
        self.agent.guardrail_classifier = GuardrailClassifier(ScriptedModel([
            ModelResponse(message=json.dumps({"verdict": verdict}), finish_reason="stop")
            for verdict in ("clear", "clear", "suspicious")
        ]), token_counter=len)
        self.agent.interaction_store.update_policy(
            self.app.state.authenticator.settings.tenant, "core_task_list", "builtin:core_task_list",
            mode="allow", guardrails_exempt=False, expected_revision=0, actor_id="test-owner",
        )
        dispatched = []
        self.agent.tool_runtime.handlers["core_task_list"] = lambda *_: dispatched.append(1) or {"data": "private-material-253"}
        response = await self.http.post("/a2a/owner/message:stream", headers=self.headers("owner-a"), json={
            "message": {"messageId": "guarded-stream", "contextId": "guarded-stream-chat",
                        "role": "ROLE_USER", "parts": [{"text": "inspect result"}]},
        })
        self.assertEqual(response.status_code, 200, response.text)
        for private in ("private-reasoning-251", "private-material-253", "core_task_list", "function_response"):
            self.assertNotIn(private, response.text)
        listed = await self.http.get("/a2a/owner/tasks", headers=self.headers("owner-a"))
        task = next(task for task in listed.json()["tasks"] if task["contextId"] == "guarded-stream-chat")
        waiting = await self.pending(task["id"])
        self.assertEqual(waiting["kind"], "guardrail")
        material = await self.http.get(f"/api/guardrails/{waiting['wait_id']}/material", headers=self.headers("owner-a"))
        self.assertIn("private-material-253", material.text)
        rejected = await self.http.post(f"/api/guardrails/{waiting['wait_id']}/decision", headers=self.headers("owner-a"),
                                        json={"decision": "reject", "subject_digest": waiting["subject_digest"]})
        self.assertEqual(rejected.status_code, 200, rejected.text)
        self.agent._runtime_cache.clear()
        result = await asyncio.to_thread(self.agent.resume_task, task["id"])
        self.assertEqual(result.message, "continued without material")
        self.assertEqual(dispatched, [1])
        self.assertNotIn("private-material-253", self.agent.model.calls[-1].context)

    async def test_external_task_guardrail_wait_resumes_only_after_owner_decision(self):
        from core_agent.guardrails import GuardrailClassifier

        detector = ScriptedModel([
            ModelResponse(message='{"verdict":"suspicious"}', finish_reason="stop"),
        ])
        self.agent.guardrail_classifier = GuardrailClassifier(detector, token_counter=len)
        task = await self.submit("external-a", "guarded-message", "guarded-external-chat")
        self.assertEqual(task["status"]["state"], "TASK_STATE_WORKING")
        self.assertEqual(self.model.calls, ())
        waiting = await self.pending(task["id"])
        self.assertEqual(waiting["kind"], "guardrail")
        url = f"/api/guardrails/{waiting['wait_id']}"
        material = await self.http.get(url + "/material", headers=self.headers("owner-b"))
        self.assertEqual(material.status_code, 200, material.text)
        self.assertIn("Answer briefly", json.dumps(material.json()["material"]))
        decision = {"decision": "reject", "subject_digest": waiting["subject_digest"]}
        denied = await self.http.post(url + "/decision", headers=self.headers("external-a"), json=decision)
        self.assertEqual(denied.status_code, 403)
        accepted = await self.http.post(url + "/decision", headers=self.headers("owner-a"), json=decision)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.agent._runtime_cache.clear()
        result = await asyncio.to_thread(self.agent.resume_task, task["id"])
        self.assertEqual(result.message, "verified")
        self.assertEqual(len(detector.calls), 1)
        self.assertNotIn("Answer briefly", self.model.calls[-1].context)
        self.assertIn("MATERIAL_", self.model.calls[-1].context)
        # Direct resume bypasses the recovery coordinator's settled callback.
        self.reconcile_workflows(task["id"])
        public = await self.http.get("/a2a/external/tasks/" + task["id"], headers=self.headers("external-a"))
        self.assertEqual(public.json()["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertNotIn(waiting["wait_id"], public.text)

    async def pending(self, task_id):
        response = await self.http.get("/api/interactions", headers=self.headers("owner-a"), params={"task_id": task_id})
        self.assertEqual(response.status_code, 200, response.text)
        pending = response.json()["interactions"]
        self.assertEqual(len(pending), 1, pending)
        return pending[0]

    async def test_external_task_asks_owner_with_private_publication_and_followup_after_wait(self):
        question, answer = "private-owner-question-739", "private-owner-answer-851"
        self.agent.model = ScriptedModel([
            ModelResponse(message="private-intermediate-735", reasoning="private-reasoning-739",
                          tool_requests=(ToolRequest("ask-1", "core_ask_owner", {"question": question}),)),
            ModelResponse(message="public-final-result"),
        ])
        response = await asyncio.wait_for(self.http.post(
            "/a2a/external/message:stream", headers=self.headers("external-a"),
            json={"message": {"messageId": "private-wait", "contextId": "external-question-chat",
                              "role": "ROLE_USER", "parts": [{"text": "ask the owners"}]}},
        ), 5)
        self.assertEqual(response.status_code, 200, response.text)
        listed = await self.http.get("/a2a/external/tasks", headers=self.headers("external-a"))
        self.assertEqual(listed.status_code, 200, listed.text)
        task_id = next(task["id"] for task in listed.json()["tasks"] if task["contextId"] == "external-question-chat")
        record = self.agent.workflow_store.lookup_task(task_id)
        self.assertEqual(record.state, "WAITING_INPUT")
        for private in (question, "private-intermediate-735", "private-reasoning-739", "core_ask_owner"):
            self.assertNotIn(private, response.text)
        approval = await self.pending(record.task_id)
        self.assertEqual(approval["kind"], "tool_approval")
        self.assertEqual(approval["subject"]["arguments"]["question"], question)
        approved = await self.http.post(f"/api/hitl/{approval['wait_id']}/decision", headers=self.headers("owner-a"),
                                        json={"decision": "allow", "subject_digest": approval["subject_digest"]})
        self.assertEqual(approved.status_code, 200, approved.text)
        await asyncio.to_thread(self.agent.resume_task, record.task_id)
        question_wait = await self.pending(record.task_id)
        self.assertEqual(question_wait["kind"], "owner_question")
        public = await self.http.get("/a2a/external/tasks/" + record.task_id, headers=self.headers("external-a"))
        self.assertEqual(public.json()["status"]["state"], "TASK_STATE_WORKING")
        self.assertNotIn(question, public.text)
        followup = await self.http.post(
            "/a2a/external/message:send", headers=self.headers("external-a"),
            json={"message": {"messageId": "correction", "taskId": record.task_id,
                              "role": "ROLE_USER", "parts": [{"text": "use-order-51"}]}},
        )
        self.assertEqual(followup.status_code, 200, followup.text)
        self.assertEqual(len(self.agent.model.calls), 1)
        path = f"/api/questions/{question_wait['wait_id']}/answer"
        payload = {"answer": answer, "subject_digest": question_wait["subject_digest"]}
        self.assertEqual((await self.http.post(path, headers=self.headers("external-a"), json=payload)).status_code, 403)
        answered = await self.http.post(path, headers=self.headers("owner-b"), json=payload)
        self.assertEqual(answered.status_code, 200, answered.text)
        result = await asyncio.to_thread(self.agent.resume_task, record.task_id)
        self.assertEqual(result.message, "public-final-result")
        current = self.agent.workflow_store.lookup_task(record.task_id)
        self.assertEqual(current.snapshot["tool_calls"], 1)
        model_context = json.dumps(self.agent.model.calls[-1].messages)
        self.assertIn(answer, model_context)
        self.assertIn("use-order-51", model_context)
        self.reconcile_workflows(record.task_id)
        final = await self.http.get("/a2a/external/tasks/" + record.task_id, headers=self.headers("external-a"))
        self.assertEqual(final.json()["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertIn("public-final-result", final.text)
        for private in (question, answer, "private-intermediate-735", "private-reasoning-739"):
            self.assertNotIn(private, final.text)

    async def test_rejection_returns_tool_error_without_execution(self):
        self.agent.model = ScriptedModel([
            ModelResponse(tool_requests=(ToolRequest("list-1", "core_task_list", {}),)),
            ModelResponse(message="continued after refusal"),
        ])
        with patch.object(self.agent.task_scheduler, "list", side_effect=AssertionError("must not dispatch")):
            task = await self.submit("owner-a", "list-approval", "owner-approval-chat")
            approval = await self.pending(task["id"])
            rejected = await self.http.post(f"/api/hitl/{approval['wait_id']}/decision", headers=self.headers("owner-b"),
                                            json={"decision": "reject", "subject_digest": approval["subject_digest"]})
            self.assertEqual(rejected.status_code, 200, rejected.text)
            result = await asyncio.to_thread(self.agent.resume_task, task["id"])
        self.assertEqual(result.message, "continued after refusal")
        self.assertIn("OWNER_APPROVAL_REJECTED", json.dumps(self.agent.model.calls[-1].messages))

    async def test_unknown_python_failure_requires_reconciliation_without_replay(self):
        self.agent.model = ScriptedModel([
            ModelResponse(tool_requests=(ToolRequest("python-1", "core_python_exec", {"code": "perform_effect()"}),)),
        ])
        self.agent.interaction_store.update_policy(
            self.app.state.authenticator.settings.tenant, "core_python_exec", "builtin:core_python_exec",
            mode="allow", guardrails_exempt=False, expected_revision=0, actor_id="test-owner",
        )
        effects = []

        def failed_after_start(_arguments, _run_id):
            effects.append("started")
            raise RuntimeError("lost process outcome")

        self.agent.tool_runtime.handlers["core_python_exec"] = failed_after_start
        task = await self.submit("owner-a", "python-unknown", "python-unknown-chat")
        self.assertEqual(task["status"]["state"], "TASK_STATE_FAILED")
        record = self.agent.workflow_store.lookup_task(task["id"])
        self.assertEqual((record.state, record.error_code), ("ABORTED", "SIDE_EFFECT_UNKNOWN"))
        self.assertEqual(effects, ["started"])


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL is required")
class PostgresOwnerRuntimeAPITests(OwnerRuntimeAPITests):
    use_postgres = True

    async def test_allowed_call_survives_new_pool_and_is_executed_once(self):
        self.agent.model = ScriptedModel([
            ModelResponse(tool_requests=(ToolRequest("list-1", "core_task_list", {}),)),
        ])
        task = await self.submit("external-a", "restart-hitl", "restart-hitl-chat")
        approval = await self.pending(task["id"])
        response = await self.http.post(f"/api/hitl/{approval['wait_id']}/decision", headers=self.headers("owner-a"),
                                        json={"decision": "allow", "subject_digest": approval["subject_digest"]})
        self.assertEqual(response.status_code, 200, response.text)
        original = self.agent.workflow_store.lookup_task(task["id"])
        self.app.state.close()

        second_model = ScriptedModel([ModelResponse(message="resumed once")])
        second_model.model = "auth-test-model"
        with patch("core_agent.runtime.CoreAgent.recover_workflows"):
            second = create_app(model=second_model, database=PostgresDatabase(TEST_DATABASE_URL),
                                auth_transport=httpx.MockTransport(self.introspect))
        self.addCleanup(second.state.close)
        resumed = second.state.core_agent
        calls = []
        resumed.tool_runtime.handlers["core_task_list"] = lambda _arguments, _run: calls.append("dispatched") or {"tasks": []}
        result = await asyncio.to_thread(resumed.resume_task, task["id"])
        self.assertEqual(result.message, "resumed once")
        current = resumed.workflow_store.lookup_task(task["id"])
        self.assertEqual(current.run_id, original.run_id)
        self.assertEqual(current.snapshot["tool_calls"], 1)
        wait = resumed.workflow_store.get_wait(approval["wait_id"], tenant_id=current.tenant_id)
        self.assertIsNotNone(wait.applied_at)
        self.assertEqual(wait.outcome["reason"], "allowed")
        self.assertEqual(calls, ["dispatched"])
        repeated = await asyncio.to_thread(resumed.resume_task, task["id"])
        self.assertEqual(repeated, result)
        self.assertEqual(calls, ["dispatched"])
        self.assertEqual(len(second_model.calls), 1)
