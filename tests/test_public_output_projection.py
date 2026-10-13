"""Canonical public Messages and model-selected Artifacts share one projection."""
import copy
import asyncio
import os
import threading
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from a2a.types import Message, Role, Task, TaskState, TaskStatus

from core_agent.a2a import Artifact, Part
from core_agent.database import reconcile_workflow_task
from core_agent.errors import CoreError
from core_agent.a2a_sdk import CoreAgentExecutor, TaskStreamPublisher, ScopedMemoryTaskStore
from core_agent.database import public_artifact_parts
from core_agent.auth import AuthenticatedCallContext, ScopeUser
from core_agent.database import PostgresDatabase, PostgresTaskStore
from psycopg.types.json import Jsonb


class PublicOutputProjectionTests(unittest.TestCase):
    def record(self, *, state="RUNNING", messages=(), artifacts=(), result=None, version=4):
        return {"tenant_id": "company", "owner_id": "owner", "context_id": "chat", "task_id": "task",
            "run_id": "run", "state": state, "version": version, "error_code": None,
            "snapshot": {"output_contract_version": 2, "public_messages": list(messages),
                         "published_artifacts": list(artifacts)}, "result": result}

    def task(self):
        return Task(id="task", context_id="chat", status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
            history=[Message(message_id="initial", task_id="task", context_id="chat", role=Role.ROLE_USER,
                             parts=[{"text": "Do the work"}])])

    def project(self, task, record, **options):
        return reconcile_workflow_task(task, run_id=record["run_id"], state=record["state"],
            result=record["result"], error_code=record["error_code"], version=record["version"], record=record, **options)

    def test_plain_final_text_is_a_message_without_an_automatic_artifact(self):
        record = self.record(state="COMPLETED", result={"output_contract_version": 2, "message": "Today or tomorrow?"})
        task = self.task()
        self.project(task, record)
        self.assertEqual(task.status.state, TaskState.TASK_STATE_COMPLETED)
        self.assertEqual(list(task.artifacts), [])
        self.assertEqual(task.status.message.message_id, "task:final")
        self.assertEqual(task.status.message.parts[0].text, "Today or tomorrow?")
        self.assertEqual([message.message_id for message in task.history], ["initial", "task:final"])

    def test_working_public_messages_are_persistent_ordered_and_not_duplicated(self):
        record = self.record(messages=[{"message_id": "run:call-1", "text": "Preparing"},
                                       {"message_id": "run:call-2", "text": "Halfway there"}])
        task = self.task()
        self.project(task, record)
        self.assertEqual(task.status.state, TaskState.TASK_STATE_WORKING)
        self.assertEqual(task.status.message.message_id, "run:call-2")
        self.assertEqual([message.message_id for message in task.history], ["initial", "run:call-1", "run:call-2"])
        before = task.SerializeToString(deterministic=True)
        self.assertFalse(self.project(task, record))
        self.assertEqual(task.SerializeToString(deterministic=True), before)
        advanced = {**record, "version": 9}
        self.assertFalse(self.project(task, advanced), "private runtime revisions must not republish unchanged output")
        self.assertEqual(task.SerializeToString(deterministic=True), before)
        del task.history[:]
        self.assertEqual(task.status.message.parts[0].text, "Halfway there")

    def test_private_owner_wait_does_not_publish_private_question_or_drop_public_history(self):
        record = self.record(state="WAITING_INPUT", messages=[{"message_id": "public", "text": "Checking details"}])
        record["snapshot"]["private_question"] = "owner-only-canary"
        task = self.task()
        self.project(task, record)
        self.assertEqual(task.status.state, TaskState.TASK_STATE_WORKING)
        self.assertEqual(dict(task.metadata)["core_agent_wait"]["kind"], "owner")
        self.assertFalse(task.status.HasField("message"))
        self.assertEqual(task.history[-1].parts[0].text, "Checking details")
        self.assertNotIn(b"owner-only-canary", task.SerializeToString())

    def test_public_history_preserves_followup_message_order_and_removes_uncommitted_preview(self):
        task = self.task()
        first = {"message_id": "first", "text": "Preparing"}
        self.project(task, self.record(messages=[first], version=2))
        task.history.append(Message(message_id="followup", task_id="task", context_id="chat",
                                    role=Role.ROLE_USER, parts=[{"text": "Include detail"}]))
        task.history.append(Message(message_id="preview", role=Role.ROLE_AGENT,
                                    parts=[{"text": "uncommitted-canary"}]))
        self.project(task, self.record(messages=[first, {"message_id": "second", "text": "Done"}], version=3))
        self.assertEqual([item.message_id for item in task.history], ["initial", "first", "followup", "second"])
        self.assertNotIn(b"uncommitted-canary", task.SerializeToString())

    def test_unknown_pinned_output_version_is_not_treated_as_legacy(self):
        record = self.record(state="COMPLETED", result={"message": "answer"})
        record["snapshot"]["output_contract_version"] = 99
        with self.assertRaises(CoreError) as error:
            self.project(self.task(), record)
        self.assertEqual(error.exception.code, "CHECKPOINT_INVALID")

    def test_legacy_record_still_publishes_its_final_artifact(self):
        record = self.record(state="COMPLETED", result={"message": "old answer"})
        record["snapshot"] = {}
        task = self.task()
        self.project(task, record)
        self.assertEqual(task.artifacts[0].parts[0].text, "old answer")
        self.assertFalse(task.status.HasField("message"))

    def test_explicit_artifacts_publish_while_working_and_survive_terminal_states(self):
        artifact = Artifact("selected", (Part.text("chosen text"), Part("data", {"field": "json-canary"}),
            Part.file(b"\x00\xff", filename="binary.bin", media_type="application/octet-stream")), 1,
            {"core_agent_artifact": {"version": 1, "complete": True, "immutable": True}})
        loader = SimpleNamespace(load=lambda *_args, **_kwargs: (artifact,))
        service = SimpleNamespace(ResponseArtifactService=lambda _files: loader)
        record = self.record(artifacts=[{"schema_version": 1, "artifact_id": "selected", "name": "Chosen",
            "parts": [{"type": "text", "ref": {"limit_bytes": 25000000}}]}])
        task = self.task()
        with patch.dict("sys.modules", {"core_agent.response_artifacts": service}):
            self.project(task, record, response_files_service=object())
            self.assertEqual(task.status.state, TaskState.TASK_STATE_WORKING)
            self.assertEqual(len(task.artifacts), 1)
            self.assertEqual([part.WhichOneof("content") for part in task.artifacts[0].parts], ["text", "data", "raw"])
            before = task.artifacts[0].SerializeToString(deterministic=True)
            for state in ("COMPLETED", "FAILED", "CANCELLED"):
                terminal = copy.deepcopy(record)
                terminal.update(state=state, version=5, result={"output_contract_version": 2, "message": "Done"})
                projected = self.task()
                self.project(projected, terminal, response_files_service=object())
                self.assertEqual(len(projected.artifacts), 1)
                self.assertEqual(projected.artifacts[0].SerializeToString(deterministic=True), before)

    def test_real_scoped_immutable_batch_projects_typed_artifact_and_final_message_separately(self):
        from google.protobuf.json_format import MessageToDict
        from core_agent.artifacts import InMemoryArtifactStore
        from core_agent.response_artifacts import ResponseArtifactService
        from core_agent.response_files import ResponseFileService
        from core_agent.workspace import ChatWorkspaces, WorkspaceBinding
        with tempfile.TemporaryDirectory() as root:
            workspaces = ChatWorkspaces(root)
            binding = WorkspaceBinding("company", "owner", "chat")
            workspace = workspaces.workspace(binding)
            (workspace / "report.bin").write_bytes(b"\0immutable\xff")
            files = ResponseFileService(workspaces, InMemoryArtifactStore())
            manifests = ResponseArtifactService(files).prepare(binding, [{"name": "Report", "parts": [
                {"type": "text", "text": "Explicit notes", "media_type": "text/markdown"},
                {"type": "data", "data": {"count": 2}}, {"type": "file", "path": "report.bin"}]}],
                task_id="task", run_id="run", publication_id="call", limit_bytes=25_000_000)
            (workspace / "report.bin").unlink()
            task = self.task()
            self.project(task, self.record(state="COMPLETED", artifacts=manifests,
                result={"output_contract_version": 2, "message": "Delivered"}), response_files_service=files)
            artifact = task.artifacts[0]
            self.assertEqual((artifact.artifact_id, artifact.name), (manifests[0]["artifact_id"], "Report"))
            self.assertEqual(artifact.parts[0].media_type, "text/markdown")
            self.assertEqual(MessageToDict(artifact.parts[1].data), {"count": 2})
            self.assertEqual(artifact.parts[2].raw, b"\0immutable\xff")
            self.assertEqual(MessageToDict(artifact.metadata)["core_agent_artifact"],
                             {"version": 1, "complete": True, "immutable": True})
            self.assertEqual(task.status.message.parts[0].text, "Delivered")
            self.assertNotIn("Explicit notes", task.status.message.parts[0].text)


