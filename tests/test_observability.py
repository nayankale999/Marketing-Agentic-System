"""W9: secret redaction + tracing initialisation."""

import io
import logging

import pytest
import structlog
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.observability.logging import (
    REDACTED,
    SECRET_PATTERNS,
    _redact_string,
    redact_secrets,
)
from app.observability.tracing import init_tracing


@pytest.mark.parametrize(
    "literal",
    [
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234",
        "github_pat_11ABCDEFGH0_abcd1234efgh5678",
        "sk-1234567890abcdefghijklmnop",
        "xoxb-1234567890-abcdefghij",
        "Bearer eyJabc.def.ghi",
        "Bearer 1234567890abcdef",
        "AKIAABCDEFGHIJKLMNOP",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signaturepart",
    ],
)
def test_redact_string_strips_known_patterns(literal: str) -> None:
    text = f"prefix {literal} suffix"
    redacted = _redact_string(text)
    assert REDACTED in redacted
    assert literal not in redacted


def test_redact_secrets_processor_walks_nested_dicts() -> None:
    event_dict: dict = {
        "event": "login.failed",
        "token": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234",
        "context": {
            "headers": {"Authorization": "Bearer abc123def456"},
            "params": ["sk-1234567890abcdefghijklmnop", "safe"],
        },
        "count": 1,
    }
    redact_secrets(None, "info", event_dict)
    assert event_dict["token"] == REDACTED
    assert event_dict["context"]["headers"]["Authorization"] == f"{REDACTED}"
    assert event_dict["context"]["params"][0] == REDACTED
    assert event_dict["context"]["params"][1] == "safe"
    assert event_dict["count"] == 1


def test_structlog_redacts_in_rendered_output(capsys: pytest.CaptureFixture[str]) -> None:
    # Configure a one-shot structlog pipeline that captures rendered output.
    buf = io.StringIO()
    structlog.configure(
        processors=[
            redact_secrets,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=buf),
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=False,
    )
    log = structlog.get_logger("test")
    leak = "ghp_TESTLEAKTOKEN1234ABCDEFGH"
    log.info("user_signed_in", token=leak)
    rendered = buf.getvalue()
    assert leak not in rendered
    assert REDACTED in rendered


def test_secret_pattern_set_is_non_empty() -> None:
    assert len(SECRET_PATTERNS) >= 4


def test_init_tracing_exports_spans_to_in_memory_exporter() -> None:
    exporter = InMemorySpanExporter()
    # OpenTelemetry's `set_tracer_provider` is one-shot per process: subsequent
    # calls log a warning and are ignored, even with force=True at our layer.
    # The init_tracing() conftest already triggered for app startup happens
    # first, so we use the *returned* provider directly here.
    provider = init_tracing(exporter=exporter, processor_factory=SimpleSpanProcessor, force=True)
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("w9.span") as span:
        span.set_attribute("kind", "test")

    spans = exporter.get_finished_spans()
    assert any(s.name == "w9.span" for s in spans)
    found = next(s for s in spans if s.name == "w9.span")
    assert found.attributes is not None
    assert found.attributes.get("kind") == "test"
