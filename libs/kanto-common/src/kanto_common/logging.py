"""Structured logging built on structlog.

Production services emit one JSON record per line; local development
gets a colored human-readable renderer. Switch via the ``log_format``
field on :class:`kanto_common.config.KantoBaseSettings` (env var
``KANTO_LOG_FORMAT``).

Every record automatically picks up:

* ``timestamp`` (ISO 8601 with timezone)
* ``service`` (resolved from the active settings)
* ``level``
* ``trace_id`` and ``span_id`` (from the OpenTelemetry context, if any)
* ``isolate_accession`` (when bound via :func:`bind_accession`)
* Anything else the call-site adds via ``log.info("msg", key=value)``

Sensitive fields (passwords, full DSNs, private keys) are scrubbed by
:func:`_scrub_processor` before the record is emitted.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog
from opentelemetry import trace
from structlog.contextvars import (
    bind_contextvars,
    bound_contextvars,
    clear_contextvars,
    unbind_contextvars,
)
from structlog.types import EventDict, Processor, WrappedLogger

from kanto_common.config import LogFormat

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def setup_logging(
    *,
    service_name: str,
    log_level: str,
    log_format: LogFormat,
) -> None:
    """Configure stdlib ``logging`` and structlog in one place.

    Idempotent — safe to call from tests' setup hooks. Replaces any
    previous configuration.
    """
    # Reset stdlib logging so re-configuration in tests is clean.
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    root.setLevel(log_level.upper())

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_service_name(service_name),
        _add_otel_context,
        _scrub_processor,
    ]

    final_processor: Processor
    if log_format is LogFormat.JSON:
        final_processor = structlog.processors.JSONRenderer()
    else:
        final_processor = structlog.dev.ConsoleRenderer(colors=False)

    structlog.configure(
        processors=[*shared_processors, final_processor],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(log_level.upper())
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger with the standard processor chain."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]


@contextmanager
def bind_accession(accession: str) -> Iterator[None]:
    """Bind ``isolate_accession`` to the logger context for the block.

    All log records emitted while this context is active include the
    accession. Nesting is supported; on exit the accession key is
    removed.

    Example::

        with bind_accession("PDT00012345.1"):
            log.info("downloading FASTA")  # logs include isolate_accession
    """
    with bound_contextvars(isolate_accession=accession):
        yield


def bind_context(**kwargs: Any) -> None:
    """Bind arbitrary keys to the logger context (no auto-cleanup)."""
    bind_contextvars(**kwargs)


def unbind_context(*keys: str) -> None:
    """Remove keys previously bound with :func:`bind_context`."""
    unbind_contextvars(*keys)


def reset_context() -> None:
    """Clear all context vars. Useful between tests."""
    clear_contextvars()


# ---------------------------------------------------------------------------
# Processors
# ---------------------------------------------------------------------------


def _add_service_name(service_name: str) -> Processor:
    """Closure: bake the service name into the log record on every call."""

    def processor(
        _logger: WrappedLogger, _method_name: str, event_dict: EventDict
    ) -> EventDict:
        event_dict.setdefault("service", service_name)
        return event_dict

    return processor


def _add_otel_context(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """Inject the active OTel trace_id and span_id into the record.

    Logs without an active span do not get these fields, which keeps
    grep-friendly output uncluttered. When a span is active, both
    fields appear formatted as the standard hex strings.
    """
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx.is_valid:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


# Substrings that indicate a value is sensitive and must be scrubbed.
# Matched case-insensitively against the *key*. The right approach for
# logging passwords is "don't" — but defensive scrubbing catches the
# accidents that slip past code review.
_SENSITIVE_KEY_RE = re.compile(
    r"(password|passwd|secret|token|apikey|api_key|private_key|dsn|connection_string)",
    re.IGNORECASE,
)
_DSN_PASSWORD_RE = re.compile(r"://([^:/@\s]+):([^@\s]+)@")


def _scrub_processor(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """Replace sensitive values with ``'***'`` and redact DSN passwords."""
    for key in list(event_dict.keys()):
        if _SENSITIVE_KEY_RE.search(key):
            event_dict[key] = "***"
            continue
        value = event_dict[key]
        if isinstance(value, str):
            redacted = _DSN_PASSWORD_RE.sub(r"://\1:***@", value)
            if redacted != value:
                event_dict[key] = redacted
    return event_dict


__all__ = [
    "bind_accession",
    "bind_context",
    "get_logger",
    "reset_context",
    "setup_logging",
    "unbind_context",
]
