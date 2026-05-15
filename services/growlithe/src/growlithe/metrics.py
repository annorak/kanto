"""Prometheus instruments for Growlithe.

We use the OpenTelemetry metrics API rather than the prometheus_client
library directly because kanto-commons already wires the OTel SDK in
``setup_tracing``; the OTLP-to-Prometheus bridge runs in the in-cluster
otel-collector. The metric names below follow the
``kanto_growlithe_*`` convention so dashboards can grep for them
cleanly across services.

Each metric is created lazily on first access via ``_meter`` so unit
tests that don't bind a meter don't fail at import time.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

from opentelemetry import metrics
from opentelemetry.metrics import Counter, Histogram


@cache
def _meter() -> metrics.Meter:
    return metrics.get_meter("growlithe")


@dataclass(frozen=True, slots=True)
class GrowlitheMetrics:
    """Bundle of OTel instruments used by the pipeline."""

    poll_cycles_started: Counter
    poll_cycles_completed: Counter
    poll_cycles_failed: Counter
    isolates_discovered: Counter
    isolates_filtered: Counter
    poll_cycle_duration_seconds: Histogram
    upstream_request_seconds: Histogram

    @classmethod
    def build(cls) -> GrowlitheMetrics:
        m = _meter()
        return cls(
            poll_cycles_started=m.create_counter(
                "kanto_growlithe_poll_cycles_started_total",
                description="Poll cycles started (any reason, including skipped).",
            ),
            poll_cycles_completed=m.create_counter(
                "kanto_growlithe_poll_cycles_completed_total",
                description="Poll cycles that ran to completion without error.",
            ),
            poll_cycles_failed=m.create_counter(
                "kanto_growlithe_poll_cycles_failed_total",
                description="Poll cycles that aborted with an unhandled error.",
            ),
            isolates_discovered=m.create_counter(
                "kanto_growlithe_isolates_discovered_total",
                description="IsolateDiscovered events emitted, labelled by organism.",
            ),
            isolates_filtered=m.create_counter(
                "kanto_growlithe_isolates_filtered_total",
                description=(
                    "Rows dropped pre-emit, labelled by reason "
                    "(qc_failed | missing_required | bad_version)."
                ),
            ),
            poll_cycle_duration_seconds=m.create_histogram(
                "kanto_growlithe_poll_cycle_duration_seconds",
                description="End-to-end poll cycle wall time per organism.",
                unit="s",
            ),
            upstream_request_seconds=m.create_histogram(
                "kanto_growlithe_upstream_request_seconds",
                description="Latency of one upstream HTTP request.",
                unit="s",
            ),
        )


__all__ = ["GrowlitheMetrics"]
