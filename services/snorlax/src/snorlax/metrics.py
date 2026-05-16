"""OpenTelemetry instruments for Snorlax.

The per-stage histograms and counters map 1:1 onto the pipeline
stages: download → validate → prodigal → upload → modal-spawn →
mew-update. Each stage gets:

* a duration histogram (seconds, no label cardinality explosion),
* a success counter,
* a failure counter labelled by ``reason``.

The in-flight gauge is observable rather than a free-form counter so
``snorlax_in_flight_isolates`` can never drift on crash — it's
recomputed from the pipeline's live counter every scrape.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import cache

from opentelemetry import metrics
from opentelemetry.metrics import (
    CallbackOptions,
    Counter,
    Histogram,
    Observation,
)


@cache
def _meter() -> metrics.Meter:
    return metrics.get_meter("snorlax")


@dataclass(frozen=True, slots=True)
class SnorlaxMetrics:
    """Bundle of OTel instruments used by the pipeline."""

    events_processed: Counter
    events_succeeded: Counter
    events_failed: Counter
    events_dlqed: Counter

    stage_duration: Histogram
    stage_errors: Counter

    download_bytes: Histogram
    protein_count: Histogram

    @classmethod
    def build(cls, in_flight_callback: Callable[[], int] | None = None) -> SnorlaxMetrics:
        """Construct the metric instruments.

        ``in_flight_callback`` is the source for the
        ``snorlax_in_flight_isolates`` gauge — a zero-arg callable
        returning the current count. Pass ``None`` in tests that don't
        care about the gauge.
        """
        m = _meter()
        if in_flight_callback is not None:
            m.create_observable_gauge(
                "kanto_snorlax_in_flight_isolates",
                callbacks=[_make_callback(in_flight_callback)],
                description="Isolates currently being processed by this pod.",
            )
        return cls(
            events_processed=m.create_counter(
                "kanto_snorlax_events_processed_total",
                description="IsolateDiscovered events drained from the stream.",
            ),
            events_succeeded=m.create_counter(
                "kanto_snorlax_events_succeeded_total",
                description="Events fully processed end-to-end.",
            ),
            events_failed=m.create_counter(
                "kanto_snorlax_events_failed_total",
                description="Events that hit a terminal per-isolate failure, "
                "labelled by failure category (qc_failed | not_found | "
                "validation | prodigal_failure).",
            ),
            events_dlqed=m.create_counter(
                "kanto_snorlax_events_dlqed_total",
                description="Events routed to the DLQ after retry exhaustion, labelled by stage.",
            ),
            stage_duration=m.create_histogram(
                "kanto_snorlax_stage_duration_seconds",
                description="Wall time per pipeline stage.",
                unit="s",
            ),
            stage_errors=m.create_counter(
                "kanto_snorlax_stage_errors_total",
                description="Errors raised at each pipeline stage, labelled by stage and category.",
            ),
            download_bytes=m.create_histogram(
                "kanto_snorlax_download_bytes",
                description="Size of the genome FASTA downloaded from NCBI.",
                unit="By",
            ),
            protein_count=m.create_histogram(
                "kanto_snorlax_protein_count",
                description="Protein count produced by Prodigal per isolate.",
            ),
        )


def _make_callback(
    getter: Callable[[], int],
) -> Callable[[CallbackOptions], Iterable[Observation]]:
    """Wrap a ``() -> int`` callable as an OTel observable callback."""

    def _callback(_options: CallbackOptions) -> Iterable[Observation]:
        return [Observation(int(getter()))]

    return _callback


__all__ = ["SnorlaxMetrics"]
