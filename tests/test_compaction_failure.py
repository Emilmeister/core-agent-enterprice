import asyncio
import copy
import io
import json
import os
import tempfile
import threading
import unittest
import uuid
from dataclasses import replace
from unittest.mock import patch

from core_agent.errors import CoreError
from core_agent.model import ModelResponse
from tests import test_runtime_observability as fixtures
from tests.test_runtime_observability import run_request


class CompactionFailureTests(unittest.TestCase):
    semantic_agent = fixtures.SemanticRuntimeTests.semantic_agent
    semantic_answer = staticmethod(fixtures.SemanticRuntimeTests.semantic_answer)

    def pressure_agent(self, *, max_turns=10, summary_error=None):
        summaries = []
        def invalid_summary(call):
            summaries.append(call)
            if len(summaries) == 2 and summary_error is not None:
                raise summary_error()
            return ModelResponse(message="PRIVATE invalid summary", finish_reason="stop")
        agent, model, connector = self.semantic_agent(invalid_summary, context_window=50000, max_turns=max_turns)
        model._responses[0] = replace(model._responses[0], reasoning_replay={"reasoning_content": "PRIVATE reasoning " * 12000})
        return agent, model, connector, summaries

    def assert_settled(self, agent, task_id, summaries, *, failure_count=2):
        record = agent.workflow_store.lookup_task(task_id)
        self.assertEqual(record.state, "FAILED")
        self.assertEqual(record.error_code, "CONTEXT_UNRECOVERABLE")
        self.assertEqual(record.snapshot["compaction_operation"]["attempts"], 2)
        self.assertEqual(len(summaries), 2)
        self.assertTrue(record.snapshot["execution_owner"]["cleanup_confirmed"])
        for entries in (agent._runtime_cache, agent._run_contexts, agent._run_scopes,
                        agent._runtime_generations, agent._run_mcp_connectors):
            self.assertNotIn(record.run_id, entries)
        self.assertTrue(any("confirmed result" in item["content"] for item in record.snapshot["context"]["transcript"]))
        events = agent.event_store.events(record.run_id)
        failures = [e for e in events if e.kind == "context.compaction.failed"]
        self.assertEqual([e.data["reason"] for e in failures], ["invalid_json"] * failure_count)
        self.assertNotIn("PRIVATE", str([e.data for e in failures]))
        before = copy.deepcopy(record.snapshot["context"])
        agent._recover_workflows_once()
        self.assertEqual(agent.workflow_store.lookup_task(task_id).snapshot["context"], before)
        self.assertEqual(len(summaries), 2)
        return record

    def test_two_failed_pressure_summaries_settle_once_without_tool_replay(self):
        agent, _, connector, summaries = self.pressure_agent()
        with patch.object(connector, "call", wraps=connector.call) as call:
            with self.assertRaises(CoreError) as caught:
                agent.run(run_request(), task_id="pressure-failed")
            self.assertEqual(caught.exception.code, "CONTEXT_UNRECOVERABLE")
            self.assert_settled(agent, "pressure-failed", summaries)
            self.assertEqual(call.call_count, 1)

    def test_recovery_of_exhausted_checkpoint_settles_without_more_model_or_tool_calls(self):
        self.recover_exhausted()

    def test_queued_followup_does_not_restart_exhausted_compaction(self):
        self.recover_exhausted(followup=True)

    def test_exhausted_turn_budget_does_not_complete_failed_compaction(self):
        self.recover_exhausted(max_turns=4)

    def test_unknown_second_summary_with_followup_does_not_reset_attempts(self):
        self.recover_exhausted(followup=True, unknown=True)

    def recover_exhausted(self, *, followup=False, max_turns=10, unknown=False):
        class Crash(BaseException):
            pass
        agent, model, connector, summaries = self.pressure_agent(max_turns=max_turns, summary_error=Crash if unknown else None)
        transition = agent._record_transition
        def crash_after_second_failure(*args, **kwargs):
            record = transition(*args, **kwargs)
            if kwargs["event_kind"] == "context.compaction.failed" and kwargs["event_data"]["attempt"] == 2:
                raise Crash()
            return record
        with patch.object(connector, "call", wraps=connector.call) as call:
            with patch.object(agent, "_record_transition", side_effect=crash_after_second_failure):
                with self.assertRaises(Crash):
                    agent.run(run_request(), task_id="pressure-recovery")
            before = agent.workflow_store.lookup_task("pressure-recovery")
            self.assertEqual(before.state, "RUNNING")
            context = copy.deepcopy(before.snapshot["context"])
            model_calls = len(model.calls)
            agent._runtime_cache.clear()
            if followup:
                agent.enqueue_message({"prompt": "accepted correction"}, task_id=before.task_id,
                    message_id="followup", session_id=before.context_id)
            agent._resume_recovered_workflow(before, threading.Event())
            after = self.assert_settled(agent, "pressure-recovery", summaries, failure_count=1 if unknown else 2)
            if followup:
                transcript = after.snapshot["context"]["transcript"]
                self.assertEqual(transcript[:len(context["transcript"])], context["transcript"])
                self.assertEqual(transcript[-1]["kind"], "unprocessed_due_to_failure")
                self.assertIn("accepted correction", transcript[-1]["content"])
            else:
                self.assertEqual(after.snapshot["context"], context)
            self.assertEqual(len(model.calls), model_calls)
            self.assertEqual(call.call_count, 1)

    def test_failed_cleanup_preserves_intent_and_only_retries_cleanup(self):
        agent, model, connector, summaries = self.pressure_agent()
        manager = agent.tool_runtime.environment_manager
        with patch.object(manager, "close_execution_tree", side_effect=RuntimeError("cleanup unavailable")):
            agent.run(run_request(), task_id="cleanup-failed")
        pending = agent.workflow_store.lookup_task("cleanup-failed")
        self.assertNotEqual(pending.state, "FAILED")
        self.assertEqual(pending.snapshot["terminal_intent"]["error_code"], "CONTEXT_UNRECOVERABLE")
        calls = len(model.calls)
        agent._resume_recovered_workflow(pending, threading.Event())
        self.assert_settled(agent, "cleanup-failed", summaries)
        self.assertEqual(len(model.calls), calls)


