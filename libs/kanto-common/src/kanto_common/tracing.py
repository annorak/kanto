"""OpenTelemetry tracing setup and helpers.

Two responsibilities:

1. **Initialization** — :func:`setup_tracing` configures the global
   tracer provider with an OTLP HTTP exporter (production) or a
   console exporter (local dev / when no endpoint is configured). It
   sets the standard service-name and environment resource
   attributes that show up in every span.

2. **Context propagation across non-HTTP boundaries** — The OTel SDK
   gives us automatic propagation through HTTP, but Kanto's main
   transports are Kafka messages and Modal function calls. The
   :func:`inject_context_into_carrier` and
   :func:`extract_context_from_carrier` helpers serialize / deserialize
   the W3C ``traceparent`` header into a plain dict that the streaming
   wrapper can stash in Kafka headers.

The module is a thin shim. It stays small so that the SDK choice
(OTel) and the exporter choice (OTLP) can be swapped without
touching service code.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.propagators.textmap import (
    CarrierT,
    Getter,
    Setter,
    default_getter,
    default_setter,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Span, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from kanto_common.config import TracingSettings

# Module-level singleton. ``setup_tracing`` is idempotent and
# overwrites; that's fine for a process that only initializes once at
# startup.
_PROPAGATOR = TraceContextTextMapPropagator()


def setup_tracing(
    *,
    service_name: str,
    environment: str,
    settings: TracingSettings,
    extra_resource_attrs: dict[str, str] | None = None,
) -> TracerProvider:
    """Initialize the global OTel tracer provider.

    Idempotent within a process; calling twice replaces the prior
    provider. Returns the provider so tests can flush it on shutdown.

    Behaviour:
    * If ``settings.endpoint`` is set → OTLP HTTP exporter, batched.
    * Otherwise → console exporter, simple (synchronous) processor —
      suitable for ``KANTO_ENV=dev``.
    """
    attrs: dict[str, str] = {
        "service.name": service_name,
        "deployment.environment": environment,
    }
    if extra_resource_attrs:
        attrs.update(extra_resource_attrs)

    sampler = ParentBased(TraceIdRatioBased(settings.sample_rate))
    provider = TracerProvider(
        resource=Resource.create(attrs),
        sampler=sampler,
    )

    if settings.endpoint is not None:
        headers = _parse_headers(settings.headers)
        exporter = OTLPSpanExporter(
            endpoint=settings.endpoint,
            headers=headers,
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
    else:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    return provider


def _parse_headers(raw: str | None) -> dict[str, str] | None:
    """Parse 'k=v,k=v' OTLP header config into a dict."""
    if not raw:
        return None
    result: dict[str, str] = {}
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if "=" not in piece:
            raise ValueError(f"OTel headers must be 'key=value' pairs; got: {piece!r}")
        key, value = piece.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def get_tracer(name: str) -> Tracer:
    """Convenience wrapper over ``trace.get_tracer`` so service code only
    imports from ``kanto_common``.
    """
    return trace.get_tracer(name)


@contextmanager
def span(
    name: str,
    attributes: dict[str, str | int | float | bool] | None = None,
) -> Iterator[Span]:
    """Start a span on the active tracer; sets attributes on entry."""
    tracer = trace.get_tracer("kanto_common")
    with tracer.start_as_current_span(name) as current:
        if attributes:
            for key, value in attributes.items():
                current.set_attribute(key, value)
        yield current


# ---------------------------------------------------------------------------
# Carrier propagation across non-HTTP boundaries (Kafka headers, Modal args)
# ---------------------------------------------------------------------------


def inject_context_into_carrier(carrier: dict[str, str]) -> dict[str, str]:
    """Mutate ``carrier`` in place with W3C ``traceparent`` / ``tracestate``.

    Returns the same dict for caller convenience. If no span is active,
    the carrier is unchanged.
    """
    _PROPAGATOR.inject(carrier=carrier, setter=_DICT_SETTER)
    return carrier


def extract_context_from_carrier(carrier: dict[str, str]) -> otel_context.Context:
    """Parse the W3C headers out of ``carrier`` and return an OTel Context.

    The caller is responsible for activating it via
    :func:`opentelemetry.context.attach`.
    """
    return _PROPAGATOR.extract(carrier=carrier, getter=_DICT_GETTER)


@contextmanager
def use_extracted_context(carrier: dict[str, str]) -> Iterator[None]:
    """Temporarily activate the trace context carried in ``carrier``.

    Useful for streaming consumers: they read the headers off the
    Kafka message and want every span produced while processing that
    message to chain to the upstream span.
    """
    ctx = extract_context_from_carrier(carrier)
    token = otel_context.attach(ctx)
    try:
        yield
    finally:
        otel_context.detach(token)


# ---------------------------------------------------------------------------
# Setter / getter implementations for the dict carrier.
# ---------------------------------------------------------------------------


class _DictSetter(Setter[dict[str, str]]):
    def set(self, carrier: dict[str, str], key: str, value: str) -> None:
        carrier[key] = value


class _DictGetter(Getter[dict[str, str]]):
    def get(self, carrier: dict[str, str], key: str) -> list[str] | None:
        if key in carrier:
            return [carrier[key]]
        return None

    def keys(self, carrier: dict[str, str]) -> list[str]:
        return list(carrier.keys())


_DICT_SETTER: Setter[dict[str, str]] = _DictSetter()
_DICT_GETTER: Getter[dict[str, str]] = _DictGetter()

# Re-export the textmap default helpers for downstream callers that
# want to plug a non-dict carrier in.
__all__ = [
    "CarrierT",
    "default_getter",
    "default_setter",
    "extract_context_from_carrier",
    "get_tracer",
    "inject_context_into_carrier",
    "setup_tracing",
    "span",
    "use_extracted_context",
]
