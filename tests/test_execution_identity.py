import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from a2a.types import a2a_pb2
from google.protobuf.json_format import MessageToDict

from core_agent.database import reconcile_workflow_task
from core_agent.errors import CoreError
from core_agent.execution_identity import execution_host, stopped_by_restart
from core_agent.execution_reconciliation import read_stop_confirmation


HOST = dict(version=1, platform="docker", slot="a" * 64, boot_id="b" * 32,
            pid_namespace=123, init_start=100)


class ExecutionIdentityTests(unittest.TestCase):
    def test_restart_requires_same_slot_same_boot_and_later_init(self):
        self.assertTrue(stopped_by_restart(HOST, {**HOST, "init_start": 200, "pid_namespace": 124}))
        for current in (HOST, None, {}, {**HOST, "boot_id": "c" * 32}, {**HOST, "slot": "d" * 64},
                        {**HOST, "platform": "kubernetes"}, {**HOST, "init_start": 99},
                        {**HOST, "pid_namespace": 124}, {**HOST, "init_start": True}):
            with self.subTest(current=current):
                self.assertFalse(stopped_by_restart(HOST, current))
                self.assertFalse(stopped_by_restart(None, current))

    def test_docker_identity_comes_from_mounts_not_hostname(self):
        values = {
            "/proc/self/mountinfo": "\n".join(f"1 2 0:1 /docker/containers/{'a' * 64}/{name} /etc/{name} ro - ext4 /dev/test rw" for name in ("hosts", "hostname")),
            "/proc/sys/kernel/random/boot_id": "b" * 32,
            "/proc/1/stat": "1 (core-agent) S " + " ".join(["0"] * 18 + ["100"]),
        }
        def read(path):
            return values[str(path)]
        with patch("core_agent.execution_identity.sys.platform", "linux"), patch("os.getpid", return_value=1), \
                patch.dict(os.environ, {"HOSTNAME": "forged"}, clear=True), \
                patch.object(Path, "read_text", read), patch("os.stat", return_value=SimpleNamespace(st_ino=123)):
            self.assertEqual(execution_host(), HOST)
            values["/proc/self/mountinfo"] = values["/proc/self/mountinfo"].replace("/etc/hosts ro", "/unrelated ro")
            self.assertIsNone(execution_host())

    def test_non_pid_one_cannot_attest_container_restart(self):
        with patch("core_agent.execution_identity.sys.platform", "linux"), patch("os.getpid", return_value=42):
            self.assertIsNone(execution_host())

    def test_kubernetes_requires_trusted_pod_and_container(self):
        values = {"/proc/self/mountinfo": "", "/proc/sys/kernel/random/boot_id": "b" * 32,
                  "/proc/1/stat": "1 (core-agent) S " + " ".join(["0"] * 18 + ["100"])}
        pod = "12345678-1234-1234-1234-123456789abc"
        with patch("core_agent.execution_identity.sys.platform", "linux"), patch("os.getpid", return_value=1), \
                patch.dict(os.environ, {"CORE_AGENT_POD_UID": pod, "CORE_AGENT_CONTAINER_NAME": "agent"}, clear=True), \
                patch.object(Path, "read_text", lambda path: values[str(path)]), patch("os.stat", return_value=SimpleNamespace(st_ino=123)):
            self.assertEqual(execution_host()["slot"], pod + "/agent")
            del os.environ["CORE_AGENT_CONTAINER_NAME"]
            self.assertIsNone(execution_host())

    def test_operator_confirmation_is_bounded_exact_and_tenant_scoped(self):
        value = dict(version=1, tenant_id="company", owner_id="owner", run_id="run", instance_id="instance",
                     worker_id="worker", generation="generation", evidence="Docker: exact container stopped")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proof.json"
            path.write_text(json.dumps(value))
            self.assertEqual(read_stop_confirmation(path, "company"), value)
            with self.assertRaises(CoreError):
                read_stop_confirmation(path, "other")
            for invalid in ({**value, "version": True}, {**value, "generation": ""}, {**value, "extra": 1}):
                path.write_text(json.dumps(invalid))
                with self.assertRaises(CoreError):
                    read_stop_confirmation(path, "company")
            path.write_text(" " * 65537)
            with self.assertRaises(CoreError):
                read_stop_confirmation(path, "company")
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(CoreError):
                read_stop_confirmation(link, "company")

    def test_projection_reports_cleanup_without_private_host_and_clears_on_terminal(self):
        for version in (1, 2):
            for pending in ({"terminal_intent": {"state": "ABORTED"}, "execution_cleanup_pending": True},
                            {"execution_cleanup_pending": True}):
                with self.subTest(version=version, pending=pending):
                    task = a2a_pb2.Task(id="task", context_id="chat")
                    record = dict(run_id="run", task_id="task", context_id="chat", tenant_id="tenant", owner_id="owner",
                                  state="EXECUTING", snapshot={"output_contract_version": version,
                                      "execution_owner": {"execution_host": HOST}, **pending}, result=None)
                    self.assertTrue(reconcile_workflow_task(task, run_id="run", state="EXECUTING", version=5, record=record))
                    metadata = MessageToDict(task.metadata)
                    self.assertEqual(metadata["core_agent_execution"], {"version": 1, "state": "cleanup_pending"})
                    self.assertNotIn("execution_host", json.dumps(metadata))
                    record.update(state="ABORTED", error_code="SIDE_EFFECT_UNKNOWN")
                    reconcile_workflow_task(task, run_id="run", state="ABORTED", version=6,
                                            error_code="SIDE_EFFECT_UNKNOWN", record=record)
                    self.assertEqual(task.status.state, a2a_pb2.TASK_STATE_FAILED)
                    self.assertNotIn("core_agent_execution", task.metadata)
