import tempfile
import time
import unittest
from pathlib import Path

from core_agent.audit import InMemoryAuditLog
from core_agent.config import AgentConfig, PlatformConfig, RunRequest
from core_agent.durability import CheckpointStore, InMemoryEventStore, LeaseManager, RecoveryManager
from core_agent.errors import CoreError
from core_agent.mcp import InMemoryMcpConnector
from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
from core_agent.observability import FailingExporter, RecordingExporter, Telemetry
from core_agent.runtime import CoreAgent
from core_agent.tasks import TaskScheduler
from core_agent.tools import ApprovalManager, ApprovalMode, PolicyEngine, ToolDefinition, ToolRegistry, ToolRuntime
from core_agent.execution import ExecutionEnvironmentManager, ExecutionResult
from memory_service.service import MemoryService


class IsolatedBackend:
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

    def create(self, spec):
        return type(
            "Environment",
            (),
            {
                "id": f"env-{spec.run_id}",
                "spec": spec,
                "execute": lambda inner, request: ExecutionResult(0, "tool-ok", "", (), ()),
                "destroy": lambda inner: None,
            },
        )()


def platform():
    return PlatformConfig(
        allowed_builtin_tools={"core.terminal.exec", "core.task.start", "core.task.wait", "core.delegate"},
        denied_builtin_tools=set(),
        allowed_mcp_servers={"memory"},
        denied_mcp_tools={},
        allowed_skills=set(),
        supported_features={"memory", "background_tasks", "delegation", "terminal", "mcp", "human_input"},
        a2a_protocol_versions=("1.0",),
        a2a_bindings=("HTTP+JSON",),
        max_model_turns=10,
        max_tool_calls=10,
    )


def agent_config(memory="optional", *, max_turns=10):
    return AgentConfig.from_dict(
        {
            "schema_version": "v1alpha1",
            "agent": {"name": "runtime-test", "profile_prompt": "Act as a test agent."},
            "model": {"route": "scripted"},
            "features": {
                "memory": memory,
                "background_tasks": True,
                "delegation": True,
                "terminal": True,
                "filesystem_mutation": False,
                "mcp": True,
                "skills": False,
                "human_input": True,
            },
            "tools": {
                "builtins": {
                    "default": "deny",
                    "allow": ["core.terminal.exec", "core.task.*", "core.delegate"],
                    "deny": [],
                },
                "mcp": {
                    "default": "deny",
                    "allow_servers": ["memory"],
                    "allow_tools": {"memory": ["search", "read", "create", "update", "split"]},
                },
            },
            "skills": {"default": "deny", "allow": []},
            "context": {"compact_at_working_ratio": 0.90, "compact_to_working_ratio": 0.15},
            "approval": {"mode": "on_risk"},
            "execution": {"environment_profile": "isolated-test"},
            "observability": {"otel_profile": "test"},
            "budgets": {"model_turns": max_turns, "tool_calls": 10},
        }
    )


def run_request(memory=True):
    mcp = []
    if memory:
        mcp.append(
            {
                "name": "memory",
                "role": "memory",
                "required": False,
                "transport": {"type": "streamable_http", "url": "https://memory.test/mcp"},
            }
        )
    return RunRequest.from_dict({"prompt": "Do it", "mcp": mcp, "skills": []})


def make_agent(model, *, memory="optional", connector=None, telemetry=None, max_turns=10):
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            "core.terminal.exec",
            "execute",
            {
                "type": "object",
                "properties": {"argv": {"type": "array", "items": {"type": "string"}}},
                "required": ["argv"],
                "additionalProperties": False,
            },
            mutating=False,
            risk_tags=frozenset(),
        )
    )
    tool_runtime = ToolRuntime(
        registry,
        PolicyEngine(ApprovalMode.ON_RISK),
        ApprovalManager(),
        ExecutionEnvironmentManager(IsolatedBackend()),
    )
    return CoreAgent(
        platform_config=platform(),
        agent_config=agent_config(memory, max_turns=max_turns),
        model=model,
        tool_runtime=tool_runtime,
        mcp_connector=connector or InMemoryMcpConnector(),
        task_scheduler=TaskScheduler(),
        event_store=InMemoryEventStore(),
        checkpoint_store=CheckpointStore(),
        audit_log=InMemoryAuditLog(),
        telemetry=telemetry or Telemetry(RecordingExporter()),
    )


