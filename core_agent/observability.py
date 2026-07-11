from __future__ import annotations

import secrets
import contextvars
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

    def __init__(self, *, endpoint=None, service_name="core-agent"):
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
        trace_exporter = OTLPSpanExporter(
            endpoint=f"{base}/v1/traces" if base else None
        )
        metric_exporter = OTLPMetricExporter(
            endpoint=f"{base}/v1/metrics" if base else None
        )
        log_exporter = OTLPLogExporter(endpoint=f"{base}/v1/logs" if base else None)
        self.trace_provider = TracerProvider(resource=resource)
        self.trace_provider.add_span_processor(BatchSpanProcessor(trace_exporter))
        self.meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[PeriodicExportingMetricReader(metric_exporter)],
        )
        self.logger_provider = LoggerProvider(resource=resource)
        self.logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(log_exporter)
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
            sdk_span.end()
            return
        with self.tracer.start_as_current_span(
            span.name,
            context=self._otel_context(span.context),
            attributes=span.attributes,
            links=self._links(span.links),
        ):
            pass

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
    _token: object = None

    def __enter__(self):
        self._token = self.telemetry._current.set(self.context)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._token is not None:
            self.telemetry._current.reset(self._token)
        self.end()

    def end(self):
        if self.ended:
            return
        self.ended = True
        self.telemetry._export_span(self)


class Telemetry:
    FORBIDDEN_LABELS = {"task_id", "run_id", "tenant_id", "user_id", "trace_id"}
    CONTENT_KEYS = {
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
    }

    def __init__(self, exporter, content_enabled=False):
        self.exporter = exporter
        self.content_enabled = content_enabled
        self.dropped_records = 0
        self._current = contextvars.ContextVar(
            f"telemetry-current-{id(self)}", default=None
        )

    @classmethod
    def otlp(cls, *, endpoint=None, service_name="core-agent", content_enabled=False):
        return cls(
            OtlpExporter(endpoint=endpoint, service_name=service_name),
            content_enabled=content_enabled,
        )

    def extract(self, carrier):
        value = carrier.get("traceparent")
        if not value:
            return TraceContext(_hex(16), _hex(8))
        parts = value.split("-")
        if len(parts) != 4:
            return TraceContext(_hex(16), _hex(8))
        return TraceContext(parts[1], parts[2], parts[3])

    def inject(self, context, carrier):
        carrier["traceparent"] = context.traceparent

    def span(self, name, *, attributes=None, parent=None, links=(), _new_trace=False):
        attrs = dict(attributes or {})
        if not self.content_enabled:
            attrs = {
                key: value
                for key, value in attrs.items()
                if key not in self.CONTENT_KEYS
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

    def start_background_span(self, name, linked_context):
        return self.span(name, links=(linked_context,), _new_trace=True)

    def _export_span(self, span):
        try:
            self.exporter.export_span(span)
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
                if key not in self.CONTENT_KEYS
            }
        try:
            self.exporter.export_log((body, attributes))
        except Exception:
            self.dropped_records += 1

    def shutdown(self):
        shutdown = getattr(self.exporter, "shutdown", None)
        if shutdown:
            shutdown()
