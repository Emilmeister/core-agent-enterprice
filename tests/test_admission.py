import asyncio
import base64
import hashlib
import json
import os
import uuid
import threading
import unittest
from unittest.mock import AsyncMock, patch
from functools import wraps
from types import SimpleNamespace

import httpx
from a2a.types import Message, Role, SendMessageRequest, Task, TaskState, TaskStatus
from a2a.utils.errors import InvalidParamsError

from tests.app_support import create_app
from core_agent.auth import AuthenticatedCallContext, Principal, ScopeUser
from core_agent.admission import PostgresRootAdmission
from core_agent.config import RunRequest
from core_agent.database import PostgresDatabase
from core_agent.errors import CoreError

from tests.test_auth import AuthAppTestCase, ISSUER, TEST_DATABASE_URL


class BorrowedAdmissionValidationTests(unittest.TestCase):
    def test_invalid_message_is_rejected_before_borrowed_connection_access(self):
        admission = PostgresRootAdmission(None, SimpleNamespace(database=None))
        for message in (Message(message_id=" ", role=Role.ROLE_USER),
                        Message(message_id="invalid", role=Role.ROLE_AGENT)):
            with self.subTest(message=message), self.assertRaises(InvalidParamsError):
                admission._admit_transaction(message, None, None, connection=object())


