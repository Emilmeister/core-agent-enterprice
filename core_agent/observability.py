from __future__ import annotations

import secrets
import contextvars
import os
import re
from dataclasses import dataclass

from .errors import CoreError


def _hex(bytes_count):
    return secrets.token_hex(bytes_count)


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    span_id: str
    trace_flags: str = "01"
    tenant_id: str | None = None
    authorization: str | None = None

    @property
    def traceparent(self):
        return f"00-{self.trace_id}-{self.span_id}-{self.trace_flags}"


class RecordingExporter:
    def __init__(self):
        self.spans = []
        self.metrics = []
        self.logs = []

    def export_span(self, span):
        self.spans.append(span)

    def export_metric(self, metric):
        self.metrics.append(metric)

    def export_log(self, record):
        self.logs.append(record)


class FailingExporter:
    def export_span(self, span):
        raise RuntimeError("export failed")

    def export_metric(self, metric):
        raise RuntimeError("export failed")

    def export_log(self, record):
        raise RuntimeError("export failed")


class OtlpExporter:
    """Bridge the small runtime interface to the official OTel SDK/OTLP HTTP exporters."""

    def __init__(
        self,
        *,
        endpoint=None,
        trace_endpoint=None,
        metric_endpoint=None,
        log_endpoint=None,
        service_name="core-agent",
    ):
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create(
            {"service.name": service_name, "telemetry.semconv.version": "1.43.0"}
        )
        base = endpoint.rstrip("/") if endpoint else None
        trace_endpoint = trace_endpoint or (f"{base}/v1/traces" if base else None)
        metric_endpoint = metric_endpoint or (f"{base}/v1/metrics" if base else None)
        log_endpoint = log_endpoint or (f"{base}/v1/logs" if base else None)
        self.trace_provider = TracerProvider(resource=resource)
        if trace_endpoint:
            self.trace_provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=trace_endpoint))
            )
        metric_readers = []
        if metric_endpoint:
            metric_readers.append(
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=metric_endpoint)
                )
            )
        self.meter_provider = MeterProvider(
            resource=resource,
            metric_readers=metric_readers,
        )
        self.logger_provider = LoggerProvider(resource=resource)
        if log_endpoint:
            self.logger_provider.add_log_record_processor(
                BatchLogRecordProcessor(OTLPLogExporter(endpoint=log_endpoint))
            )
        self.tracer = self.trace_provider.get_tracer("core_agent", "1.0")
        self.meter = self.meter_provider.get_meter("core_agent", "1.0")
        self.logger = self.logger_provider.get_logger("core_agent", "1.0")
        self._counters = {}
        self._active_spans = {}

    @staticmethod
    def _otel_context(context):
        from opentelemetry.trace import (
            NonRecordingSpan,
            SpanContext,
            TraceFlags,
            TraceState,
            set_span_in_context,
        )

        span_context = SpanContext(
            trace_id=int(context.trace_id, 16),
            span_id=int(context.span_id, 16),
            is_remote=True,
            trace_flags=TraceFlags(int(context.trace_flags, 16)),
            trace_state=TraceState(),
        )
        return set_span_in_context(NonRecordingSpan(span_context))

    @staticmethod
    def _links(links):
        from opentelemetry.trace import Link, SpanContext, TraceFlags, TraceState

        return [
            Link(
                SpanContext(
                    int(link.trace_id, 16),
                    int(link.span_id, 16),
                    True,
                    TraceFlags(int(link.trace_flags, 16)),
                    TraceState(),
                )
            )
            for link in links
            if link is not None
        ]

    def start_span(self, name, *, parent, attributes, links):
        sdk_span = self.tracer.start_span(
            name,
            context=self._otel_context(parent) if parent else None,
            attributes=attributes,
            links=self._links(links),
        )
        context = sdk_span.get_span_context()
        runtime_context = TraceContext(
            f"{context.trace_id:032x}",
            f"{context.span_id:016x}",
            f"{int(context.trace_flags):02x}",
        )
        self._active_spans[runtime_context.span_id] = sdk_span
        return runtime_context

    def export_span(self, span):
        sdk_span = self._active_spans.pop(span.context.span_id, None)
        if sdk_span:
            try:
                self._set_status(sdk_span, span.status_code, span.status_message)
            finally:
                sdk_span.end()
            return
        with self.tracer.start_as_current_span(
            span.name,
            context=self._otel_context(span.context),
            attributes=span.attributes,
            links=self._links(span.links),
        ) as sdk_span:
            self._set_status(sdk_span, span.status_code, span.status_message)

    @staticmethod
    def _set_status(sdk_span, code, message=""):
        from opentelemetry.trace import Status, StatusCode

        status_code = StatusCode.ERROR if code == "ERROR" else StatusCode.OK
        sdk_span.set_status(Status(status_code, message if code == "ERROR" else None))

    def set_span_attribute(self, context, key, value):
        sdk_span = self._active_spans.get(context.span_id)
        if sdk_span:
            sdk_span.set_attribute(key, value)

    def export_metric(self, metric):
        name, value, labels = metric
        counter = self._counters.setdefault(name, self.meter.create_counter(name))
        counter.add(value, attributes=labels)

    def export_log(self, record):
        body, attributes = record
        self.logger.emit(body=body, attributes=attributes)

    def shutdown(self):
        self.trace_provider.shutdown()
        self.meter_provider.shutdown()
        self.logger_provider.shutdown()


