import dataclasses
import time
import unittest

from core_agent.a2a import (
    CORE_EXTENSION_URI,
    A2AService,
    AgentCard,
    Artifact,
    Message,
    Part,
    Task,
    TaskState,
    map_core_state,
    parse_run_request,
)
from core_agent.config import AgentConfig, PlatformConfig, RunRequest, compile_effective_config
from core_agent.errors import CoreError


def platform_config(**changes):
    values = {
        "allowed_builtin_tools": {
            "core.terminal.exec",
            "core.terminal.write",
            "core.fs.apply_patch",
            "core.task.start",
            "core.task.get",
            "core.task.list",
            "core.task.wait",
            "core.task.cancel",
            "core.delegate",
        },
        "denied_builtin_tools": set(),
        "allowed_mcp_servers": {"repo", "memory"},
        "denied_mcp_tools": {"repo": {"delete_repository"}},
        "allowed_skills": {"database-review", "release-notes"},
        "supported_features": {
            "memory",
            "background_tasks",
            "delegation",
            "terminal",
            "filesystem_mutation",
            "mcp",
            "skills",
            "human_input",
        },
        "a2a_protocol_versions": ("1.0",),
        "a2a_bindings": ("HTTP+JSON",),
    }
    values.update(changes)
    return PlatformConfig(**values)


def agent_config(**changes):
    raw = {
        "schema_version": "v1alpha1",
        "agent": {"name": "test-agent", "profile_prompt": "Be useful."},
        "model": {"route": "test-model"},
        "features": {
            "memory": "optional",
            "background_tasks": True,
            "delegation": True,
            "terminal": True,
            "filesystem_mutation": True,
            "mcp": True,
            "skills": True,
            "human_input": True,
        },
        "tools": {
            "builtins": {
                "default": "deny",
                "allow": [
                    "core.terminal.exec",
                    "core.fs.apply_patch",
                    "core.task.*",
                    "core.delegate",
                ],
                "deny": [],
            },
            "mcp": {
                "default": "deny",
                "allow_servers": ["repo", "memory"],
                "allow_tools": {
                    "repo": ["search", "read_file", "delete_repository"],
                    "memory": ["search", "read", "create", "update", "split"],
                },
            },
        },
        "skills": {"default": "deny", "allow": ["database-review"]},
        "context": {
            "compact_at_working_ratio": 0.90,
            "compact_to_working_ratio": 0.15,
        },
        "approval": {"mode": "on_risk"},
        "execution": {"environment_profile": "isolated-test"},
        "observability": {"otel_profile": "test"},
    }
    for key, value in changes.items():
        raw[key] = value
    return AgentConfig.from_dict(raw)


def request(*, memory_required=False, extra_mcp=(), skills=("database-review",)):
    mcp = [
        {
            "name": "repo",
            "role": "repository",
            "required": True,
            "transport": {"type": "streamable_http", "url": "https://repo.test/mcp"},
        },
        {
            "name": "memory",
            "role": "memory",
            "required": memory_required,
            "transport": {"type": "streamable_http", "url": "https://memory.test/mcp"},
        },
        *extra_mcp,
    ]
    return RunRequest.from_dict(
        {
            "prompt": "Do the work",
            "mcp": mcp,
            "skills": [{"name": name, "source": f"file:///skills/{name}"} for name in skills],
        }
    )


DISCOVERED = {
    "repo": {
        "search": {"type": "object"},
        "read_file": {"type": "object"},
        "delete_repository": {"type": "object"},
    },
    "memory": {
        "search": {"type": "object"},
        "read": {"type": "object"},
        "create": {"type": "object"},
        "update": {"type": "object"},
        "split": {"type": "object"},
        "delete": {"type": "object"},
    },
}


