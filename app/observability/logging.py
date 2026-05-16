"""Structured logging with secret-pattern redaction.

`init_logging()` is idempotent and configures structlog for the process.
`redact_secrets()` is a structlog processor that walks the event dict and
replaces anything matching a known secret pattern with `[REDACTED]`. Run
it as early as possible in the processor chain so subsequent renderers
never see the raw value.
"""

import logging
import re
from collections.abc import MutableMapping
from typing import Any

import structlog

# Patterns we never want to see in logs. Add new ones here.
SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ghp_[A-Za-z0-9_]{20,}"),  # GitHub personal access token
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),  # GitHub fine-grained PAT
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),  # OpenAI / Anthropic-style API key
    re.compile(r"xoxb-[A-Za-z0-9-]{20,}"),  # Slack bot token
    re.compile(r"xoxp-[A-Za-z0-9-]{20,}"),  # Slack user token
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE),  # generic Bearer token
    re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),  # JWT
]

REDACTED = "[REDACTED]"

_INITIALISED = False


def _redact_string(value: str) -> str:
    for pattern in SECRET_PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, MutableMapping):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        coerced = [_redact_value(v) for v in value]
        return type(value)(coerced) if isinstance(value, tuple) else coerced
    return value


def redact_secrets(
    logger: object, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: redact known secret patterns in every string value."""
    for key, value in list(event_dict.items()):
        event_dict[key] = _redact_value(value)
    return event_dict


def init_logging(level: int = logging.INFO) -> None:
    """Configure structlog + stdlib logging once. Idempotent."""
    global _INITIALISED
    if _INITIALISED:
        return
    _INITIALISED = True

    logging.basicConfig(level=level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            redact_secrets,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> Any:
    """Return a structlog logger. Typed loosely because structlog's runtime
    type depends on the configured wrapper class."""
    return structlog.get_logger(name)
