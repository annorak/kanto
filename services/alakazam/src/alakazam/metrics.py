"""OpenTelemetry instruments for Alakazam.

Per task-08 §7, Alakazam ships:

* a final novelty-score histogram (so the operator can eyeball the
  distribution and confirm the alert threshold is sane),
* per-component score histograms (NN distance, coverage,
  Mahalanobis),
* per-component latency histograms,
* a tiered-scoring promotion counter — fraction-of-isolates that
  crossed the candidate threshold is its (computed / total) ratio,
* event counters (processed, succeeded, failed, DLQed),
* an in-flight gauge for the consumer's live load,
* a species-centroid-cache size gauge so a forgotten refresh shows
  up on the dashboard.

The histogram bucket boundaries are picked to give useful resolution
in the 0..2 range (where most of the action lives) without exploding
the time-series cardinality.
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

from alakazam.scoring.combiner import ScoringResult
from alakazam.scoring.strategy import ComponentName


@cache
def _meter() -> metrics.Meter:
    return metrics.get_meter("alakazam")


@dataclass(frozen=True, slots=True)
class AlakazamMetrics:
    """Bundle of OTel instruments used by the scoring path."""

    events_processed: Counter
    events_succeeded: Counter
    events_failed: Counter
    events_dlqed: Counter

    novelty_score: Histogram
    component_score: Histogram
    component_duration: Histogram
    candidates_promoted: Counter
    component_skipped: Counter

    @classmethod
    def build(
        cls,
        *,
        in_flight_callback: Callable[[], int] | None = None,
        centroid_cache_size_callback: Callable[[], int] | None = None,
    ) -> AlakazamMetrics:
        m = _meter()
        if in_flight_callback is not None:
            m.create_observable_gauge(
                "kanto_alakazam_in_flight_isolates",
                callbacks=[_int_callback(in_flight_callback)],
                description="Isolates currently being scored by this pod.",
            )
        if centroid_cache_size_callback is not None:
            m.create_observable_gauge(
                "kanto_alakazam_species_cache_size",
                callbacks=[_int_callback(centroid_cache_size_callback)],
                description=("Species centroids currently held in the in-memory cache."),
            )
        return cls(
            events_processed=m.create_counter(
                "kanto_alakazam_events_processed_total",
                description="EmbeddingsReady events drained from kanto.embedded.",
            ),
            events_succeeded=m.create_counter(
                "kanto_alakazam_events_succeeded_total",
                description="Isolates scored, persisted, and re-emitted.",
            ),
            events_failed=m.create_counter(
                "kanto_alakazam_events_failed_total",
                description=(
                    "Events that hit a terminal per-isolate failure, labelled by category."
                ),
            ),
            events_dlqed=m.create_counter(
                "kanto_alakazam_events_dlqed_total",
                description=("Events routed to the DLQ after retry exhaustion."),
            ),
            novelty_score=m.create_histogram(
                "kanto_alakazam_novelty_score",
                description="Combined novelty score per scored isolate.",
            ),
            component_score=m.create_histogram(
                "kanto_alakazam_component_score",
                description=("Per-component score histogram, labelled by component."),
            ),
            component_duration=m.create_histogram(
                "kanto_alakazam_component_duration_seconds",
                description=("Wall time per scoring component, labelled by component."),
                unit="s",
            ),
            candidates_promoted=m.create_counter(
                "kanto_alakazam_candidates_promoted_total",
                description=(
                    "Isolates whose NN distance promoted them into the expensive tier-2 path."
                ),
            ),
            component_skipped=m.create_counter(
                "kanto_alakazam_component_skipped_total",
                description=(
                    "Times a strategy declined to compute, labelled by component and reason."
                ),
            ),
        )

    # ------------------------------------------------------------------
    # Convenience: record a finished ScoringResult in one call.
    # ------------------------------------------------------------------

    def record_scoring(self, result: ScoringResult, *, was_candidate: bool) -> None:
        """Record every metric derived from one :class:`ScoringResult`.

        Centralising the recording keeps the consumer code free of
        per-metric noise and means a future metric (e.g. a per-organism
        breakdown) lands here, not in the service module.
        """
        self.novelty_score.record(result.novelty_score)
        self.events_processed.add(1)
        if was_candidate:
            self.candidates_promoted.add(1)
        for component_result in (
            result.nn_distance,
            result.coverage,
            result.mahalanobis,
        ):
            labels = {"component": component_result.name.value}
            self.component_duration.record(
                component_result.duration_seconds,
                attributes=labels,
            )
            if component_result.was_computed:
                # mypy: was_computed guarantees score is not None
                assert component_result.score is not None
                self.component_score.record(
                    component_result.score,
                    attributes=labels,
                )
            else:
                skip_labels = dict(labels)
                if component_result.skip_reason is not None:
                    skip_labels["reason"] = component_result.skip_reason.value
                self.component_skipped.add(1, attributes=skip_labels)

    def name_for(self, name: ComponentName) -> str:
        """Stable label value for the component name. Useful for tests."""
        return name.value


def _int_callback(
    getter: Callable[[], int],
) -> Callable[[CallbackOptions], Iterable[Observation]]:
    def _callback(_options: CallbackOptions) -> Iterable[Observation]:
        return [Observation(int(getter()))]

    return _callback


__all__ = ["AlakazamMetrics"]
