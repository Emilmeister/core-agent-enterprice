import threading
import unittest
from dataclasses import replace
from unittest.mock import patch

from core_agent.errors import CoreError
from core_agent.execution import (
    ExecutionEnvironmentManager,
    ExecutionResult,
    EnvironmentSpec,
)
from core_agent.tasks import (
    CapabilitySet,
    DelegationContract,
    DurableMailbox,
    Notification,
    TaskScheduler,
    derive_child_capabilities,
)
from core_agent.tools import (
    ToolCall,
    ToolDefinition,
    ToolRegistry,
    ToolResult,
    ToolRuntime,
)


class RecordingBackend:
    local = True
    capabilities = {
        "pty",
        "process_groups",
        "workspace_separation",
        "resource_limits",
        "process_tree_teardown",
    }

    def __init__(self):
        self.created = []
        self.executed = []
        self.destroyed = []

    def create(self, spec):
        environment = type(
            "RecordingEnvironment",
            (),
            {
                "id": f"env-{len(self.created) + 1}",
                "spec": spec,
                "execute": self._execute,
                "destroy": lambda inner: self.destroyed.append(inner.id),
            },
        )()
        self.created.append(environment)
        return environment

    def _execute(self, request):
        self.executed.append(request)
        return ExecutionResult(
            exit_code=0, stdout="ok", stderr="", artifacts=(), side_effects=()
        )


class MissingTerminalBackend:
    local = True
    capabilities = set()


