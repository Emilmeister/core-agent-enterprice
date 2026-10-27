"""Public output is explicit, durable and visible before the next model turn."""
import asyncio
import hashlib
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from core_agent.interactions import tool_origin
from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
from tests.test_auth import AuthAppTestCase


class PublicOutputRuntimeTests(AuthAppTestCase):
    async def asyncSetUp(self):
        with patch("core_agent.runtime.CoreAgent.recover_workflows"):
            await super().asyncSetUp()

    def script(self, *responses):
        model = ScriptedModel(responses)
        self.app.state.core_agent.model = model
        return model

    def current(self, context_id):
        agent = self.app.state.core_agent
        run_id = next(run_id for run_id, scope in agent._run_scopes.items() if scope["session_id"] == context_id)
        return agent.workflow_store.get(run_id, tenant_id=self.app.state.authenticator.settings.tenant)

    def policy(self, name, mode):
        agent = self.app.state.core_agent
        tenant = self.app.state.authenticator.settings.tenant
        origin = tool_origin(name)
        current = agent.interaction_store.get_policy(tenant, name, origin)
        agent.interaction_store.update_policy(tenant, name, origin, mode=mode,
            guardrails_exempt=False, expected_revision=current.revision, actor_id="owner-test")

    @staticmethod
    def call(name, arguments, call_id="publish"):
        return ModelResponse(tool_requests=(ToolRequest(call_id, name, arguments),))

    async def test_normal_reply_is_a_durable_message_without_an_implicit_artifact(self):
        self.script(ModelResponse(message="Москва: +10 °C"))
        task = await self.submit("external-a", "plain", "plain-chat")
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(task["status"]["message"]["parts"][0]["text"], "Москва: +10 °C")
        self.assertFalse(task.get("artifacts"))
        fetched = await self.http.get(f"/a2a/external/tasks/{task['id']}",
            headers=self.headers("external-a"), params={"historyLength": 0})
        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(fetched.json()["status"]["message"], task["status"]["message"])
        self.assertFalse(fetched.json().get("artifacts"))

    async def test_explicit_artifacts_append_before_the_task_finishes(self):
        model = self.script(
            self.call("core_response_artifacts", {"artifacts": [
                {"name": "weather", "parts": [{"type": "data", "data": {"temperature": 10}}]}]}),
            self.call("core_response_artifacts", {"artifacts": [
                {"name": "report", "parts": [{"type": "text", "text": "Облачно"}]}]}, "publish-two"),
            ModelResponse(message="Прогноз подготовлен"),
        )
        observed = []
        original = model.generate

        def generate(**kwargs):
            if model.calls:
                record = self.current("artifact-chat")
                observed.append((record.state, len(record.snapshot.get("published_artifacts", []))))
            return original(**kwargs)

        model.generate = generate
        task = await self.submit("external-a", "typed", "artifact-chat")
        self.assertEqual([count for _, count in observed], [1, 2])
        self.assertTrue(all(state != "COMPLETED" for state, _ in observed))
        self.assertEqual([a["name"] for a in task["artifacts"]], ["weather", "report"])
        self.assertEqual(task["artifacts"][0]["parts"][0]["data"], {"temperature": 10})
        self.assertEqual(task["artifacts"][1]["parts"][0]["text"], "Облачно")
        self.assertEqual(task["status"]["message"]["parts"][0]["text"], "Прогноз подготовлен")

    async def test_working_message_is_committed_without_ending_the_task(self):
        model = self.script(self.call("core_message_send", {"text": "Проверяю прогноз"}),
            ModelResponse(message="Готово"))
        observed = []
        original = model.generate

        def generate(**kwargs):
            if model.calls:
                record = self.current("message-chat")
                observed.append((record.state, record.snapshot.get("public_messages", [])))
            return original(**kwargs)

        model.generate = generate
        task = await self.submit("external-a", "progress", "message-chat")
        self.assertEqual(len(observed), 1)
        self.assertNotEqual(observed[0][0], "COMPLETED")
        self.assertEqual(observed[0][1][0]["text"], "Проверяю прогноз")
        self.assertEqual(task["status"]["message"]["parts"][0]["text"], "Готово")
        fetched = await self.http.get(f"/a2a/external/tasks/{task['id']}",
            headers=self.headers("external-a"), params={"historyLength": 10})
        self.assertEqual([p["text"] for m in fetched.json().get("history", [])
                          if m["role"] == "ROLE_AGENT" for p in m["parts"]], ["Проверяю прогноз", "Готово"])
        self.assertFalse(task.get("artifacts"))

    async def test_hitl_cannot_publish_until_the_owner_allows_the_call(self):
        self.policy("core_response_artifacts", "require_hitl")
        self.script(self.call("core_response_artifacts", {"artifacts": [
            {"name": "pending", "parts": [{"type": "text", "text": "PRIVATE_UNAPPROVED_OUTPUT"}]}]}),
            ModelResponse(message="Готово"))
        task = await self.submit("external-a", "approval", "approval-chat")
        self.assertNotEqual(task["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertFalse(task.get("artifacts"))
        self.assertNotIn("PRIVATE_UNAPPROVED_OUTPUT", str(task))
        record = self.app.state.core_agent.workflow_store.lookup_task(task["id"])
        self.assertFalse(record.snapshot.get("published_artifacts"))

    async def test_denied_publication_does_not_create_an_artifact(self):
        self.policy("core_response_artifacts", "deny")
        self.script(self.call("core_response_artifacts", {"artifacts": [
            {"parts": [{"type": "text", "text": "DENIED_OUTPUT"}]}]}), ModelResponse(message="Продолжаю"))
        task = await self.submit("external-a", "denied", "denied-chat")
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertFalse(task.get("artifacts"))
        self.assertNotIn("DENIED_OUTPUT", str(task))

    async def test_file_alias_publishes_immutable_selection_and_empty_selection_is_a_noop(self):
        from core_agent.workspace import WorkspaceBinding
        model = self.script(self.call("core_response_files", {"paths": ["report.txt"]}),
            self.call("core_response_files", {"paths": []}, "empty"), ModelResponse(message="Delivered"))
        generate = model.generate
        observed = []
        def files(**kwargs):
            description = kwargs["tools"]["core_response_files"]["description"]
            self.assertIn("relative workspace paths", description)
            self.assertIn("test.csv", description)
            record = self.current("file-alias-chat")
            workspace = self.app.state.core_agent.response_files_service.workspaces.workspace(
                WorkspaceBinding(record.tenant_id, record.owner_id, record.context_id))
            if not model.calls:
                (workspace / "report.txt").write_bytes(b"captured")
            else:
                observed.append(len(record.snapshot["published_artifacts"]))
                (workspace / "report.txt").write_bytes(b"changed")
            return generate(**kwargs)
        model.generate = files
        task = await self.submit("external-a", "file-alias", "file-alias-chat")
        self.assertEqual(observed, [1, 1])
        self.assertEqual(len(task["artifacts"]), 1)
        self.assertEqual(task["artifacts"][0]["parts"][0]["raw"], "Y2FwdHVyZWQ=")
        self.assertEqual(task["status"]["message"]["parts"][0]["text"], "Delivered")

    async def test_result_review_keeps_prepared_artifact_private_then_recovery_publishes_once(self):
        from core_agent.guardrails import GuardrailClassifier
        from core_agent.material_reviews import MemoryMaterialReviewStore, PostgresMaterialReviewStore
        agent = self.app.state.core_agent
        for reason in ("allowed", "rejected"):
            with self.subTest(reason=reason):
                detector = ScriptedModel([ModelResponse(message=json.dumps({"verdict": verdict}), finish_reason="stop")
                    for verdict in ("clear", "suspicious", "clear")])
                detector.context_window, detector.max_tokens = 128000, 512
                detector.count_tokens = lambda text: max(1, len(text) // 4)
                agent.guardrail_classifier = GuardrailClassifier(detector, clock=agent.workflow_store.current_time)
                agent.material_review_store = (PostgresMaterialReviewStore(agent.workflow_store.database, agent.workflow_store)
                    if self.use_postgres else MemoryMaterialReviewStore(agent.workflow_store))
                self.script(self.call("core_response_artifacts", {"artifacts": [
                    {"parts": [{"type": "text", "text": "PRIVATE_RESULT_REVIEW_CANARY"}]}]}),
                    ModelResponse(message="Reviewed"))
                task = await self.submit("external-a", "review-" + reason, "review-chat-" + reason)
                record = agent.workflow_store.lookup_task(task["id"])
                self.assertEqual(record.state, "WAITING_INPUT")
                self.assertIn("prepared_public_output", record.snapshot)
                self.assertFalse(record.snapshot["published_artifacts"])
                self.assertFalse(task.get("artifacts"))
                self.assertNotIn("PRIVATE_RESULT_REVIEW_CANARY", str(task))
                agent.workflow_store.resolve_wait(record.snapshot["wait_id"], tenant_id=record.tenant_id,
                    outcome={"reason": reason}, actor_id="owner-test")
                agent._runtime_cache.clear()
                with patch.object(agent.tool_runtime, "execute", side_effect=AssertionError("prepared publication replay")):
                    result = await asyncio.to_thread(agent.resume_task, task["id"])
                self.assertEqual(result.message, "Reviewed")
                self.assertEqual(result.usage.tool_calls, 1)
                completed = agent.workflow_store.lookup_task(task["id"])
                self.assertNotIn("prepared_public_output", completed.snapshot)
                self.assertEqual(len(completed.snapshot["published_artifacts"]), int(reason == "allowed"))
                await asyncio.to_thread(agent.resume_task, task["id"])
                self.assertEqual(len(agent.workflow_store.lookup_task(task["id"]).snapshot["published_artifacts"]),
                                 int(reason == "allowed"))


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "set TEST_DATABASE_URL for PostgreSQL public runtime tests")
class PostgresPublicOutputRuntimeTests(PublicOutputRuntimeTests):
    use_postgres = True
    durable_blobs = True

    async def test_cold_public_output_survives_timer_followup_and_cancel_transactions(self):
        agent = self.app.state.core_agent
        workflow = agent.workflow_store
        blobs = agent.response_files_service.artifact_store
        contents, reads = {}, []
        model = self.script(
            self.call("core_response_artifacts", {"artifacts": [
                {"parts": [{"type": "text", "text": "Published before waiting"}]}]}),
            self.call("core_wait_until", {"until": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}, "timer"))
        original_generate = model.generate

        def evict():
            with blobs._cache_lock:
                blobs._cache.clear()
                blobs._cache_bytes = 0

        def remote_get(digest, size):
            with blobs.database.pool.connection(timeout=1) as connection:
                connection.execute("SELECT 1")
            reads.append(digest)
            return contents[digest]

        def remote_put(content):
            digest = hashlib.sha256(content).hexdigest()
            contents[digest] = bytes(content)
            return digest

        def generate(**call):
            if model.calls:
                record = self.current("cold-wait")
                for manifest in record.snapshot["published_artifacts"]:
                    for part in manifest["parts"]:
                        ref = part["ref"]
                        contents[ref["sha256"]] = blobs.get(record.tenant_id, ref["blob_id"])[1]
                blobs.blob_store = SimpleNamespace(tenant_id=record.tenant_id, get=remote_get, put=remote_put)
                blobs.database.pool.resize(1, 1)
                blobs.database.pool.timeout = 2
                evict()
            return original_generate(**call)

        model.generate = generate
        policy_scope = agent.interaction_store.policy_scope
        @contextmanager
        def cold_policy(*args, **kwargs):
            if blobs.blob_store is not None:
                evict()
            with policy_scope(*args, **kwargs) as scope:
                yield scope
        self.enterContext(patch.object(agent.interaction_store, "policy_scope", cold_policy))
        task = await self.submit("external-a", "publish-and-wait", "cold-wait")
        def remove_test_task():
            with workflow.database.transaction() as connection:
                connection.execute("DELETE FROM core_root_messages WHERE task_id = %s AND tenant_id = %s",
                                   (task["id"], self.app.state.authenticator.settings.tenant))
                connection.execute("DELETE FROM core_a2a_tasks WHERE task_id = %s AND tenant = %s",
                                   (task["id"], self.app.state.authenticator.settings.tenant))
        self.addCleanup(remove_test_task)
        async with asyncio.timeout(5):
            while True:
                record = workflow.lookup_task(task["id"])
                if record.state in {"WAITING_TASK", "COMPLETED", "FAILED", "ABORTED", "CANCELLED"}:
                    break
                await asyncio.sleep(0.01)
        self.assertEqual(record.state, "WAITING_TASK", record.error_code)
        self.assertTrue(reads)
        reads.clear()
        append = workflow.append_inbound
        cold = [True]
        def append_after_eviction(*args, **kwargs):
            if cold:
                cold.clear()
                evict()
            return append(*args, **kwargs)
        with patch.object(workflow, "append_inbound", side_effect=append_after_eviction):
            response = await self.http.post("/a2a/external/message:send", headers=self.headers("external-a"),
                json={"message": {"messageId": "wake-cold", "taskId": task["id"], "role": "ROLE_USER",
                                  "parts": [{"text": "Stop after preserving the publication"}]}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(reads)
        self.assertEqual(len(workflow.pending_inbound(record)), 1)
        reads.clear()
        begin = workflow.begin_terminal
        def begin_after_eviction(*args, **kwargs):
            evict()
            return begin(*args, **kwargs)
        with patch.object(workflow, "begin_terminal", side_effect=begin_after_eviction):
            cancelled = await self.http.post(f"/a2a/external/tasks/{task['id']}:cancel",
                                            headers=self.headers("external-a"), json={})
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["status"]["state"], "TASK_STATE_CANCELED")
        self.assertTrue(reads)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(len(workflow.lookup_task(task["id"]).snapshot["published_artifacts"]), 1)


class NestedPublicOutputRuntimeTests(unittest.TestCase):
    from tests.test_tool_approvals import ToolApprovalTests as _Fixture
    from tests.test_python_waits import PythonWaitTests as _Python
    setUp = _Fixture.setUp
    app = _Fixture.app
    call = staticmethod(_Fixture.call)
    start = staticmethod(_Fixture.start)
    policy = staticmethod(_Fixture.policy)
    python = _Python.python

    def test_nested_message_result_review_stops_python_before_publication_and_recovers_once(self):
        from dataclasses import replace
        from core_agent.guardrails import GuardrailClassifier
        from core_agent.material_reviews import MemoryMaterialReviewStore
        from core_agent.workflow import SuspendedRun
        from core_agent.artifacts import InMemoryArtifactStore
        from core_agent.response_files import ResponseFileService
        from core_agent.tools import RESPONSE_ARTIFACTS_TOOL, RESPONSE_FILES_TOOL
        from core_agent.workspace import ChatWorkspaces, WorkspaceBinding
        calls = [("core_message_send", {"text": "Approved nested reply"}, "public_messages"),
                 ("core_response_artifacts", {"artifacts": [{"parts": [
                     {"type": "text", "text": "Approved nested artifact"}]}]}, "published_artifacts"),
                 ("core_response_files", {"paths": ["report.txt"]}, "published_artifacts")]
        for name, arguments, field in calls:
          for reason in ("allowed", "rejected"):
            with self.subTest(reason=reason, tool=name):
                agent, model = self.app(self.call("core_python_exec", {"code": "tools.call(); remainder"}),
                    ModelResponse(message="done"))
                agent.platform_config = replace(agent.platform_config,
                    allowed_builtin_tools=(*agent.platform_config.allowed_builtin_tools, name))
                from core_agent.config import AgentConfig
                raw = agent.agent_config.to_dict()
                raw["tools"]["builtins"]["allow"] = [*raw["tools"]["builtins"].get("allow", []), name]
                agent.agent_config = AgentConfig.from_dict(raw)
                if name != "core_message_send":
                    workspaces = ChatWorkspaces(self.temp.name + "/nested-" + name + reason)
                    agent.response_files_service = ResponseFileService(workspaces, InMemoryArtifactStore())
                    for definition in (RESPONSE_ARTIFACTS_TOOL, RESPONSE_FILES_TOOL):
                        if definition.name not in agent.tool_runtime.registry.names():
                            agent.tool_runtime.registry.register(definition)
                    (workspaces.workspace(WorkspaceBinding("company", "owner", "chat")) / "report.txt").write_bytes(b"nested file")
                self.policy(agent, "allow", "core_python_exec")
                self.policy(agent, "allow", name)
                detector = ScriptedModel([ModelResponse(message=json.dumps({"verdict": verdict}), finish_reason="stop")
                    for verdict in ("clear", "suspicious", "clear")])
                detector.context_window, detector.max_tokens = 128000, 512
                detector.count_tokens = lambda text: max(1, len(text) // 4)
                agent.guardrail_classifier = GuardrailClassifier(detector, clock=lambda: self.now[0])
                agent.material_review_store = MemoryMaterialReviewStore(agent.workflow_store)
                with self.python(agent, name=name, arguments=arguments):
                    sleeping = self.start(agent)
                self.assertIsInstance(sleeping, SuspendedRun)
                record = agent.workflow_store.lookup_task("task")
                self.assertEqual(record.snapshot[field], [])
                self.assertIn("prepared_public_output", record.snapshot)
                self.assertEqual((self.prefix, self.remainder, self.stops), (1, 0, 1))
                agent.workflow_store.resolve_wait(sleeping.wait_id, tenant_id="company",
                    outcome={"reason": reason}, actor_id="owner")
                agent._runtime_cache.clear()
                with patch("core_agent.runtime.execute_python", side_effect=AssertionError("Python replay")):
                    result = agent.resume_task("task")
                self.assertEqual(result.message, "done")
                self.assertEqual(result.usage.tool_calls, 2)
                completed = agent.workflow_store.lookup_task("task")
                self.assertEqual(len(completed.snapshot[field]), int(reason == "allowed"))
                self.assertNotIn("prepared_public_output", completed.snapshot)

    def test_children_inherit_legacy_missing_pin_and_new_roots_pin_v2(self):
        from dataclasses import replace
        agent, _model = self.app(ModelResponse(message="unused"))
        root = agent._new_workflow({"prompt": "root"}, task_id="root", identity="owner", session_id="chat",
            tenant_id="company", defer_initialization=True)[0]
        self.assertEqual(root.snapshot["output_contract_version"], 2)
        legacy = replace(root, snapshot={key: value for key, value in root.snapshot.items()
                                        if key != "output_contract_version"})
        agent.workflow_store._records[root.run_id] = legacy
        child = agent._new_workflow({"prompt": "child"}, task_id="child", identity="owner", session_id="chat",
            tenant_id="company", parent_run_id=root.run_id, defer_initialization=True)[0]
        self.assertEqual(child.snapshot["output_contract_version"], 1)

    def test_legacy_skill_and_reconnect_instruction_compilation_preserves_output_semantics(self):
        from types import SimpleNamespace
        agent, _model = self.app(ModelResponse(message="unused"))
        agent.reply_hub = SimpleNamespace()
        agent._model_streams_deltas = True
        effective = SimpleNamespace(enabled_capability_policies={"response_files", "public_messages"},
                                    skills={"known"})
        for snapshot in ({}, {"skills": [{"name": "known", "instructions": "Read scoped files"}]}):
            with self.subTest(snapshot=snapshot):
                # Direct compilation follows validated admission/recovery pinning.
                snapshot["kernel_instructions"] = agent._kernel_snapshot(1)
                compiled = agent._compile_instructions({"agent": {}}, effective, snapshot,
                    model_tools={"core_response_files": {}})
                self.assertIn("replaces your previous selection", compiled.text)
                self.assertIn("[] clears the set", compiled.text)
                self.assertNotIn("core_response_artifacts", compiled.text)
                self.assertNotIn("core_message_send", compiled.text)
                self.assertNotIn("attach any explicitly chosen response artifacts", compiled.text)


class IncomingPublicArtifactRuntimeTests(AuthAppTestCase):
    from tests.test_remote_file_results_runtime import RemoteFileResultTests as _Fixture
    asyncSetUp = _Fixture.asyncSetUp
    durable_blobs = True
    remote_contract_version = 3

    async def test_separate_early_remote_artifact_batches_are_all_reviewed_and_projected(self):
        from core_agent.remote_agents import RemoteAgentConnection, RemoteEvent
        from core_agent.interactions import TIMEOUT_KEYS
        settings = self.agent.interaction_store.get_settings(self.tenant)
        self.agent.interaction_store.update_settings(self.tenant,
            {**{key: getattr(settings, key) for key in TIMEOUT_KEYS}, "remote_poll_interval_seconds": 1}, settings.revision)
        marker = {"core_agent_artifact": {"version": 1, "complete": True, "immutable": True}}
        artifacts = [{"artifactId": name, "name": name, "parts": [{"text": name + "-content-canary"}],
                      "metadata": marker} for name in ("first", "second")]
        self.response = RemoteEvent("task", "TASK_STATE_WORKING", "", False, (), "remote", "remote-context",
                                    artifacts=(artifacts[0],), public_messages_complete=True)
        updates = [RemoteEvent("task", "TASK_STATE_WORKING", "", False, (), "remote", "remote-context",
                             artifacts=tuple(artifacts), public_messages_complete=True),
                   RemoteEvent("task", "TASK_STATE_COMPLETED", "", True, (), "remote", "remote-context",
                             artifacts=tuple(artifacts), public_messages_complete=True)]
        self.settle = False
        scheduler = self.agent.task_scheduler
        now = [scheduler.clock()]
        def settle_polls(contract, *args, **kwargs):
            task = type(scheduler).start_remote(scheduler, contract, *args, **kwargs)
            self.operation_id = task.id
            for _ in range(2):
                try:
                    scheduler.wait(task.id, .5, owner_id=kwargs["owner_id"], tenant_id=kwargs["tenant_id"])
                except TimeoutError:
                    pass
                now[0] += 2
                scheduler.recover(tenant_id=self.tenant)
            return scheduler.wait(task.id, 3, owner_id=kwargs["owner_id"], tenant_id=kwargs["tenant_id"])
        with patch.object(RemoteAgentConnection, "get_task", side_effect=updates), patch.object(
                scheduler, "clock", side_effect=lambda: now[0]), patch.object(
                scheduler, "start_remote", side_effect=settle_polls):
            task = await self.submit("owner-a", "early-batches", "early-batches-chat")
        record = self.agent.workflow_store.lookup_task(task["id"])
        self.record = record
        self.assertEqual(record.state, "COMPLETED", record.error_code)
        remote = self.agent.task_scheduler.get(self.operation_id, owner_id=record.run_id, tenant_id=record.tenant_id)
        self.assertEqual(len(remote.result["file_batch_ids"]), 2)
        from core_agent.workspace import WorkspaceBinding
        binding = WorkspaceBinding(record.tenant_id, record.owner_id, record.context_id)
        for batch_id in remote.result["file_batch_ids"]:
            batch = self.agent.chat_file_service.store.get(batch_id, record.tenant_id)
            self.assertEqual(batch["state"], "published")
            material = self.agent.chat_file_service.review_material(batch_id, binding,
                run_id=record.run_id, task_id=record.task_id)
            self.assertTrue(all(entry["relative_path"] in self.model.calls[-1].context
                                for entry in material["manifest"]["entries"]))
        self.assertNotIn("first-content-canary", self.model.calls[-1].context)
        self.assertNotIn("second-content-canary", self.model.calls[-1].context)