class RunRequestTests(unittest.TestCase):
    def test_run_request_has_exactly_prompt_mcp_and_skills(self):
        parsed = RunRequest.from_dict({"prompt": "ok", "mcp": [], "skills": []})
        self.assertEqual(parsed.prompt, "ok")
        with self.assertRaises(CoreError) as caught:
            RunRequest.from_dict({"prompt": "ok", "mcp": [], "skills": [], "session_id": "x"})
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")

    def test_prompt_must_be_nonempty_and_mcp_names_unique(self):
        with self.assertRaises(CoreError):
            RunRequest.from_dict({"prompt": "", "mcp": [], "skills": []})
        duplicate = {
            "prompt": "x",
            "mcp": [
                {"name": "same", "transport": {"type": "stdio", "command": "one"}},
                {"name": "same", "transport": {"type": "stdio", "command": "two"}},
            ],
            "skills": [],
        }
        with self.assertRaises(CoreError) as caught:
            RunRequest.from_dict(duplicate)
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")

    def test_a2a_message_maps_prompt_and_extension_without_duplication(self):
        message = Message(
            role="user",
            parts=(Part.text("Fix the test"),),
            extensions=(CORE_EXTENSION_URI,),
            metadata={CORE_EXTENSION_URI: {"mcp": [], "skills": []}},
            context_id="context-1",
        )
        parsed = parse_run_request(message, requested_extensions={CORE_EXTENSION_URI})
        self.assertEqual(parsed.to_dict(), {"prompt": "Fix the test", "mcp": [], "skills": []})
        self.assertNotIn("prompt", message.metadata[CORE_EXTENSION_URI])

    def test_required_extension_and_content_type_are_validated_before_work(self):
        missing = Message(role="user", parts=(Part.text("x"),), metadata={})
        with self.assertRaises(CoreError) as caught:
            parse_run_request(missing, requested_extensions=set())
        self.assertEqual(caught.exception.code, "A2A_EXTENSION_REQUIRED")

        unsupported = Message(
            role="user",
            parts=(Part(kind="video", data="artifact://video"),),
            extensions=(CORE_EXTENSION_URI,),
            metadata={CORE_EXTENSION_URI: {"mcp": [], "skills": []}},
        )
        with self.assertRaises(CoreError) as caught:
            parse_run_request(unsupported, requested_extensions={CORE_EXTENSION_URI})
        self.assertEqual(caught.exception.code, "CONTENT_TYPE_NOT_SUPPORTED")


class ConfigurationTests(unittest.TestCase):
    def test_effective_config_is_intersection_with_deny_precedence(self):
        effective = compile_effective_config(platform_config(), agent_config(), request(), DISCOVERED)
        self.assertEqual(
            effective.builtin_tools,
            frozenset(
                {
                    "core.terminal.exec",
                    "core.fs.apply_patch",
                    "core.task.start",
                    "core.task.get",
                    "core.task.list",
                    "core.task.wait",
                    "core.task.cancel",
                    "core.delegate",
                }
            ),
        )
        self.assertEqual(effective.mcp_tools["repo"], frozenset({"search", "read_file"}))
        self.assertEqual(
            effective.mcp_tools["memory"], frozenset({"search", "read", "create", "update", "split"})
        )
        self.assertEqual(effective.skills, frozenset({"database-review"}))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            effective.digest = "changed"

    def test_memory_disabled_removes_optional_server_tools_and_policy(self):
        raw = agent_config().to_dict()
        raw["features"]["memory"] = "disabled"
        effective = compile_effective_config(platform_config(), AgentConfig.from_dict(raw), request(), DISCOVERED)
        self.assertNotIn("memory", effective.mcp_tools)
        self.assertNotIn("memory", effective.enabled_capability_policies)
        self.assertTrue(any(w.code == "CAPABILITY_FILTERED" and w.capability == "memory" for w in effective.warnings))

    def test_required_memory_fails_when_disabled_or_missing(self):
        raw = agent_config().to_dict()
        raw["features"]["memory"] = "disabled"
        with self.assertRaises(CoreError) as caught:
            compile_effective_config(platform_config(), AgentConfig.from_dict(raw), request(memory_required=True), DISCOVERED)
        self.assertEqual(caught.exception.code, "CAPABILITY_DISABLED")

        raw["features"]["memory"] = "required"
        no_memory = RunRequest.from_dict({"prompt": "x", "mcp": [], "skills": []})
        with self.assertRaises(CoreError) as caught:
            compile_effective_config(platform_config(), AgentConfig.from_dict(raw), no_memory, {})
        self.assertEqual(caught.exception.code, "REQUIRED_CAPABILITY_MISSING")

    def test_unknown_config_field_and_conflicting_delegation_are_rejected(self):
        raw = agent_config().to_dict()
        raw["surprise"] = True
        with self.assertRaises(CoreError) as caught:
            AgentConfig.from_dict(raw)
        self.assertEqual(caught.exception.code, "CONFIG_INVALID")

        raw = agent_config().to_dict()
        raw["features"]["background_tasks"] = False
        with self.assertRaises(CoreError) as caught:
            AgentConfig.from_dict(raw)
        self.assertEqual(caught.exception.code, "CONFIG_CONFLICT")

    def test_disabled_tool_is_not_discoverable_and_stale_call_is_denied(self):
        raw = agent_config().to_dict()
        raw["tools"]["builtins"]["deny"] = ["core.terminal.exec"]
        effective = compile_effective_config(platform_config(), AgentConfig.from_dict(raw), request(), DISCOVERED)
        self.assertNotIn("core.terminal.exec", effective.model_tool_catalog)
        with self.assertRaises(CoreError) as caught:
            effective.require_tool("core.terminal.exec")
        self.assertEqual(caught.exception.code, "CAPABILITY_DISABLED")

    def test_effective_digest_is_stable_and_contains_no_secrets(self):
        effective_a = compile_effective_config(platform_config(), agent_config(), request(), DISCOVERED)
        effective_b = compile_effective_config(platform_config(), agent_config(), request(), DISCOVERED)
        self.assertEqual(effective_a.digest, effective_b.digest)
        self.assertNotIn("TOKEN", effective_a.audit_snapshot)
        self.assertNotIn("secret", effective_a.audit_snapshot.lower())