class BackgroundTaskTests(unittest.TestCase):
    def test_thread_start_failure_does_not_admit_or_register_task(self):
        scheduler = TaskScheduler()
        admissions = []

        with patch(
            "core_agent.tasks.threading.Thread.start",
            side_effect=RuntimeError("thread unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
                scheduler.start(
                    lambda: "done",
                    owner_id="parent",
                    admission=lambda _connection: admissions.append("admitted"),
                )

        self.assertEqual(admissions, [])
        self.assertEqual(scheduler.list(owner_id="parent"), ())

    def test_start_returns_immediately_and_main_can_continue(self):
        gate = threading.Event()
        scheduler = TaskScheduler()

        def background():
            gate.wait(1)
            return "finished"

        started = scheduler.start(background, owner_id="parent")
        self.assertIn(started.state, {"submitted", "working"})
        main_work = ["continued"]
        self.assertEqual(main_work, ["continued"])
        gate.set()
        terminal = scheduler.wait(started.id, timeout=1)
        self.assertEqual(terminal.state, "completed")
        self.assertEqual(terminal.result, "finished")
        scheduler.close()

    def test_start_preserves_caller_supplied_task_id(self):
        scheduler = TaskScheduler()
        try:
            task = scheduler.start(
                lambda: "done",
                owner_id="parent",
                task_id="caller-task-id",
            )
            self.assertEqual(task.id, "caller-task-id")
            self.assertEqual(
                scheduler.wait("caller-task-id", timeout=1).state,
                "completed",
            )
        finally:
            scheduler.close()

    def test_wait_is_passive_and_mailbox_delivery_is_at_least_once_until_ack(self):
        scheduler = TaskScheduler()
        started = scheduler.start(lambda: "done", owner_id="parent")
        terminal = scheduler.wait(started.id, timeout=1)
        self.assertEqual(scheduler.active_compute_waiters, 0)
        mailbox = scheduler.mailbox("parent")
        first = mailbox.poll()
        second = mailbox.poll()
        self.assertEqual(first, second)
        self.assertEqual(first[0].task_id, terminal.id)
        mailbox.ack(first[0].id)
        self.assertEqual(mailbox.poll(), ())
        scheduler.close()

    def test_mailbox_deduplicates_same_task_event_revision(self):
        mailbox = DurableMailbox("parent")
        event = Notification(
            id="notice-1",
            owner_id="parent",
            task_id="child-1",
            kind="task.completed",
            revision=3,
            payload={"artifact": "a"},
        )
        self.assertTrue(mailbox.deliver(event))
        self.assertFalse(mailbox.deliver(event))
        self.assertEqual(mailbox.poll(), (event,))

    def test_cancel_marks_task_and_invokes_cancellation(self):
        cancelled = threading.Event()
        release = threading.Event()
        scheduler = TaskScheduler()

        def work(cancel_event):
            while not cancel_event.is_set() and not release.is_set():
                cancel_event.wait(0.01)
            if cancel_event.is_set():
                cancelled.set()
            return "stopped"

        task = scheduler.start(work, owner_id="parent", accepts_cancel_event=True)
        scheduler.cancel(task.id)
        self.assertTrue(cancelled.wait(1))
        self.assertEqual(scheduler.wait(task.id, timeout=1).state, "canceled")
        with self.assertRaises(CoreError) as caught:
            scheduler.cancel(task.id)
        self.assertEqual(caught.exception.code, "TASK_NOT_CANCELABLE")
        release.set()
        scheduler.close()

    def test_cancel_invokes_owned_process_cleanup_once(self):
        cleaned = []
        scheduler = TaskScheduler()

        def work(cancel_event):
            cancel_event.wait(1)
            return "stopped"

        task = scheduler.start(
            work,
            owner_id="parent",
            accepts_cancel_event=True,
            on_cancel=lambda: cleaned.append(task.id),
        )
        scheduler.cancel(task.id)
        self.assertEqual(scheduler.wait(task.id, timeout=1).state, "canceled")
        self.assertEqual(cleaned, [task.id])
        with self.assertRaises(CoreError):
            scheduler.cancel(task.id)
        self.assertEqual(cleaned, [task.id])

    def test_cancel_wins_when_cancel_aware_function_raises(self):
        started = threading.Event()
        scheduler = TaskScheduler()

        def work(cancel_event):
            started.set()
            cancel_event.wait(1)
            raise RuntimeError("stopped after cancellation")

        task = scheduler.start(work, owner_id="parent", accepts_cancel_event=True)
        try:
            self.assertTrue(started.wait(1))
            scheduler.cancel(task.id)
            terminal = scheduler.wait(task.id, timeout=1)
            self.assertEqual(terminal.state, "canceled")
        finally:
            scheduler.close()

    def test_required_pending_child_prevents_parent_completion(self):
        gate = threading.Event()
        scheduler = TaskScheduler()
        child = scheduler.start(lambda: gate.wait(1), owner_id="parent", required=True)
        with self.assertRaises(CoreError) as caught:
            scheduler.assert_can_complete_parent("parent")
        self.assertEqual(caught.exception.code, "REQUIRED_TASK_PENDING")
        gate.set()
        scheduler.wait(child.id, timeout=1)
        scheduler.assert_can_complete_parent("parent")
        scheduler.close()

    def test_active_kind_count_enforces_fanout_without_counting_completed(self):
        gate = threading.Event()
        scheduler = TaskScheduler()
        first = scheduler.start(
            lambda: gate.wait(1), owner_id="parent", kind="subagent"
        )
        self.assertEqual(
            scheduler.count(
                owner_id="parent", kind="subagent", active_only=True
            ),
            1,
        )
        gate.set()
        scheduler.wait(first.id, timeout=1)
        self.assertEqual(
            scheduler.count(
                owner_id="parent", kind="subagent", active_only=True
            ),
            0,
        )
        scheduler.close()


class DelegationTests(unittest.TestCase):
    def setUp(self):
        self.parent = CapabilitySet(
            tools=frozenset(
                {
                    "core_terminal_exec",
                    "core_fs_apply_patch",
                    "core_task_wait",
                    "core_delegate",
                    "core_memory_search",
                    "core_memory_read",
                }
            ),
            mcp={"repo": frozenset({"search", "read_file"})},
            skills=frozenset({"database-review", "release-notes"}),
            features=frozenset({"delegation", "background_tasks", "memory"}),
            budgets={"turns": 100, "tool_calls": 200, "depth": 2, "fan_out": 4},
            kernel_version="kernel-v1",
            tenant_id="tenant-1",
            memory_namespace="session/context-1",
        )

    def contract(self, **changes):
        raw = {
            "instruction": "Review database migrations and report concrete risks.",
            "tools": ["core_terminal_exec", "repo_search"],
            "skills": ["database-review"],
            "budget": {"turns": 20, "tool_calls": 40},
        }
        raw.update(changes)
        return DelegationContract.from_dict(raw)

    def test_child_capabilities_are_exact_intersection_and_inherit_kernel_tenant_budget(
        self,
    ):
        child = derive_child_capabilities(self.parent, self.contract(), current_depth=0)
        self.assertEqual(child.tools, frozenset({"core_terminal_exec"}))
        self.assertEqual(child.mcp, {"repo": frozenset({"search"})})
        self.assertEqual(child.skills, frozenset({"database-review"}))
        self.assertEqual(child.budgets["turns"], 20)
        self.assertEqual(child.budgets["tool_calls"], 40)
        self.assertEqual(child.kernel_version, "kernel-v1")
        self.assertEqual(child.tenant_id, "tenant-1")

    def test_child_cannot_expand_tools_mcp_skills_or_budget(self):
        """Each refusal names what was refused: the model has to fix one list."""
        invalid_contracts = [
            (
                self.contract(tools=["core_terminal_exec", "core_terminal_write"]),
                "core_terminal_write",
            ),
            (self.contract(tools=["repo_delete_repository"]), "repo_delete_repository"),
            (self.contract(tools=["absent_search"]), "absent_search"),
            (self.contract(skills=["unknown"]), "skill unknown"),
            (self.contract(budget={"turns": 101, "tool_calls": 40}), "budget turns"),
        ]
        for contract, expected in invalid_contracts:
            with self.subTest(expected=expected):
                with self.assertRaises(CoreError) as caught:
                    derive_child_capabilities(self.parent, contract, current_depth=0)
                self.assertEqual(caught.exception.code, "CAPABILITY_DISABLED")
                self.assertIn(expected, str(caught.exception))

    def test_one_tool_list_carries_built_ins_and_mcp_tools_alike(self):
        """The model names tools the way the catalogue showed them, in one list."""
        child = derive_child_capabilities(
            self.parent,
            self.contract(tools=["core_terminal_exec", "repo_search", "repo_read_file"]),
            current_depth=0,
        )
        self.assertEqual(child.tools, frozenset({"core_terminal_exec"}))
        self.assertEqual(child.mcp, {"repo": frozenset({"search", "read_file"})})

    def test_delegate_contract_rejects_ambiguous_budget_and_unknown_fields(self):
        for changes in (
            {"budget": {"max_steps": 3}},
            {"budget": {"turns": 2}},
            {"budget": {"tool_calls": 1}},
            {"budget": {"turns": 0, "tool_calls": 1}},
            {"budget": {"turns": 2, "tool_calls": False}},
            {"result_schema": '{"type":"object"}'},
            {"background": "yes"},
        ):
            raw = {
                "instruction": "Review",
                "tools": [],
                "skills": [],
                "budget": {"turns": 2, "tool_calls": 1},
                **changes,
            }
            with self.subTest(changes=changes):
                with self.assertRaises(CoreError) as caught:
                    DelegationContract.from_dict(raw)
                self.assertEqual(caught.exception.code, "TOOL_ARGUMENT_INVALID")

        joined = DelegationContract.from_dict(
            {
                "instruction": "Review",
                "tools": [],
                "skills": [],
                "budget": {"turns": 2, "tool_calls": 1},
            }
        )
        background = DelegationContract.from_dict(
            {
                "instruction": "Review",
                "tools": [],
                "skills": [],
                "budget": {"turns": 2, "tool_calls": 1},
                "background": True,
            }
        )
        self.assertFalse(joined.background)
        self.assertTrue(background.background)

    def test_memory_is_shared_only_when_a_memory_tool_is_delegated_explicitly(self):
        # Memory is a built-in tool family now, so a child gets it only by name.
        child = derive_child_capabilities(self.parent, self.contract(), current_depth=0)
        self.assertEqual(
            [tool for tool in child.tools if tool.startswith("core_memory_")], []
        )
        self.assertIsNone(child.memory_namespace)

        shared = self.contract(tools=["core_terminal_exec", "core_memory_search"])
        child = derive_child_capabilities(self.parent, shared, current_depth=0)
        self.assertEqual(
            child.tools, frozenset({"core_terminal_exec", "core_memory_search"})
        )
        # Delegating one memory tool must not hand over the rest of the family.
        self.assertNotIn("core_memory_read", child.tools)
        self.assertEqual(child.memory_namespace, "session/context-1")

        # A memory tool the parent does not hold cannot be granted to the child.
        with self.assertRaises(CoreError) as caught:
            derive_child_capabilities(
                self.parent,
                self.contract(tools=["core_memory_delete"]),
                current_depth=0,
            )
        self.assertEqual(caught.exception.code, "CAPABILITY_DISABLED")

    def test_depth_and_fanout_are_enforced(self):
        child = derive_child_capabilities(
            self.parent, self.contract(), current_depth=1
        )
        self.assertEqual(child.budgets["depth"], 2)
        first_level = derive_child_capabilities(
            self.parent,
            self.contract(tools=["core_delegate"]),
            current_depth=0,
        )
        second_level = derive_child_capabilities(
            self.parent,
            self.contract(tools=["core_delegate"]),
            current_depth=1,
        )
        self.assertIn("core_delegate", first_level.tools)
        self.assertNotIn("core_delegate", second_level.tools)

        with self.assertRaises(CoreError) as caught:
            derive_child_capabilities(self.parent, self.contract(), current_depth=2)
        self.assertEqual(caught.exception.code, "BUDGET_EXCEEDED")

        uncapped = replace(
            self.parent, budgets={**self.parent.budgets, "depth": 99}
        )
        with self.assertRaises(CoreError) as caught:
            derive_child_capabilities(uncapped, self.contract(), current_depth=2)
        self.assertEqual(caught.exception.code, "BUDGET_EXCEEDED")

class ToolRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.backend = RecordingBackend()
        self.environment_manager = ExecutionEnvironmentManager(self.backend)
        self.registry = ToolRegistry()
        self.registry.register(
            ToolDefinition(
                name="core_terminal_exec",
                description="execute",
                input_schema={
                    "type": "object",
                    "properties": {
                        "argv": {"type": "array", "items": {"type": "string"}}
                    },
                    "required": ["argv"],
                    "additionalProperties": False,
                },
                mutating=False,
                risk_tags=frozenset(),
            )
        )
        self.registry.register(
            ToolDefinition(
                name="external.publish",
                description="publish",
                input_schema={
                    "type": "object",
                    "properties": {
                        "target": {"type": "string"},
                        "body": {"type": "string"},
                    },
                    "required": ["target", "body"],
                    "additionalProperties": False,
                },
                mutating=True,
                risk_tags=frozenset({"external_write", "acts_as_user"}),
            )
        )
        self.runtime = ToolRuntime(
            registry=self.registry,
            environment_manager=self.environment_manager,
            event_sink=lambda event: self.events.append(event.kind),
        )

    def test_name_collision_unknown_tool_and_invalid_arguments_never_execute(self):
        with self.assertRaises(CoreError) as caught:
            self.registry.register(self.registry.get("core_terminal_exec"))
        self.assertEqual(caught.exception.code, "TOOL_NAME_COLLISION")
        with self.assertRaises(CoreError) as caught:
            self.runtime.execute(ToolCall("call-0", "missing", {}), run_id="run-1")
        self.assertEqual(caught.exception.code, "TOOL_NOT_FOUND")
        with self.assertRaises(CoreError) as caught:
            self.runtime.execute(
                ToolCall("call-1", "core_terminal_exec", {"argv": "not-array"}),
                run_id="run-1",
            )
        self.assertEqual(caught.exception.code, "TOOL_ARGUMENT_INVALID")
        self.assertEqual(self.backend.executed, [])

    def test_a_rejected_argument_is_named_and_its_value_is_not(self):
        """A bare code gives the model nothing to correct, so it starts guessing."""
        cases = {
            '{"argv": ["ls"]}': "arguments.argv must be an array, got string",
            "": "arguments.argv is required",
        }
        for argv, expected in cases.items():
            with self.subTest(argv=argv):
                arguments = {"argv": argv} if argv else {}
                with self.assertRaises(CoreError) as caught:
                    self.runtime.execute(
                        ToolCall("call-1", "core_terminal_exec", arguments),
                        run_id="run-1",
                    )
                self.assertEqual(caught.exception.code, "TOOL_ARGUMENT_INVALID")
                self.assertEqual(str(caught.exception), expected)
        # The path and the type are diagnostics; the value is request content.
        with self.assertRaises(CoreError) as caught:
            self.runtime.execute(
                ToolCall("call-2", "core_terminal_exec", {"argv": ["ls", 7]}),
                run_id="run-1",
            )
        self.assertEqual(
            str(caught.exception), "arguments.argv[1] must be a string, got integer"
        )
        self.assertEqual(self.backend.executed, [])

    def test_validation_happens_before_execution(self):
        result = self.runtime.execute(
            ToolCall("call-2", "core_terminal_exec", {"argv": ["python", "-V"]}),
            run_id="run-1",
        )
        self.assertIsInstance(result, ToolResult)
        self.assertEqual(
            self.events[:3],
            ["tool.requested", "tool.validated", "tool.started"],
        )
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(len(self.backend.executed), 1)

    def test_completed_terminal_failure_is_a_failed_tool_result(self):
        self.environment_manager.execute_transient = lambda _request, _run_id: (
            ExecutionResult(7, "", "bad command", (), (), status="failed")
        )
        result = self.runtime.execute(
            ToolCall("call-failed", "core_terminal_exec", {"argv": ["false"]}),
            run_id="run-1",
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "TOOL_RETURNED_FAILED")
        self.assertEqual(self.events[-1], "tool.failed")

    def test_input_request_is_not_approval(self):
        request = self.runtime.request_input(
            "Choose branch", {"type": "string"}, run_id="run-1"
        )
        self.assertEqual(request.kind, "information_required")
        self.assertFalse(hasattr(request, "argument_digest"))


class TerminalSessionContractTests(unittest.TestCase):
    def test_backend_without_terminal_primitives_is_rejected(self):
        with self.assertRaises(CoreError) as caught:
            ExecutionEnvironmentManager(MissingTerminalBackend())
        self.assertEqual(caught.exception.code, "EXECUTION_ENVIRONMENT_UNAVAILABLE")

    def test_parent_and_child_get_separate_sessions_and_workspace_ids(self):
        backend = RecordingBackend()
        manager = ExecutionEnvironmentManager(backend)
        parent = manager.create(
            EnvironmentSpec(
                tenant_id="tenant-1",
                run_id="parent",
                workspace_snapshot="sha256:base",
                writable_paths=("/workspace",),
                network_allowlist=(),
                secrets={},
            )
        )
        child = manager.create(
            EnvironmentSpec(
                tenant_id="tenant-1",
                run_id="child",
                parent_run_id="parent",
                workspace_snapshot="sha256:base",
                writable_paths=("/workspace",),
                network_allowlist=(),
                secrets={},
            )
        )
        self.assertNotEqual(parent.id, child.id)
        self.assertNotEqual(parent.spec.session_id, child.spec.session_id)
        self.assertNotEqual(parent.spec.workspace_id, child.spec.workspace_id)
        self.assertEqual(parent.spec.workspace_snapshot, child.spec.workspace_snapshot)

    def test_environment_spec_declares_process_separation_without_os_sandbox_claim(
        self,
    ):
        spec = EnvironmentSpec(
            tenant_id="tenant-1",
            run_id="run-1",
            workspace_snapshot="sha256:base",
            writable_paths=("/workspace",),
            network_allowlist=(),
            secrets={},
        ).hardened()
        self.assertEqual(spec.isolation_level, "process")
        self.assertFalse(spec.os_security_boundary)
        self.assertFalse(spec.runtime_socket_mounted)
        self.assertFalse(spec.service_account_token_mounted)

    def test_secret_is_injected_for_one_call_but_absent_from_result_and_telemetry(self):
        backend = RecordingBackend()
        manager = ExecutionEnvironmentManager(backend)
        environment = manager.create(
            EnvironmentSpec(
                tenant_id="tenant-1",
                run_id="run-1",
                workspace_snapshot="sha256:base",
                writable_paths=("/workspace",),
                network_allowlist=(),
                secrets={"TOKEN": "secret-value"},
            )
        )
        result = manager.execute(
            environment.id, {"argv": ["tool"], "secret_refs": ["TOKEN"]}
        )
        self.assertNotIn("secret-value", repr(result))
        self.assertNotIn("secret-value", repr(manager.telemetry_records))
        self.assertNotIn("secret-value", repr(environment.spec.checkpoint_dict()))
        manager.destroy(environment.id)
        self.assertIn(environment.id, backend.destroyed)


if __name__ == "__main__":
    unittest.main()