@dataclass
class Span:
    telemetry: object
    name: str
    context: TraceContext
    attributes: dict
    links: tuple[TraceContext, ...] = ()
    ended: bool = False
    status_code: str = "UNSET"
    status_message: str = ""
    _token: object = None

    def __enter__(self):
        self._token = self.telemetry._current.set(self.context)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._token is not None:
            self.telemetry._current.reset(self._token)
        self.end(exc)

    def set_attribute(self, key, value):
        if self.ended or not self.telemetry._content_allowed(key):
            return
        self.attributes[key] = value
        self.telemetry._set_span_attribute(self.context, key, value)

    def set_attributes(self, attributes):
        for key, value in attributes.items():
            self.set_attribute(key, value)

    def record_error(self, error):
        if self.ended:
            return
        self.status_code = "ERROR"
        self.status_message = type(error).__name__
        self.set_attribute("error.type", type(error).__name__)
        code = getattr(error, "code", None)
        if code:
            self.set_attribute("core_agent.error.code", code)

    def end(self, error=None):
        if self.ended:
            return
        if error is None and self.status_code == "UNSET":
            self.status_code = "OK"
        elif error is not None:
            self.record_error(error)
        self.ended = True
        self.telemetry._export_span(self)


class Telemetry:
    FORBIDDEN_LABELS = {"task_id", "run_id", "tenant_id", "user_id", "trace_id"}
    CONTENT_KEYS = {
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
        "input.value",
        "output.value",
        "tool.description",
        "tool.json_schema",
        "tool.parameters",
    }
    CONTENT_PREFIXES = (
        "llm.input_messages.",
        "llm.output_messages.",
        "llm.tools.",
    )

    def __init__(self, exporter, content_enabled=False):
        self.exporter = exporter
        self.content_enabled = content_enabled
        self.dropped_records = 0
        self._current = contextvars.ContextVar(
            f"telemetry-current-{id(self)}", default=None
        )

    @classmethod
    def otlp(
        cls,
        *,
        endpoint=None,
        trace_endpoint=None,
        metric_endpoint=None,
        log_endpoint=None,
        service_name="core-agent",
        content_enabled=False,
    ):
        return cls(
            OtlpExporter(
                endpoint=endpoint,
                trace_endpoint=trace_endpoint,
                metric_endpoint=metric_endpoint,
                log_endpoint=log_endpoint,
                service_name=service_name,
            ),
            content_enabled=content_enabled,
        )

    @classmethod
    def otlp_from_env(cls, *, service_name="core-agent", content_enabled=None):
        endpoints = {
            "endpoint": os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
            "trace_endpoint": os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"),
            "metric_endpoint": os.getenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"),
            "log_endpoint": os.getenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"),
        }
        if not any(endpoints.values()):
            return None
        if content_enabled is None:
            content_enabled = os.getenv(
                "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "false"
            ).lower() in {"1", "true", "yes"}
        return cls.otlp(
            **endpoints,
            service_name=service_name,
            content_enabled=content_enabled,
        )

    def extract(self, carrier):
        value = carrier.get("traceparent")
        if not value:
            return None
        parts = value.split("-")
        if (
            len(parts) != 4
            or parts[0] != "00"
            or not re.fullmatch(r"[0-9a-f]{32}", parts[1])
            or not re.fullmatch(r"[0-9a-f]{16}", parts[2])
            or not re.fullmatch(r"[0-9a-f]{2}", parts[3])
            or int(parts[1], 16) == 0
            or int(parts[2], 16) == 0
        ):
            self.dropped_records += 1
            return None
        return TraceContext(parts[1], parts[2], parts[3])

    def inject(self, context, carrier):
        carrier["traceparent"] = context.traceparent

    @staticmethod
    def _span_kind(name):
        if name.startswith("gen_ai."):
            return "LLM"
        if name in {"core_agent.task.execute", "core_agent.subagent.execute"}:
            return "AGENT"
        if name in {"core_agent.tool.execute", "mcp.client"} or name.startswith(
            "core_agent.terminal."
        ):
            return "TOOL"
        if name.endswith(".rerank"):
            return "RERANKER"
        if name.endswith(".embed"):
            return "EMBEDDING"
        if name.startswith("memory_service.search."):
            return "RETRIEVER"
        return "CHAIN"

    def _content_allowed(self, key):
        return self.content_enabled or (
            key not in self.CONTENT_KEYS
            and not any(key.startswith(prefix) for prefix in self.CONTENT_PREFIXES)
        )

    def span(self, name, *, attributes=None, parent=None, links=(), _new_trace=False):
        attrs = dict(attributes or {})
        attrs.setdefault("openinference.span.kind", self._span_kind(name))
        if not self.content_enabled:
            attrs = {
                key: value for key, value in attrs.items() if self._content_allowed(key)
            }
        if parent is None and not _new_trace:
            parent = self._current.get()
        starter = getattr(self.exporter, "start_span", None)
        context = (
            starter(name, parent=parent, attributes=attrs, links=links)
            if starter
            else TraceContext(
                parent.trace_id if parent else _hex(16),
                _hex(8),
                parent.trace_flags if parent else "01",
            )
        )
        return Span(self, name, context, attrs, tuple(links))

    def start_background_span(self, name, linked_context, *, attributes=None):
        return self.span(
            name,
            attributes=attributes,
            links=(linked_context,) if linked_context is not None else (),
            _new_trace=True,
        )

    def current_context(self):
        return self._current.get()

    def _export_span(self, span):
        try:
            self.exporter.export_span(span)
        except Exception:
            self.dropped_records += 1

    def _set_span_attribute(self, context, key, value):
        setter = getattr(self.exporter, "set_span_attribute", None)
        if not setter:
            return
        try:
            setter(context, key, value)
        except Exception:
            self.dropped_records += 1

    def metric(self, name, value, *, labels=None):
        labels = labels or {}
        if self.FORBIDDEN_LABELS & set(labels):
            raise CoreError("TELEMETRY_CARDINALITY_VIOLATION")
        try:
            self.exporter.export_metric((name, value, dict(labels)))
        except Exception:
            self.dropped_records += 1

    def log(self, body, *, attributes=None):
        attributes = dict(attributes or {})
        if not self.content_enabled:
            attributes = {
                key: value
                for key, value in attributes.items()
                if self._content_allowed(key)
            }
        try:
            self.exporter.export_log((body, attributes))
        except Exception:
            self.dropped_records += 1

    def shutdown(self):
        shutdown = getattr(self.exporter, "shutdown", None)
        if shutdown:
            shutdown()