class AuthAdmissionTests(AuthAppTestCase):
    async def asyncTearDown(self):
        if hasattr(self, "release"):
            self.release.set()
        executor = self.app.state.a2a_request_handler.agent_executor
        async with asyncio.timeout(5):
            while executor._active_executions:
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)

    async def send(self, message_id, context_id=None, *, token="external-a", text="Answer briefly", immediate=False, **message_fields):
        message = {"messageId": message_id, "role": "ROLE_USER", "parts": [{"text": text}], **message_fields}
        if context_id is not None:
            message["contextId"] = context_id
        kind = "owner" if token.startswith("owner") else "external"
        return await self.http.post(
            f"/a2a/{kind}/message:send", headers=self.headers(token),
            json={"message": message, "configuration": {"returnImmediately": immediate}},
        )

    def block_model(self):
        started, release = threading.Event(), threading.Event()
        generate = self.model.generate

        self.model_entries = 0

        @wraps(generate)
        def waiting(**kwargs):
            self.model_entries += 1
            started.set()
            release.wait(10)
            return generate(**kwargs)

        self.release = release
        self.model.generate = waiting
        self.addCleanup(release.set)
        return started, release

    def context(self, token="external-a"):
        subject = self.tokens[token]["sub"]
        actor = Principal(
            hashlib.sha256((ISSUER + "\0" + subject).encode()).hexdigest(),
            os.environ["CORE_AGENT_TENANT_ID"], token.startswith("owner"), token.startswith("external"),
        )
        return AuthenticatedCallContext(user=ScopeUser(actor.owner_id), tenant=actor.tenant, state={"principal": actor})

    async def finish_workers(self):
        if hasattr(self, "release"):
            self.release.set()
        executor = self.app.state.a2a_request_handler.agent_executor
        async with asyncio.timeout(5):
            while executor._active_executions:
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)

    @staticmethod
    def sdk_message(message_id, context_id):
        return Message(message_id=message_id, context_id=context_id, role=Role.ROLE_USER, parts=[{"text": "Answer briefly"}])

    async def test_concurrent_roots_and_duplicates_start_only_one_workflow(self):
        started, _release = self.block_model()
        responses = await asyncio.gather(*(self.send(name, "race", immediate=True) for name in ("a", "a", "b")))
        for response in responses:
            self.assertEqual(response.status_code, 200, response.text)
        tasks = [response.json()["task"] for response in responses]
        self.assertEqual(tasks[0]["id"], tasks[1]["id"])
        unique = {task["id"]: task for task in tasks}
        self.assertEqual(len(unique), 2)
        self.assertEqual(sum(task["status"]["state"] == "TASK_STATE_FAILED" for task in unique.values()), 1)
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        self.assertEqual(self.model_entries, 1)
        await self.finish_workers()
        self.assertEqual(len(self.model.calls), 1)

    async def test_other_chat_runs_while_first_chat_is_blocked(self):
        started, _release = self.block_model()
        first = await self.send("a", "one", immediate=True)
        self.assertEqual(first.status_code, 200, first.text)
        # The shared detector's first review has finished once the model blocks.
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        responses = [first, await self.send("b", "two", immediate=True)]
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertTrue(all(response.json()["task"]["status"]["state"] != "TASK_STATE_FAILED" for response in responses))
        async with asyncio.timeout(2):
            while self.model_entries < 2:
                await asyncio.sleep(0.01)
        self.assertEqual(len(self.model.calls), 0)
        await self.finish_workers()
        self.assertEqual(len(self.model.calls), 2)

    async def test_owners_deduplicate_by_actor_and_preserve_external_execution_owner(self):
        external = (await self.send("external", "external-chat")).json()["task"]
        first = (await self.send("shared-id", "external-chat", token="owner-a")).json()["task"]
        second = (await self.send("shared-id", "external-chat", token="owner-b")).json()["task"]
        self.assertEqual(len({external["id"], first["id"], second["id"]}), 3)
        for task in (first, second):
            record = self.app.state.core_agent.workflow_store.lookup_task(task["id"])
            self.assertEqual(record.owner_id, self.context().user.user_name)
        for token, task in (("owner-a", first), ("owner-b", second)):
            repeated = await self.send("shared-id", "external-chat", token=token)
            self.assertEqual(repeated.json()["task"]["id"], task["id"])
        other = await self.send("shared-id", "own-chat", token="external-b")
        self.assertEqual(other.status_code, 200, other.text)
        self.assertEqual(len(self.model.calls), 4)

    async def test_busy_duplicate_stays_failed_after_slot_is_released(self):
        started, _release = self.block_model()
        first = (await self.send("first", "chat", immediate=True)).json()["task"]
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        busy = (await self.send("busy", "chat")).json()["task"]
        await self.finish_workers()
        again = (await self.send("busy", "chat")).json()["task"]
        self.assertEqual(again["id"], busy["id"])
        self.assertEqual(again["status"]["state"], "TASK_STATE_FAILED")
        next_task = (await self.send("fresh", "chat")).json()["task"]
        self.assertNotIn(next_task["id"], (first["id"], busy["id"]))
        self.assertEqual(next_task["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(len(self.model.calls), 2)

    async def test_previous_root_is_pinned_by_admission_and_not_request_metadata(self):
        workflow = self.app.state.core_agent.workflow_store
        first = (await self.send("first-history", "history-chat")).json()["task"]
        first_record = workflow.lookup_task(first["id"])
        self.assertIn("previous_root_run_id", first_record.snapshot)
        self.assertIsNone(first_record.snapshot["previous_root_run_id"])
        other = (await self.send("other-history", "other-chat")).json()["task"]
        other_record = workflow.lookup_task(other["id"])
        second = (await self.send("second-history", "history-chat", token="owner-b",
            metadata={"previous_root_run_id": other_record.run_id})).json()["task"]
        second_record = workflow.lookup_task(second["id"])
        self.assertEqual(second_record.snapshot["previous_root_run_id"], first_record.run_id)
        self.assertEqual(second_record.owner_id, first_record.owner_id)
        self.assertIsNone(other_record.snapshot["previous_root_run_id"])
        duplicate = await self.send("first-history", "history-chat")
        self.assertEqual(duplicate.json()["task"]["id"], first["id"])
        third = (await self.send("third-history", "history-chat", token="owner-a")).json()["task"]
        third_record = workflow.lookup_task(third["id"])
        self.assertEqual(third_record.snapshot["previous_root_run_id"], second_record.run_id)
        self.assertEqual(workflow.get(second_record.run_id, tenant_id=second_record.tenant_id,
                                     owner_id=second_record.owner_id).snapshot["previous_root_run_id"],
                         first_record.run_id)

    async def test_busy_task_does_not_become_previous_root(self):
        started, _release = self.block_model()
        first = (await self.send("active-history", "history-chat", immediate=True)).json()["task"]
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        busy = (await self.send("busy-history", "history-chat")).json()["task"]
        self.assertEqual(busy["status"]["state"], "TASK_STATE_FAILED")
        await self.finish_workers()
        next_task = (await self.send("next-history", "history-chat")).json()["task"]
        workflow = self.app.state.core_agent.workflow_store
        first_record = workflow.lookup_task(first["id"])
        next_record = workflow.lookup_task(next_task["id"])
        self.assertEqual(next_record.snapshot.get("previous_root_run_id"), first_record.run_id)

    async def test_invalid_role_id_and_url_have_no_effect_but_raw_is_protected_input(self):
        for fields in ({"role": "ROLE_AGENT"}, {"messageId": " "}, {"parts": [{"url": "https://example.test/a"}]}):
            message = {"messageId": "invalid", "contextId": "invalid-chat", "role": "ROLE_USER", "parts": [{"text": "Answer"}], **fields}
            response = await self.http.post("/a2a/external/message:send", headers=self.headers("external-a"), json={"message": message})
            self.assertIn(response.status_code, (400, 415), response.text)
        listing = await self.http.get("/a2a/external/tasks", headers=self.headers("external-a"))
        self.assertEqual(listing.json().get("tasks", []), [])
        self.assertEqual(len(self.model.calls), 0)
        response = await self.send("valid-file", "invalid-chat", parts=[
            {"raw": "aGVsbG8=", "filename": "incoming.txt", "mediaType": "text/plain"}])
        self.assertEqual(response.status_code, 200, response.text)
        task = response.json()["task"]
        receipt = task["metadata"]["file_receipt"]
        self.assertEqual(receipt["entries"][0]["actual_name"], "incoming.txt")
        batch = self.app.state.core_agent.chat_file_service.store.get(receipt["batch_id"], self.context().tenant)
        self.assertEqual(batch["state"], "published")
        self.assertEqual(batch["owner_id"], self.context().user.user_name)
        self.assertEqual(len(self.model.calls), 1)

    async def file_transport(self, binding, message, *, token="external-a"):
        kind = "owner" if token.startswith("owner") else "external"
        params = {"message": message}
        rpc = not binding.startswith("/")
        body = {"jsonrpc": "2.0", "id": "files-rpc", "method": binding, "params": params} if rpc else params
        response = await self.http.post(f"/a2a/{kind}{'/' if rpc else binding}", headers=self.headers(token), json=body)
        self.assertEqual(response.status_code, 200, response.text)
        if "stream" in binding.lower():
            frames = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
            frame = frames[0]["result"] if rpc else frames[0]
            return frame["task"]
        return (response.json()["result"] if rpc else response.json())["task"]

    async def test_file_roots_all_bindings_keep_receipts_after_limit_change_and_rotation(self):
        agent, context = self.app.state.core_agent, self.context()
        store = agent.interaction_store
        settings_values = {"hitl_timeout_seconds": 86400, "owner_answer_timeout_seconds": 86400,
                           "guardrails_timeout_seconds": 86400, "attachment_limit_bytes": 25000000}
        for index, binding in enumerate(("/message:send", "/message:stream", "SendMessage", "SendStreamingMessage")):
            store.update_settings(context.tenant, settings_values, store.get_settings(context.tenant).revision)
            parts = [{"raw": base64.b64encode(raw).decode(), "filename": "report.txt", "mediaType": "text/plain"}
                     for raw in (b"first report", b"second report")]
            if index % 2:
                parts.insert(0, {"text": "Read both files"})
            message = {"messageId": f"file-root-{index}", "contextId": f"file-chat-{index}", "role": "ROLE_USER", "parts": parts}
            first = await self.file_transport(binding, message)
            receipt = first["metadata"]["file_receipt"]
            self.assertEqual([entry["actual_name"] for entry in receipt["entries"]], ["report.txt", "report_2.txt"])
            batch = agent.chat_file_service.store.get(receipt["batch_id"], context.tenant)
            self.assertEqual((batch["owner_id"], batch["context_id"], batch["state"]),
                             (context.user.user_name, message["contextId"], "published"))
            model_calls = len(self.model.calls)
            store.update_settings(context.tenant, {**settings_values, "attachment_limit_bytes": 1}, store.get_settings(context.tenant).revision)
            repeated = await self.file_transport(binding, message, token="external-a-replaced")
            self.assertEqual(repeated["id"], first["id"])
            self.assertEqual(repeated["metadata"]["file_receipt"], receipt)
            self.assertEqual(len(self.model.calls), model_calls)
            denied = await self.http.get(f"/a2a/external/tasks/{first['id']}", headers=self.headers("external-b"))
            self.assertEqual(denied.status_code, 404)
            owner = await self.http.get(f"/a2a/owner/tasks/{first['id']}", headers=self.headers("owner-b"))
            self.assertEqual(owner.json()["metadata"]["file_receipt"], receipt)
        store.update_settings(context.tenant, settings_values, store.get_settings(context.tenant).revision)
        owner_message = {"messageId": "owner-files", "contextId": "file-chat-0", "role": "ROLE_USER",
                         "parts": [{"raw": "bmV3", "filename": "report.txt", "mediaType": "text/plain"}]}
        owner_task = await self.file_transport("/message:send", owner_message, token="owner-a")
        owner_receipt = owner_task["metadata"]["file_receipt"]
        owner_batch = agent.chat_file_service.store.get(owner_receipt["batch_id"], context.tenant)
        self.assertEqual(owner_batch["owner_id"], context.user.user_name)
        self.assertEqual(owner_receipt["entries"][0]["actual_name"], "report_3.txt")

    async def test_file_followups_all_bindings_ack_original_receipts_while_model_is_blocked(self):
        started, release = self.block_model()
        agent, context = self.app.state.core_agent, self.context()
        values = {"hitl_timeout_seconds": 86400, "owner_answer_timeout_seconds": 86400,
                  "guardrails_timeout_seconds": 86400, "attachment_limit_bytes": 25000000}
        for index, binding in enumerate(("/message:send", "/message:stream", "SendMessage", "SendStreamingMessage")):
            started.clear()
            release.clear()
            agent.interaction_store.update_settings(context.tenant, values, agent.interaction_store.get_settings(context.tenant).revision)
            root = (await self.send(f"file-active-{index}", f"file-active-chat-{index}", immediate=True)).json()["task"]
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            parts = [{"raw": "YWJj", "filename": "follow.txt", "mediaType": "text/plain"}]
            if index % 2:
                parts.insert(0, {"text": "Use this additional file"})
            message = {"messageId": f"file-follow-{index}", "taskId": root["id"], "contextId": root["contextId"],
                       "role": "ROLE_USER", "parts": parts}
            accepted = await self.file_transport(binding, message)
            metadata = accepted["metadata"]
            self.assertEqual(metadata["accepted_message_id"], message["messageId"])
            receipt = metadata["accepted_file_receipt"]
            batch = agent.chat_file_service.store.get(receipt["batch_id"], context.tenant)
            self.assertEqual(batch["state"], "accepted_quarantine")
            before = len(self.model.calls)
            agent.interaction_store.update_settings(context.tenant, {**values, "attachment_limit_bytes": 1}, agent.interaction_store.get_settings(context.tenant).revision)
            again = await self.file_transport(binding, message, token="external-a-replaced")
            self.assertEqual(again["metadata"]["accepted_file_receipt"], receipt)
            self.assertEqual(len(self.model.calls), before)
            stored = await self.http.get(f"/a2a/external/tasks/{root['id']}", headers=self.headers("external-a"))
            self.assertNotIn("accepted_file_receipt", stored.json().get("metadata", {}))
            await self.finish_workers()
            batch = agent.chat_file_service.store.get(receipt["batch_id"], context.tenant)
            self.assertEqual(batch["state"], "published")

    async def test_invalid_second_file_and_aggregate_oversize_have_no_admission_effect(self):
        context = self.context()
        agent = self.app.state.core_agent
        settings = agent.interaction_store.get_settings(context.tenant)
        agent.interaction_store.update_settings(context.tenant, {"hitl_timeout_seconds": 86400,
            "owner_answer_timeout_seconds": 86400, "guardrails_timeout_seconds": 86400,
            "attachment_limit_bytes": 7}, settings.revision)
        for binding in ("/message:send", "/message:stream", "SendMessage", "SendStreamingMessage"):
            for index, raw in enumerate(("YR==", "MTIzNDU=")):
                message = {"messageId": f"bad-files-{binding}-{index}", "contextId": "reject-files", "role": "ROLE_USER",
                           "parts": [{"text": "Do not deliver"}, {"raw": "MTIzNA==", "filename": "first.txt", "mediaType": "text/plain"},
                                     {"raw": raw, "filename": "second.txt", "mediaType": "text/plain"}]}
                rpc = not binding.startswith("/")
                payload = {"message": message}
                body = {"jsonrpc": "2.0", "id": "bad", "method": binding, "params": payload} if rpc else payload
                response = await self.http.post(f"/a2a/external{'/' if rpc else binding}", headers=self.headers("external-a"), json=body)
                self.assertEqual(response.status_code, 200 if rpc else 400, response.text)
                error = response.json()["error"]
                info = (error["data"] if rpc else error["details"])[0]["metadata"]
                self.assertEqual(info["code"], "INVALID_FILE_ENCODING" if index == 0 else "ATTACHMENTS_TOO_LARGE")
                if index:
                    self.assertEqual((info["allowed_bytes"], info["actual_bytes"]), ("7", "9"))
        listing = await self.http.get("/a2a/external/tasks", headers=self.headers("external-a"))
        self.assertEqual(listing.json().get("tasks", []), [])
        self.assertEqual(len(self.model.calls), 0)

    async def test_canonical_metadata_and_data_parts_deduplicate(self):
        parts = [{"data": {"b": [1, 2], "a": "value"}}]
        first = await self.send("structured", "data-chat", metadata={"b": 2, "a": 1}, parts=parts)
        again = await self.send("structured", "data-chat", metadata={"a": 1, "b": 2}, parts=[{"data": {"a": "value", "b": [1, 2]}}])
        self.assertEqual(first.json()["task"]["id"], again.json()["task"]["id"])
        changed = await self.send("structured", "data-chat", metadata={"a": 3, "b": 2}, parts=parts)
        self.assertEqual(changed.status_code, 400, changed.text)
        self.assertEqual(len(self.model.calls), 1)

    async def test_legacy_task_and_workflow_context_cannot_be_claimed(self):
        handler = self.app.state.a2a_request_handler
        context = self.context()
        task = Task(id=str(uuid.uuid4()), context_id="legacy-task", status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED))
        await handler.task_store.save(task, context)
        legacy, *_ = self.app.state.core_agent._new_workflow(
            {"prompt": "legacy"}, task_id=str(uuid.uuid4()), identity=context.user.user_name,
            session_id="legacy-run", tenant_id=context.tenant, defer_initialization=True,
        )
        self.app.state.core_agent.workflow_store.transition(
            legacy.run_id, tenant_id=legacy.tenant_id, owner_id=legacy.owner_id,
            expected_version=legacy.version, state="FAILED", snapshot=legacy.snapshot,
            event_kind="task.failed", error_code="TEST_LEGACY",
        )
        for chat in ("legacy-task", "legacy-run"):
            response = await self.send("new-" + chat, chat, token="owner-a")
            self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(len(self.model.calls), 0)

    async def test_setup_failure_releases_initial_lease_and_preserves_history_for_recovery(self):
        handler = self.app.state.a2a_request_handler
        with patch.object(handler._request_context_builder, "build", new=AsyncMock(side_effect=InvalidParamsError("setup failed"))):
            response = await self.send("recover", "recover-chat")
        self.assertEqual(response.status_code, 400, response.text)
        repeated = await self.send("recover", "recover-chat")
        task = repeated.json()["task"]
        self.assertEqual(task["status"]["state"], "TASK_STATE_SUBMITTED")
        self.assertEqual(len(task["history"]), 1)
        self.assertEqual(task["history"][0]["taskId"], task["id"])
        self.assertEqual(len(self.model.calls), 0)
        result = await asyncio.to_thread(self.app.state.core_agent.resume_task, task["id"])
        self.assertEqual(result.message, "verified")
        self.assertEqual(len(self.model.calls), 1)

    async def test_recovered_setup_failure_projects_result_to_every_public_read(self):
        handler = self.app.state.a2a_request_handler
        agent = self.app.state.core_agent
        original_admit = handler.admission_handler
        accepted = []

        async def capture_admission(message, context):
            result = await original_admit(message, context)
            accepted.append(result)
            return result

        with patch.object(handler, "admission_handler", side_effect=capture_admission), patch.object(
            handler._request_context_builder, "build", new=AsyncMock(side_effect=InvalidParamsError("setup failed"))
        ):
            response = await self.send("projection", "projection-chat")
        self.assertEqual(response.status_code, 400, response.text)
        task_id = accepted[0].task.id
        await asyncio.to_thread(agent._recover_workflows_once)
        async with asyncio.timeout(5):
            while agent.workflow_store.lookup_task(task_id).state != "COMPLETED" or task_id in agent._recovery_workers:
                await asyncio.sleep(0.01)
        record = agent.workflow_store.lookup_task(task_id)
        # List must project before applying a terminal-state filter.
        listing = await self.http.get(
            "/a2a/external/tasks", headers=self.headers("external-a"),
            params={"status": "TASK_STATE_COMPLETED"},
        )
        self.assertEqual([task["id"] for task in listing.json().get("tasks", [])], [task_id])
        fetched = await self.http.get(f"/a2a/external/tasks/{task_id}", headers=self.headers("external-a"))
        self.assertEqual(fetched.status_code, 200, fetched.text)
        task = fetched.json()
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(task["artifacts"][0]["parts"][0]["text"], "verified")
        provenance = task["artifacts"][0]["metadata"]["provenance"]
        self.assertEqual(provenance["run_id"], record.run_id)
        self.assertEqual(provenance["shared_budget"], record.result["shared_budget"])
        self.assertEqual(provenance["completion_reason"], record.result["completion_reason"])
        self.assertEqual(len(task["history"]), 1)
        duplicate = await self.send("projection", "projection-chat")
        self.assertEqual(duplicate.json()["task"], task)
        subscription = await self.http.post(f"/a2a/external/tasks/{task_id}:subscribe", headers=self.headers("external-a"))
        frames = [json.loads(line[5:]) for line in subscription.text.splitlines() if line.startswith("data:")]
        self.assertEqual(frames, [{"task": task}])
        self.assertEqual(len(self.model.calls), 1)
        denied = await self.http.get(f"/a2a/external/tasks/{task_id}", headers=self.headers("external-b"))
        self.assertEqual(denied.status_code, 404, denied.text)
        owner = await self.http.get(f"/a2a/owner/tasks/{task_id}", headers=self.headers("owner-b"))
        self.assertEqual(owner.json(), task)

    async def test_disconnected_stream_recovery_projects_safe_failure(self):
        handler = self.app.state.a2a_request_handler
        agent = self.app.state.core_agent
        context = self.context()
        stream = handler.on_message_send_stream(
            SendMessageRequest(message=self.sdk_message("stream-failure", "stream-failure-chat")), context
        )
        initial = await anext(stream)
        await stream.aclose()
        admitted = context.state["initial_admission"]
        agent.workflow_store.release_lease(
            admitted.run_id, tenant_id=context.tenant, worker_id=agent._worker_id,
            token=admitted.lease_token,
        )
        with patch.object(agent, "_resolve_capabilities", side_effect=CoreError("MCP_AUTH_FAILED", "private provider detail")):
            await asyncio.to_thread(agent._recover_workflows_once)
            async with asyncio.timeout(5):
                while agent.workflow_store.lookup_task(initial.id).state != "FAILED" or initial.id in agent._recovery_workers:
                    await asyncio.sleep(0.01)
        fetched = await self.http.get(f"/a2a/external/tasks/{initial.id}", headers=self.headers("external-a"))
        self.assertEqual(fetched.status_code, 200, fetched.text)
        task = fetched.json()
        self.assertEqual(task["status"]["state"], "TASK_STATE_FAILED")
        self.assertEqual(task["status"]["message"]["parts"][0]["text"], "MCP_AUTH_FAILED")
        self.assertNotIn("private provider detail", fetched.text)
        duplicate = await self.send("stream-failure", "stream-failure-chat")
        self.assertEqual(duplicate.json()["task"], task)
        subscription = await self.http.post(f"/a2a/external/tasks/{initial.id}:subscribe", headers=self.headers("external-a"))
        frames = [json.loads(line[5:]) for line in subscription.text.splitlines() if line.startswith("data:")]
        self.assertEqual(frames, [{"task": task}])
        # A late SDK snapshot must not erase the terminal recovery projection.
        await handler.task_store.save(initial, context)
        after = await self.http.get(f"/a2a/external/tasks/{initial.id}", headers=self.headers("external-a"))
        self.assertEqual(after.json(), task)

    async def test_streams_publish_admitted_task_and_duplicate_never_reexecutes(self):
        message = {"messageId": "stream", "contextId": "stream-chat", "role": "ROLE_USER", "parts": [{"text": "Answer briefly"}]}
        response = await self.http.post("/a2a/external/message:stream", headers=self.headers("external-a"), json={"message": message})
        frames = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
        first = frames[0]["task"]
        self.assertEqual(first["status"]["state"], "TASK_STATE_SUBMITTED")
        self.assertEqual(len(first["history"]), 1)
        rpc = await self.http.post("/a2a/external/", headers=self.headers("external-a"), json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": message}})
        self.assertEqual(rpc.json()["result"]["task"]["id"], first["id"])
        again = await self.http.post("/a2a/external/", headers=self.headers("external-a"), json={"jsonrpc": "2.0", "id": 2, "method": "SendStreamingMessage", "params": {"message": message}})
        repeated = [json.loads(line[5:]) for line in again.text.splitlines() if line.startswith("data:")]
        self.assertEqual(len(repeated), 1)
        self.assertEqual(repeated[0]["result"]["task"]["id"], first["id"])
        self.assertEqual(len(self.model.calls), 1)

    async def test_initial_message_repeated_as_followup_does_not_add_a_user_turn(self):
        started, _release = self.block_model()
        task = (await self.send("initial", "same-turn", immediate=True)).json()["task"]
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        followup = await self.send("initial", "same-turn", taskId=task["id"])
        self.assertEqual(followup.status_code, 200, followup.text)
        self.assertEqual(followup.json()["task"]["id"], task["id"])
        await self.finish_workers()
        self.assertEqual(len(self.model.calls), 1)

    async def test_invalid_empty_prompt_and_stale_capability_metadata_fail_before_admission(self):
        for extra in ({"text": ""}, {"metadata": {"urn:core-agent:run-capabilities:v1": {"mcp": ["unexpected"]}}}):
            response = await self.send("invalid", "input", **extra)
            self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(len(self.model.calls), 0)

    async def test_immediate_followup_and_owner_cancel_use_admitted_workflow(self):
        started, release = self.block_model()
        task = (await self.send("root", "active", immediate=True)).json()["task"]
        followup = await self.send("followup", "active", taskId=task["id"], text="New instructions")
        self.assertEqual(followup.status_code, 200, followup.text)
        self.assertEqual(followup.json()["task"]["id"], task["id"])
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        agent = self.app.state.core_agent
        signal = agent.signal_task_cancel

        def cancel_and_release(task_id):
            signal(task_id)
            release.set()

        with patch.object(agent, "signal_task_cancel", side_effect=cancel_and_release):
            response = await self.http.post(f'/a2a/owner/tasks/{task["id"]}:cancel', headers=self.headers("owner-a"))
        self.assertEqual(response.status_code, 200, response.text)
        await self.finish_workers()
        record = agent.workflow_store.lookup_task(task["id"])
        self.assertEqual(record.owner_id, self.context().user.user_name)
        self.assertEqual(record.state, "CANCELLED")
        next_task = await self.send("after-cancel", "active")
        self.assertEqual(next_task.json()["task"]["status"]["state"], "TASK_STATE_COMPLETED")

    async def test_nonterminal_wait_holds_slot_and_child_does_not_become_root(self):
        handler = self.app.state.a2a_request_handler
        context = self.context()
        admission = await handler.admission_handler(self.sdk_message("waiting", "wait-chat"), context)
        agent = self.app.state.core_agent
        record = agent.workflow_store.lookup_task(admission.task.id)
        record = agent.workflow_store.transition(
            record.run_id, tenant_id=record.tenant_id, owner_id=record.owner_id,
            expected_version=record.version, state="WAITING_BACKGROUND", snapshot=record.snapshot,
            event_kind="task.waiting", lease_token=admission.lease_token,
        )
        child, *_ = agent._new_workflow(
            {"prompt": "child"}, task_id=str(uuid.uuid4()), identity=record.owner_id,
            session_id=record.context_id, tenant_id=record.tenant_id, parent_run_id=record.run_id,
            defer_initialization=True,
        )
        response = await self.send("busy-wait", "wait-chat")
        self.assertEqual(response.json()["task"]["metadata"]["error"]["activeTaskId"], record.task_id)
        self.assertNotEqual(child.task_id, record.task_id)
        self.assertEqual(len(self.model.calls), 0)
        for pending in (child, record):
            agent.workflow_store.transition(
                pending.run_id, tenant_id=pending.tenant_id, owner_id=pending.owner_id,
                expected_version=pending.version, state="FAILED", snapshot=pending.snapshot,
                event_kind="task.failed", error_code="TEST_FINISHED",
                **({"lease_token": admission.lease_token} if pending is record else {}),
            )

    async def test_busy_is_saved_and_duplicate_returns_same_task(self):
        started, release = self.block_model()
        try:
            first = await self.send("first", "chat", immediate=True)
            self.assertEqual(first.status_code, 200, first.text)
            first = first.json()["task"]
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            busy = await self.send("second", "chat", immediate=True)
            self.assertEqual(busy.status_code, 200, busy.text)
            busy = busy.json()["task"]
            self.assertNotEqual(first["id"], busy["id"])
            self.assertEqual(busy["status"]["state"], "TASK_STATE_FAILED")
            self.assertEqual(busy["metadata"]["error"], {"code": "CONTEXT_BUSY", "activeTaskId": first["id"]})
            repeated = await self.send("second", "chat")
            self.assertEqual(repeated.json()["task"]["id"], busy["id"])
            saved = await self.http.get(f'/a2a/external/tasks/{busy["id"]}', headers=self.headers("external-a"))
            self.assertEqual(saved.json()["id"], busy["id"])
            with self.assertRaisesRegex(Exception, "TASK_NOT_FOUND"):
                self.app.state.core_agent.workflow_store.lookup_task(busy["id"])
        finally:
            release.set()

    async def test_completed_duplicate_without_context_and_token_replacement(self):
        first = await self.send("same")
        repeated = await self.send("same", token="external-a-replaced")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(first.json()["task"]["id"], repeated.json()["task"]["id"])
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(len(repeated.json()["task"]["history"]), 1)

    async def test_changed_message_or_context_conflicts(self):
        first = await self.send("same", "chat")
        for text, context in (("different", "chat"), ("Answer briefly", "other")):
            with self.subTest(context=context):
                response = await self.send("same", context, text=text)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("MESSAGE_ID_CONFLICT", response.text)
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(first.json()["task"]["status"]["state"], "TASK_STATE_COMPLETED")

    async def test_external_cannot_capture_another_callers_context(self):
        await self.send("first", "private")
        response = await self.send("second", "private", token="external-b")
        self.assertEqual(response.status_code, 404, response.text)
        self.assertNotIn("activeTaskId", response.text)
        self.assertEqual(len(self.model.calls), 1)


class MemoryAtomicAdmissionTests(AuthAppTestCase):
    context = AuthAdmissionTests.context
    sdk_message = staticmethod(AuthAdmissionTests.sdk_message)

    @property
    def admission(self):
        return self.app.state.core_agent.tool_runtime.environment_manager.validate_workspace_scope.__self__

    async def test_callback_admissions_and_empty_chat_roll_back_as_one_unit(self):
        agent, admission = self.app.state.core_agent, self.admission
        task_store = admission.task_store
        def failed(admit):
            binding = admission.ensure_chat(self.context("owner-a"))
            self.assertIsNone(admission.chats[(binding.tenant_id, binding.context_id)]["latest_root_run_id"])
            for index in range(2):
                result = admit(self.sdk_message(str(index), "transaction-" + str(index)), RunRequest(prompt="transaction"), self.context())
                self.assertIsNotNone(result.run_id)
                self.assertIn(result.run_id, agent.workflow_store._records)
            raise CoreError("TEST_CALLBACK_FAILED")
        with self.assertRaisesRegex(CoreError, "TEST_CALLBACK_FAILED"):
            await admission.transaction(self.context(), failed)
        for values in (admission.chats, admission.messages, task_store._owners, task_store._impl.tasks,
                       agent.workflow_store._records, agent.workflow_store._leases, agent.workflow_store._budgets,
                       agent._run_scopes, agent.event_store._events, agent.checkpoint_store._values, agent.audit_log._records):
            self.assertFalse(values)
        self.assertFalse(self.model.calls)
        await self.submit("external-a", "after", "after")

    async def test_all_async_locks_acquired_before_workflow_guard_and_callback_is_sync(self):
        admission, entered = self.admission, threading.Event()
        workflow = self.app.state.core_agent.workflow_store
        def callback(admit):
            self.assertTrue(admission.lock.locked())
            self.assertTrue(admission.task_store._access_lock.locked())
            self.assertTrue(admission.task_store._impl.lock.locked())
            self.assertTrue(workflow._lock._is_owned())
            entered.set()
            return admit(self.sdk_message("locked", "locked"), RunRequest(prompt="locked"), self.context())
        await admission.task_store._impl.lock.acquire()
        task = asyncio.create_task(admission.transaction(self.context(), callback))
        try:
            await asyncio.sleep(0)
            self.assertFalse(entered.is_set())
            self.assertFalse(workflow._records)
            def thread_can_lock():
                acquired = workflow._lock.acquire(timeout=1)
                if acquired:
                    workflow._lock.release()
                return acquired
            self.assertTrue(await asyncio.to_thread(thread_can_lock))
        finally:
            admission.task_store._impl.lock.release()
        self.assertIsNotNone((await task).run_id)

    async def test_transaction_duplicate_busy_and_original_owner_use_common_core(self):
        admission = self.admission
        first = await admission.transaction(self.context(), lambda admit:
            admit(self.sdk_message("first", "shared"), RunRequest(prompt="first"), self.context()))
        def callback(admit):
            duplicate = admit(self.sdk_message("first", "shared"), RunRequest(prompt="first"), self.context())
            busy = admit(self.sdk_message("busy", "shared"), RunRequest(prompt="busy"), self.context("owner-a"))
            return duplicate, busy
        duplicate, busy = await admission.transaction(self.context("owner-a"), callback)
        self.assertEqual(duplicate.task.id, first.task.id)
        self.assertEqual(busy.task.status.state, TaskState.TASK_STATE_FAILED)
        self.assertEqual(dict(busy.task.metadata)["error"]["activeTaskId"], first.task.id)
        self.assertEqual(admission.chats[(self.context().tenant, "shared")]["owner_id"], self.context().user.user_name)


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL is required")
class PostgresAuthAdmissionTests(AuthAdmissionTests):
    use_postgres = True

    async def test_restart_recovers_only_configured_company_before_batch_limit_and_projection(self):
        from core_agent.model import ModelResponse, ScriptedModel
        from core_agent.workflow import PostgresWorkflowStore, TERMINAL_STATES
        from psycopg.types.json import Jsonb

        first_agent = self.app.state.core_agent
        first_agent._recovery_stop.set()
        first_agent._recovery_thread.join(5)
        self.assertFalse(first_agent._recovery_thread.is_alive())
        first_context = self.context("owner-a")
        admitted = []
        for index in range(102):
            context = self.context("owner-a")
            message = self.sdk_message("foreign-" + str(index), "foreign-chat-" + str(index))
            message.parts[0].text = "foreign company prompt"
            accepted = await self.app.state.a2a_request_handler.admission_handler(message, context)
            admitted.append(accepted)
            first_agent.workflow_store.release_lease(accepted.run_id, tenant_id=context.tenant,
                worker_id=first_agent._worker_id, token=accepted.lease_token)
        database = PostgresDatabase(TEST_DATABASE_URL)
        self.addCleanup(database.close)
        second_tenant = "company-b-" + uuid.uuid4().hex
        def cleanup_companies():
            workflows = PostgresWorkflowStore(database)
            for admission in admitted:
                record = workflows.lookup_task(admission.task.id)
                if record.state not in TERMINAL_STATES:
                    workflows.transition(record.run_id, tenant_id=record.tenant_id,
                        owner_id=record.owner_id, expected_version=record.version,
                        state="CANCELLED", snapshot=record.snapshot, event_kind="test.cleanup")
        self.addCleanup(cleanup_companies)
        with database.transaction() as connection:
            connection.execute("UPDATE core_runs SET state='COMPLETED', result=%s, version=version+1 WHERE run_id=%s",
                (Jsonb({"message": "foreign company result", "complete": True,
                        "completion_reason": "completed", "usage": {"model_turns": 0, "tool_calls": 0}}), admitted[0].run_id))
        self.app.state.close()
        self.app.state.database.close()

        def foreign_state():
            with database.pool.connection() as connection:
                return (
                    connection.execute("SELECT * FROM core_runs WHERE tenant_id=%s ORDER BY run_id", (first_context.tenant,)).fetchall(),
                    connection.execute("SELECT * FROM core_a2a_tasks WHERE tenant=%s ORDER BY task_id", (first_context.tenant,)).fetchall(),
                )

        before = foreign_state()
        second_model = ScriptedModel([ModelResponse(message="company B result")])
        second_model.model = "auth-test-model"
        with patch.dict(os.environ, {"CORE_AGENT_TENANT_ID": second_tenant}):
            seed_database = PostgresDatabase(TEST_DATABASE_URL)
            self.addCleanup(seed_database.close)
            with patch("core_agent.runtime.CoreAgent.recover_workflows"):
                seed = create_app(model=second_model, database=seed_database,
                                  auth_transport=httpx.MockTransport(self.introspect))
            self.addCleanup(seed.state.close)
            second_context = self.context("owner-a")
            message = self.sdk_message("own", "own-chat")
            message.parts[0].text = "company B prompt"
            accepted = await seed.state.a2a_request_handler.admission_handler(message, second_context)
            admitted.append(accepted)
            seed.state.core_agent.workflow_store.release_lease(accepted.run_id, tenant_id=second_context.tenant,
                worker_id=seed.state.core_agent._worker_id, token=accepted.lease_token)
            seed.state.close()
            seed_database.close()
            self.assertEqual(foreign_state(), before)
            recovered_database = PostgresDatabase(TEST_DATABASE_URL)
            self.addCleanup(recovered_database.close)
            recovered = create_app(model=second_model, database=recovered_database,
                                   auth_transport=httpx.MockTransport(self.introspect))
        self.addCleanup(recovered.state.close)
        agent = recovered.state.core_agent
        async with asyncio.timeout(5):
            while agent.workflow_store.lookup_task(accepted.task.id).state != "COMPLETED" or agent._recovery_workers:
                await asyncio.sleep(0.02)
        self.assertEqual([call.context for call in second_model.calls], ["company B prompt"])
        self.assertEqual(agent.workflow_store.lookup_task(accepted.task.id).result["message"], "company B result")
        self.assertEqual(foreign_state(), before)
        self.assertFalse(self.model.calls)

    async def test_borrowed_connection_keeps_root_dedup_busy_and_history_in_outer_transaction(self):
        previous_task = (await self.send("previous", "borrowed-chat")).json()["task"]
        agent = self.app.state.core_agent
        previous = agent.workflow_store.lookup_task(previous_task["id"])
        admission = agent.workspace_cleanup.admission
        database = self.app.state.database
        context = self.context("owner-a")
        request = RunRequest.from_dict({"prompt": "Answer briefly"})
        message = self.sdk_message("borrowed", "borrowed-chat")
        with database.transaction() as connection:
            accepted = admission._admit_transaction(message, request, context, connection=connection)
            repeated = admission._admit_transaction(message, request, context, connection=connection)
            busy = admission._admit_transaction(self.sdk_message("borrowed-busy", "borrowed-chat"),
                request, context, connection=connection)
            self.assertEqual(repeated.task.id, accepted.task.id)
            self.assertIsNone(repeated.run_id)
            self.assertEqual(busy.task.status.state, TaskState.TASK_STATE_FAILED)
            self.assertIsNone(busy.run_id)
            self.assertEqual(dict(busy.task.metadata)["error"]["activeTaskId"], accepted.task.id)
            row = connection.execute("SELECT * FROM core_runs WHERE run_id=%s", (accepted.run_id,)).fetchone()
            self.assertEqual(row["owner_id"], previous.owner_id)
            self.assertEqual(row["snapshot"]["previous_root_run_id"], previous.run_id)
            self.assertEqual(row["lease_token"], accepted.lease_token)
            self.assertEqual(len(accepted.task.history), 1)
            self.assertEqual(accepted.task.history[0].task_id, accepted.task.id)
            self.assertEqual(connection.execute("SELECT used_model_turns FROM core_budget_ledgers WHERE root_run_id=%s",
                (accepted.run_id,)).fetchone()["used_model_turns"], 1)
            with database.pool.connection() as observer:
                self.assertIsNone(observer.execute("SELECT 1 FROM core_runs WHERE run_id=%s", (accepted.run_id,)).fetchone())
                self.assertIsNone(observer.execute("SELECT 1 FROM core_a2a_tasks WHERE task_id=%s", (busy.task.id,)).fetchone())
        repeated = await admission.admit(message, request, self.context("owner-a"))
        self.assertEqual(repeated.task.id, accepted.task.id)
        self.assertEqual(len(self.model.calls), 1)
        record = agent.workflow_store.lookup_task(accepted.task.id)
        agent.workflow_store.transition(record.run_id, tenant_id=record.tenant_id, owner_id=record.owner_id,
            expected_version=record.version, state="FAILED", snapshot=record.snapshot,
            event_kind="test.finished", error_code="TEST_FINISHED", lease_token=accepted.lease_token)

    async def test_outer_rollback_removes_borrowed_admission_and_caller_writes(self):
        agent = self.app.state.core_agent
        admission = agent.workspace_cleanup.admission
        database = self.app.state.database
        context = self.context()
        with self.assertRaisesRegex(CoreError, "TEST_OUTER_ROLLBACK"):
            with database.transaction() as connection:
                accepted = admission._admit_transaction(self.sdk_message("rollback-borrowed", "rollback-chat"),
                    RunRequest.from_dict({"prompt": "Answer briefly"}), context, connection=connection)
                # A caller write after admission belongs to this same commit boundary.
                connection.execute("UPDATE core_chats SET workspace_revision=workspace_revision+1 WHERE tenant_id=%s AND context_id=%s",
                    (context.tenant, accepted.task.context_id))
                self.assertIsNotNone(connection.execute("SELECT 1 FROM core_root_messages WHERE task_id=%s", (accepted.task.id,)).fetchone())
                raise CoreError("TEST_OUTER_ROLLBACK")
        with database.pool.connection() as connection:
            for table, field in (("core_chats", "tenant_id"), ("core_root_messages", "tenant_id"),
                    ("core_a2a_tasks", "tenant"), ("core_runs", "tenant_id"), ("core_budget_ledgers", "tenant_id"),
                    ("core_events", "tenant_id"), ("core_checkpoints", "tenant_id"), ("core_audit_records", "tenant_id"), ("core_outbox", "tenant_id")):
                self.assertEqual(connection.execute(f"SELECT count(*) AS n FROM {table} WHERE {field}=%s",
                    (context.tenant,)).fetchone()["n"], 0, table)
        self.assertEqual(agent._run_scopes, {})
        self.assertEqual(len(self.model.calls), 0)

    async def test_enterprise_history_and_creation_ledger_are_not_retention_targets(self):
        agent = self.app.state.core_agent
        tasks = []
        for message_id in ("retained-first", "retained-next"):
            response = await self.send(message_id, "retained-chat")
            self.assertEqual(response.status_code, 200, response.text)
            tasks.append((message_id, response.json()["task"]))
        await self.finish_workers()
        for message_id, task in tasks:
            persisted = await self.http.get(
                f"/a2a/external/tasks/{task['id']}", headers=self.headers("external-a")
            )
            self.assertEqual(persisted.status_code, 200, persisted.text)
            record = agent.workflow_store.lookup_task(task["id"])
            with self.assertRaises(CoreError) as caught:
                agent.delete_run_data(record.tenant_id, record.run_id, operator_principal_id="operator")
            self.assertEqual(caught.exception.code, "RETENTION_PROHIBITED")
            self.assertEqual(agent.workflow_store.lookup_task(task["id"]), record)
            repeated = await self.send(message_id, "retained-chat")
            self.assertEqual(repeated.json()["task"], persisted.json())
        self.assertEqual(len(self.model.calls), 2)

    def second_app(self):
        app = create_app(
            model=self.model, base_url="https://agent.example.test",
            auth_transport=httpx.MockTransport(self.introspect),
            database=PostgresDatabase(TEST_DATABASE_URL),
        )
        self.addCleanup(app.state.close)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://agent.example.test")
        self.addAsyncCleanup(client.aclose)
        return app, client

    async def test_concurrent_duplicates_across_independent_pools(self):
        self.block_model()
        other_app, other_http = self.second_app()
        body = {"message": {"messageId": "race", "contextId": "race", "role": "ROLE_USER", "parts": [{"text": "Answer briefly"}]}, "configuration": {"returnImmediately": True}}
        requests = [client.post("/a2a/external/message:send", headers=self.headers("external-a"), json=body) for client in (self.http, other_http, self.http, other_http)]
        responses = await asyncio.gather(*requests)
        for response in responses:
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len({response.json()["task"]["id"] for response in responses}), 1)
        self.release.set()
        async with asyncio.timeout(5):
            while any(app.state.a2a_request_handler.agent_executor._active_executions for app in (self.app, other_app)):
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        self.assertEqual(self.model_entries, 1)
        self.assertEqual(len(self.model.calls), 1)
        task_id = responses[0].json()["task"]["id"]
        self.assertEqual(self.app.state.core_agent.workflow_store.lookup_task(task_id).state, "COMPLETED")
        with self.app.state.database.pool.connection() as connection:
            count = connection.execute("SELECT count(*) AS n FROM core_root_messages WHERE tenant_id = %s", (self.context().tenant,)).fetchone()["n"]
        self.assertEqual(count, 1)

    async def test_secondary_app_preserves_the_blocked_model_contract(self):
        started, release = self.block_model()
        other_app, other_http = self.second_app()
        response = await other_http.post(
            "/a2a/external/message:send", headers=self.headers("external-a"),
            json={"message": {"messageId": "second-process", "role": "ROLE_USER", "parts": [{"text": "Answer briefly"}]}, "configuration": {"returnImmediately": True}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        release.set()
        async with asyncio.timeout(5):
            while other_app.state.a2a_request_handler.agent_executor._active_executions:
                await asyncio.sleep(0.01)
        task_id = response.json()["task"]["id"]
        record = other_app.state.core_agent.workflow_store.lookup_task(task_id)
        try:
            self.assertEqual(record.state, "COMPLETED")
            self.assertEqual(len(self.model.calls), 1)
        finally:
            if record.state == "RUNNING":
                # Keep a failed regression probe from contaminating later recovery tests.
                other_app.state.core_agent.workflow_store.transition(
                    record.run_id, tenant_id=record.tenant_id, owner_id=record.owner_id,
                    expected_version=record.version, state="FAILED", snapshot=record.snapshot,
                    event_kind="test.finished", error_code="TEST_FINISHED",
                )

    async def test_shutdown_after_admission_preserves_task_for_restart_recovery(self):
        handler = self.app.state.a2a_request_handler
        agent = self.app.state.core_agent
        original_admit = handler.admission_handler
        accepted = []

        async def admit_then_stop(message, context):
            result = await original_admit(message, context)
            accepted.append(result)
            agent._closed.set()
            return result

        with patch.object(handler, "admission_handler", side_effect=admit_then_stop):
            response = await self.send("shutdown", "shutdown-chat")
        self.assertEqual(response.status_code, 200, response.text)
        task = response.json()["task"]
        record = agent.workflow_store.lookup_task(task["id"])
        self.assertEqual(record.state, "RUNNING")
        self.assertEqual(len(self.model.calls), 0)
        with self.app.state.database.transaction() as connection:
            connection.execute("UPDATE core_runs SET lease_expires_at = 0 WHERE run_id = %s", (record.run_id,))
        self.app.state.close()
        self.app, self.http = self.second_app()
        async with asyncio.timeout(5):
            while self.app.state.core_agent.workflow_store.lookup_task(task["id"]).state != "COMPLETED":
                await asyncio.sleep(0.01)
        self.assertIn(task["status"]["state"], ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"))
        async with asyncio.timeout(5):
            while True:
                recovered = await self.http.get(f'/a2a/external/tasks/{task["id"]}', headers=self.headers("external-a"))
                if recovered.json()["status"]["state"] == "TASK_STATE_COMPLETED":
                    break
                await asyncio.sleep(0.01)
        self.assertEqual(self.app.state.core_agent.workflow_store.lookup_task(task["id"]).run_id, accepted[0].run_id)
        self.assertEqual(len(recovered.json()["history"]), 1)
        self.assertEqual(len(self.model.calls), 1)

    async def test_completed_and_busy_duplicates_survive_restart(self):
        started, _release = self.block_model()
        first = (await self.send("first", "restart", immediate=True)).json()["task"]
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        busy = (await self.send("busy", "restart")).json()["task"]
        await self.finish_workers()
        self.app.state.close()
        self.app, self.http = self.second_app()
        for message_id, expected in (("first", first), ("busy", busy)):
            response = await self.send(message_id, "restart", token="external-a-replaced")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["task"]["id"], expected["id"])
            self.assertEqual(len(response.json()["task"]["history"]), 1)
        self.assertEqual(len(self.model.calls), 1)

    async def test_failure_inside_admission_rolls_back_all_writes_and_local_scope(self):
        handler = self.app.state.a2a_request_handler
        store = handler.task_store.inner
        save = store._save
        agent = self.app.state.core_agent
        scopes = dict(agent._run_scopes)
        tenant = self.context().tenant

        def save_then_fail(*args, **kwargs):
            save(*args, **kwargs)
            raise InvalidParamsError("Injected before commit")

        with patch.object(store, "_save", side_effect=save_then_fail):
            result = await self.send("rollback", "rollback-chat")
        self.assertEqual(result.status_code, 400, result.text)
        with self.app.state.database.pool.connection() as connection:
            for table, field in (("core_chats", "tenant_id"), ("core_root_messages", "tenant_id"), ("core_a2a_tasks", "tenant"), ("core_runs", "tenant_id"), ("core_budget_ledgers", "tenant_id"), ("core_events", "tenant_id"), ("core_checkpoints", "tenant_id"), ("core_audit_records", "tenant_id"), ("core_outbox", "tenant_id")):
                count = connection.execute(f"SELECT count(*) AS n FROM {table} WHERE {field} = %s", (tenant,)).fetchone()["n"]
                self.assertEqual(count, 0, table)
        self.assertEqual(agent._run_scopes, scopes)
        retry = await self.send("rollback", "rollback-chat")
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(len(self.model.calls), 1)

    async def test_commit_before_sdk_start_recovers_same_root_after_restart(self):
        handler = self.app.state.a2a_request_handler
        accepted = await handler.admission_handler(self.sdk_message("crash", "crash-chat"), self.context())
        original = self.app.state.core_agent.workflow_store.lookup_task(accepted.task.id)
        self.assertEqual(len(accepted.task.history), 1)
        self.assertEqual(len(self.model.calls), 0)
        database = self.app.state.database
        with database.transaction() as connection:
            connection.execute("UPDATE core_runs SET lease_expires_at = 0 WHERE run_id = %s", (original.run_id,))
        self.app.state.close()
        self.app, self.http = self.second_app()
        async with asyncio.timeout(5):
            while True:
                response = await self.http.get(f"/a2a/external/tasks/{accepted.task.id}", headers=self.headers("external-a"))
                task = response.json()
                if task["status"]["state"] == "TASK_STATE_COMPLETED":
                    break
                await asyncio.sleep(0.02)
        record = self.app.state.core_agent.workflow_store.lookup_task(accepted.task.id)
        self.assertEqual(record.run_id, original.run_id)
        self.assertEqual(len(task["history"]), 1)
        self.assertEqual(task["history"][0]["messageId"], "crash")
        repeated = await self.send("crash", "crash-chat")
        self.assertEqual(repeated.json()["task"]["id"], accepted.task.id)
        self.assertEqual(len(self.model.calls), 1)
        with self.app.state.database.pool.connection() as connection:
            row = connection.execute("SELECT latest_root_run_id FROM core_chats WHERE tenant_id = %s AND context_id = %s", (record.tenant_id, record.context_id)).fetchone()
        self.assertEqual(row["latest_root_run_id"], original.run_id)

    async def test_disconnect_after_initial_stream_frame_keeps_committed_workflow(self):
        handler = self.app.state.a2a_request_handler
        context = self.context()
        params = SendMessageRequest(message=self.sdk_message("disconnect", "disconnect-chat"))
        stream = handler.on_message_send_stream(params, context)
        initial = await anext(stream)
        await stream.aclose()
        self.assertEqual(initial.status.state, TaskState.TASK_STATE_SUBMITTED)
        record = self.app.state.core_agent.workflow_store.lookup_task(initial.id)
        with self.app.state.database.pool.connection() as connection:
            lease = connection.execute("SELECT lease_token FROM core_runs WHERE run_id = %s", (record.run_id,)).fetchone()["lease_token"]
        self.assertEqual(lease, context.state["initial_admission"].lease_token)
        self.assertEqual(len(self.model.calls), 0)
        with self.app.state.database.transaction() as connection:
            connection.execute("UPDATE core_runs SET lease_expires_at = 0 WHERE run_id = %s", (record.run_id,))
        await asyncio.to_thread(self.app.state.core_agent.resume_task, initial.id)
        self.assertEqual(len(self.model.calls), 1)
