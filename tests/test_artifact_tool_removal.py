import importlib.util
import copy
import hashlib
import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from dataclasses import replace
from unittest.mock import patch

from core_agent.a2a import Message, Part, parse_run_request
from core_agent.errors import CoreError
from core_agent.config import AgentConfig, compile_effective_config
from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
from core_agent.runtime import CoreAgent
from core_agent.config import RunRequest
from tests.app_support import create_app
from tests.test_auth import TEST_DATABASE_URL
from core_agent.database import PostgresDatabase
from core_agent.workflow import PostgresWorkflowStore


RETIRED_TOOLS = ("core_artifact_save", "core_artifact_load", "core_artifact_list")
RETIRED_ENV = (
    "ARTIFACT_STORAGE_ENABLED", "ARTIFACT_STORAGE_TYPE", "ARTIFACT_MONGODB_URL",
    "ARTIFACT_S3_BUCKET", "ARTIFACT_S3_REGION", "ARTIFACT_S3_TENANT_ID",
    "ARTIFACT_S3_ACCESS_KEY_ID", "ARTIFACT_S3_SECRET_ACCESS_KEY",
    "ARTIFACT_S3_ENDPOINT_URL", "ARTIFACT_S3_CONNECT_TIMEOUT",
    "ARTIFACT_S3_READ_TIMEOUT", "ARTIFACT_S3_BOTO_MAX_ATTEMPTS",
    "ARTIFACT_S3_RETRY_INITIAL_DELAY", "ARTIFACT_S3_RETRY_MAX_DELAY",
    "ARTIFACT_S3_RETRY_MAX_TOTAL_SECONDS", "RUNTIME_SAVE_INPUT_BLOBS_AS_ARTIFACTS",
)


class ArtifactToolRemovalTests(unittest.TestCase):
    def build(self, *, database=None, **environment):
        model = ScriptedModel([ModelResponse(message="ok")])
        model.model = "removal-test"
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        with patch.dict(os.environ, {
            "CORE_AGENT_ENVIRONMENT": "test", "SESSION_STORAGE_TYPE": "in-memory",
            "CORE_AGENT_MEMORY": "disabled", "LOCAL_WORKSPACE_ROOT": workspace.name,
            **environment,
        }, clear=True):
            app = create_app(model=model, database=database)
        self.addCleanup(app.state.close)
        return app, model

    def test_catalog_handlers_state_and_module_are_removed(self):
        app, _ = self.build()
        agent = app.state.core_agent
        self.assertFalse(set(RETIRED_TOOLS) & set(agent.tool_runtime.registry.names()))
        self.assertFalse(set(RETIRED_TOOLS) & set(agent.tool_runtime.handlers))
        self.assertFalse(hasattr(agent, "artifact_service"))
        self.assertNotIn("artifact_service", inspect.signature(CoreAgent).parameters)
        self.assertIsNone(importlib.util.find_spec("core_agent.artifact_service"))
        instructions = json.dumps(agent.kernel_compiler.capability_policies)
        for name in RETIRED_TOOLS:
            self.assertNotIn(name, instructions)

    def test_retired_allowlist_names_and_dotted_aliases_have_upgrade_diagnostics(self):
        for canonical in RETIRED_TOOLS:
            for name in (canonical, canonical.replace("_", ".")):
                with self.subTest(name=name):
                    with self.assertRaises(CoreError) as caught:
                        self.build(CORE_AGENT_ALLOWED_BUILTIN_TOOLS=name)
                    self.assertEqual(caught.exception.code, "CONFIG_INVALID")
                    self.assertIn("CORE_AGENT_ALLOWED_BUILTIN_TOOLS", str(caught.exception))
                    self.assertIn(name, str(caught.exception))
                    self.assertIn("retired", str(caught.exception).lower())

    def test_retired_environment_is_rejected_without_values(self):
        for name in RETIRED_ENV:
            with self.subTest(name=name):
                with self.assertRaises(CoreError) as caught:
                    self.build(**{name: "PRIVATE_VALUE_CANARY"})
                self.assertEqual(caught.exception.code, "CONFIG_INVALID")
                self.assertIn(name, str(caught.exception))
                self.assertIn("retired", str(caught.exception).lower())
                self.assertNotIn("PRIVATE_VALUE_CANARY", str(caught.exception))

    def test_saved_config_cannot_restore_retired_catalog_or_policy(self):
        app, _ = self.build()
        agent = app.state.core_agent
        raw = agent.agent_config.to_dict()
        raw["features"]["artifacts"] = True
        raw["tools"]["builtins"]["allow"] = ["core_task_list", *RETIRED_TOOLS]
        platform = replace(agent.platform_config, allowed_builtin_tools={"core_task_list", *RETIRED_TOOLS},
                           supported_features={"background_tasks", "artifacts"})
        effective = compile_effective_config(platform, AgentConfig.from_dict(raw), (), {})
        self.assertFalse(set(RETIRED_TOOLS) & set(effective.model_tool_catalog))
        self.assertNotIn("artifacts", effective.enabled_capability_policies)
        self.assertFalse(set(RETIRED_TOOLS) & set(agent._tool_catalog(effective, {})))

    def test_retired_settings_and_tool_aliases_fail_before_runtime_allocation(self):
        for environment in ({"ARTIFACT_MONGODB_CUSTOM": "PRIVATE_VALUE_CANARY"},
                            {"CORE_AGENT_ALLOWED_BUILTIN_TOOLS": "core.artifact.save"}):
            with self.subTest(environment=tuple(environment)), patch("core_agent.app._state", side_effect=AssertionError("runtime allocated")):
                with self.assertRaises(CoreError) as caught:
                    self.build(**environment)
                self.assertEqual(caught.exception.code, "CONFIG_INVALID")
                self.assertNotIn("PRIVATE_VALUE_CANARY", str(caught.exception))

    def test_legacy_binary_batch_is_explicitly_refused_before_model_or_storage(self):
        app, model = self.build()
        message = Message("user", (Part.text("keep the files"), Part.file(b"", filename="empty.bin"),
                                   Part.file(bytes(range(256)), filename="last.bin")), (), {}, context_id="chat")
        request = parse_run_request(message)
        original = request.attachments
        with self.assertRaises(CoreError) as caught:
            app.state.store_attachments(request, "owner", "chat")
        self.assertEqual(caught.exception.code, "CONTENT_TYPE_NOT_SUPPORTED")
        self.assertEqual(request.attachments, original)
        self.assertEqual(request.prompt, "keep the files")
        self.assertEqual(model.calls, ())
        self.assertFalse(hasattr(app.state.core_agent, "artifact_service"))

    def test_response_mime_types_survive_slim_image_without_legacy_import(self):
        expected = {
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".odt": "application/vnd.oasis.opendocument.text",
            ".ods": "application/vnd.oasis.opendocument.spreadsheet",
            ".odp": "application/vnd.oasis.opendocument.presentation",
            ".epub": "application/epub+zip", ".rtf": "application/rtf",
            ".7z": "application/x-7z-compressed", ".webp": "image/webp",
        }
        source = "import json,mimetypes; mimetypes.knownfiles=[]; mimetypes.init(files=[]); " + (
            "assert '.pptx' not in mimetypes.types_map; import core_agent.response_files; " +
            "print(json.dumps({ext:mimetypes.guess_type('file'+ext)[0] for ext in " + repr(list(expected)) + "}))"
        )
        result = subprocess.run([sys.executable, "-c", source], check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout), expected)


