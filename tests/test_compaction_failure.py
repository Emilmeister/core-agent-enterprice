import copy
import json
import os
import threading
import unittest
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
