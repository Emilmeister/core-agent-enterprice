import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core_agent.errors import CoreError
from core_agent.execution import LocalTerminalBackend, TerminalSessionManager
from tests.app_support import create_app
from core_agent.model import ModelResponse, ScriptedModel, ToolRequest
from tests.test_auth import AuthAppTestCase, TEST_DATABASE_URL


class ChatWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def manager(self):
        manager = TerminalSessionManager(
            LocalTerminalBackend(self.root / "scratch", chat_root=self.root / "chats")
        )
        self.addCleanup(manager.close)
        return manager

    @staticmethod
    def bind(manager, run_id, *, tenant="company", owner="external-a", context="chat-a"):
        from core_agent.workspace import WorkspaceBinding

        manager.bind_run(run_id, WorkspaceBinding(tenant, owner, context))

    @staticmethod
    def python(manager, run_id, code):
        return manager.execute_transient({"argv": [sys.executable, "-c", code]}, run_id)

    def test_chat_files_survive_new_run_and_manager_restart(self):
        manager = self.manager()
        self.bind(manager, "first")
        result = self.python(manager, "first", "open('result.txt', 'w').write('persistent')")
        self.assertEqual(result.exit_code, 0, result.stdout)
        path = manager.workspace_file("first", "result.txt")
        manager.destroy_run("first")
        self.assertEqual(path.read_text(), "persistent")
        manager.close()

        reopened = self.manager()
        self.bind(reopened, "next")
        result = self.python(reopened, "next", "print(open('result.txt').read())")
        self.assertEqual(result.exit_code, 0, result.stdout)
        self.assertIn("persistent", result.stdout)
        self.assertEqual(reopened.workspace_file("next", "result.txt"), path)

    def test_scope_components_separate_folders_and_cannot_rebind_a_run(self):
        manager = self.manager()
        paths = set()
        for index, changes in enumerate(({}, {"tenant": "other"}, {"owner": "external-b"}, {"context": "chat-b"})):
            run_id = f"run-{index}"
            self.bind(manager, run_id, **changes)
            result = self.python(manager, run_id, "from pathlib import Path; assert not Path('marker').exists(); Path('marker').touch()")
            self.assertEqual(result.exit_code, 0, result.stdout)
            paths.add(manager.workspace_file(run_id, "marker"))
        self.assertEqual(len(paths), 4)
        with self.assertRaises(CoreError) as caught:
            self.bind(manager, "run-0", owner="external-b")
        self.assertEqual(caught.exception.code, "WORKSPACE_SCOPE_CONFLICT")

    def test_execution_without_trusted_binding_fails_before_start(self):
        manager = self.manager()
        with self.assertRaises(CoreError) as caught:
            self.python(manager, "unbound", "raise AssertionError('must not run')")
        self.assertEqual(caught.exception.code, "WORKSPACE_SCOPE_REQUIRED")
        self.assertFalse(manager._run_environments)

    def test_destroy_releases_binding_even_if_no_process_was_started(self):
        manager = self.manager()
        self.bind(manager, "no-process")
        manager.destroy_run("no-process")
        self.assertNotIn("no-process", manager._run_bindings)

    def test_a_symlink_in_the_chat_path_cannot_redirect_workspace_creation(self):
        from core_agent.workspace import WorkspaceBinding

        manager = self.manager()
        self.bind(manager, "first")
        self.python(manager, "first", "open('marker', 'w').close()")
        path = manager.workspace_file("first", "marker").parent
        manager.destroy_run("first")
        saved = path.with_name("original-workspace")
        path.rename(saved)
        target = self.root / "foreign"
        target.mkdir()
        path.symlink_to(target, target_is_directory=True)
        with self.assertRaises(CoreError):
            manager.bind_run("second", WorkspaceBinding("company", "external-a", "chat-a"))
            self.python(manager, "second", "open('escaped', 'w').close()")
        self.assertFalse((target / "escaped").exists())

    def test_persistent_chat_never_restores_old_base_snapshot(self):
        snapshot = self.root / "old-snapshot"
        snapshot.mkdir()
        (snapshot / "deleted.txt").write_text("old")
        manager = TerminalSessionManager(LocalTerminalBackend(
            self.root / "scratch", chat_root=self.root / "chats", base_snapshot=str(snapshot),
        ))
        self.addCleanup(manager.close)
        for run in ("first", "second"):
            self.bind(manager, run)
            result = self.python(manager, run, "from pathlib import Path; assert not Path('deleted.txt').exists()")
            self.assertEqual(result.exit_code, 0, result.stdout)
            manager.destroy_run(run)


class RuntimeChatWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict(os.environ, {
            "CORE_AGENT_ENVIRONMENT": "test",
            "CORE_AGENT_MEMORY": "disabled",
            "LOCAL_WORKSPACE_ROOT": self.temp.name + "/scratch",
            "CHAT_WORKSPACE_ROOT": self.temp.name + "/chats",
            "CORE_AGENT_ALLOWED_BUILTIN_TOOLS": "core_terminal_exec,core_python_exec",
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def app(self, tool, arguments):
        model = ScriptedModel([
            ModelResponse(tool_requests=(ToolRequest("call", tool, arguments),)),
            ModelResponse(message="done"),
        ])
        model.model = "workspace-test"
        app = create_app(model=model)
        self.addCleanup(app.state.close)
        return app, model

    def assert_persistent_chat(self, read_tool, read_arguments):
        app, model = self.app("core_terminal_exec", {
            "argv": [sys.executable, "-c", "open('shared.txt','w').write('saved-for-chat')"],
        })
        result = app.state.core_agent.run({"prompt": "write"}, identity="external-a", session_id="chat", tenant_id="company")
        self.assertEqual(result.message, "done")
        self.assertNotIn("WORKSPACE_SCOPE_REQUIRED", model.calls[1].context)
        app.state.close()

        reopened, model = self.app(read_tool, read_arguments)
        result = reopened.state.core_agent.run({"prompt": "read"}, identity="external-a", session_id="chat", tenant_id="company")
        self.assertEqual(result.message, "done")
        self.assertIn("saved-for-chat", model.calls[1].context)

    def test_terminal_uses_persistent_chat_after_app_restart(self):
        self.assert_persistent_chat("core_terminal_exec", {"argv": [sys.executable, "-c", "print(open('shared.txt').read())"]})

    def test_python_reads_terminal_files_after_app_restart(self):
        self.assert_persistent_chat("core_python_exec", {"code": "print(open('shared.txt').read())"})

    def test_recovery_reconstructs_binding_from_persisted_workflow(self):
        app, _model = self.app("core_terminal_exec", {"argv": ["pwd"]})
        agent = app.state.core_agent
        record, *_ = agent._new_workflow({"prompt": "resume"}, task_id="task", identity="external-a", session_id="chat", tenant_id="company")
        manager = agent.tool_runtime.environment_manager
        manager._run_bindings.clear()
        agent._runtime_cache.clear()
        agent._load_workflow_runtime(record)
        from core_agent.workspace import WorkspaceBinding
        self.assertEqual(manager._run_bindings[record.run_id], WorkspaceBinding("company", "external-a", "chat"))

    def test_roots_cannot_overlap_in_either_direction(self):
        for changes in (
            {"CHAT_WORKSPACE_ROOT": self.temp.name},
            {"DURABLE_STORAGE_ROOT": self.temp.name + "/chats/durable"},
            {"LOCAL_WORKSPACE_ROOT": self.temp.name + "/chats/scratch"},
        ):
            with self.subTest(changes=changes), patch.dict(os.environ, changes):
                with self.assertRaises(CoreError) as caught:
                    self.app("core_terminal_exec", {"argv": ["pwd"]})
                self.assertEqual(caught.exception.code, "CONFIG_INVALID")

    def test_completed_text_runs_do_not_retain_workspace_bindings(self):
        app, model = self.app("core_terminal_exec", {"argv": ["pwd"]})
        model._responses = [ModelResponse(message="done"), ModelResponse(message="done")]
        for index in range(2):
            app.state.core_agent.run({"prompt": "text only"}, session_id=f"chat-{index}")
        manager = app.state.core_agent.tool_runtime.environment_manager
        self.assertFalse(manager._run_environments)
        self.assertFalse(manager._run_bindings)


    def test_child_background_and_background_recovery_use_parent_chat(self):
        import copy
        import threading
        from core_agent.tools import ToolCall

        with patch.dict(os.environ, {"CORE_AGENT_ALLOWED_BUILTIN_TOOLS": "core_terminal_exec,core_delegate,core_task_start"}):
            app, model = self.app("core_terminal_exec", {
                "argv": [sys.executable, "-c", "assert open('parent.txt').read()=='parent'; open('child.txt','w').write('child')"],
            })
        agent = app.state.core_agent
        record, *_ = agent._new_workflow({"prompt": "parent"}, task_id="parent", identity="external-a", session_id="chat", tenant_id="company")
        manager = agent.tool_runtime.environment_manager
        self.assertEqual(manager.execute_transient({"argv": [sys.executable, "-c", "open('parent.txt','w').write('parent')"]}, record.run_id).exit_code, 0)
        arguments = {"tool": "core_terminal_exec", "arguments": {
            "argv": [sys.executable, "-c", "assert open('parent.txt').read()=='parent'; open('background.txt','w').write('background'); print('background')"],
        }}
        token = agent.workflow_store.acquire_lease(record.run_id, tenant_id="company", owner_id=record.owner_id,
                                                 worker_id=agent._worker_id, ttl=60)
        snapshot = copy.deepcopy(record.snapshot)
        snapshot.update(tool_calls=1, pending_call={"id": "start", "name": "core_task_start", "arguments": arguments})
        try:
            record = agent._record_transition(record, state="MODEL_RESPONDED", snapshot=snapshot,
                                             event_kind="tool.attempt.started", consume_tool_calls=1, lease_token=token)
            background = agent._task_start(arguments, record.run_id, wait_context=(
                record, record.snapshot, ToolCall("start", "core_task_start", arguments), token,
            ))
        finally:
            agent.workflow_store.release_lease(record.run_id, tenant_id="company", worker_id=agent._worker_id, token=token)
        task = agent.task_scheduler.wait(background["task_id"], timeout=5, owner_id=record.run_id, tenant_id="company")
        self.assertEqual(task.state, "completed", task.error)
        background_record = agent.workflow_store.lookup_task(task.id)
        contract = {"workflow_version": 1, "run_id": background_record.run_id, "task_id": task.id,
                    "tenant_id": background_record.tenant_id, "identity": background_record.owner_id}
        agent._runtime_cache.clear()
        recovered = agent._recover_background_tool(contract, threading.Event())
        self.assertIn("background", recovered["stdout"])
        self.assertEqual(recovered, task.result)
        self.assertNotIn(contract["run_id"], manager._run_bindings)
        self.assertEqual(manager.workspace_file(record.run_id, "background.txt").read_text(), "background")
        child = agent._delegate({"instruction": "write a child file", "tools": ["core_terminal_exec"], "skills": [], "budget": {"turns": 3, "tool_calls": 1}}, record.run_id)
        self.assertEqual(child["state"], "completed", child)
        self.assertEqual(manager.workspace_file(record.run_id, "child.txt").read_text(), "child")
        self.assertNotIn("WORKSPACE_SCOPE_REQUIRED", model.calls[1].context)


class AuthenticatedWorkspaceTests(AuthAppTestCase):
    async def test_authenticated_startup_requires_chat_root(self):
        with patch.dict(os.environ, {"CHAT_WORKSPACE_ROOT": ""}):
            with self.assertRaises(CoreError) as caught:
                create_app(model=self.model)
        self.assertEqual(caught.exception.code, "CONFIG_INVALID")
        self.assertEqual(len(self.model.calls), 0)

    async def test_owner_uses_external_chats_original_workspace_and_other_caller_is_denied(self):
        from core_agent.workspace import WorkspaceBinding

        self.model._responses = [
            ModelResponse(tool_requests=(ToolRequest("write", "core_terminal_exec", {
                "argv": [sys.executable, "-c", "open('shared.txt','w').write('external-file')"],
            }),)),
            ModelResponse(message="written"),
            ModelResponse(tool_requests=(ToolRequest("read", "core_terminal_exec", {
                "argv": [sys.executable, "-c", "print(open('shared.txt').read())"],
            }),)),
            ModelResponse(message="read"),
        ]
        first = await self.submit("external-a", "first", "shared-chat")
        second = await self.submit("owner-a", "second", "shared-chat")
        self.assertIn("external-file", self.model.calls[3].context)
        result = await self.http.post("/a2a/external/message:send", headers=self.headers("external-b"), json={
            "message": {"messageId": "foreign", "contextId": "shared-chat", "role": "ROLE_USER", "parts": [{"text": "read"}]},
        })
        self.assertEqual(result.status_code, 404, result.text)
        self.assertEqual(len(self.model.calls), 4)
        agent = self.app.state.core_agent
        original = agent.workflow_store.lookup_task(first['id'])
        continued = agent.workflow_store.lookup_task(second['id'])
        self.assertEqual(original.owner_id, continued.owner_id)
        manager = agent.tool_runtime.environment_manager
        workspace = manager.backend.chats.workspace(WorkspaceBinding(original.tenant_id, original.owner_id, original.context_id))
        self.assertEqual((workspace / 'shared.txt').read_text(), 'external-file')
        self.assertFalse(manager._environments)

    async def test_legacy_workflow_without_authenticated_chat_mapping_cannot_open_files(self):
        with self.assertRaises(CoreError) as caught:
            self.app.state.core_agent.run({"prompt": "legacy"}, identity="company-owners", session_id="unmapped", tenant_id=os.environ["CORE_AGENT_TENANT_ID"])
        self.assertEqual(caught.exception.code, "WORKSPACE_SCOPE_REQUIRED")
        self.assertEqual(len(self.model.calls), 0)
        manager = self.app.state.core_agent.tool_runtime.environment_manager
        self.assertFalse(manager._run_environments)


@unittest.skipUnless(TEST_DATABASE_URL, "requires PostgreSQL")
class PostgresAuthenticatedWorkspaceTests(AuthenticatedWorkspaceTests):
    use_postgres = True


if __name__ == "__main__":
    unittest.main()
