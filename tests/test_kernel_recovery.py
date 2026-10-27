import copy
import os
import unittest
import uuid
from unittest.mock import patch

from core_agent.errors import CoreError
from core_agent.kernel import KernelCompiler
from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
from core_agent.tools import ToolDefinition
from tests.test_runtime_observability import make_agent, run_request


class StreamingScriptedModel(ScriptedModel):
    def generate(self, *, context, tools, instructions, messages=None, on_delta=None):
        response = super().generate(context=context, tools=tools, instructions=instructions, messages=messages)
        if on_delta and response.message:
            on_delta(response.message, None)
        return response


def kernel(version):
    compiler = KernelCompiler(f"SAFETY {version}", f"HOST {version}", f"KERNEL {version}",
                              {"memory": f"MEMORY {version}"})
    return compiler


class KernelRecoveryTests(unittest.TestCase):
    def admit(self, *, deferred=False, store=None):
        first = make_agent(StreamingScriptedModel([]), kernel_compiler=kernel("V1"),
                           workflow_store=store)
        self.addCleanup(first.close)
        record, *_ = first._new_workflow(run_request(), task_id="kernel-task", identity="anonymous",
                                        session_id="kernel-chat", tenant_id="default",
                                        defer_initialization=deferred)
        first.close()
        return first, record

    def test_upgrade_uses_admitted_kernel_for_generation_and_budget(self):
        for deferred in (False, True):
            with self.subTest(deferred=deferred):
                first, record = self.admit(deferred=deferred)
                model = StreamingScriptedModel([
                    ModelResponse(message="done"),
                    ModelResponse(message="new task"),
                ])
                second = make_agent(model, workflow_store=first.workflow_store,
                                    kernel_compiler=kernel("V2"))
                self.addCleanup(second.close)
                self.assertEqual(second.resume_task(record.task_id).message, "done")
                for call in model.calls:
                    self.assertIn("KERNEL V1", call.instructions)
                    self.assertNotIn("core_response_begin", call.instructions)
                    self.assertNotIn("V2", call.instructions)
                self.assertTrue(model.calls[0].tools)
                resumed = first.workflow_store.lookup_task(record.task_id)
                self.assertEqual(resumed.snapshot["kernel_instructions"], record.snapshot["kernel_instructions"])
                raw, discovered, effective = second._resolve_capabilities(run_request())
                budget = second._context_compactor(raw, effective, discovered, resumed.snapshot).budget
                frozen = second._compile_instructions(raw, effective, resumed.snapshot, model_tools=model.calls[0].tools).text
                from core_agent.runtime import CURRENT_TASK_SUMMARY_PREFIX
                self.assertEqual(budget.system_and_kernel_tokens,
                    second.token_counter(second._request_instructions(frozen))
                    + second.token_counter(CURRENT_TASK_SUMMARY_PREFIX))
                second.run(run_request(), task_id="new-kernel-task", session_id="new-chat")
                self.assertIn("KERNEL V2", model.calls[-1].instructions)
                second.close()

    def test_legacy_match_migrates_but_mismatch_and_uninitialized_fail_before_reconnect(self):
        for version, deferred in (("V1", False), ("V2", False), ("V1", True)):
            with self.subTest(version=version, deferred=deferred):
                first, record = self.admit(deferred=deferred)
                snapshot = copy.deepcopy(record.snapshot)
                snapshot.pop("kernel_instructions", None)
                record = first._record_transition(record, state=record.state, snapshot=snapshot,
                                                 event_kind="test.legacy-kernel")
                model = StreamingScriptedModel([ModelResponse(message="done")])
                second = make_agent(model, workflow_store=first.workflow_store,
                                    kernel_compiler=kernel(version))
                self.addCleanup(second.close)
                if version == "V1" and not deferred:
                    second.platform_config.denied_builtin_tools.add("core_memory_search")
                    self.assertEqual(second.resume_task(record.task_id).message, "done")
                    self.assertIn("kernel_instructions", first.workflow_store.lookup_task(record.task_id).snapshot)
                    self.assertNotIn("MEMORY V1", model.calls[0].instructions)
                    self.assertNotIn("core_memory_search", model.calls[0].tools)
                else:
                    with self.assertRaises(CoreError) as error:
                        second.resume_task(record.task_id)
                    self.assertEqual(error.exception.code, "CHECKPOINT_INVALID")
                    self.assertFalse(model.calls)
                    self.assertEqual(second.mcp_connector.connections, ())
                    after = first.workflow_store.lookup_task(record.task_id).snapshot
                    for name in ("compiled_instructions", "protected_kernel_digest", "context"):
                        self.assertEqual(after.get(name), snapshot.get(name))
                second.close()

    def test_legacy_phase_instructions_are_validated_but_not_sent_after_upgrade(self):
        for pinned in (False, True):
            for phase in ("work", "answer"):
                with self.subTest(pinned=pinned, phase=phase):
                    first, record = self.admit()
                    snapshot = copy.deepcopy(record.snapshot)
                    snapshot["kernel_instructions"] = kernel("V1").snapshot(legacy=True)
                    snapshot["response_phase"] = phase
                    raw, _discovered, effective = first._resolve_capabilities(run_request())
                    compiled = first._compile_instructions(raw, effective, snapshot, legacy_response_phase=phase)
                    snapshot["compiled_instructions"] = compiled.text
                    snapshot["protected_kernel_digest"] = compiled.protected_digest
                    if not pinned:
                        del snapshot["kernel_instructions"]
                    first._record_transition(record, state=record.state, snapshot=snapshot,
                                             event_kind="test.legacy-phase")
                    model = ScriptedModel([ModelResponse(message="done")])
                    second = make_agent(model, workflow_store=first.workflow_store,
                                        kernel_compiler=kernel("V2" if pinned else "V1"))
                    self.addCleanup(second.close)
                    result = second.resume_task(record.task_id)
                    self.assertTrue(result.complete)
                    self.assertEqual(result.usage.model_turns, 1)
                    self.assertEqual(result.usage.tool_calls, 0)
                    self.assertIn("KERNEL V1", model.calls[0].instructions)
                    self.assertNotIn("KERNEL V2", model.calls[0].instructions)
                    self.assertNotIn("core_response_begin", model.calls[0].instructions)
                    self.assertNotIn("public answer phase", model.calls[0].instructions)
                    self.assertTrue(model.calls[0].tools)
                    second.close()

    def test_old_kernel_snapshot_requires_exact_shape_and_digest(self):
        legacy = kernel("V1").snapshot(legacy=True)
        self.assertEqual(KernelCompiler.from_snapshot(legacy).snapshot(legacy=True), legacy)
        variants = []
        changed = copy.deepcopy(legacy)
        changed["response_phase_instructions"]["answer"] = "changed"
        variants.append(changed)
        extra = {**legacy, "unexpected": "field"}
        variants.append(extra)
        missing = copy.deepcopy(legacy)
        del missing["response_phase_instructions"]["work_v1"]
        variants.append(missing)
        for snapshot in variants:
            with self.subTest(snapshot=snapshot), self.assertRaises(CoreError) as error:
                KernelCompiler.from_snapshot(snapshot)
            self.assertEqual(error.exception.code, "CHECKPOINT_INVALID")

    def test_legacy_answer_phase_keeps_new_tool_batch_when_input_arrives(self):
        for source in ("followup", "notification"):
            with self.subTest(source=source):
                first, record = self.admit()
                snapshot = copy.deepcopy(record.snapshot)
                snapshot["kernel_instructions"] = kernel("V1").snapshot(legacy=True)
                snapshot["response_phase"] = "answer"
                first._record_transition(record, state=record.state, snapshot=snapshot,
                                         event_kind="test.legacy-answer")

                class InputModel(ScriptedModel):
                    def generate(inner, **kwargs):
                        response = super().generate(**kwargs)
                        if len(inner.calls) == 1:
                            if source == "followup":
                                second.enqueue_message(run_request(), task_id=record.task_id,
                                    message_id="late-input", identity=record.owner_id,
                                    session_id=record.context_id, tenant_id=record.tenant_id)
                            else:
                                task = second.task_scheduler.start(lambda: {"message": "child done"},
                                    owner_id=record.run_id, tenant_id=record.tenant_id,
                                    required=False, kind="test", contract={})
                                second.task_scheduler.wait(task.id, timeout=2, owner_id=record.run_id,
                                                           tenant_id=record.tenant_id)
                        return response

                model = InputModel([
                    ModelResponse(tool_requests=tuple(ToolRequest(name, "core_terminal_exec", {"argv": [name]})
                                                      for name in ("first", "second"))),
                    ModelResponse(message="done"),
                ])
                second = make_agent(model, workflow_store=first.workflow_store, kernel_compiler=kernel("V2"))
                self.addCleanup(second.close)
                result = second.resume_task(record.task_id)
                self.assertTrue(result.complete)
                self.assertEqual(result.usage.tool_calls, 2)
                self.assertEqual(second.tool_runtime.execution_count, 2)
                self.assertEqual([message["tool_call_id"] for message in model.calls[-1].messages
                                  if message["role"] == "tool"], ["first", "second"])
                second.close()

    def test_legacy_saved_final_is_reconsidered_after_late_input(self):
        first, record = self.admit()
        snapshot = copy.deepcopy(record.snapshot)
        snapshot["kernel_instructions"] = kernel("V1").snapshot(legacy=True)
        snapshot["response_phase"] = "answer"
        snapshot["pending_response"] = first._response_dict(ModelResponse(message="old answer"))
        first._record_transition(record, state="MODEL_RESPONDED", snapshot=snapshot,
                                 event_kind="test.legacy-final")
        model = ScriptedModel([ModelResponse(message="updated answer")])
        second = make_agent(model, workflow_store=first.workflow_store, kernel_compiler=kernel("V2"))
        self.addCleanup(second.close)
        second.enqueue_message(run_request(), task_id=record.task_id, message_id="late-input",
            identity=record.owner_id, session_id=record.context_id, tenant_id=record.tenant_id)
        result = second.resume_task(record.task_id)
        self.assertTrue(result.complete)
        self.assertEqual(result.message, "updated answer")
        self.assertEqual(len(model.calls), 1)

    def test_corrupt_kernel_snapshot_fails_before_reconnect(self):
        first, record = self.admit()
        snapshot = copy.deepcopy(record.snapshot)
        snapshot["kernel_instructions"] = {"version": 1, "base_kernel": "tampered"}
        first._record_transition(record, state=record.state, snapshot=snapshot, event_kind="test.corrupt-kernel")
        model = ScriptedModel([ModelResponse(message="unexpected")])
        second = make_agent(model, workflow_store=first.workflow_store, kernel_compiler=kernel("V2"))
        self.addCleanup(second.close)
        with self.assertRaises(CoreError) as error:
            second.resume_task(record.task_id)
        self.assertEqual(error.exception.code, "CHECKPOINT_INVALID")
        self.assertFalse(model.calls)
        self.assertEqual(second.mcp_connector.connections, ())

    def test_invalid_initializing_kernel_is_rejected_before_guardrail_classifier(self):
        first, record = self.admit(deferred=True)
        snapshot = copy.deepcopy(record.snapshot)
        snapshot["kernel_instructions"]["base_kernel"] = "tampered with unchanged digest"
        first._record_transition(record, state=record.state, snapshot=snapshot, event_kind="test.corrupt-kernel")
        model = ScriptedModel([ModelResponse(message="unexpected")])
        second = make_agent(model, workflow_store=first.workflow_store, kernel_compiler=kernel("V2"))
        self.addCleanup(second.close)
        second.material_review_store = object()
        with patch.object(second, "_guard_initial_input", side_effect=AssertionError("classifier reached")) as guard:
            with self.assertRaises(CoreError) as error:
                second.resume_task(record.task_id)
        self.assertEqual(error.exception.code, "CHECKPOINT_INVALID")
        guard.assert_not_called()
        self.assertFalse(model.calls)
        self.assertEqual(second.mcp_connector.connections, ())

    def test_children_and_background_inherit_admitted_kernel_after_upgrade(self):
        first, parent = self.admit()

        class BackgroundModel(StreamingScriptedModel):
            def generate(inner, **kwargs):
                if inner.calls:
                    current = second.workflow_store.lookup_task(parent.task_id)
                    second.task_scheduler.wait(current.snapshot["background_admission"]["task_id"], timeout=2)
                return super().generate(**kwargs)

        model = BackgroundModel([
            ModelResponse(tool_requests=(ToolRequest("start", "core_task_start", {
                "tool": "core_terminal_exec", "arguments": {"argv": ["check"]}, "required": True}),)),
            ModelResponse(message="done"),
        ])
        second = make_agent(model, workflow_store=first.workflow_store, kernel_compiler=kernel("V2"))
        self.addCleanup(second.close)
        child, *_ = second._new_workflow(run_request(), task_id="kernel-child", identity="anonymous",
            session_id=parent.context_id, tenant_id=parent.tenant_id, parent_run_id=parent.run_id)
        self.assertEqual(child.snapshot["kernel_instructions"], parent.snapshot["kernel_instructions"])
        self.assertIn("KERNEL V1", child.snapshot["compiled_instructions"])
        second.tool_runtime.registry.register(ToolDefinition(
            "core_task_start", "start background tool", {"type": "object"}, mutating=False,
            risk_tags=frozenset()))
        self.assertEqual(second.resume_task(parent.task_id).message, "done")
        resumed = second.workflow_store.lookup_task(parent.task_id)
        background = second.workflow_store.lookup_task(resumed.snapshot["background_admission"]["task_id"])
        self.assertEqual(background.state, "COMPLETED")
        self.assertEqual(background.snapshot["kernel_instructions"], parent.snapshot["kernel_instructions"])
        self.assertIn("KERNEL V1", background.snapshot["compiled_instructions"])

    @unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is required")
    def test_postgres_reopen_with_new_kernel_keeps_original_instructions(self):
        import psycopg
        from psycopg.conninfo import make_conninfo
        from psycopg.sql import SQL, Identifier
        from core_agent.database import PostgresDatabase
        from core_agent.workflow import PostgresWorkflowStore

        url = os.environ["TEST_DATABASE_URL"]
        schema = "kernel_upgrade_" + uuid.uuid4().hex
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(SQL("CREATE SCHEMA {}").format(Identifier(schema)))
        def drop_schema():
            with psycopg.connect(url, autocommit=True) as connection:
                connection.execute(SQL("DROP SCHEMA {} CASCADE").format(Identifier(schema)))
        self.addCleanup(drop_schema)
        dsn = make_conninfo(url, options="-c search_path=" + schema)
        database = PostgresDatabase(dsn, min_size=0, max_size=3)
        self.addCleanup(database.close)
        database.migrate()
        first, record = self.admit(store=PostgresWorkflowStore(database))
        database.close()
        reopened = PostgresDatabase(dsn, min_size=0, max_size=3)
        self.addCleanup(reopened.close)
        model = StreamingScriptedModel([ModelResponse(message="done")])
        second = make_agent(model, workflow_store=PostgresWorkflowStore(reopened),
                            kernel_compiler=kernel("V2"))
        self.addCleanup(second.close)
        self.assertEqual(second.resume_task(record.task_id).message, "done")
        self.assertIn("KERNEL V1", model.calls[0].instructions)
        self.assertNotIn("V2", model.calls[0].instructions)
        resumed = second.workflow_store.lookup_task(record.task_id)
        self.assertEqual(resumed.snapshot["compiled_instructions"], record.snapshot["compiled_instructions"])
        self.assertEqual(resumed.snapshot["protected_kernel_digest"], record.snapshot["protected_kernel_digest"])


if __name__ == "__main__":
    unittest.main()
