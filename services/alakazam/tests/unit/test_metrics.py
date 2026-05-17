"""Tests for the OTel metrics bundle.

We exercise the recording path end-to-end with a real OTel
:class:`InMemoryMetricReader` so a regression in the histogram /
counter wiring shows up here rather than at integration time.
"""

from __future__ import annotations

from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from alakazam.metrics import AlakazamMetrics
from alakazam.scoring.combiner import ScoringResult
from alakazam.scoring.strategy import (
    ComponentName,
    ComponentResult,
    SkipReason,
)


def _setup_reader() -> InMemoryMetricReader:
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    return reader


def _collect_names(reader: InMemoryMetricReader) -> set[str]:
    data = reader.get_metrics_data()
    names: set[str] = set()
    if data is None:
        return names
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                names.add(m.name)
    return names


def test_record_scoring_records_every_component() -> None:
    reader = _setup_reader()
    # Cache invalidation: the module-level @cache on _meter() returns a
    # stale meter across tests. Reset it.
    from alakazam import metrics as alakazam_metrics

    alakazam_metrics._meter.cache_clear()  # type: ignore[attr-defined]

    bundle = AlakazamMetrics.build(
        in_flight_callback=lambda: 0,
        centroid_cache_size_callback=lambda: 5,
    )
    result = ScoringResult(
        novelty_score=0.4,
        above_threshold=False,
        nn_distance=ComponentResult.computed(ComponentName.NN_DISTANCE, 0.4, duration_seconds=0.01),
        coverage=ComponentResult.skipped(
            ComponentName.COVERAGE, SkipReason.BELOW_CANDIDATE_THRESHOLD
        ),
        mahalanobis=ComponentResult.skipped(ComponentName.MAHALANOBIS, SkipReason.CENTROID_MISSING),
    )
    bundle.record_scoring(result, was_candidate=False)
    names = _collect_names(reader)
    assert "kanto_alakazam_novelty_score" in names
    assert "kanto_alakazam_component_score" in names
    assert "kanto_alakazam_component_duration_seconds" in names
    assert "kanto_alakazam_component_skipped_total" in names
    assert "kanto_alakazam_events_processed_total" in names
    assert "kanto_alakazam_species_cache_size" in names


def test_name_for_returns_value() -> None:
    from alakazam import metrics as alakazam_metrics

    alakazam_metrics._meter.cache_clear()  # type: ignore[attr-defined]
    _setup_reader()
    bundle = AlakazamMetrics.build()
    assert bundle.name_for(ComponentName.COVERAGE) == "coverage"
