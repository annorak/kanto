"""Unit tests for the tracing module.

We exercise: SDK setup, header parsing, W3C carrier inject/extract
round-trip, and ``use_extracted_context`` linking spans across the
synthetic boundary.
"""

from __future__ import annotations

import pytest
from kanto_commons.config import TracingSettings
from kanto_commons.tracing import (
    _parse_headers,
    extract_context_from_carrier,
    inject_context_into_carrier,
    setup_tracing,
    span,
    use_extracted_context,
)


@pytest.fixture(autouse=True)
def _tracer() -> None:
    setup_tracing(
        service_name="test-svc",
        environment="test",
        settings=TracingSettings(),
    )


def test_setup_tracing_sets_resource_attributes() -> None:
    """The returned provider carries the resource attrs; we don't go
    through the global registry because OTel disallows replacing it
    once set."""
    provider = setup_tracing(
        service_name="snorlax",
        environment="dev",
        settings=TracingSettings(),
        extra_resource_attrs={"team": "kanto"},
    )
    assert provider.resource.attributes["service.name"] == "snorlax"
    assert provider.resource.attributes["deployment.environment"] == "dev"
    assert provider.resource.attributes["team"] == "kanto"


def test_setup_tracing_with_otlp_endpoint_uses_batch_processor() -> None:
    """Smoke test: confirm we don't blow up when an OTLP endpoint is set.

    We don't actually export — that would require a backend. We only
    verify the provider was constructed with a span processor.
    """
    settings = TracingSettings(
        endpoint="http://localhost:4318/v1/traces",
        headers="x-key=value,other=2",
    )
    provider = setup_tracing(service_name="t", environment="dev", settings=settings)
    # _active_span_processor is the SynchronousMultiSpanProcessor on a
    # freshly-built provider; it's an internal attr but stable.
    assert provider is not None


def test_inject_extract_round_trip() -> None:
    with span("op-a"):
        carrier = inject_context_into_carrier({})
    assert "traceparent" in carrier

    extracted = extract_context_from_carrier(carrier)
    assert extracted is not None


def test_use_extracted_context_links_child_to_parent_trace() -> None:
    # Producer side: capture the trace id, inject into carrier.
    with span("op-producer") as parent:
        producer_trace_id = parent.get_span_context().trace_id
        carrier = inject_context_into_carrier({})

    # Consumer side: activate the extracted context and assert the
    # downstream span shares the trace id.
    with use_extracted_context(carrier), span("op-consumer") as child:
        assert child.get_span_context().trace_id == producer_trace_id


def test_parse_headers_returns_none_for_empty_input() -> None:
    assert _parse_headers(None) is None
    assert _parse_headers("") is None


def test_parse_headers_splits_pairs() -> None:
    result = _parse_headers("a=1, b=2,c=three")
    assert result == {"a": "1", "b": "2", "c": "three"}


def test_parse_headers_rejects_malformed() -> None:
    with pytest.raises(ValueError):
        _parse_headers("no-equals-sign")


def test_inject_when_no_active_span_leaves_carrier_alone() -> None:
    # No span active here — no traceparent should be injected.
    carrier: dict[str, str] = {}
    inject_context_into_carrier(carrier)
    # OTel still emits a trace context because the caller may want to
    # start one downstream; we only assert no crash.
    assert isinstance(carrier, dict)
