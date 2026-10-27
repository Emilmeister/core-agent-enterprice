import copy
import io
import json
import logging
import math
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

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


class ErrorLogDiagnosticsTests(unittest.TestCase):
    def test_exception_groups_keep_bounded_transport_causes(self):
        from core_agent.errors import error_log_fields

        error = CoreError("MCP_CONNECTION_FAILED", "PRIVATE-CANARY")
        error.__cause__ = ExceptionGroup("PRIVATE-CANARY", [
            URLError(ConnectionRefusedError(111, "PRIVATE-CANARY"))])
        diagnostic = error_log_fields(error)
        self.assertEqual([item["error_type"] for item in diagnostic["causes"]],
                         ["ExceptionGroup", "URLError", "ConnectionRefusedError"])
        self.assertEqual(diagnostic["causes"][-1]["errno"], 111)
        self.assertNotIn("PRIVATE-CANARY", json.dumps(diagnostic))
        error.__cause__ = ExceptionGroup("PRIVATE-CANARY", [ValueError("PRIVATE-CANARY") for _ in range(100)])
        self.assertEqual(len(error_log_fields(error)["causes"]), 5)

    def test_foreign_exception_class_name_cannot_mask_original_failure(self):
        from core_agent.errors import error_log_fields

        class NameFailure(type):
            def __getattribute__(self, name):
                if name == "__name__":
                    raise RuntimeError("PRIVATE-CANARY")
                return super().__getattribute__(name)

        class ForeignError(Exception, metaclass=NameFailure):
            pass

        error = ForeignError("PRIVATE-CANARY")
        self.assertEqual(error_log_fields(error), {"error_type": "Exception"})

    def test_standalone_error_log_is_structured_and_best_effort(self):
        from core_agent.errors import log_error

        error = CoreError("MODEL_UNAVAILABLE", "PRIVATE-CANARY", retryable=True)
        with self.assertLogs("core_agent.runtime", "WARNING") as captured:
            log_error("model.failed", error, task_id="task-1")
        payload = json.loads(captured.records[0].getMessage())
        self.assertEqual(payload, {"event": "model.failed", "error_type": "CoreError",
                                  "error_code": "MODEL_UNAVAILABLE", "retryable": True, "task_id": "task-1"})
        self.assertNotIn("PRIVATE-CANARY", captured.output[0])
        with self.assertLogs("core_agent.runtime", "WARNING") as captured:
            log_error("delivery.failed", error, task_id="task-1", delivery_id="/private/path?secret",
                      context_id=123, run_id="x" * 257)
        payload = json.loads(captured.records[0].getMessage())
        self.assertEqual(payload["task_id"], "task-1")
        self.assertFalse({"delivery_id", "context_id", "run_id"} & payload.keys())
        logger = Mock(spec=logging.Logger)
        logger.log.side_effect = RuntimeError("PRIVATE-CANARY")
        self.assertIsNone(log_error("model.failed", error, logger=logger, level=logging.ERROR))
        logger.log.assert_called_once()

    def test_httpx_and_requests_statuses_exclude_response_content(self):
        import httpx
        import requests
        from core_agent.errors import error_log_fields

        secret = "PRIVATE-CANARY"
        request = httpx.Request("POST", "https://user:" + secret + "@remote.test?key=" + secret)
        response = httpx.Response(503, request=request, content=secret, headers={"Authorization": secret})
        other = requests.Response()
        other.status_code = 403
        other._content = secret.encode()
        other.url = str(request.url)
        other.headers["Authorization"] = secret
        errors = ((httpx.HTTPStatusError(secret, request=request, response=response), 503),
                  (requests.HTTPError(secret, response=other), 403))
        for error, status in errors:
            with self.subTest(error=type(error).__name__):
                diagnostic = error_log_fields(error)
                self.assertEqual(diagnostic["http_status"], status)
                self.assertNotIn(secret, json.dumps(diagnostic))

    def test_private_causes_survive_suppression_without_messages_or_credentials(self):
        from core_agent.errors import error_log_fields

        secret = "PRIVATE-CANARY-password"
        provider = HTTPError("https://user:" + secret + "@model.test", 429, secret,
                             {"Authorization": secret}, io.BytesIO(secret.encode()))
        try:
            try:
                raise provider
            except HTTPError:
                raise CoreError("MODEL_UNAVAILABLE", secret, retryable=True,
                                data={"body": secret}) from None
        except CoreError as error:
            diagnostic = error_log_fields(error)
        self.assertEqual(diagnostic["error_code"], "MODEL_UNAVAILABLE")
        self.assertIs(diagnostic["retryable"], True)
        self.assertEqual(diagnostic["causes"][0]["error_type"], "HTTPError")
        self.assertEqual(diagnostic["causes"][0]["http_status"], 429)
        self.assertNotIn(secret, json.dumps(diagnostic))
        exporter = RecordingExporter()
        wrapper = CoreError("MODEL_UNAVAILABLE", secret)
        wrapper.__cause__ = provider
        with Telemetry(exporter).span("gen_ai.chat") as span:
            span.record_error(wrapper)
        self.assertEqual(json.loads(span.attributes["core_agent.error.causes"])[0]["http_status"], 429)
        self.assertNotIn(secret, json.dumps(span.attributes))
        self.assertEqual(provider.fp.getvalue(), secret.encode())
        provider.close()

    def test_cause_cycles_transport_reason_and_forged_fields_are_bounded(self):
        from core_agent.errors import error_log_fields

        nested = TimeoutError(110, "PRIVATE-CANARY")
        transport = URLError(nested)
        error = CoreError("MODEL_UNAVAILABLE")
        error.__cause__ = transport
        nested.__cause__ = error
        error.model_failure = {"kind": "timeout", "exception_type": "TimeoutError",
            "elapsed_ms": -1, "timeout_seconds": math.nan, "max_tokens": True,
            "prompt_tokens": "PRIVATE-CANARY", "reasoning_available": "PRIVATE-CANARY",
            "request_phase": "PRIVATE-CANARY", "body": "PRIVATE-CANARY"}
        error.diagnostics = Mock()
        error.diagnostics.as_dict.return_value = {"reason": "PRIVATE-CANARY"}
        diagnostic = error_log_fields(error)
        self.assertEqual(diagnostic["provider_failure"], {"kind": "timeout", "exception_type": "TimeoutError"})
        self.assertEqual(len(diagnostic["causes"]), 2)
        self.assertEqual(diagnostic["causes"][1]["errno"], 110)
        error.diagnostics.as_dict.assert_not_called()
        self.assertNotIn("PRIVATE-CANARY", json.dumps(diagnostic))
        error.sqlstate = "23505"
        self.assertEqual(error_log_fields(error)["sqlstate"], "23505")
        error.sqlstate = "PRIVATE-CANARY"
        self.assertNotIn("sqlstate", error_log_fields(error))
        for _ in range(20):
            wrapper = CoreError("MODEL_UNAVAILABLE")
            wrapper.__cause__ = error
            error = wrapper
        self.assertEqual(len(error_log_fields(error)["causes"]), 5)

    def test_model_failure_has_finite_usage_request_details_and_project_frames(self):
        from core_agent.errors import error_log_fields
        from core_agent.model import CompatibleHttpModel

        secret = "PRIVATE-CANARY"
        payload = {"choices": [{"message": {"content": None, "reasoning_content": secret},
                                "finish_reason": "length"}], "usage": {
            "prompt_tokens": 123, "completion_tokens": 16384, "total_tokens": 16507,
            "completion_tokens_details": {"reasoning_tokens": 16384}}}
        model = CompatibleHttpModel(api_format="openai", model="test", timeout=20, max_tokens=16384)
        with patch("core_agent.model.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())), \
                patch("core_agent.model.time.monotonic", side_effect=[100, 100.25]):
            try:
                model.generate(context=secret, instructions=secret, tools={})
            except CoreError as error:
                diagnostic = error_log_fields(error)
            else:
                self.fail("reasoning-only response must remain an error")
        self.assertEqual(diagnostic["provider_failure"], {
            "kind": "no_text", "exception_type": "CoreError", "response_format": "none",
            "finish_reason": "length", "elapsed_ms": 250, "timeout_seconds": 20.0,
            "max_tokens": 16384, "request_phase": "parse", "prompt_tokens": 123,
            "completion_tokens": 16384, "total_tokens": 16507, "reasoning_tokens": 16384,
            "reasoning_available": True})
        self.assertTrue(any(frame["filename"] == "model.py" for frame in diagnostic["traceback"]))
        for frame in diagnostic["traceback"]:
            self.assertEqual(set(frame), {"filename", "function", "line"})
            self.assertNotIn("/", frame["filename"])
            self.assertGreater(frame["line"], 0)
        self.assertNotIn(secret, json.dumps(diagnostic))

    def test_invalid_tool_response_preserves_error_and_private_span_diagnostics(self):
        from core_agent.errors import error_log_fields
        from core_agent.model import CompatibleHttpModel

        secret = "PRIVATE-CANARY"
        payload = {"choices": [{"message": {"tool_calls": [{"function": secret}]},
                                "finish_reason": "tool_calls"}]}
        model = CompatibleHttpModel(api_format="openai", model="test")
        exporter = RecordingExporter()
        with patch("core_agent.model.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())):
            try:
                with Telemetry(exporter).span("gen_ai.chat"):
                    model.generate(context="test", instructions="test", tools={})
            except CoreError as error:
                self.assertEqual(error.code, "MODEL_RESPONSE_INVALID")
                self.assertFalse(error.retryable)
                self.assertEqual(error.data, {})
                diagnostic = error_log_fields(error)
            else:
                self.fail("invalid tool function must remain an error")
        self.assertEqual(diagnostic["provider_failure"]["kind"], "invalid_response")
        attrs = exporter.spans[0].attributes
        self.assertEqual(attrs["core_agent.error.code"], "MODEL_RESPONSE_INVALID")
        self.assertEqual(json.loads(attrs["core_agent.error.provider_failure"]), diagnostic["provider_failure"])
        self.assertNotIn(secret, json.dumps(attrs))


if __name__ == "__main__":
    unittest.main()