class A2ATests(unittest.TestCase):
    def test_core_states_map_only_to_standard_a2a_states(self):
        expected = {
            "CREATED": TaskState.SUBMITTED,
            "QUEUED": TaskState.SUBMITTED,
            "RUNNING": TaskState.WORKING,
            "WAITING_TASK": TaskState.WORKING,
            "PAUSED": TaskState.WORKING,
            "WAITING_INPUT": TaskState.INPUT_REQUIRED,
            "WAITING_APPROVAL": TaskState.INPUT_REQUIRED,
            "WAITING_AUTH": TaskState.AUTH_REQUIRED,
            "COMPLETED": TaskState.COMPLETED,
            "FAILED": TaskState.FAILED,
            "ABORTED": TaskState.FAILED,
            "CANCELLED": TaskState.CANCELED,
            "REJECTED": TaskState.REJECTED,
        }
        self.assertEqual({key: map_core_state(key) for key in expected}, expected)

    def test_agent_card_reflects_effective_capabilities(self):
        raw = agent_config().to_dict()
        raw["features"]["memory"] = "disabled"
        effective = compile_effective_config(platform_config(), AgentConfig.from_dict(raw), request(), DISCOVERED)
        card = AgentCard.from_effective_config(effective)
        encoded = card.to_dict()
        self.assertEqual(encoded["supportedInterfaces"][0]["protocolVersion"], "1.0")
        self.assertTrue(encoded["capabilities"]["streaming"])
        self.assertNotIn("memory", encoded["skills"])
        self.assertNotIn("memory", str(encoded["capabilities"]).lower())

    def test_nonblocking_task_survives_stream_disconnect_and_is_queryable(self):
        def handler(run_request, task_context):
            time.sleep(0.02)
            return Artifact.text("result", provenance={"prompt": run_request.prompt})

        service = A2AService(handler=handler, agent_card=AgentCard.minimal("test"))
        message = Message.from_run_request(request())
        task = service.send_message(message, return_immediately=True)
        self.assertIsInstance(task, Task)
        self.assertIn(task.state, {TaskState.SUBMITTED, TaskState.WORKING})
        stream = service.subscribe_to_task(task.id)
        stream.close()
        completed = service.wait_for_terminal(task.id, timeout=1)
        self.assertEqual(completed.state, TaskState.COMPLETED)
        self.assertEqual(service.get_task(task.id).artifacts[0].parts[0].data, "result")
        self.assertEqual(service.list_tasks(context_id=task.context_id)[0].id, task.id)
        service.close()

    def test_updates_are_ordered_and_push_delivery_is_idempotent(self):
        delivered = []

        service = A2AService(
            handler=lambda request, context: Artifact.text("ok"),
            agent_card=AgentCard.minimal("test"),
        )
        task = service.send_message(Message.from_run_request(request()), return_immediately=True)
        service.register_push(task.id, delivered.append)
        terminal = service.wait_for_terminal(task.id, timeout=1)
        service.redeliver_push(task.id)
        unique = {(event.task_id, event.sequence, event.revision) for event in delivered}
        self.assertLessEqual(len(unique), len(delivered))
        self.assertEqual(sorted(event.sequence for event in terminal.history), [event.sequence for event in terminal.history])
        self.assertEqual(len({event.sequence for event in terminal.history}), len(terminal.history))
        service.close()

    def test_terminal_task_rejects_more_messages_and_cancel_is_idempotent(self):
        service = A2AService(
            handler=lambda request, context: Artifact.text("ok"),
            agent_card=AgentCard.minimal("test"),
        )
        task = service.send_message(Message.from_run_request(request()), return_immediately=True)
        terminal = service.wait_for_terminal(task.id, timeout=1)
        with self.assertRaises(CoreError) as caught:
            service.send_to_task(terminal.id, Message.user("more"))
        self.assertEqual(caught.exception.code, "TASK_TERMINAL")
        with self.assertRaises(CoreError) as caught:
            service.cancel_task(terminal.id)
        self.assertEqual(caught.exception.code, "TASK_NOT_CANCELABLE")
        service.close()


if __name__ == "__main__":
    unittest.main()