class ArtifactRemovalRecoveryTests(unittest.TestCase):
    use_postgres = False

    def setUp(self):
        database = PostgresDatabase(TEST_DATABASE_URL) if self.use_postgres else None
        if database is not None:
            self.addCleanup(database.close)
        self.app, self.model = ArtifactToolRemovalTests.build(self, database=database,
            SESSION_STORAGE_TYPE="postgres" if self.use_postgres else "in-memory")
        if self.use_postgres:
            self.assertIsInstance(self.app.state.core_agent.workflow_store, PostgresWorkflowStore)
        self.tenant = "removal-recovery-" + uuid.uuid4().hex

    def old_snapshot(self, record, effective):
        snapshot = copy.deepcopy(record.snapshot)
        raw = snapshot["admission"]["agent_config"]
        raw["features"]["artifacts"] = True
        raw["tools"]["builtins"]["allow"] = sorted(set(raw["tools"]["builtins"]["allow"]) | set(RETIRED_TOOLS))
        for platform in (snapshot["admission"]["platform_config"], snapshot["effective_platform_config"]):
            platform["allowed_builtin_tools"] = sorted(set(platform["allowed_builtin_tools"]) | set(RETIRED_TOOLS))
            platform["supported_features"] = sorted(set(platform["supported_features"]) | {"artifacts"})
        # The pre-removal compiler's persisted audit shape: only these two fields
        # differ for its default named-file capability. Verified against that compiler.
        audit = json.loads(effective.audit_snapshot)
        audit["builtin_tools"] = sorted(set(audit["builtin_tools"]) | set(RETIRED_TOOLS))
        audit["policies"] = sorted(set(audit["policies"]) | {"artifacts"})
        snapshot["effective_config_digest"] = hashlib.sha256(json.dumps(audit, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return snapshot

    def admitted(self, task_id):
        agent = self.app.state.core_agent
        task_id += "-" + uuid.uuid4().hex
        return agent._new_workflow(RunRequest("work"), task_id=task_id, identity="owner",
                                   session_id=task_id, tenant_id=self.tenant)

    def test_pre_removal_snapshot_is_rejected_and_current_snapshot_recovers(self):
        agent = self.app.state.core_agent
        record, _raw, _discovered, effective = self.admitted("old-artifact-config")
        store = agent.workflow_store
        worker = "old-snapshot-fixture"
        token = store.acquire_lease(record.run_id, tenant_id=record.tenant_id, owner_id=record.owner_id, worker_id=worker, ttl=60)
        agent._record_transition(record, state=record.state, snapshot=self.old_snapshot(record, effective),
                                 event_kind="fixture.old_snapshot", lease_token=token)
        store.release_lease(record.run_id, tenant_id=record.tenant_id, worker_id=worker, token=token)
        agent._runtime_cache.clear()
        with self.assertRaises(CoreError) as caught:
            agent.resume_task(record.task_id)
        self.assertEqual(caught.exception.code, "CHECKPOINT_INVALID")
        self.assertEqual(self.model.calls, ())
        rejected = store.lookup_task(record.task_id)
        self.assertEqual((rejected.state, rejected.error_code), ("FAILED", "CHECKPOINT_INVALID"))
        self.assertEqual(rejected.snapshot["admission"]["agent_config"]["features"]["artifacts"], True)

        current, *_ = self.admitted("current-artifact-free-config")
        agent._runtime_cache.clear()
        result = agent.resume_task(current.task_id)
        self.assertEqual(result.message, "ok")
        self.assertFalse(set(RETIRED_TOOLS) & set(self.model.calls[-1].tools))
        self.assertEqual(store.lookup_task(current.task_id).state, "COMPLETED")

    def test_retired_executing_mutation_retains_unknown_intent_without_replay(self):
        agent = self.app.state.core_agent
        record, _raw, _discovered, effective = self.admitted("old-artifact-mutation")
        store = agent.workflow_store
        worker = "stopped-artifact-fixture"
        token = store.acquire_lease(record.run_id, tenant_id=record.tenant_id, owner_id=record.owner_id, worker_id=worker, ttl=60)
        record = store.register_execution(record, instance_id=agent.tool_runtime.environment_manager.instance_id,
                                          worker_id=worker, generation=token, lease_token=token)
        pending = {"id": "old-save", "name": "core_artifact_save", "arguments": {"filename": "report.txt", "content": "prepared before upgrade"}}
        snapshot = self.old_snapshot(record, effective)
        snapshot.update(pending_call=pending, tool_queue=[pending], tool_calls=1, pending_mutating=True)
        snapshot["pending_response"] = CoreAgent._response_dict(ModelResponse(tool_requests=(
            ToolRequest(pending["id"], pending["name"], pending["arguments"]),)))
        store.consume_budget(record, tool_calls=1)
        record = agent._record_transition(record, state="EXECUTING", snapshot=snapshot, event_kind="tool.intent",
            event_data={"tool_call_id": pending["id"], "mutating": True}, lease_token=token)
        store.confirm_execution(record, record.snapshot["execution_owner"])
        store.release_lease(record.run_id, tenant_id=record.tenant_id, worker_id=worker, token=token)
        dispatches = []
        agent.tool_runtime.handlers["core_artifact_save"] = lambda *args: dispatches.append(args)
        agent._runtime_cache.clear()
        with self.assertRaises(CoreError) as caught:
            agent.resume_task(record.task_id)
        self.assertEqual(caught.exception.code, "SIDE_EFFECT_UNKNOWN")
        self.assertEqual(dispatches, [])
        self.assertEqual(self.model.calls, ())
        aborted = store.lookup_task(record.task_id)
        self.assertEqual((aborted.state, aborted.error_code), ("ABORTED", "SIDE_EFFECT_UNKNOWN"))
        self.assertEqual(aborted.snapshot["pending_call"], pending)
        self.assertEqual(aborted.snapshot["pending_mutating"], True)
        scope = {"tenant_id": record.tenant_id} if self.use_postgres else {}
        reconciliation = [item for item in agent.audit_log.records(record.run_id, **scope)
                          if item.kind == "execution.reconciliation_required"]
        self.assertEqual(len(reconciliation), 1)
        self.assertEqual(reconciliation[0].data["tool_call_id"], pending["id"])


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL is required")
class PostgresArtifactRemovalRecoveryTests(ArtifactRemovalRecoveryTests):
    use_postgres = True