class RuntimeTests(unittest.TestCase):
    def test_invalid_request_fails_before_model_or_tools(self):
        model = ScriptedModel([ModelResponse(message="should not run")])
        agent = make_agent(model)
        with self.assertRaises(CoreError) as caught:
            agent.run({"prompt": "x", "mcp": [], "skills": [], "extra": True})
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")
        self.assertEqual(model.calls, ())
        self.assertEqual(agent.tool_runtime.execution_count, 0)
        agent.close()

    def test_agent_loop_executes_valid_tool_then_returns_final_message(self):
        model = ScriptedModel(
            [
                ModelResponse(tool_requests=(ToolRequest("call-1", "core.terminal.exec", {"argv": ["check"]}),)),
                ModelResponse(message="done"),
            ]
        )
        agent = make_agent(model, memory="disabled")
        result = agent.run(run_request(memory=False))
        self.assertEqual(result.message, "done")
        self.assertEqual(result.terminal_state, "completed")
        self.assertEqual(result.usage.tool_calls, 1)
        self.assertEqual(len(model.calls), 2)
        second_context = model.calls[1].context
        self.assertIn("tool-ok", second_context)
        self.assertNotIn("reasoning", result.to_dict())
        agent.close()

    def test_memory_disabled_never_connects_or_discovers_memory(self):
        connector = InMemoryMcpConnector(
            catalogs={"memory": {"search": {"type": "object"}}},
            results={"memory.search": {"results": [{"text": "secret memory"}]}},
        )
        model = ScriptedModel([ModelResponse(message="done")])
        agent = make_agent(model, memory="disabled", connector=connector)
        result = agent.run(run_request(memory=True))
        self.assertEqual(result.message, "done")
        self.assertEqual(connector.connections, ())
        self.assertNotIn("memory.search", model.calls[0].tools)
        self.assertNotIn("MEMORY", model.calls[0].instructions)
        agent.close()

    def test_memory_mcp_is_filtered_and_visible_only_when_enabled(self):
        connector = InMemoryMcpConnector(
            catalogs={
                "memory": {
                    "search": {"type": "object"},
                    "read": {"type": "object"},
                    "delete": {"type": "object"},
                }
            }
        )
        model = ScriptedModel([ModelResponse(message="done")])
        agent = make_agent(model, connector=connector)
        agent.run(run_request(memory=True))
        self.assertEqual(connector.connections, ("memory",))
        self.assertIn("memory.search", model.calls[0].tools)
        self.assertIn("memory.read", model.calls[0].tools)
        self.assertNotIn("memory.delete", model.calls[0].tools)
        agent.close()

    def test_budget_stops_loop_without_automatic_growth(self):
        model = ScriptedModel([ModelResponse(continue_reasoning=True)] * 5)
        agent = make_agent(model, memory="disabled", max_turns=2)
        with self.assertRaises(CoreError) as caught:
            agent.run(run_request(memory=False))
        self.assertEqual(caught.exception.code, "BUDGET_EXCEEDED")
        self.assertEqual(len(model.calls), 2)
        agent.close()

    def test_audit_is_append_only_and_records_before_publication(self):
        model = ScriptedModel([ModelResponse(message="done")])
        agent = make_agent(model, memory="disabled")
        result = agent.run(run_request(memory=False))
        audit = agent.audit_log.records(result.run_id)
        events = agent.event_store.events(result.run_id)
        self.assertEqual([record.sequence for record in audit], sorted(record.sequence for record in audit))
        self.assertEqual(audit[-1].kind, "task.completed")
        self.assertLessEqual(audit[-1].written_at, events[-1].published_at)
        with self.assertRaises(CoreError):
            agent.audit_log.replace(result.run_id, 1, {"changed": True})
        agent.close()


class DurabilityTests(unittest.TestCase):
    def test_lease_allows_only_one_owner_and_can_be_recovered(self):
        leases = LeaseManager()
        first = leases.acquire("run-1", "worker-a", ttl=0.02)
        with self.assertRaises(CoreError) as caught:
            leases.acquire("run-1", "worker-b", ttl=1)
        self.assertEqual(caught.exception.code, "LEASE_LOST")
        time.sleep(0.03)
        second = leases.acquire("run-1", "worker-b", ttl=1)
        self.assertNotEqual(first.token, second.token)
        with self.assertRaises(CoreError):
            leases.renew("run-1", "worker-a", first.token, ttl=1)

    def test_recovery_restores_pending_approval_input_and_notifications(self):
        events = InMemoryEventStore()
        checkpoints = CheckpointStore()
        events.append("run-1", "task.started", {})
        events.append("run-1", "approval.required", {"approval_id": "apr-1"})
        events.append("run-1", "input.required", {"input_id": "input-1"})
        events.append("run-1", "task.notification", {"notification_id": "notice-1"})
        checkpoints.save("run-1", events.revision("run-1"), {"state": "waiting"})
        restored = RecoveryManager(events, checkpoints).recover("run-1")
        self.assertEqual(restored.pending_approvals, ("apr-1",))
        self.assertEqual(restored.pending_inputs, ("input-1",))
        self.assertEqual(restored.pending_notifications, ("notice-1",))
        self.assertEqual(restored.revision, events.revision("run-1"))

    def test_unknown_mutating_side_effect_is_not_replayed(self):
        events = InMemoryEventStore()
        checkpoints = CheckpointStore()
        events.append("run-1", "tool.intent", {"tool_call_id": "call-1", "mutating": True})
        checkpoints.save("run-1", 1, {"state": "tool_running"})
        with self.assertRaises(CoreError) as caught:
            RecoveryManager(events, checkpoints).recover("run-1")
        self.assertEqual(caught.exception.code, "SIDE_EFFECT_UNKNOWN")
        self.assertEqual(events.count("run-1", kind="tool.started"), 0)


