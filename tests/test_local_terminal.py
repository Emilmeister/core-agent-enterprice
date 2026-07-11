import concurrent.futures
import os
import sys
import tempfile
import unittest
from pathlib import Path

from core_agent.errors import CoreError
from core_agent.execution import EnvironmentSpec, LocalTerminalBackend, TerminalSessionManager


def spec(run_id, owner_id, snapshot, durable_root):
    return EnvironmentSpec(
        tenant_id="tenant-1",
        run_id=run_id,
        owner_id=owner_id,
        workspace_snapshot=str(snapshot),
        durable_root=str(durable_root),
        writable_paths=(".",),
        network_allowlist=(),
        environment_allowlist=("SAFE",),
        secrets={},
    )


class LocalTerminalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.local_root = root / "local"
        self.durable_root = root / "s3"
        self.snapshot = self.durable_root / "snapshots" / "base"
        self.snapshot.mkdir(parents=True)
        (self.snapshot / "base.txt").write_text("base\n", encoding="utf-8")
        self.manager = TerminalSessionManager(LocalTerminalBackend(self.local_root))

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def test_main_and_children_use_distinct_local_workspaces_from_same_snapshot(self):
        main = self.manager.create(spec("run-1", "main", self.snapshot, self.durable_root))
        child = self.manager.create(spec("run-1", "child-1", self.snapshot, self.durable_root))
        self.assertNotEqual(main.id, child.id)
        self.assertNotEqual(main.workspace, child.workspace)
        self.assertTrue(main.workspace.is_relative_to(self.local_root))
        self.assertTrue(child.workspace.is_relative_to(self.local_root))
        self.assertFalse(main.workspace.is_relative_to(self.durable_root))
        self.assertEqual((main.workspace / "base.txt").read_text(), "base\n")
        (main.workspace / "base.txt").write_text("main\n", encoding="utf-8")
        self.assertEqual((child.workspace / "base.txt").read_text(), "base\n")

    def test_parallel_sessions_have_distinct_ptys_process_groups_and_output(self):
        main = self.manager.create(spec("run-1", "main", self.snapshot, self.durable_root))
        child = self.manager.create(spec("run-1", "child-1", self.snapshot, self.durable_root))

        def execute(session, owner, marker):
            return self.manager.execute(
                session.id,
                {
                    "argv": [
                        sys.executable,
                        "-c",
                        f"import pathlib,time; pathlib.Path('{marker}.txt').write_text('{marker}'); time.sleep(.03); print('{marker}')",
                    ],
                    "timeout": 1,
                },
                owner_id=owner,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            main_future = pool.submit(execute, main, "main", "MAIN")
            child_future = pool.submit(execute, child, "child-1", "CHILD")
            main_result, child_result = main_future.result(), child_future.result()
        self.assertEqual(main_result.stdout.strip(), "MAIN")
        self.assertEqual(child_result.stdout.strip(), "CHILD")
        self.assertNotEqual(main_result.terminal_session_id, child_result.terminal_session_id)
        self.assertNotEqual(main_result.process_group_id, child_result.process_group_id)
        self.assertTrue(main_result.used_pty)
        self.assertTrue(child_result.used_pty)
        self.assertTrue((main.workspace / "MAIN.txt").exists())
        self.assertFalse((main.workspace / "CHILD.txt").exists())
        self.assertTrue((child.workspace / "CHILD.txt").exists())

    def test_session_owner_is_checked_for_execute_write_read_wait_and_destroy(self):
        session = self.manager.create(spec("run-1", "child-1", self.snapshot, self.durable_root))
        operations = (
            lambda: self.manager.execute(session.id, {"argv": [sys.executable, "-V"]}, owner_id="main"),
            lambda: self.manager.start(session.id, {"argv": [sys.executable, "-V"]}, owner_id="main"),
            lambda: self.manager.read(session.id, "missing", owner_id="main"),
            lambda: self.manager.write(session.id, "missing", "x", owner_id="main"),
            lambda: self.manager.wait(session.id, "missing", owner_id="main"),
            lambda: self.manager.destroy(session.id, owner_id="main"),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaises(CoreError) as caught:
                    operation()
                self.assertEqual(caught.exception.code, "POLICY_DENIED")

    def test_interactive_pty_accepts_input_and_returns_one_bounded_result(self):
        session = self.manager.create(spec("run-1", "child-1", self.snapshot, self.durable_root))
        process = self.manager.start(
            session.id,
            {
                "argv": [sys.executable, "-u", "-c", "value=input(); print('got:'+value)"],
                "max_output_bytes": 1024,
            },
            owner_id="child-1",
        )
        self.manager.write(session.id, process.id, "hello\n", owner_id="child-1")
        result = self.manager.wait(session.id, process.id, owner_id="child-1", timeout=1)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("got:hello", result.stdout)
        self.assertFalse(result.truncated)

    def test_process_environment_is_clean_and_only_explicit_allowlist_is_added(self):
        session = self.manager.create(spec("run-1", "main", self.snapshot, self.durable_root))
        os.environ["UNSCOPED_SECRET"] = "hidden"
        try:
            result = self.manager.execute(
                session.id,
                {
                    "argv": [
                        sys.executable,
                        "-c",
                        "import os; print(os.getenv('UNSCOPED_SECRET','missing')+':'+os.getenv('SAFE','missing'))",
                    ],
                    "env": {"SAFE": "visible"},
                },
                owner_id="main",
            )
        finally:
            os.environ.pop("UNSCOPED_SECRET", None)
        self.assertEqual(result.stdout.strip(), "missing:visible")

        with self.assertRaises(CoreError) as caught:
            self.manager.execute(
                session.id,
                {"argv": [sys.executable, "-V"], "env": {"NOT_ALLOWED": "x"}},
                owner_id="main",
            )
        self.assertEqual(caught.exception.code, "POLICY_DENIED")

    def test_invalid_argv_cwd_and_timeout_fail_closed(self):
        session = self.manager.create(spec("run-1", "main", self.snapshot, self.durable_root))
        for request in (
            {"argv": "echo unsafe"},
            {"argv": [sys.executable, "-V"], "cwd": "../escape"},
        ):
            with self.subTest(request=request):
                with self.assertRaises(CoreError) as caught:
                    self.manager.execute(session.id, request, owner_id="main")
                self.assertEqual(caught.exception.code, "TOOL_ARGUMENT_INVALID")

        result = self.manager.execute(
            session.id,
            {"argv": [sys.executable, "-c", "import time; time.sleep(5)"], "timeout": 0.03},
            owner_id="main",
        )
        self.assertTrue(result.timed_out)
        self.assertEqual(result.status, "timed_out")
        self.assertEqual(result.cleanup, "process_group_terminated")


if __name__ == "__main__":
    unittest.main()
