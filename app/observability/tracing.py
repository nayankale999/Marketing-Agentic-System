"""OpenTelemetry tracing initialisation.

Defaults to the OTLP gRPC exporter pointing at `settings.otel_exporter_otlp_endpoint`.
If the collector isn't reachable, BatchSpanProcessor logs warnings but the app
continues — tracing failures must never break a request.

Tests that want to capture spans pass `exporter=InMemorySpanExporter()` and
`processor_class=SimpleSpanProcessor` and set `force=True` to override the
already-initialised provider.
"""

from collections.abc import Callable
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

from app.settings.config import get_settings

_INITIALISED = False
_PROVIDER: TracerProvider | None = None

ProcessorFactory = Callable[[SpanExporter], Any]


def init_tracing(
    *,
    exporter: SpanExporter | None = None,
    processor_factory: ProcessorFactory = BatchSpanProcessor,
    force: bool = False,
) -> TracerProvider:
    """Set up the global TracerProvider. Returns the provider in use.

    Idempotent unless `force=True` (used by tests that want to swap exporters).
    """
    global _INITIALISED, _PROVIDER

    if _INITIALISED and not force and _PROVIDER is not None:
        return _PROVIDER

    settings = get_settings()
    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.version": "0.0.1",
        }
    )

    provider = TracerProvider(resource=resource)
    if exporter is None:
        exporter = OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint, insecure=True)
    provider.add_span_processor(processor_factory(exporter))
    trace.set_tracer_provider(provider)

    _INITIALISED = True
    _PROVIDER = provider
    return provider


def instrument_app(app: Any) -> None:
    """Auto-instrument a FastAPI app. Safe to call multiple times — the
    instrumentor is itself idempotent on a given app.
    """
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app)