class ObservabilityTests(unittest.TestCase):
    def test_w3c_context_propagates_without_becoming_authorization(self):
        exporter = RecordingExporter()
        telemetry = Telemetry(exporter)
        incoming = {"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}
        context = telemetry.extract(incoming)
        outgoing = {}
        telemetry.inject(context, outgoing)
        self.assertEqual(outgoing["traceparent"], incoming["traceparent"])
        self.assertIsNone(context.tenant_id)
        self.assertIsNone(context.authorization)

    def test_background_task_uses_new_trace_with_link_not_long_request_span(self):
        exporter = RecordingExporter()
        telemetry = Telemetry(exporter)
        with telemetry.span("core_agent.task.submit") as submission:
            linked = telemetry.start_background_span("core_agent.task.execute", submission.context)
        self.assertTrue(submission.ended)
        self.assertNotEqual(linked.context.trace_id, submission.context.trace_id)
        self.assertEqual(linked.links[0].trace_id, submission.context.trace_id)
        linked.end()

    def test_content_is_off_and_metric_labels_are_bounded(self):
        exporter = RecordingExporter()
        telemetry = Telemetry(exporter, content_enabled=False)
        with telemetry.span(
            "gen_ai.chat",
            attributes={
                "gen_ai.input.messages": "secret prompt",
                "gen_ai.tool.call.arguments": {"token": "secret"},
                "core_agent.task.state": "working",
            },
        ):
            pass
        attrs = exporter.spans[-1].attributes
        self.assertNotIn("gen_ai.input.messages", attrs)
        self.assertNotIn("gen_ai.tool.call.arguments", attrs)
        self.assertEqual(attrs["core_agent.task.state"], "working")
        with self.assertRaises(CoreError) as caught:
            telemetry.metric("core_agent.tasks", 1, labels={"task_id": "task-1"})
        self.assertEqual(caught.exception.code, "TELEMETRY_CARDINALITY_VIOLATION")

    def test_exporter_failure_does_not_damage_task_or_audit(self):
        audit = InMemoryAuditLog()
        telemetry = Telemetry(FailingExporter())
        audit.append("run-1", "task.completed", {"status": "ok"})
        with telemetry.span("core_agent.task.execute"):
            pass
        self.assertEqual(audit.records("run-1")[0].kind, "task.completed")
        self.assertEqual(telemetry.dropped_records, 1)

    def test_memory_service_continues_core_mcp_trace_and_owns_internal_spans(self):
        exporter = RecordingExporter()
        telemetry = Telemetry(exporter)
        with tempfile.TemporaryDirectory() as temp:
            service = MemoryService(Path(temp), telemetry=telemetry)
            service.search("bootstrap", namespace="session/context-1")
            content = (
                "---\nid: mem-1\ntitle: One\nnamespace: session/context-1\nkind: fact\n"
                "status: active\ncreated_at: now\nupdated_at: now\nsources: []\n---\nAlice knows Bob.\n"
            )
            with telemetry.span("mcp.client", attributes={"mcp.server": "memory"}) as client_span:
                carrier = {}
                telemetry.inject(client_span.context, carrier)
                service.create("session/one.md", content, 0, trace_carrier=carrier)
            names = [span.name for span in exporter.spans]
            self.assertIn("mcp.client", names)
            self.assertIn("memory_service.mcp.request", names)
            self.assertIn("memory_service.ner", names)
            self.assertIn("memory_service.index_publish", names)
            core_internal = [name for name in names if name.startswith("core_agent.memory.")]
            self.assertEqual(core_internal, [])
            memory_span = next(span for span in exporter.spans if span.name == "memory_service.mcp.request")
            self.assertEqual(memory_span.context.trace_id, client_span.context.trace_id)
            service.close()


if __name__ == "__main__":
    unittest.main()
