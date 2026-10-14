import copy
import math
import unittest
from unittest.mock import Mock

from core_agent.errors import CoreError
from core_agent.observability import FailingExporter, RecordingExporter, Telemetry


class GuardrailOperatorTelemetryTests(unittest.TestCase):
    def event(self, kind="guardrail.classified"):
        return {
            "event_id": "d52c987e-d93d-5d80-9968-9daf6ebfc623",
            "kind": kind,
            "created_at": 1760000000.0,
            "data": {
                "run_id": "run-1", "task_id": "task-1", "tenant_id": "company-1",
                "context_id": "chat-1", "source_id": "call-1", "review_id": "review-1",
                "source_kind": "tool_result", "verdict": "unverified", "reason": "timeout",
                "attempts_used": 1, "input_tokens_used": 20, "prompt_tokens": 10,
                "completion_tokens": 5, "elapsed_seconds": 60.0,
                "detector_deadline": 1760000060.0, "owner_deadline": 1760086400.0,
            },
        }

    def test_committed_events_have_correlated_spans_and_logs_without_content_capture(self):
        exporter = RecordingExporter()
        telemetry = Telemetry(exporter)
        with telemetry.span("core_agent.task.execute") as parent:
            for kind in ("guardrail.classified", "guardrail.wait.entered",
                         "guardrail.wait.resolved", "guardrail.wait.applied"):
                event = self.event(kind)
                if kind != "guardrail.classified":
                    event["data"]["wait_id"] = "wait-1"
                if kind.endswith(("resolved", "applied")):
                    event["data"]["resolution_reason"] = "allowed"
                self.assertTrue(telemetry.publish_diagnostic(event))
        spans = [span for span in exporter.spans if span.name.startswith("core_agent.guardrail.")]
        self.assertEqual(len(spans), 4)
        self.assertEqual(len(exporter.logs), 4)
        for span, (kind, attrs) in zip(spans, exporter.logs):
            self.assertEqual(span.context.trace_id, parent.context.trace_id)
            self.assertEqual(span.attributes["core_agent.diagnostic.event_id"], self.event()["event_id"])
            self.assertEqual(attrs["a2a.task.id"], "task-1")
            self.assertEqual(attrs["core_agent.guardrail.detector_deadline"], 1760000060.0)
            self.assertEqual(attrs["core_agent.guardrail.owner_deadline"], 1760086400.0)
            self.assertEqual(kind, span.name.removeprefix("core_agent."))

    def test_export_failure_is_reported_without_throwing_or_acknowledging_delivery(self):
        telemetry = Telemetry(FailingExporter())
        self.assertFalse(telemetry.publish_diagnostic(self.event()))
        self.assertGreater(telemetry.dropped_records, 0)

    def test_replayed_telemetry_retains_the_committed_event_identity(self):
        exporter = RecordingExporter()
        telemetry = Telemetry(exporter)
        event = self.event()
        self.assertTrue(telemetry.publish_diagnostic(event))
        self.assertTrue(telemetry.publish_diagnostic(copy.deepcopy(event)))
        self.assertEqual([span.attributes["core_agent.diagnostic.event_id"] for span in exporter.spans],
                         [event["event_id"], event["event_id"]])

    def test_private_or_unknown_data_is_rejected_even_with_content_capture(self):
        for field in ("payload", "content_digest", "owner_answer", "arguments", "stderr", "provider_error"):
            with self.subTest(field=field):
                exporter = RecordingExporter()
                event = self.event()
                event["data"][field] = "PRIVATE-CANARY"
                self.assertFalse(Telemetry(exporter, content_enabled=True).publish_diagnostic(event))
                self.assertEqual((exporter.spans, exporter.logs), ([], []))

    def test_invalid_enums_numbers_identifiers_and_unrelated_events_do_not_reach_exporters(self):
        cases = [
            ("verdict", "PRIVATE-CANARY"), ("reason", "a detailed raw exception"),
            ("source_id", "/workspace/private/file"), ("source_kind", "arbitrary-kind"),
            ("attempts_used", True), ("attempts_used", -1), ("input_tokens_used", 2**64),
            ("elapsed_seconds", math.nan), ("elapsed_seconds", math.inf),
            ("detector_deadline", "PRIVATE-CANARY"),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                exporter = RecordingExporter()
                event = self.event()
                event["data"][field] = value
                self.assertFalse(Telemetry(exporter, content_enabled=True).publish_diagnostic(event))
                self.assertEqual((exporter.spans, exporter.logs), ([], []))
        exporter = RecordingExporter()
        event = self.event("public.answer")
        self.assertFalse(Telemetry(exporter).publish_diagnostic(event))
        self.assertEqual((exporter.spans, exporter.logs), ([], []))


class SandboxOperatorTelemetryTests(unittest.TestCase):
    def test_private_diagnostics_are_visible_in_operator_span_and_log_only(self):
        from core_agent.errors import SandboxDiagnostics

        exporter = RecordingExporter()
        error = CoreError("EXECUTION_ENVIRONMENT_UNAVAILABLE", diagnostics=SandboxDiagnostics(
            stage="network", reason="timeout", elapsed_ms=25, cleanup="confirmed"))
        with Telemetry(exporter).span("core_agent.terminal.process") as span:
            span.record_error(error)
        self.assertEqual(error.data, {})
        self.assertEqual(span.attributes["core_agent.sandbox.stage"], "network")
        self.assertEqual(span.attributes["core_agent.sandbox.reason"], "timeout")
        self.assertEqual(span.attributes["core_agent.sandbox.cleanup"], "confirmed")
        self.assertTrue(any(body == "sandbox.failed" for body, _ in exporter.logs))

    def test_arbitrary_error_diagnostics_never_invoke_a_serializer_or_export_contents(self):
        exporter = RecordingExporter()
        error = CoreError("EXECUTION_ENVIRONMENT_UNAVAILABLE")
        error.diagnostics = Mock()
        error.diagnostics.as_dict.return_value = {"stage": "PRIVATE-CANARY"}
        with Telemetry(exporter, content_enabled=True).span("core_agent.terminal.process") as span:
            span.record_error(error)
        error.diagnostics.as_dict.assert_not_called()
        self.assertFalse(any(key.startswith("core_agent.sandbox.") for key in span.attributes))
        self.assertNotIn("PRIVATE-CANARY", str(exporter.logs))

    def test_failing_exporter_does_not_replace_a_sandbox_error(self):
        from core_agent.errors import SandboxDiagnostics

        error = CoreError("SIDE_EFFECT_UNKNOWN", diagnostics=SandboxDiagnostics(
            stage="cleanup", reason="timeout", elapsed_ms=25, cleanup="unconfirmed"))
        telemetry = Telemetry(FailingExporter())
        with telemetry.span("core_agent.terminal.process") as span:
            span.record_error(error)
        self.assertEqual(error.code, "SIDE_EFFECT_UNKNOWN")
        self.assertEqual(error.data, {})
        self.assertGreater(telemetry.dropped_records, 0)

    def test_forged_typed_diagnostics_are_revalidated_before_export(self):
        from core_agent.errors import SandboxDiagnostics

        exporter = RecordingExporter()
        diagnostic = SandboxDiagnostics(stage="network", reason="timeout", elapsed_ms=25, cleanup="confirmed")
        object.__setattr__(diagnostic, "reason", "PRIVATE-CANARY")
        error = CoreError("SIDE_EFFECT_UNKNOWN", diagnostics=diagnostic)
        with Telemetry(exporter, content_enabled=True).span("core_agent.terminal.process") as span:
            span.record_error(error)
        self.assertFalse(any(key.startswith("core_agent.sandbox.") for key in span.attributes))
        self.assertNotIn("PRIVATE-CANARY", str(exporter.logs))


if __name__ == "__main__":
    unittest.main()