class SummaryDiagnosticsTests(unittest.TestCase):
    def test_validation_reasons_never_expose_response_content(self):
        from core_agent.context import StructuredSummarizer, SUMMARY_SECTIONS, CompactionError
        valid = {key: [] for key in SUMMARY_SECTIONS}
        cases = [
            ("PRIVATE broken", "stop", "invalid_json"),
            (json.dumps(valid), "length", "output_truncated"),
            ('{"Goal":[],"Goal":[]}', "stop", "duplicate_json_key"),
            ('{"PRIVATE":[]}', "stop", "invalid_sections"),
            (json.dumps({**valid, "Goal": "PRIVATE"}), "stop", "invalid_entries"),
            (json.dumps({**valid, "Goal": [{"text": "PRIVATE", "basis": [], "sources": ["x"]}]}), "stop", "invalid_entry"),
            (json.dumps({**valid, "Goal": [{"text": "PRIVATE", "basis": "fact", "sources": ["x"]}]}), "stop", "invalid_sources"),
        ]
        for message, finish, reason in cases:
            with self.subTest(reason=reason):
                summarizer = StructuredSummarizer(len, lambda **_: ModelResponse(message=message, finish_reason=finish))
                with self.assertRaises(CompactionError) as caught:
                    summarizer((), 1000)
                self.assertEqual(caught.exception.reason, reason)
                self.assertEqual(caught.exception.data, {})
                self.assertNotIn("PRIVATE", str(caught.exception))


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is required")
class PostgresCompactionFailureTests(CompactionFailureTests):
    def pressure_agent(self, *, max_turns=10, summary_error=None):
        from core_agent.database import PostgresDatabase, PostgresEventStore, PostgresCheckpointStore, PostgresAuditLog
        from core_agent.workflow import PostgresWorkflowStore
        from tests import test_postgres_persistence as postgres_fixtures
        database = PostgresDatabase(os.environ["TEST_DATABASE_URL"], min_size=0, max_size=3)
        self.addCleanup(database.close)
        database.migrate()
        postgres_fixtures.PostgresRestartTests._reset(self, database)
        agent, model, connector, summaries = super().pressure_agent(max_turns=max_turns, summary_error=summary_error)
        agent.workflow_store = PostgresWorkflowStore(database)
        agent.event_store = PostgresEventStore(database)
        agent.checkpoint_store = PostgresCheckpointStore(database)
        agent.audit_log = PostgresAuditLog(database)
        return agent, model, connector, summaries


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is required")
class PostgresOwnerAnswerCompactionTests(unittest.TestCase):
    def test_two_answered_questions_survive_pressure_compaction_and_restart_on_model_wire(self):
        """Prove runtime dataflow with valid summaries, not model retention quality."""
        import psycopg
        from psycopg.conninfo import make_conninfo
        from psycopg.sql import Identifier, SQL
        from a2a.types import Message, Role
        from core_agent.auth import AuthenticatedCallContext, Principal, ScopeUser
        from core_agent.context import SUMMARY_SECTIONS
        from core_agent.database import PostgresDatabase
        from core_agent.model import CompatibleHttpModel
        from core_agent.runtime import CURRENT_TASK_SUMMARY_PREFIX
        from tests.app_support import create_app

        class ProcessStopped(BaseException):
            pass

        summary_inputs, normal_wires, summaries, rendered_summaries = [], [], [], []
        final = {"route": "ROUTE_C", "artifact": "artifact-retention",
                 "amount": "427.50 EUR", "release": "PENDING; NOT RELEASED"}
        questions = [{"question": f"Ready for batch {index}?", "options": [f"GO{index}"]}
                     for index in (1, 2)]

        class Model(CompatibleHttpModel):
            def _open(inner, body, headers):
                if "response_format" in body:
                    self.assertNotIn("tools", body)
                    data = json.loads(body["messages"][-1]["content"])
                    summary_inputs.append(data)
                    index = len(summary_inputs)
                    records = json.dumps(data["records"])
                    answered = [json.loads(item["content"]) for item in data["records"] if item["kind"] == "tool_result"]
                    answered = [item for item in answered if item["tool_call_id"] == f"ask-{index}"]
                    self.assertEqual(answered, [{"tool_call_id": f"ask-{index}", "tool_name": "core_ask_owner",
                                                "status": "succeeded", "output": {"answer": f"GO{index}"}}])
                    calls = [call for item in data["records"] if item["kind"] == "assistant_tool_calls"
                             for call in json.loads(item["content"]) if call["id"] == f"ask-{index}"]
                    self.assertEqual(calls, [{"id": f"ask-{index}", "function": {
                        "name": "core_ask_owner", "arguments": questions[index - 1]}}])
                    self.assertTrue(f"ROUTE_{'B' if index == 1 else 'C'}" in records, f"summary {index} lost latest route")
                    if index == 2:
                        self.assertTrue("Question 1 answered GO1" in records,
                                        "second summary input lost the completed first question")
                    sources = list(dict.fromkeys(source for item in data["records"]
                                                 for source in item["sources"]))
                    value = {section: [] for section in SUMMARY_SECTIONS}
                    facts = {
                        "Decisions": f"Latest route ROUTE_{'B' if index == 1 else 'C'}; amount 427.50 EUR.",
                        "Completed": "; ".join(f"Question {i} answered GO{i}; batch {i} received" for i in range(1, index + 1)),
                        "Artifacts": "artifact-retention",
                        "Pending": "Ask question 2" if index == 1 else "Both questions answered; return final JSON. Release PENDING; NOT RELEASED.",
                    }
                    for section, text in facts.items():
                        evidence = ([source for source in sources if source.rsplit(":", 1)[1] in {"3", "4", "6", "7"}]
                                    if section == "Completed" else sources[-1:])
                        value[section] = [{"text": text, "basis": "fact", "sources": evidence}]
                    content = json.dumps(value, separators=(",", ":"))
                    summaries.append(content)
                    message = {"role": "assistant", "content": content}
                else:
                    normal_wires.append(copy.deepcopy(body))
                    index = len(normal_wires)
                    if index > 1:
                        self.assertTrue(any(message.get("content") == rendered_summaries[index - 2]
                                            for message in body["messages"]))
                    if index < 3:
                        message = {"role": "assistant", "content": None, "tool_calls": [{
                            "id": f"ask-{index}", "type": "function", "function": {
                                "name": "core_ask_owner", "arguments": json.dumps(questions[index - 1])}}]}
                    else:
                        message = {"role": "assistant", "content": json.dumps(final)}
                return io.BytesIO(json.dumps({"choices": [{"message": message,
                    "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}]}).encode())

        url = os.environ["TEST_DATABASE_URL"]
        schema = "owner_compaction_" + uuid.uuid4().hex
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(SQL("CREATE SCHEMA {}").format(Identifier(schema)))
        def remove_schema():
            with psycopg.connect(url, autocommit=True) as connection:
                connection.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(schema)))
        self.addCleanup(remove_schema)
        dsn = make_conninfo(url, options="-c search_path=" + schema)
        model = Model(api_format="openai", model="local-retention", base_url="https://model.test/v1")
        prompt = ("Ask exactly two owner questions, in order: " + json.dumps(questions)
                  + ". After each answer read its batch. Batch 2 supersedes batch 1. Return latest route, "
                  "artifact, amount, release as JSON. Do not repeat answered questions. ")
        prompt += "Inert archive padding. " * ((5409 - len(prompt)) // 22 + 1)
        prompt = prompt[:5409]
        batches = [(f"Batch {index}: latest ROUTE_{route}; artifact-retention; 427.50 EUR; "
                    "release PENDING; NOT RELEASED. " + "Inert archive padding. " * 1800)[:39300]
                   for index, route in ((1, "B"), (2, "C"))]
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
            "CORE_AGENT_ENVIRONMENT": "development", "CORE_AGENT_MEMORY": "disabled",
            "SESSION_STORAGE_TYPE": "postgres", "TASK_STORAGE_TYPE": "postgres",
            "DATABASE_AUTO_MIGRATE": "false", "DURABLE_STORAGE_ROOT": root + "/blobs",
            "LOCAL_WORKSPACE_ROOT": root + "/workspaces", "CORE_AGENT_ALLOWED_BUILTIN_TOOLS": "core_ask_owner",
            "CHAT_WORKSPACE_ROOT": root + "/chats", "CORE_AGENT_TENANT_ID": "tenant",
            "KEYCLOAK_ISSUER_URL": "https://issuer.test/realms/test", "KEYCLOAK_CLIENT_ID": "test-agent",
            "KEYCLOAK_CLIENT_SECRET": "test-only-secret", "KEYCLOAK_AUDIENCE": "test-agent",
            "CORE_AGENT_ALLOWED_SKILLS": "", "LLM_CONTEXT_WINDOW": "20480", "LLM_MAX_TOKENS": "4096",
            "EVENTS_COMPACTION_INTERVAL": "0", "LOG_LEVEL": "ERROR",
        }, clear=True), patch("core_agent.runtime.CoreAgent.recover_workflows", return_value=()):
            def reopen():
                database = PostgresDatabase(dsn, min_size=0, max_size=3)
                self.addCleanup(database.close)
                database.migrate()
                app = create_app(model=model, database=database)
                self.addCleanup(app.state.close)
                store = app.state.core_agent.interaction_store
                policy = store.get_policy("tenant", "core_ask_owner", "builtin:core_ask_owner")
                if policy.mode != "allow":
                    store.update_policy("tenant", "core_ask_owner", "builtin:core_ask_owner", mode="allow",
                                        guardrails_exempt=False, expected_revision=policy.revision, actor_id="test-owner")
                return app, app.state.core_agent

            app, agent = reopen()
            actor = Principal("owner", "tenant", True, False)
            call_context = AuthenticatedCallContext(user=ScopeUser(actor.owner_id), tenant="tenant",
                                                    state={"principal": actor})
            admitted = asyncio.run(app.state.a2a_request_handler.admission_handler(
                Message(message_id="retention-input", context_id="retention-chat", role=Role.ROLE_USER,
                        parts=[{"text": prompt}]), call_context))
            task_id = admitted.task.id
            agent.workflow_store.release_lease(admitted.run_id, tenant_id="tenant",
                                               worker_id=agent._worker_id, token=admitted.lease_token)
            agent.resume_task(task_id)
            instructions = agent.workflow_store.lookup_task(task_id).snapshot["compiled_instructions"]
            transcripts = []
            for index, batch in enumerate(batches, 1):
                record = agent.workflow_store.lookup_task(task_id)
                self.assertEqual(record.state, "WAITING_INPUT")
                wait = agent.workflow_store.get_wait(record.snapshot["wait_id"], tenant_id="tenant")
                self.assertEqual(wait.subject, questions[index - 1])
                agent.enqueue_message({"prompt": batch}, task_id=record.task_id, message_id=f"batch-{index}",
                                      identity=actor.owner_id, session_id=record.context_id, tenant_id="tenant")
                agent.workflow_store.resolve_wait(wait.wait_id, tenant_id="tenant",
                                                  outcome={"reason": "answer", "answer": f"GO{index}"})
                compact = agent._compact_context
                def stop_after_commit(*args, **kwargs):
                    self.assertEqual(args[2].budget.system_and_kernel_tokens,
                                     agent.token_counter(agent._request_instructions(instructions))
                                     + agent.token_counter(CURRENT_TASK_SUMMARY_PREFIX))
                    saved = compact(*args, **kwargs)
                    if len(summaries) == index:
                        raise ProcessStopped()
                    return saved
                with patch.object(agent, "_compact_context", side_effect=stop_after_commit):
                    with self.assertRaises(ProcessStopped):
                        agent.resume_task(record.task_id)
                saved = agent.workflow_store.lookup_task(record.task_id)
                context = copy.deepcopy(saved.snapshot["context"])
                transcripts.append(context["transcript"])
                summary = next(item for item in context["active"] if item["kind"] == "summary")
                self.assertEqual(json.loads(summary["content"]), json.loads(summaries[-1]))
                rendered_summaries.append(agent._model_content(next(
                    item for item in agent._context_from_dict(context).active if item.kind == "summary")))
                self.assertEqual(next(item["content"] for item in context["active"] if item["kind"] == "prompt"), prompt)
                self.assertEqual(len(context["transcript"]), 1 + 3 * index)
                self.assertEqual(set(summary["provenance"]["sources"]),
                                 {f"{record.run_id}:{sequence}" for sequence in range(1, 2 + 3 * index)})
                app.state.close()
                app, agent = reopen()
                restored = agent.workflow_store.lookup_task(record.task_id).snapshot
                self.assertEqual(restored["context"], context)
                self.assertEqual(restored["compiled_instructions"], instructions)
                result = agent.resume_task(record.task_id)
                self.assertEqual(len(summaries), index, "Restart must reuse the committed summary")
            self.assertEqual(json.loads(result.message), final)
            self.assertEqual((result.usage.model_turns, result.usage.tool_calls), (5, 2))
            self.assertEqual(transcripts[1][:len(transcripts[0])], transcripts[0])
            record = agent.workflow_store.lookup_task(task_id)
            events = [event.data for event in agent.event_store.events(record.run_id, tenant_id="tenant")
                      if event.kind == "context.compacted"]
            self.assertEqual([event["replaced_sequence_range"] for event in events], [[1, 4], [1, 7]])
            self.assertTrue(all(event["before"] >= 0.90 * event["working_capacity"] for event in events))
            self.assertEqual(len(normal_wires), 3)
            self.assertTrue(all(wire["messages"][0]["content"].startswith(instructions + "\n\n")
                                for wire in normal_wires))
