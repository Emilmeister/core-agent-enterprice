"""Public peer history stays separate from model material and private review."""

import unittest
import uuid
import os
import copy
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

from core_agent.remote_agents import _task_event
from tests import test_remote_operations as remote_tests
from tests.test_auth import AuthAppTestCase
from core_agent.workflow import WorkflowRecord
from core_agent.database import PostgresDatabase
from core_agent.postgres_tasks import PostgresTaskScheduler
from core_agent.remote_operations import RemoteA2AExecutor
from tests import test_remote_inbound_files as inbound_tests
from core_agent.peer_conversations import _files
from core_agent.errors import CoreError
from tests import test_owner_history as history_tests


class PeerConversationOperationTests(unittest.TestCase):
    def test_request_provenance_rejects_private_metadata_and_unbounded_capture(self):
        from core_agent.peer_conversations import freeze_request_provenance, validate_request_provenance
        identity = {"material_kind": "json", "material_digest": "a" * 64}
        value = {"version": 1, "sources": {"run:1": {"run_id": "run", "sequence": 1, "materials": [identity]}}}
        for source in ({**value["sources"]["run:1"], "payload": "private prompt"},
                       {**value["sources"]["run:1"], "materials": [{**identity, "reasoning": "private"}]},
                       {**value["sources"]["run:1"], "materials": [{"material_kind": "json"}]}):
            with self.assertRaises(CoreError) as error:
                validate_request_provenance({"version": 1, "sources": {"run:1": source}})
            self.assertEqual(error.exception.code, "CHECKPOINT_INVALID")
        with self.assertRaises(CoreError):
            freeze_request_provenance({"context": {"active": [{"provenance": {"sources": {
                str(index): value["sources"]["run:1"] for index in range(4097)}}}]}})

    setUp = remote_tests.RemoteOperationTests.setUp
    connection = remote_tests.RemoteOperationTests.connection
    event = remote_tests.RemoteOperationTests.event
    settle = remote_tests.RemoteOperationTests.settle
    start = remote_tests.RemoteOperationTests.start
    recover = remote_tests.RemoteOperationTests.recover

    def snapshot(self, text="Ready", state="WORKING"):
        return _task_event({"id": "remote-task", "contextId": "remote-context",
            "status": {"state": "TASK_STATE_" + state,
                       "message": {"messageId": "progress", "role": "ROLE_AGENT",
                                   "parts": [{"text": text}]}},
            "history": [
                {"messageId": "echo", "role": "ROLE_USER", "parts": [{"text": "private echoed prompt"}]},
                {"messageId": "reply", "role": "ROLE_AGENT", "parts": [{"text": "First public reply"}],
                 "metadata": {"reasoning": "private thinking", "headers": "private-key"}}]}, direct=True)

    def test_public_history_deduplicates_and_never_enters_progress_or_mailbox(self):
        self.responses = [self.snapshot(), self.snapshot(), self.snapshot("Done", "COMPLETED")]
        task = self.start()
        row = self.scheduler._remote[task.id]
        self.assertIn("conversation", row, "Public peer conversation is not persisted")
        self.recover()
        self.recover()
        messages = row["conversation"]["messages"]
        self.assertEqual([item["text"] for item in messages], ["First public reply", "Ready", "Done"])
        public = str(row["conversation"])
        for private in ("private thinking", "private echoed prompt", "private-key", "metadata"):
            self.assertNotIn(private, public)
        self.assertEqual(task.result["text"], "Done")
        self.assertNotIn("messages", str(self.scheduler.mailbox(self.owner, self.tenant).poll()))

    def test_visible_interest_caps_polling_then_restores_original_age_and_deadline(self):
        self.responses = [self.event(), self.event(), self.event()]
        task = self.start(timeout_seconds=5000, poll_interval_seconds=300)
        self.now += 800
        interest = self.scheduler.observe_remote(task.id, owner_id=self.owner, tenant_id=self.tenant, visible=True)
        self.assertEqual(interest, self.now + 45)
        self.recover(advance=0)
        row = self.scheduler._remote[task.id]
        self.assertEqual(row["checkpoint"]["next_poll_at"], self.now + 15)
        self.assertEqual(row["checkpoint"]["deadline"], 6000)
        self.assertEqual(self.scheduler.observe_remote(task.id, owner_id=self.owner, tenant_id=self.tenant, visible=False), interest)
        self.recover(advance=46)
        self.assertEqual(row["checkpoint"]["next_poll_at"], self.now + 300)
        self.assertEqual([call[0] for call in self.calls], ["Send", "Get", "Get"])

    def test_legacy_snapshot_without_history_uses_stable_public_content_identity(self):
        self.responses = [self.event(text="public progress"), self.event(text="public progress")]
        task = self.start()
        self.recover()
        row = self.scheduler._remote[task.id]
        self.assertIn("conversation", row)
        self.assertEqual([m["text"] for m in row["conversation"]["messages"]], ["public progress"])
        self.assertFalse(row["conversation"]["history_truncated"])

    def test_history_private_reasoning_parts_are_ignored(self):
        snapshot = self.snapshot()
        self.assertTrue(hasattr(snapshot, "messages"), "Validated public history is discarded")
        self.assertEqual([m["text"] for m in snapshot.messages], ["First public reply", "Ready"])
        private = replace(snapshot, text="private-key")
        self.responses = [private]
        task = self.start()
        self.assertNotIn("private-key", str(self.scheduler._remote[task.id]["conversation"]))

    def test_private_parts_do_not_reappear_as_legacy_snapshot_text(self):
        private = _task_event({"id": "remote-task", "contextId": "remote-context",
            "status": {"state": "TASK_STATE_WORKING", "message": {"messageId": "hidden",
                "role": "ROLE_AGENT", "parts": [{"text": "private reasoning",
                    "metadata": {"kind": "reasoning"}}]}}}, direct=True)
        self.responses = [private]
        task = self.start()
        self.assertEqual(self.scheduler._remote[task.id]["conversation"]["messages"], [])

    def test_bounded_public_history_marks_real_truncation(self):
        history = [{"messageId": str(i), "role": "ROLE_AGENT", "parts": [{"text": str(i)}]}
                   for i in range(205)]
        event = _task_event({"id": "remote-task", "contextId": "remote-context",
            "status": {"state": "TASK_STATE_WORKING"}, "history": history}, direct=True)
        self.responses = [event]
        task = self.start()
        conversation = self.scheduler._remote[task.id]["conversation"]
        self.assertTrue(conversation["history_truncated"])
        self.assertEqual(len(conversation["messages"]), 200)

    def test_private_artifact_and_human_wait_message_are_not_public_history(self):
        event = _task_event({"id": "remote-task", "contextId": "remote-context",
            "status": {"state": "TASK_STATE_INPUT_REQUIRED", "message": {"messageId": "review",
                "role": "ROLE_AGENT", "parts": [{"text": "private owner review"}]}},
            "history": [{"messageId": "review", "role": "ROLE_AGENT", "parts": [{"text": "private owner review"}]}],
            "artifacts": [{"artifactId": "private", "parts": [{"text": "private artifact"}],
                           "metadata": {"visibility": "private"}}]}, direct=True)
        self.responses = [event]
        task = self.start()
        self.assertEqual(self.scheduler._remote[task.id]["conversation"]["messages"], [])

    def test_canonical_reasoning_and_replay_metadata_preserve_only_public_parts(self):
        event = _task_event({"id": "remote-task", "contextId": "remote-context",
            "status": {"state": "TASK_STATE_WORKING", "message": {"messageId": "mixed",
                "role": "ROLE_AGENT", "parts": [
                    {"text": "private canonical reasoning", "metadata": {"adk_thought": True}},
                    {"text": "private replay", "metadata": {"kind": "replay"}}, {"text": "Public response"}]}},
            "artifacts": [{"artifactId": "private-replay", "parts": [{"text": "private artifact replay"}],
                           "metadata": {"kind": "provider_replay"}}],
            "history": [{"messageId": "private-thinking", "role": "ROLE_AGENT",
                         "parts": [{"text": "private container thinking"}], "metadata": {"thinking": True}}]}, direct=True)
        self.responses = [event]
        task = self.start()
        self.assertEqual([m["text"] for m in self.scheduler._remote[task.id]["conversation"]["messages"]],
                         ["Public response"])


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "TEST_DATABASE_URL required")
class PostgresPeerConversationTests(unittest.TestCase):
    setUp = remote_tests.PostgresRemoteOutboundTests.setUp
    cleanup_rows = remote_tests.PostgresRemoteOutboundTests.cleanup_rows
    start = remote_tests.PostgresRemoteOutboundTests.start
    settle = remote_tests.RemoteOperationTests.settle
    connection = remote_tests.RemoteOperationTests.connection
    event = remote_tests.RemoteOperationTests.event
    snapshot = PeerConversationOperationTests.snapshot

    def test_public_history_and_observation_survive_restart_without_new_send(self):
        self.contract.update(timeout_seconds=5000, poll_interval_seconds=300)
        self.responses = [self.snapshot(), self.snapshot("Done", "COMPLETED")]
        task = self.start()
        expires = self.scheduler.observe_remote(task.id, owner_id=self.owner, tenant_id=self.tenant, visible=True)
        with self.database.transaction() as conn:
            saved = conn.execute("SELECT * FROM core_background_tasks WHERE id=%s", (task.id,)).fetchone()
            self.assertEqual(saved["remote_observed_until"], expires)
            self.assertEqual(len(saved["remote_conversation"]["messages"]), 2)
            conn.execute("UPDATE core_background_tasks SET checkpoint=jsonb_set(checkpoint,'{next_poll_at}','1') WHERE id=%s", (task.id,))
        self.scheduler.close()
        self.registry.delete(self.tenant, self.peer["id"], expected_revision=1, actor_id="owner")
        database = PostgresDatabase(os.environ["TEST_DATABASE_URL"], min_size=0, max_size=1)
        self.addCleanup(database.close)
        self.scheduler = PostgresTaskScheduler(database)
        self.addCleanup(self.scheduler.close)
        self.scheduler.register("remote_a2a", RemoteA2AExecutor(self.scheduler, self.registry))
        self.scheduler.recover(tenant_id=self.tenant)
        self.settle()
        with database.transaction() as conn:
            updated = conn.execute("SELECT * FROM core_background_tasks WHERE id=%s", (task.id,)).fetchone()
        self.assertEqual(updated["state"], "completed")
        self.assertEqual(updated["checkpoint"]["deadline"], saved["checkpoint"]["deadline"])
        self.assertEqual([m["text"] for m in updated["remote_conversation"]["messages"]],
                         ["First public reply", "Ready", "Done"])
        self.assertEqual([call[0] for call in self.calls], ["Send", "Get"])


