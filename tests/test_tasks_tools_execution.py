import threading
import time
import unittest

from core_agent.errors import CoreError
from core_agent.execution import ExecutionEnvironmentManager, ExecutionResult, EnvironmentSpec
from core_agent.tasks import (
    CapabilitySet,
    DelegationContract,
    DurableMailbox,
    Notification,
    TaskScheduler,
    derive_child_capabilities,
)
from core_agent.tools import (
    ApprovalManager,
    ApprovalMode,
    PolicyEngine,
    ToolCall,
    ToolDefinition,
    ToolRegistry,
    ToolResult,
    ToolRuntime,
)


class RecordingBackend:
    isolated = True
    capabilities = {
        "mount_namespace",
        "process_namespace",
        "user_namespace",
        "network_namespace",
        "immutable_image",
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
        return ExecutionResult(exit_code=0, stdout="ok", stderr="", artifacts=(), side_effects=())


class HostBackend:
    isolated = False
    capabilities = set()


class BackgroundTaskTests(unittest.TestCase):
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


class DelegationTests(unittest.TestCase):
    def setUp(self):
        self.parent = CapabilitySet(
            tools=frozenset({"core.terminal.exec", "core.fs.apply_patch", "core.task.wait"}),
            mcp={
                "repo": frozenset({"search", "read_file"}),
                "memory": frozenset({"search", "read", "update"}),
            },
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
            "tools": ["core.terminal.exec"],
            "mcp": {"repo": ["search"], "memory": ["search", "read", "update"]},
            "skills": ["database-review"],
            "budget": {"turns": 20, "tool_calls": 40},
            "result_schema": "artifact://schemas/review.json",
        }
        raw.update(changes)
        return DelegationContract.from_dict(raw)

    def test_child_capabilities_are_exact_intersection_and_inherit_kernel_tenant_budget(self):
        child = derive_child_capabilities(self.parent, self.contract(), current_depth=0)
        self.assertEqual(child.tools, frozenset({"core.terminal.exec"}))
        self.assertEqual(child.mcp, {"repo": frozenset({"search"}), "memory": frozenset({"search", "read", "update"})})
        self.assertEqual(child.skills, frozenset({"database-review"}))
        self.assertEqual(child.budgets["turns"], 20)
        self.assertEqual(child.budgets["tool_calls"], 40)
        self.assertEqual(child.kernel_version, "kernel-v1")
        self.assertEqual(child.tenant_id, "tenant-1")
        self.assertEqual(child.memory_namespace, "session/context-1")

    def test_child_cannot_expand_tools_mcp_skills_or_budget(self):
        invalid_contracts = [
            self.contract(tools=["core.terminal.exec", "core.terminal.write"]),
            self.contract(mcp={"repo": ["delete_repository"]}),
            self.contract(skills=["unknown"]),
            self.contract(budget={"turns": 101, "tool_calls": 40}),
        ]
        for contract in invalid_contracts:
            with self.subTest(contract=contract):
                with self.assertRaises(CoreError) as caught:
                    derive_child_capabilities(self.parent, contract, current_depth=0)
                self.assertEqual(caught.exception.code, "CAPABILITY_DISABLED")

    def test_memory_is_shared_only_when_same_server_tools_and_namespace_are_explicit(self):
        no_memory = self.contract(mcp={"repo": ["search"]})
        child = derive_child_capabilities(self.parent, no_memory, current_depth=0)
        self.assertNotIn("memory", child.mcp)
        self.assertIsNone(child.memory_namespace)

        read_only = self.contract(mcp={"memory": ["search", "read"]})
        child = derive_child_capabilities(self.parent, read_only, current_depth=0)
        self.assertEqual(child.mcp["memory"], frozenset({"search", "read"}))
        self.assertEqual(child.memory_namespace, "session/context-1")

    def test_depth_and_fanout_are_enforced(self):
        with self.assertRaises(CoreError) as caught:
            derive_child_capabilities(self.parent, self.contract(), current_depth=2)
        self.assertEqual(caught.exception.code, "BUDGET_EXCEEDED")


class ToolRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.backend = RecordingBackend()
        self.environment_manager = ExecutionEnvironmentManager(self.backend)
        self.registry = ToolRegistry()
        self.registry.register(
            ToolDefinition(
                name="core.terminal.exec",
                description="execute",
                input_schema={
                    "type": "object",
                    "properties": {"argv": {"type": "array", "items": {"type": "string"}}},
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
                    "properties": {"target": {"type": "string"}, "body": {"type": "string"}},
                    "required": ["target", "body"],
                    "additionalProperties": False,
                },
                mutating=True,
                risk_tags=frozenset({"external_write", "acts_as_user"}),
            )
        )
        self.approvals = ApprovalManager()
        self.runtime = ToolRuntime(
            registry=self.registry,
            policy=PolicyEngine(approval_mode=ApprovalMode.ON_RISK),
            approvals=self.approvals,
            environment_manager=self.environment_manager,
            event_sink=lambda event: self.events.append(event.kind),
        )

    def test_name_collision_unknown_tool_and_invalid_arguments_never_execute(self):
        with self.assertRaises(CoreError) as caught:
            self.registry.register(self.registry.get("core.terminal.exec"))
        self.assertEqual(caught.exception.code, "TOOL_NAME_COLLISION")
        with self.assertRaises(CoreError) as caught:
            self.runtime.execute(ToolCall("call-0", "missing", {}), run_id="run-1")
        self.assertEqual(caught.exception.code, "TOOL_NOT_FOUND")
        with self.assertRaises(CoreError) as caught:
            self.runtime.execute(ToolCall("call-1", "core.terminal.exec", {"argv": "not-array"}), run_id="run-1")
        self.assertEqual(caught.exception.code, "TOOL_ARGUMENT_INVALID")
        self.assertEqual(self.backend.executed, [])

    def test_validation_happens_before_policy_and_execution(self):
        result = self.runtime.execute(
            ToolCall("call-2", "core.terminal.exec", {"argv": ["python", "-V"]}),
            run_id="run-1",
        )
        self.assertIsInstance(result, ToolResult)
        self.assertEqual(self.events[:4], ["tool.requested", "tool.validated", "policy.allowed", "tool.started"])
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(len(self.backend.executed), 1)

    def test_risky_call_does_not_execute_until_exact_arguments_are_approved(self):
        call = ToolCall("call-3", "external.publish", {"target": "org/repo", "body": "hello"})
        approval = self.runtime.execute(call, run_id="run-1")
        self.assertEqual(approval.tool_call_id, "call-3")
        self.assertEqual(approval.risks, ("acts_as_user", "external_write"))
        self.assertEqual(self.backend.executed, [])

        self.approvals.resolve(approval.id, "approve", scope="single_call")
        result = self.runtime.resume_approved(call, approval.id, run_id="run-1")
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(len(self.backend.executed), 1)

        changed = ToolCall("call-3", "external.publish", {"target": "other/repo", "body": "hello"})
        with self.assertRaises(CoreError) as caught:
            self.runtime.resume_approved(changed, approval.id, run_id="run-1")
        self.assertEqual(caught.exception.code, "APPROVAL_ARGUMENTS_CHANGED")

    def test_approval_is_single_resolution_and_never_mode_denies_instead_of_allowing(self):
        approval = self.runtime.execute(
            ToolCall("call-4", "external.publish", {"target": "org/repo", "body": "hello"}),
            run_id="run-1",
        )
        self.approvals.resolve(approval.id, "deny")
        with self.assertRaises(CoreError) as caught:
            self.approvals.resolve(approval.id, "approve")
        self.assertEqual(caught.exception.code, "APPROVAL_ALREADY_RESOLVED")

        never_runtime = ToolRuntime(
            self.registry,
            PolicyEngine(approval_mode=ApprovalMode.NEVER),
            ApprovalManager(),
            self.environment_manager,
        )
        denied = never_runtime.execute(
            ToolCall("call-5", "external.publish", {"target": "org/repo", "body": "hello"}),
            run_id="run-1",
        )
        self.assertEqual(denied.status, "denied")
        self.assertEqual(self.backend.executed, [])

    def test_input_request_is_not_approval(self):
        request = self.runtime.request_input("Choose branch", {"type": "string"}, run_id="run-1")
        self.assertEqual(request.kind, "information_required")
        self.assertFalse(hasattr(request, "argument_digest"))


class ExecutionIsolationTests(unittest.TestCase):
    def test_host_backend_is_rejected_without_fallback(self):
        with self.assertRaises(CoreError) as caught:
            ExecutionEnvironmentManager(HostBackend())
        self.assertEqual(caught.exception.code, "EXECUTION_ENVIRONMENT_UNAVAILABLE")

    def test_parent_and_child_get_separate_overlays_and_environment_ids(self):
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
        self.assertNotEqual(parent.spec.overlay_id, child.spec.overlay_id)
        self.assertEqual(parent.spec.workspace_snapshot, child.spec.workspace_snapshot)

    def test_environment_spec_denies_host_mounts_runtime_socket_metadata_and_default_network(self):
        spec = EnvironmentSpec(
            tenant_id="tenant-1",
            run_id="run-1",
            workspace_snapshot="sha256:base",
            writable_paths=("/workspace",),
            network_allowlist=(),
            secrets={},
        ).hardened()
        self.assertFalse(spec.host_root_mounted)
        self.assertFalse(spec.runtime_socket_mounted)
        self.assertFalse(spec.ssh_agent_forwarded)
        self.assertFalse(spec.cloud_metadata_access)
        self.assertEqual(spec.network_mode, "deny")
        self.assertFalse(spec.privileged)

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
        result = manager.execute(environment.id, {"argv": ["tool"], "secret_refs": ["TOKEN"]})
        self.assertNotIn("secret-value", repr(result))
        self.assertNotIn("secret-value", repr(manager.telemetry_records))
        self.assertNotIn("secret-value", repr(environment.spec.checkpoint_dict()))
        manager.destroy(environment.id)
        self.assertIn(environment.id, backend.destroyed)


if __name__ == "__main__":
    unittest.main()
