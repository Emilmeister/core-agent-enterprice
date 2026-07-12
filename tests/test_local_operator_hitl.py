import tempfile
import threading
import unittest
from pathlib import Path

from core_agent.approvals import ApprovalManager, ApproveAllControlPlane
from core_agent.errors import CoreError
from core_agent.tools import ToolCall


def pending(manager, **changes):
    values = {
        "call": ToolCall(
            "call-1",
            "email.send",
            {
                "target": "alice@example.com",
                "subject": "Incident follow-up",
                "__runtime_trace_id": "trace-a",
            },
        ),
        "risks": {"acts_as_user", "external_write"},
        "task_id": "task-1",
        "context_id": "context-1",
        "tenant_id": "tenant-1",
        "caller_principal_id": "caller-1",
        "environment": "production",
        "policy_version": "policy-1",
    }
    values.update(changes)
    return manager.request(**values)


class LocalOperatorApprovalTests(unittest.TestCase):
    def test_digest_binds_semantic_action_and_excludes_runtime_fields(self):
        manager = ApprovalManager()
        first = pending(manager)
        same = pending(
            manager,
            call=ToolCall(
                "call-2",
                "email.send",
                {
                    "subject": "Incident follow-up",
                    "target": "alice@example.com",
                    "__runtime_trace_id": "trace-b",
                },
            ),
        )
        changed = pending(
            manager,
            call=ToolCall(
                "call-3",
                "email.send",
                {"target": "bob@example.com", "subject": "Incident follow-up"},
            ),
        )
        other_caller = pending(manager, caller_principal_id="caller-2")
        other_environment = pending(manager, environment="staging")
        self.assertEqual(first.action_digest, same.action_digest)
        self.assertEqual(
            first.action_digest,
            "sha256:68b2bf7962937fc5c5b9b08615b040a70187cbccdcefd1c9d31e69299112c77d",
        )
        self.assertNotEqual(first.action_digest, changed.action_digest)
        self.assertNotEqual(first.action_digest, other_caller.action_digest)
        self.assertNotEqual(first.action_digest, other_environment.action_digest)
        manager.close()

    def test_secret_plaintext_is_rejected_and_secret_ref_is_persisted(self):
        manager = ApprovalManager()
        with self.assertRaises(CoreError) as caught:
            pending(
                manager,
                call=ToolCall(
                    "call-secret",
                    "email.send",
                    {"target": "alice@example.com", "access_token": "top-secret"},
                ),
            )
        self.assertEqual(caught.exception.code, "SECRET_REFERENCE_REQUIRED")
        approval = pending(
            manager,
            call=ToolCall(
                "call-ref",
                "email.send",
                {
                    "target": "alice@example.com",
                    "access_token": {"secretRef": "secret://email/sender"},
                },
            ),
        )
        self.assertIn("secret://email/sender", approval.proposal.arguments_json)
        manager.close()

    def test_duplicate_approve_creates_one_reservation(self):
        manager = ApprovalManager()
        approval = pending(manager)
        control_plane = ApproveAllControlPlane()
        barrier = threading.Barrier(3)
        results = []

        def approve():
            barrier.wait()
            results.append(control_plane.approve(manager, approval))

        threads = [threading.Thread(target=approve) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(len({item.id for item in results}), 1)
        self.assertEqual(manager.get(approval.id).state, "CONSUMED")
        execution = manager.execution_for(approval.id)
        self.assertEqual(execution.state, "RESERVED")
        self.assertEqual(
            execution.idempotency_key,
            f"{approval.task_id}:{approval.id}:{approval.action_digest}",
        )
        self.assertEqual(
            manager.get(approval.id).operator_principal_id, "local-operator-stub"
        )
        manager.close()

    def test_exact_digest_is_rechecked_before_dispatch(self):
        manager = ApprovalManager()
        approval = pending(manager)
        ApproveAllControlPlane().approve(manager, approval)
        changed = ToolCall(
            approval.tool_call_id,
            approval.tool_name,
            {"target": "mallory@example.com", "subject": "Incident follow-up"},
        )
        with self.assertRaises(CoreError) as caught:
            manager.authorize_dispatch(approval.id, changed)
        self.assertEqual(caught.exception.code, "APPROVAL_ARGUMENTS_CHANGED")
        self.assertEqual(manager.execution_for(approval.id).state, "RESERVED")
        manager.close()

    def test_cancel_before_approve_wins_and_approve_before_cancel_is_not_cancelable(
        self,
    ):
        manager = ApprovalManager()
        canceled = pending(manager)
        manager.cancel(canceled.id)
        with self.assertRaises(CoreError) as caught:
            ApproveAllControlPlane().approve(manager, canceled)
        self.assertEqual(caught.exception.code, "APPROVAL_ALREADY_RESOLVED")
        self.assertIsNone(manager.execution_for(canceled.id))

        reserved = pending(manager)
        ApproveAllControlPlane().approve(manager, reserved)
        with self.assertRaises(CoreError) as caught:
            manager.cancel(reserved.id)
        self.assertEqual(caught.exception.code, "TASK_NOT_CANCELABLE")
        self.assertIsNotNone(manager.execution_for(reserved.id))
        manager.close()

    def test_cancel_approve_race_has_one_committed_winner(self):
        manager = ApprovalManager()
        approval = pending(manager)
        barrier = threading.Barrier(3)
        outcomes = []

        def run(operation):
            barrier.wait()
            try:
                operation()
                outcomes.append("committed")
            except CoreError as error:
                outcomes.append(error.code)

        threads = [
            threading.Thread(
                target=run,
                args=(lambda: ApproveAllControlPlane().approve(manager, approval),),
            ),
            threading.Thread(target=run, args=(lambda: manager.cancel(approval.id),)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        current = manager.get(approval.id)
        execution = manager.execution_for(approval.id)
        self.assertEqual(outcomes.count("committed"), 1)
        if current.state == "CANCELED":
            self.assertIsNone(execution)
        else:
            self.assertEqual(current.state, "CONSUMED")
            self.assertEqual(execution.state, "RESERVED")
        manager.close()

    def test_expired_approval_is_rejected_and_persisted(self):
        now = [100.0]
        manager = ApprovalManager(ttl_seconds=1, clock=lambda: now[0])
        approval = pending(manager)
        now[0] = 102.0
        with self.assertRaises(CoreError) as caught:
            ApproveAllControlPlane().approve(manager, approval)
        self.assertEqual(caught.exception.code, "APPROVAL_EXPIRED")
        self.assertEqual(manager.get(approval.id).state, "EXPIRED")
        self.assertIsNone(manager.execution_for(approval.id))
        manager.close()

    def test_sqlite_reopen_preserves_pending_and_reserved_records(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "approvals.sqlite3"
            first = ApprovalManager(path)
            approval = pending(first)
            first.close()

            recovered = ApprovalManager(path)
            restored = recovered.get(approval.id)
            self.assertEqual(restored.state, "PENDING")
            self.assertEqual(restored.action_digest, approval.action_digest)
            execution = ApproveAllControlPlane().approve(recovered, restored)
            recovered.close()

            reopened = ApprovalManager(path)
            self.assertEqual(reopened.get(approval.id).state, "CONSUMED")
            self.assertEqual(reopened.execution_for(approval.id).id, execution.id)
            reopened.close()


if __name__ == "__main__":
    unittest.main()