class PeerConversationFileTests(unittest.TestCase):
    setUp = inbound_tests.MemoryRemoteInboundTests.setUp
    setup_remote = inbound_tests.RemoteInboundContract.setup_remote
    connection = inbound_tests.RemoteInboundContract.connection
    contract = inbound_tests.RemoteInboundContract.contract
    event = inbound_tests.RemoteInboundContract.event
    settle = inbound_tests.RemoteInboundContract.settle
    start = inbound_tests.RemoteInboundContract.start

    def test_files_require_full_admission_publication_scope_and_whole_batch_integrity(self):
        self.responses = [self.event()]
        task = self.start()
        agent = SimpleNamespace(chat_file_service=self.service)
        row = {"result": task.result}
        self.assertEqual(_files(agent, self.binding, row, self.source, None), [])
        batch_id = task.result["file_batch_id"]
        self.service.record_decision(batch_id, self.binding, decision_ref="trusted-test-allow", allow=True)
        self.service.publish(batch_id, self.binding)
        entries = _files(agent, self.binding, row, self.source, None)
        self.assertEqual([entry["actual_name"] for entry in entries], ["binary.bin", "empty.txt"])
        self.assertTrue(all(set(entry) == {"index", "actual_name", "relative_path", "size_bytes", "sha256"}
                            for entry in entries))
        self.assertEqual(_files(agent, replace(self.binding, context_id="foreign"), row, self.source, None), [])
        path = self.workspaces.workspace(self.binding) / entries[0]["relative_path"]
        path.write_bytes(b"tampered")
        with self.assertRaises(CoreError) as error:
            _files(agent, self.binding, row, self.source, None)
        self.assertEqual(error.exception.code, "ARTIFACT_INTEGRITY_FAILED")

    def test_early_files_are_never_projected_or_admitted(self):
        self.responses = [self.event("WORKING")]
        task = self.start()
        self.assertEqual(_files(SimpleNamespace(chat_file_service=self.service), self.binding,
                                {"result": task.result}, self.source, None), [])
        self.assertFalse(self.store.rows)


