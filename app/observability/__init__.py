"""OpenTelemetry tracing + structured logging + actor-tagging middleware."""

from typing import Any

from app.observability.logging import (
    REDACTED,
    SECRET_PATTERNS,
    get_logger,
    init_logging,
    redact_secrets,
)
from app.observability.middleware import tag_span_with_actor
from app.observability.tracing import init_tracing, instrument_app


def init_observability(*, app: Any | None = None) -> None:
    """Configure logging + tracing. Idempotent.

    Pass `app` to also instrument FastAPI request spans.
    """
    init_logging()
    init_tracing()
    if app is not None:
        instrument_app(app)


__all__ = [
    "REDACTED",
    "SECRET_PATTERNS",
    "get_logger",
    "init_logging",
    "init_observability",
    "init_tracing",
    "instrument_app",
    "redact_secrets",
    "tag_span_with_actor",
]