class PublicOutputSDKTests(unittest.IsolatedAsyncioTestCase):
    def output(self, *, working=False):
        import core_agent.a2a as contract
        cls = getattr(contract, "PublishedOutput", None)
        self.assertIsNotNone(cls, "new public output contract missing")
        message = contract.Message("agent", (Part.text("Today or tomorrow?"),), context_id="chat",
                                   message_id="task:final" if not working else "run:call", task_id="task")
        artifact = Artifact("selected", (Part.text("chosen text"), Part("data", {"field": "value"}),
            Part.file(b"file", filename="report.bin", media_type="application/octet-stream")), 1,
            {"core_agent_artifact": {"version": 1, "complete": True, "immutable": True}})
        return cls(message, (artifact,) if working else (), state=contract.TaskState.WORKING if working else contract.TaskState.COMPLETED)

    def updater(self):
        actions = []
        async def add_artifact(parts, **metadata):
            actions.append(("artifact", parts, metadata))
        async def complete(*, message=None):
            actions.append(("complete", message))
        async def update_status(state, *, message=None, metadata=None, timestamp=None):
            actions.append(("status", state, message, metadata))
        return SimpleNamespace(add_artifact=add_artifact, complete=complete, update_status=update_status), actions

    async def test_final_message_does_not_emit_any_artifact_frame(self):
        updater, actions = self.updater()
        executor = CoreAgentExecutor(None, None, None, max_chunk_size=3)
        await executor._publish_artifact(updater, self.output())
        self.assertEqual([action[0] for action in actions], ["complete"])
        self.assertEqual(actions[0][1].message_id, "task:final")
        self.assertEqual(actions[0][1].parts[0].text, "Today or tomorrow?")

    async def test_committed_working_output_emits_message_and_ordered_parts_without_completion(self):
        updater, actions = self.updater()
        publisher = TaskStreamPublisher(updater, asyncio.get_running_loop())
        self.assertTrue(callable(getattr(publisher, "public_output", None)), "committed output publisher missing")
        await asyncio.to_thread(publisher.public_output, self.output(working=True))
        self.assertNotIn("complete", [action[0] for action in actions])
        messages = [action for action in actions if action[0] == "status"]
        self.assertEqual(messages[0][1], TaskState.TASK_STATE_WORKING)
        self.assertEqual(messages[0][2].message_id, "run:call")
        artifacts = [action for action in actions if action[0] == "artifact"]
        self.assertEqual(artifacts[0][2]["artifact_id"], "selected")
        self.assertEqual([part.WhichOneof("content") for part in artifacts[0][1]], ["text", "data", "raw"])

    async def test_unrepresentable_json_integer_rejects_entire_batch_before_first_frame(self):
        from dataclasses import replace
        updater, actions = self.updater()
        output = self.output(working=True)
        invalid = Artifact("bad", (Part("data", {"integer": 2**53 + 1}, media_type="application/json"),))
        with self.assertRaises(CoreError) as error:
            await CoreAgentExecutor(None, None, None)._publish_artifact(updater,
                replace(output, artifacts=(*output.artifacts, invalid)))
        self.assertEqual(error.exception.code, "ARTIFACT_INTEGRITY_FAILED")
        self.assertEqual(actions, [])
        valid = Artifact("valid", (Part("data", {"integer": 2**54}, media_type="application/json"),))
        self.assertEqual(public_artifact_parts(valid)[0].data.struct_value["integer"], 2**54)

    async def test_repeated_working_publication_and_finalization_do_not_duplicate_frames(self):
        from dataclasses import replace
        import core_agent.a2a as contract
        updater, actions = self.updater()
        publisher = TaskStreamPublisher(updater, asyncio.get_running_loop())
        output = self.output(working=True)
        await asyncio.to_thread(publisher.public_output, output)
        await asyncio.to_thread(publisher.public_output, output)
        final = replace(output, message=self.output().message, state=contract.TaskState.COMPLETED)
        executor = CoreAgentExecutor(None, None, None)
        await executor._publish_artifact(updater, final)
        await executor._publish_artifact(updater, final)
        self.assertEqual([action[0] for action in actions], ["status", "artifact", "complete"])

    async def test_non_success_terminal_output_retains_artifacts_and_finishes_with_its_actual_state(self):
        from dataclasses import replace
        from a2a.server.events.event_queue import EventQueueLegacy
        from a2a.server.tasks import TaskUpdater
        import core_agent.a2a as contract
        for state in (contract.TaskState.FAILED, contract.TaskState.CANCELED, contract.TaskState.REJECTED):
            with self.subTest(state=state):
                queue = EventQueueLegacy()
                updater = TaskUpdater(queue, "task", "chat")
                output = replace(self.output(working=True), state=state)
                await CoreAgentExecutor(None, None, None)._publish_artifact(updater, output)
                artifact, terminal = await queue.dequeue_event(), await queue.dequeue_event()
                self.assertEqual(artifact.artifact.artifact_id, "selected")
                self.assertEqual(terminal.status.state, getattr(TaskState, "TASK_STATE_" + state.value.upper()))
                self.assertTrue(updater._terminal_state_reached)
                await queue.close(immediate=True)

    async def test_native_artifact_frame_equals_canonical_task_projection(self):
        from a2a.server.events.event_queue import EventQueueLegacy
        from a2a.server.tasks import TaskUpdater
        from core_agent.database import _project_public_output
        queue = EventQueueLegacy()
        output = self.output(working=True)
        updater = TaskUpdater(queue, "task", "chat")
        await CoreAgentExecutor(None, None, None)._publish_artifact(updater, output)
        await queue.dequeue_event()  # The public working Message.
        frame = await queue.dequeue_event()
        canonical = PublicOutputProjectionTests().task()
        _project_public_output(canonical, output, version=4)
        self.assertEqual(frame.artifact.SerializeToString(deterministic=True),
                         canonical.artifacts[0].SerializeToString(deterministic=True))
        await queue.close(immediate=True)