class PeerConversationAPITests(unittest.IsolatedAsyncioTestCase):
    use_postgres = False
    automatic_tools = False
    ui_client_id = ""
    push_encryption_key = ""
    durable_blobs = False
    asyncSetUp = AuthAppTestCase.asyncSetUp
    introspect = AuthAppTestCase.introspect
    headers = staticmethod(AuthAppTestCase.headers)
    submit = AuthAppTestCase.submit
    review_material = history_tests.OwnerHistoryTests.review_material
    digest = staticmethod(history_tests.OwnerHistoryTests.digest)

    def replace_snapshot(self, snapshot):
        if self.use_postgres:
            from psycopg.types.json import Jsonb
            with self.app.state.database.transaction() as connection:
                connection.execute("UPDATE core_runs SET snapshot=%s WHERE run_id=%s AND tenant_id=%s",
                    (Jsonb(snapshot), self.source.run_id, self.tenant))
        else:
            self.agent.workflow_store._records[self.source.run_id] = replace(self.source, snapshot=snapshot)
        self.source = replace(self.source, snapshot=snapshot)

    async def test_frozen_request_dependencies_ignore_later_unrelated_denials(self):
        from core_agent.peer_conversations import freeze_request_provenance
        await self.prepare()
        service, refs, contract, checkpoint = self.prepare_request_files()
        identity = {"material_kind": "json", "material_digest": self.digest({"earlier": "causal input"})}
        snapshot = copy.deepcopy(self.source.snapshot)
        provenance = {"version": 1, "sources": {self.source.run_id + ":2": {
            "run_id": self.source.run_id, "sequence": 2, "materials": [identity]}}}
        snapshot["context"]["active"] = [{"kind": "tool_result", "provenance": provenance}]
        snapshot["remote_calls"]["1:peer-send"]["request_provenance"] = freeze_request_provenance(snapshot)
        snapshot["context"]["transcript"].append({"kind": "tool_result", "content": json.dumps({
            "tool_call_id": "unrelated-later", "tool_name": "example", "output": {"later": "unrelated"}})})
        self.replace_snapshot(snapshot)
        self.review_material(self.source, {"material_kind": "json", "material_digest": self.digest({"later": "unrelated"})})
        path = self.path + "/" + self.operation
        detail = await self.http.get(path, headers=self.headers("owner-a"))
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["material_status"], "available")
        self.assertEqual(len(detail.json()["outgoing_files"]), 2)
        self.review_material(self.source, identity)
        detail = await self.http.get(path, headers=self.headers("owner-a"))
        self.assertEqual(detail.json()["material_status"], "rejected")
        self.assertEqual((detail.json()["messages"], detail.json()["outgoing_files"]), ([], []))

    async def test_legacy_request_cutoff_excludes_later_unrelated_results(self):
        await self.prepare()
        identity = {"material_kind": "json", "material_digest": self.digest("earlier causal tool input")}
        snapshot = copy.deepcopy(self.source.snapshot)
        snapshot["context"]["transcript"].insert(-1, {"kind": "tool_result", "content": json.dumps({
            "tool_call_id": "earlier", "tool_name": "example", "output": "earlier causal tool input"}),
            "provenance": {"version": 1, "sources": {self.source.run_id + ":2": {
                "run_id": self.source.run_id, "sequence": 2, "materials": [identity]}}}})
        snapshot["context"]["transcript"].append({"kind": "tool_result", "content": json.dumps({
            "tool_call_id": "later", "tool_name": "example", "output": "later unrelated result"})})
        self.replace_snapshot(snapshot)
        self.review_material(self.source, {"material_kind": "json", "material_digest": self.digest("later unrelated result")})
        path = self.path + "/" + self.operation
        detail = await self.http.get(path, headers=self.headers("owner-a"))
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["material_status"], "available")
        self.review_material(self.source, identity)
        detail = await self.http.get(path, headers=self.headers("owner-a"))
        self.assertEqual(detail.json()["material_status"], "rejected")
        self.assertEqual(detail.json()["messages"], [])

    async def test_legacy_cutoff_keeps_imported_prior_root_dependencies(self):
        context = uuid.uuid4().hex
        await self.submit("owner-a", uuid.uuid4().hex, context)
        await self.prepare(context_id=context)
        snapshot = copy.deepcopy(self.source.snapshot)
        self.assertTrue(snapshot["context_import"]["sources"])
        self.assertTrue(snapshot["previous_root_run_id"])
        snapshot["context"]["transcript"] = [snapshot["context"]["transcript"][0],
                                              snapshot["context"]["transcript"][-1]]
        self.replace_snapshot(snapshot)
        self.review_material(self.source, {"material_kind": "json", "material_digest": self.digest("verified")})
        detail = await self.http.get(self.path + "/" + self.operation, headers=self.headers("owner-a"))
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["material_status"], "rejected")
        self.assertEqual(detail.json()["messages"], [])

    async def test_later_source_denial_blocks_derived_requests_files_and_download(self):
        await self.prepare()
        service, refs, contract, checkpoint = self.prepare_request_files()
        review = self.review_material(self.source, {"material_kind": "json",
            "material_digest": self.digest(self.source.request["prompt"])})
        before = self.agent.workflow_store.get(self.source.run_id, tenant_id=self.tenant, owner_id=self.source.owner_id)
        path = self.path + "/" + self.operation
        detail = await self.http.get(path, headers=self.headers("owner-a"))
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json().get("material_status"), "rejected")
        self.assertEqual(detail.json()["messages"], [])
        self.assertEqual(detail.json()["outgoing_files"], [])
        self.assertEqual(detail.json()["files"], [])
        summary = (await self.http.get(self.path, headers=self.headers("owner-a"))).json()["conversations"][0]
        self.assertEqual((summary["last_message"], summary["message_count"], summary["files_available"]), ("", 0, False))
        denied = await self.http.get(path + "/outgoing-files/" + refs[0]["file_id"], headers=self.headers("owner-a"))
        self.assertEqual(denied.status_code, 404, denied.text)
        self.assertNotIn(review["wait_id"], detail.text)
        self.assertEqual(self.agent.workflow_store.get(self.source.run_id, tenant_id=self.tenant, owner_id=self.source.owner_id), before)

    async def test_later_selected_file_text_alias_blocks_whole_outgoing_payload(self):
        await self.prepare()
        service, refs, contract, checkpoint = self.prepare_request_files()
        self.review_material(self.source, {"material_kind": "file_sha256",
            "material_digest": hashlib.sha256(b"different original bytes").hexdigest(),
            "text_digest": self.digest("frozen request bytes")}, source_kind="file_attachment")
        path = self.path + "/" + self.operation
        detail = await self.http.get(path, headers=self.headers("owner-a"))
        self.assertEqual(detail.json().get("material_status"), "rejected")
        self.assertEqual(detail.json()["outgoing_files"], [])
        self.assertEqual(detail.json()["messages"], [])
        denied = await self.http.get(path + "/outgoing-files/" + refs[1]["file_id"], headers=self.headers("owner-a"))
        self.assertEqual(denied.status_code, 404, denied.text)

    async def test_exact_peer_text_denial_preserves_independent_outgoing_request(self):
        await self.prepare()
        service, refs, contract, checkpoint = self.prepare_request_files()
        self.review_material(self.source, {"material_kind": "json", "material_digest": self.digest("Public reply")})
        path = self.path + "/" + self.operation
        detail = await self.http.get(path, headers=self.headers("owner-a"))
        self.assertEqual(detail.json().get("material_status"), "rejected")
        self.assertEqual([m["text"] for m in detail.json()["messages"]], ["Public outgoing request"])
        self.assertEqual(detail.json()["outgoing_files"], list(service.receipts(refs)))
        allowed = await self.http.get(path + "/outgoing-files/" + refs[0]["file_id"], headers=self.headers("owner-a"))
        self.assertEqual(allowed.status_code, 200, allowed.text)
        summary = (await self.http.get(self.path, headers=self.headers("owner-a"))).json()["conversations"][0]
        self.assertEqual((summary["last_message"], summary["message_count"]), ("", 1))

    async def test_current_remote_result_review_is_causal_and_pending_alias_is_visible(self):
        import json
        await self.prepare()
        snapshot = copy.deepcopy(self.source.snapshot)
        payload = {"task_id": self.operation, "state": "completed", "result": {"text": "Different canonical final result"}}
        snapshot["context"]["transcript"].append({"kind": "tool_result", "content": json.dumps({
            "tool_call_id": "peer-wait", "tool_name": "core_task_wait", "status": "succeeded", "output": payload})})
        identity = {"material_kind": "json", "material_digest": self.digest(payload["result"])}
        review = self.review_material(self.source, identity, reason=None)
        snapshot.setdefault("context_materials", {})["result:peer-wait"] = {"identity": {**identity, "review_id": review["review_id"]}}
        self.replace_snapshot(snapshot)
        path = self.path + "/" + self.operation
        detail = await self.http.get(path, headers=self.headers("owner-a"))
        self.assertEqual(detail.json().get("material_status"), "pending_guardrail")
        self.assertEqual([m["direction"] for m in detail.json()["messages"]], ["outgoing"])
        self.agent.workflow_store.resolve_wait(review["wait_id"], tenant_id=self.tenant, outcome={"reason": "timeout"})
        detail = await self.http.get(path, headers=self.headers("owner-a"))
        self.assertEqual(detail.json().get("material_status"), "timed_out")
        self.assertEqual([m["direction"] for m in detail.json()["messages"]], ["outgoing"])
        # An unresolved cross-source alias has no negative outcome yet.
        self.review_material(self.source, {"material_kind": "json", "material_digest": self.digest("Public outgoing request")}, reason=None)
        detail = await self.http.get(path, headers=self.headers("owner-a"))
        self.assertEqual([m["direction"] for m in detail.json()["messages"]], ["outgoing"])

    def replace_remote(self, *, contract=None, checkpoint=None):
        if self.use_postgres:
            from psycopg.types.json import Jsonb
            with self.app.state.database.transaction() as connection:
                for column, value in (("contract", contract), ("checkpoint", checkpoint)):
                    if value is not None:
                        connection.execute("UPDATE core_background_tasks SET " + column + "=%s WHERE id=%s",
                                           (Jsonb(value), self.operation))
        else:
            for key, value in (("contract", contract), ("checkpoint", checkpoint)):
                if value is not None:
                    self.scheduler._remote[self.operation][key] = value
        if contract is not None and "remote_calls" in self.source.snapshot:
            snapshot = copy.deepcopy(self.source.snapshot)
            entry = {"version": 1, "contract": contract}
            if contract["version"] == 2:
                entry["arguments_digest"] = "0" * 64
            snapshot["remote_calls"]["1:peer-send"] = entry
            self.replace_snapshot(snapshot)

    def prepare_request_files(self):
        from core_agent.workspace import WorkspaceBinding
        binding = WorkspaceBinding(self.tenant, self.source.owner_id, self.source.context_id)
        service = self.agent.response_files_service
        directory = service.workspaces.workspace(binding)
        (directory / "selected.txt").write_bytes(b"frozen request bytes")
        (directory / "second.txt").write_bytes(b"second selected file")
        (directory / "unrelated.txt").write_bytes(b"private unrelated file")
        refs = service.prepare(binding, ["selected.txt", "second.txt"],
            task_id=self.source.task_id, run_id=self.source.run_id, limit_bytes=64)
        if self.use_postgres:
            with self.app.state.database.transaction() as connection:
                row = connection.execute("SELECT contract,checkpoint FROM core_background_tasks WHERE id=%s", (self.operation,)).fetchone()
        else:
            row = self.scheduler._remote[self.operation]
        contract = {**row["contract"], "version": 2,
            "caller_scope": {key: getattr(self.source, key) for key in ("owner_id", "context_id", "task_id", "run_id")},
            "attachment_limit_bytes": 64, "outgoing_files": list(refs)}
        self.replace_remote(contract=contract)
        (directory / "selected.txt").unlink()
        (directory / "second.txt").write_bytes(b"changed after capture")
        return service, refs, contract, row["checkpoint"]

    async def prepare(self, context_id=None):
        task = await self.submit("owner-a", uuid.uuid4().hex, context_id or uuid.uuid4().hex)
        self.agent = self.app.state.core_agent
        self.tenant = self.app.state.authenticator.settings.tenant
        source = self.agent.workflow_store.by_task(task["id"], tenant_id=self.tenant, owner_id="company-owners")
        self.source = source
        self.scheduler = self.agent.task_scheduler
        contract = {"version": 1, "tenant_id": self.tenant, "owner_id": source.run_id,
            "peer_id": "peer", "peer_revision": 1, "peer_name": "delivery", "url": "https://peer.example/a2a",
            "binding": "HTTP+JSON", "message_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"remote-message:{source.run_id}:1:peer-send")), "task": "Public outgoing request",
            "timeout_seconds": 86400, "poll_interval_seconds": 300}
        def observe(claim, cancel):
            current = self.scheduler.read_remote_claim(claim)
            from core_agent.peer_conversations import public_identity
            self.scheduler.commit_remote_claim(claim, expected_revision=current["revision"],
                checkpoint={**current["checkpoint"], "send_started": True,
                    "remote_task_id": "remote-task", "remote_context_id": "remote-context",
                    "next_poll_at": current["now"] + 300},
                progress={"agent_name": "delivery", "remote_state": "TASK_STATE_WORKING"},
                conversation_observation={"messages": [{"id": public_identity("reply", "Public reply"),
                    "text": "Public reply"}], "history_truncated": False})
        self.scheduler._handlers["remote_a2a"] = observe
        operation = self.scheduler.start_remote(contract, owner_id=source.run_id, tenant_id=self.tenant,
            task_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"remote:{source.run_id}:1:peer-send")))
        for thread in tuple(self.scheduler._threads):
            thread.join(3)
        self.operation = operation.id
        self.path = "/api/chats/" + source.context_id + "/peer-conversations"
        snapshot = copy.deepcopy(source.snapshot)
        snapshot["remote_calls"] = {"1:peer-send": {"version": 1, "contract": contract}}
        snapshot["remote_admission"] = {"version": 1, "source_id": "peer-send", "attempt": 1, "task_id": operation.id}
        snapshot["context"]["transcript"].append({"kind": "assistant_tool_calls", "content": json.dumps({
            "tool_calls": [{"id": "peer-send", "function": {"name": "core_agent_send_message", "arguments": {"task": contract["task"]}}}]})})
        self.replace_snapshot(snapshot)

    async def test_owner_detail_is_passive_public_and_bound_to_admitted_chat(self):
        await self.prepare()
        before = len(self.model.calls)
        response = await self.http.get(self.path, headers=self.headers("owner-b"))
        self.assertEqual(response.status_code, 200, response.text)
        summary = response.json()["conversations"][0]
        self.assertEqual((summary["operation_id"], summary["root_task_id"]), (self.operation, self.source.task_id))
        detail = await self.http.get(self.path + "/" + self.operation, headers=self.headers("owner-b"))
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual([m["text"] for m in detail.json()["messages"]], ["Public outgoing request", "Public reply"])
        self.assertEqual(detail.json()["files"], [])
        self.assertEqual(detail.headers["cache-control"], "no-store")
        self.assertNotIn(self.source.request["prompt"], detail.text)
        self.assertEqual(len(self.model.calls), before)
        for token, status in (("external-a", 403), (None, 401)):
            denied = await self.http.get(self.path, headers=self.headers(token) if token else {})
            self.assertEqual(denied.status_code, status)
        await self.submit("owner-a", uuid.uuid4().hex, "another-chat")
        foreign = await self.http.get("/api/chats/another-chat/peer-conversations/" + self.operation,
                                      headers=self.headers("owner-a"))
        self.assertEqual(foreign.status_code, 404, foreign.text)

    async def test_selected_outgoing_files_use_frozen_scoped_snapshot_downloads(self):
        await self.prepare()
        service, refs, contract, checkpoint = self.prepare_request_files()
        detail_path = self.path + "/" + self.operation
        detail = await self.http.get(detail_path, headers=self.headers("owner-b"))
        self.assertEqual(detail.status_code, 200, detail.text)
        value = detail.json()
        self.assertEqual(value.get("outgoing_files"), list(service.receipts(refs)))
        self.assertEqual(value.get("outgoing_files_status"), "available")
        self.assertEqual(value.get("request_delivery"), "confirmed")
        self.assertEqual(value["files"], [])
        self.assertNotIn("blob_id", detail.text)
        self.assertNotIn("unrelated", detail.text)
        download = detail_path + "/outgoing-files/" + refs[0]["file_id"]
        response = await self.http.get(download, headers=self.headers("owner-b"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, b"frozen request bytes")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("attachment;", response.headers["content-disposition"])
        self.assertEqual((await self.http.get(download, headers=self.headers("external-a"))).status_code, 403)
        self.assertEqual((await self.http.get(detail_path + "/outgoing-files/" + uuid.uuid4().hex,
                                             headers=self.headers("owner-a"))).status_code, 404)
        await self.submit("owner-a", uuid.uuid4().hex, "another-file-chat")
        foreign = "/api/chats/another-file-chat/peer-conversations/" + self.operation + "/outgoing-files/" + refs[0]["file_id"]
        self.assertEqual((await self.http.get(foreign, headers=self.headers("owner-a"))).status_code, 404)
        self.replace_remote(contract={**contract, "caller_scope": {**contract["caller_scope"], "context_id": "another-file-chat"},
            "outgoing_files": [{**ref, "context_id": "another-file-chat"} for ref in refs]})
        denied = await self.http.get(detail_path, headers=self.headers("owner-a"))
        self.assertEqual(denied.status_code, 404, denied.text)

    async def test_outgoing_missing_and_tampered_batch_remains_unavailable(self):
        await self.prepare()
        service, refs, contract, checkpoint = self.prepare_request_files()
        if self.use_postgres:
            service.artifact_store._blob("sha256:" + refs[1]["sha256"]).write_bytes(b"tampered")
        else:
            service.artifact_store._content[(self.tenant, refs[1]["blob_id"])] = b"tampered"
        path = self.path + "/" + self.operation
        detail = await self.http.get(path, headers=self.headers("owner-a"))
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json().get("outgoing_files_status"), "unavailable")
        self.assertEqual(detail.json().get("outgoing_files"), list(service.receipts(refs)))
        # Even the untampered first file cannot bypass whole-batch validation.
        denied = await self.http.get(path + "/outgoing-files/" + refs[0]["file_id"], headers=self.headers("owner-a"))
        self.assertEqual(denied.status_code, 409, denied.text)
        service.artifact_store.delete(self.tenant, refs[1]["blob_id"])
        detail = await self.http.get(path, headers=self.headers("owner-a"))
        self.assertEqual(detail.json().get("outgoing_files_status"), "unavailable")

    async def test_send_marker_does_not_claim_request_or_files_delivered(self):
        await self.prepare()
        service, refs, contract, checkpoint = self.prepare_request_files()
        path = self.path + "/" + self.operation
        self.replace_remote(checkpoint={**checkpoint, "remote_task_id": None, "remote_context_id": None, "next_poll_at": None})
        detail = await self.http.get(path, headers=self.headers("owner-a"))
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json().get("request_delivery"), "unconfirmed")
        self.assertEqual(detail.json().get("outgoing_files"), list(service.receipts(refs)))
        self.replace_remote(checkpoint={**checkpoint, "send_started": False})
        detail = await self.http.get(path, headers=self.headers("owner-a"))
        self.assertEqual(detail.json().get("request_delivery"), "not_sent")
        self.assertEqual(detail.json().get("outgoing_files"), [])
        self.assertEqual(detail.json().get("outgoing_files_status"), "none")
        denied = await self.http.get(path + "/outgoing-files/" + refs[0]["file_id"], headers=self.headers("owner-a"))
        self.assertEqual(denied.status_code, 404)

    async def test_observation_requires_owner_and_visible_boolean(self):
        await self.prepare()
        path = self.path + "/" + self.operation + "/observation"
        response = await self.http.post(path, headers=self.headers("owner-a"), json={"visible": True})
        self.assertEqual(response.status_code, 200, response.text)
        expires = response.json()["expires_at"]
        other = await self.http.post(path, headers=self.headers("owner-b"), json={"visible": False})
        self.assertEqual(other.json()["expires_at"], expires)
        for body in ({"visible": 1}, {"visible": True, "timeout": 100}, {}):
            invalid = await self.http.post(path, headers=self.headers("owner-a"), json=body)
            self.assertEqual(invalid.status_code, 400)
        denied = await self.http.post(path, headers=self.headers("external-a"), json={"visible": True})
        self.assertEqual(denied.status_code, 403)

    async def test_legacy_empty_conversation_never_reconstructs_missing_peer_history(self):
        await self.prepare()
        if self.use_postgres:
            with self.app.state.database.transaction() as connection:
                connection.execute("UPDATE core_background_tasks SET remote_conversation=NULL WHERE id=%s", (self.operation,))
        else:
            self.scheduler._remote[self.operation]["conversation"] = None
        response = await self.http.get(self.path + "/" + self.operation, headers=self.headers("owner-a"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([m["direction"] for m in response.json()["messages"]], ["outgoing"])
        self.assertIsNone(response.json()["last_checked_at"])

    async def test_failure_summary_distinguishes_unknown_outcome_without_raw_errors(self):
        await self.prepare()
        if self.use_postgres:
            from psycopg.types.json import Jsonb
            with self.app.state.database.transaction() as connection:
                connection.execute("UPDATE core_background_tasks SET state='failed',error_code='SIDE_EFFECT_UNKNOWN',result=%s WHERE id=%s",
                                   (Jsonb({"remote_outcome": "unknown"}), self.operation))
        else:
            with self.scheduler._lock:
                row = self.scheduler._remote[self.operation]
                self.scheduler._finish_remote_locked(row, self.scheduler._tasks[self.operation],
                    ("failed", {"remote_outcome": "unknown"}, "SIDE_EFFECT_UNKNOWN"))
        response = await self.http.get(self.path, headers=self.headers("owner-a"))
        self.assertEqual(response.status_code, 200, response.text)
        summary = response.json()["conversations"][0]
        self.assertEqual(summary.get("error_code"), "SIDE_EFFECT_UNKNOWN")
        self.assertIs(summary.get("outcome_unknown"), True)
        if self.use_postgres:
            with self.app.state.database.transaction() as connection:
                connection.execute("UPDATE core_background_tasks SET error_code='Bearer private-error-secret' WHERE id=%s", (self.operation,))
        else:
            self.scheduler._tasks[self.operation].error = CoreError("Bearer private-error-secret")
        detail = await self.http.get(self.path + "/" + self.operation, headers=self.headers("owner-a"))
        self.assertIsNone(detail.json()["error_code"])
        self.assertNotIn("private-error-secret", detail.text)

    async def test_listing_paginates_and_child_lineage_cannot_cross_chat(self):
        await self.prepare()
        source = self.source
        child = WorkflowRecord(uuid.uuid4().hex, uuid.uuid4().hex,
            source.context_id, source.tenant_id, source.owner_id, source.run_id, "CREATED", 1,
            {"prompt": "private child prompt"}, {})
        self.agent.workflow_store._records[child.run_id] = child
        contract = {**self.scheduler._remote[self.operation]["contract"], "owner_id": child.run_id,
                    "message_id": uuid.uuid4().hex}
        newer = self.scheduler.start_remote(contract, owner_id=child.run_id, tenant_id=self.tenant,
                                           task_id=uuid.uuid4().hex)
        for thread in tuple(self.scheduler._threads):
            thread.join(3)
        first = await self.http.get(self.path + "?limit=1", headers=self.headers("owner-a"))
        self.assertEqual(first.status_code, 200, first.text)
        page = first.json()
        self.assertEqual(page["conversations"][0]["operation_id"], newer.id)
        self.assertTrue(page["next_cursor"])
        second = await self.http.get(self.path, params={"limit": 1, "cursor": page["next_cursor"]},
                                     headers=self.headers("owner-a"))
        self.assertEqual(second.json()["conversations"][0]["operation_id"], self.operation)
        self.assertIsNone(second.json()["next_cursor"])
        original = self.agent.workflow_store._records[child.run_id]
        self.agent.workflow_store._records[child.run_id] = replace(original, parent_run_id="missing-parent")
        hidden = await self.http.get(self.path + "/" + newer.id, headers=self.headers("owner-a"))
        self.assertEqual(hidden.status_code, 404, hidden.text)

    async def test_invalid_recent_lineage_does_not_hide_older_authorized_conversation(self):
        await self.prepare()
        for index in range(3):
            child = WorkflowRecord(uuid.uuid4().hex, uuid.uuid4().hex,
                self.source.context_id, self.tenant, self.source.owner_id, self.source.run_id,
                "CREATED", 1, {"prompt": "private child"}, {})
            self.agent.workflow_store._records[child.run_id] = child
            contract = {**self.scheduler._remote[self.operation]["contract"], "owner_id": child.run_id,
                        "message_id": uuid.uuid4().hex}
            self.scheduler.start_remote(contract, owner_id=child.run_id, tenant_id=self.tenant,
                                        task_id=uuid.uuid4().hex)
            for thread in tuple(self.scheduler._threads):
                thread.join(3)
            self.agent.workflow_store._records[child.run_id] = replace(child, parent_run_id="missing-parent")
        response = await self.http.get(self.path + "?limit=1", headers=self.headers("owner-a"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([r["operation_id"] for r in response.json()["conversations"]], [self.operation])

    async def test_bounded_scan_cursor_continues_past_many_invalid_rows(self):
        await self.prepare()
        bad = WorkflowRecord(uuid.uuid4().hex, uuid.uuid4().hex,
            self.source.context_id, self.tenant, self.source.owner_id, "missing-parent",
            "CREATED", 1, {"prompt": "private child"}, {})
        self.agent.workflow_store._records[bad.run_id] = bad
        original = self.scheduler._remote[self.operation]
        original_task = self.scheduler._tasks[self.operation]
        for index in range(350):
            identifier = uuid.uuid4().hex
            self.scheduler._remote[identifier] = {**original, "contract": {**original["contract"], "owner_id": bad.run_id},
                                                   "created_at": original["created_at"] + index + 1}
            self.scheduler._tasks[identifier] = replace(original_task, id=identifier, owner_id=bad.run_id)
        first = await self.http.get(self.path + "?limit=1", headers=self.headers("owner-a"))
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["conversations"], [])
        self.assertTrue(first.json()["next_cursor"])
        second = await self.http.get(self.path, params={"limit": 1, "cursor": first.json()["next_cursor"]},
                                     headers=self.headers("owner-a"))
        self.assertEqual([r["operation_id"] for r in second.json()["conversations"]], [self.operation])


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "TEST_DATABASE_URL required")
class PostgresPeerConversationAPITests(unittest.IsolatedAsyncioTestCase):
    use_postgres = True
    automatic_tools = False
    ui_client_id = ""
    push_encryption_key = ""
    durable_blobs = True
    asyncSetUp = AuthAppTestCase.asyncSetUp
    introspect = AuthAppTestCase.introspect
    headers = staticmethod(AuthAppTestCase.headers)
    submit = AuthAppTestCase.submit
    replace_remote = PeerConversationAPITests.replace_remote
    prepare_request_files = PeerConversationAPITests.prepare_request_files
    replace_snapshot = PeerConversationAPITests.replace_snapshot
    review_material = PeerConversationAPITests.review_material
    digest = staticmethod(PeerConversationAPITests.digest)
    async def prepare(self, context_id=None):
        await PeerConversationAPITests.prepare(self, context_id=context_id)
        self.addCleanup(self.cleanup_remote_tasks)

    def cleanup_remote_tasks(self):
        # Other suites intentionally use platform-wide recovery handlers.
        with self.app.state.database.transaction() as connection:
            connection.execute("DELETE FROM core_notifications WHERE tenant_id=%s", (self.tenant,))
            connection.execute("DELETE FROM core_background_tasks WHERE tenant_id=%s", (self.tenant,))

    test_owner_detail_is_passive_public_and_bound_to_admitted_chat = PeerConversationAPITests.test_owner_detail_is_passive_public_and_bound_to_admitted_chat
    test_observation_requires_owner_and_visible_boolean = PeerConversationAPITests.test_observation_requires_owner_and_visible_boolean
    test_legacy_empty_conversation_never_reconstructs_missing_peer_history = PeerConversationAPITests.test_legacy_empty_conversation_never_reconstructs_missing_peer_history
    test_failure_summary_distinguishes_unknown_outcome_without_raw_errors = PeerConversationAPITests.test_failure_summary_distinguishes_unknown_outcome_without_raw_errors
    test_selected_outgoing_files_use_frozen_scoped_snapshot_downloads = PeerConversationAPITests.test_selected_outgoing_files_use_frozen_scoped_snapshot_downloads
    test_outgoing_missing_and_tampered_batch_remains_unavailable = PeerConversationAPITests.test_outgoing_missing_and_tampered_batch_remains_unavailable
    test_send_marker_does_not_claim_request_or_files_delivered = PeerConversationAPITests.test_send_marker_does_not_claim_request_or_files_delivered
    test_later_source_denial_blocks_derived_requests_files_and_download = PeerConversationAPITests.test_later_source_denial_blocks_derived_requests_files_and_download
    test_later_selected_file_text_alias_blocks_whole_outgoing_payload = PeerConversationAPITests.test_later_selected_file_text_alias_blocks_whole_outgoing_payload
    test_exact_peer_text_denial_preserves_independent_outgoing_request = PeerConversationAPITests.test_exact_peer_text_denial_preserves_independent_outgoing_request
    test_current_remote_result_review_is_causal_and_pending_alias_is_visible = PeerConversationAPITests.test_current_remote_result_review_is_causal_and_pending_alias_is_visible
    test_frozen_request_dependencies_ignore_later_unrelated_denials = PeerConversationAPITests.test_frozen_request_dependencies_ignore_later_unrelated_denials
    test_legacy_request_cutoff_excludes_later_unrelated_results = PeerConversationAPITests.test_legacy_request_cutoff_excludes_later_unrelated_results
    test_legacy_cutoff_keeps_imported_prior_root_dependencies = PeerConversationAPITests.test_legacy_cutoff_keeps_imported_prior_root_dependencies