class PublicOutputMemoryStoreTests(unittest.TestCase):
    def test_postgres_store_has_the_same_atomic_projection_observer_contract(self):
        self.assertTrue(callable(getattr(PostgresTaskStore, "reconcile_run", None)))

    def test_trusted_run_observer_projects_owned_task_and_ignores_child_without_mapping(self):
        record = PublicOutputProjectionTests().record(messages=[{"message_id": "public", "text": "Ready"}])
        store = ScopedMemoryTaskStore()
        context = AuthenticatedCallContext(tenant="company", user=ScopeUser("owner"))
        store._save_admission(PublicOutputProjectionTests().task(), context)
        observe = getattr(store, "reconcile_run", None)
        self.assertTrue(callable(observe), "atomic memory projection observer missing")
        self.assertTrue(observe(record))
        self.assertFalse(observe(record))
        key = store._impl.owner_resolver(context)
        self.assertEqual(store._impl.tasks[key]["task"].status.message.parts[0].text, "Ready")
        child = {**record, "task_id": "child", "run_id": "child-run"}
        self.assertIsNone(observe(child))


class PublicOutputSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_memory_sdk_save_cannot_replace_committed_output_at_the_same_revision(self):
        from core_agent.workflow import InMemoryWorkflowStore, WorkflowRecord
        workflows = InMemoryWorkflowStore()
        record = PublicOutputProjectionTests().record(version=1,
            messages=[{"message_id": "committed", "text": "Published"}])
        workflows.create(WorkflowRecord(**record, parent_run_id=None, request={"prompt": "work"}))
        store = ScopedMemoryTaskStore(workflow_store=workflows)
        context = AuthenticatedCallContext(tenant="company", user=ScopeUser("owner"))
        store._save_admission(PublicOutputProjectionTests().task(), context)
        store.reconcile_run(workflows.by_task("task", tenant_id="company", owner_id="owner"))
        transient = await store.get("task", context)
        transient.status.message.message_id = "preview"
        transient.status.message.parts[0].text = "uncommitted-canary"
        transient.artifacts.add(artifact_id="uncommitted", parts=[{"text": "uncommitted-canary"}])
        await store.save(transient, context)
        cached = store._impl.tasks[store._impl.owner_resolver(context)]["task"]
        self.assertEqual(cached.status.message.message_id, "committed")
        self.assertNotIn(b"uncommitted-canary", cached.SerializeToString())

    async def test_active_subscription_recovers_committed_output_when_live_publisher_is_silent(self):
        from a2a.server.context import ServerCallContext
        from a2a.types import SubscribeToTaskRequest
        from core_agent.a2a import AgentCard
        from core_agent.a2a_sdk import build_starlette_app
        app = build_starlette_app(agent_card=AgentCard.minimal("publication-test"),
            handler=lambda *_args: Artifact.text("unused"), cancel_handler=lambda *_args: None,
            base_url="http://publication.test", followup_handler=lambda *_args: None,
            resume_handler=lambda *_args: Artifact.text("unused"))
        handler = app.state.a2a_request_handler
        context = ServerCallContext()
        task = PublicOutputProjectionTests().task()
        await handler.task_store.save(task, context)
        class Active:
            async def subscribe(self, *, include_initial_task):
                await asyncio.Event().wait()
                yield None
        handler._active_task_registry._active_tasks[task.id] = Active()
        stream = handler.on_subscribe_to_task(SubscribeToTaskRequest(id=task.id), context)
        try:
            self.assertEqual((await anext(stream)).status.state, TaskState.TASK_STATE_WORKING)
            PublicOutputProjectionTests().project(task, PublicOutputProjectionTests().record(
                messages=[{"message_id": "committed", "text": "Durable intermediate"}]))
            await handler.task_store.save(task, context)
            observed = await asyncio.wait_for(anext(stream), timeout=1)
            self.assertEqual(observed.status.message.message_id, "committed")
        finally:
            await stream.aclose()


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "set TEST_DATABASE_URL for PostgreSQL projection tests")
class PublicOutputPostgresTests(unittest.TestCase):
    def setUp(self):
        self.database = PostgresDatabase(os.environ["TEST_DATABASE_URL"], min_size=0, max_size=2)
        self.database.migrate()
        suffix = uuid.uuid4().hex
        self.record = PublicOutputProjectionTests().record(version=1)
        self.record.update(task_id="public-task-" + suffix, run_id="public-run-" + suffix,
                           context_id="public-chat-" + suffix, tenant_id="public-company-" + suffix)
        self.context = AuthenticatedCallContext(tenant=self.record["tenant_id"], user=ScopeUser("owner"))
        self.store = PostgresTaskStore(self.database)
        task = PublicOutputProjectionTests().task()
        task.id, task.context_id = self.record["task_id"], self.record["context_id"]
        self.store._save(task, self.context)
        with self.database.transaction() as connection:
            connection.execute("""INSERT INTO core_runs
                (run_id,task_id,tenant_id,owner_id,context_id,state,version,request,snapshot,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,0,0)""", tuple(self.record[key] for key in
                ("run_id", "task_id", "tenant_id", "owner_id", "context_id", "state", "version"))
                + (Jsonb({"prompt": "work"}), Jsonb(self.record["snapshot"])))
        self.addCleanup(self.clean)

    def clean(self):
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM core_outbox WHERE tenant_id=%s", (self.record["tenant_id"],))
            connection.execute("DELETE FROM core_a2a_tasks WHERE tenant=%s", (self.record["tenant_id"],))
            connection.execute("DELETE FROM core_runs WHERE run_id=%s", (self.record["run_id"],))
        self.database.close()

    def publish_record(self, connection, *, terminal=False):
        self.record["version"] = 2
        self.record["snapshot"]["public_messages"] = [{"message_id": "public", "text": "Ready"}]
        if terminal:
            self.record.update(state="COMPLETED", result={"output_contract_version": 2, "message": "Finished"})
        connection.execute("UPDATE core_runs SET state=%s,version=2,snapshot=%s,result=%s WHERE run_id=%s",
            (self.record["state"], Jsonb(self.record["snapshot"]), Jsonb(self.record["result"]), self.record["run_id"]))

    def test_atomic_observer_commits_task_and_outbox_once_and_rolls_back_together(self):
        observe = getattr(self.store, "reconcile_run", None)
        self.assertTrue(callable(observe), "atomic PostgreSQL observer missing")
        def notify(task_id, task, *, owner, tenant, connection):
            connection.execute("""INSERT INTO core_outbox
                (id,tenant_id,aggregate_type,aggregate_id,event_type,sequence,payload,available_at,created_at)
                VALUES (%s,%s,'a2a',%s,'public.output',2,%s,0,0)""",
                (uuid.uuid4().hex, tenant, task_id, Jsonb({"message_id": task.status.message.message_id})))
        self.store.enqueue_notification = notify
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.database.transaction() as connection:
                self.publish_record(connection)
                self.assertTrue(observe(self.record, connection))
                raise RuntimeError("rollback")
        with self.database.transaction() as connection:
            self.assertEqual(connection.execute("SELECT version FROM core_runs WHERE run_id=%s",
                (self.record["run_id"],)).fetchone()["version"], 1)
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM core_outbox WHERE tenant_id=%s",
                (self.record["tenant_id"],)).fetchone()["n"], 0)
            task = Task.FromString(bytes(connection.execute("SELECT payload FROM core_a2a_tasks WHERE task_id=%s",
                (self.record["task_id"],)).fetchone()["payload"]))
            self.assertFalse(task.status.HasField("message"))
        with self.database.transaction() as connection:
            self.publish_record(connection)
            self.assertTrue(observe(self.record, connection))
            self.assertFalse(observe(self.record, connection))
            self.assertIsNone(observe({**self.record, "task_id": "unmapped-child"}, connection))
        self.assertEqual(self.store._get(self.record["task_id"], self.context).status.message.message_id, "public")

    def test_get_recovery_and_stale_sdk_save_preserve_canonical_final_message(self):
        stale = self.store._get(self.record["task_id"], self.context)
        with self.database.transaction() as connection:
            self.publish_record(connection, terminal=True)
        final = self.store._get(self.record["task_id"], self.context)
        self.assertEqual(final.status.state, TaskState.TASK_STATE_COMPLETED)
        self.assertEqual(final.status.message.message_id, self.record["task_id"] + ":final")
        self.assertEqual(len(final.artifacts), 0)
        self.store._save(stale, self.context)
        again = self.store._get(self.record["task_id"], self.context)
        self.assertEqual(again.SerializeToString(deterministic=True), final.SerializeToString(deterministic=True))

    def test_get_does_not_wait_for_uncommitted_run_before_observer_takes_task_lock(self):
        run_locked, release = threading.Event(), threading.Event()
        def writer():
            with self.database.transaction() as connection:
                self.publish_record(connection)
                run_locked.set()
                if not release.wait(5):
                    raise AssertionError("reader never released writer")
                self.store.reconcile_run(self.record, connection)
        with ThreadPoolExecutor(max_workers=2) as pool:
            publishing = pool.submit(writer)
            self.assertTrue(run_locked.wait(3))
            reading = pool.submit(self.store._get, self.record["task_id"], self.context)
            try:
                task = reading.result(timeout=2)
                self.assertEqual(task.status.state, TaskState.TASK_STATE_WORKING)
            finally:
                release.set()
            publishing.result(timeout=3)
        self.assertEqual(self.store._get(self.record["task_id"], self.context).status.message.message_id, "public")


if __name__ == "__main__":
    unittest.main()
