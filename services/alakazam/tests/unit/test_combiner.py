"""Tests for the linear combiner."""

from __future__ import annotations

import pytest

from alakazam.scoring.combiner import LinearCombiner, ScoringResult
from alakazam.scoring.strategy import (
    ComponentName,
    ComponentResult,
    SkipReason,
)


def _result(name: ComponentName, score: float | None) -> ComponentResult:
    if score is None:
        return ComponentResult.skipped(name, SkipReason.BELOW_CANDIDATE_THRESHOLD)
    return ComponentResult.computed(name, score, duration_seconds=0.001)


def test_combine_all_three_components() -> None:
    combiner = LinearCombiner(weight_nn=1.0, weight_coverage=2.0, weight_mahalanobis=0.5)
    result = combiner.combine(
        nn=_result(ComponentName.NN_DISTANCE, 0.5),
        coverage=_result(ComponentName.COVERAGE, 0.3),
        mahalanobis=_result(ComponentName.MAHALANOBIS, 2.0),
    )
    # (0.5 * 1.0 + 0.3 * 2.0 + 2.0 * 0.5) / (1.0 + 2.0 + 0.5) = 2.1 / 3.5 = 0.6
    assert result == pytest.approx(0.6)


def test_tier_one_only_renormalizes() -> None:
    combiner = LinearCombiner(weight_nn=1.0, weight_coverage=2.0, weight_mahalanobis=0.5)
    result = combiner.combine(
        nn=_result(ComponentName.NN_DISTANCE, 0.4),
        coverage=_result(ComponentName.COVERAGE, None),
        mahalanobis=_result(ComponentName.MAHALANOBIS, None),
    )
    # Only NN contributed: 0.4 * 1.0 / 1.0 = 0.4
    assert result == pytest.approx(0.4)


def test_zero_weight_ignores_a_component() -> None:
    combiner = LinearCombiner(weight_nn=1.0, weight_coverage=0.0, weight_mahalanobis=0.5)
    result = combiner.combine(
        nn=_result(ComponentName.NN_DISTANCE, 1.0),
        coverage=_result(ComponentName.COVERAGE, 0.9),
        mahalanobis=_result(ComponentName.MAHALANOBIS, 2.0),
    )
    # Coverage is ignored due to zero weight.
    # (1.0 * 1.0 + 2.0 * 0.5) / (1.0 + 0.5) = 2.0 / 1.5 = ~1.333
    assert result == pytest.approx(2.0 / 1.5)


def test_all_components_skip_returns_zero() -> None:
    combiner = LinearCombiner(weight_nn=1.0, weight_coverage=2.0, weight_mahalanobis=0.5)
    result = combiner.combine(
        nn=_result(ComponentName.NN_DISTANCE, None),
        coverage=_result(ComponentName.COVERAGE, None),
        mahalanobis=_result(ComponentName.MAHALANOBIS, None),
    )
    assert result == 0.0


def test_negative_weight_rejected() -> None:
    with pytest.raises(ValueError):
        LinearCombiner(weight_nn=-0.1, weight_coverage=1.0, weight_mahalanobis=0.0)


def test_all_zero_weights_rejected() -> None:
    with pytest.raises(ValueError):
        LinearCombiner(weight_nn=0.0, weight_coverage=0.0, weight_mahalanobis=0.0)


def test_scoring_result_component_lookup() -> None:
    nn = _result(ComponentName.NN_DISTANCE, 0.1)
    cov = _result(ComponentName.COVERAGE, 0.2)
    maha = _result(ComponentName.MAHALANOBIS, 0.3)
    res = ScoringResult(
        novelty_score=0.42,
        above_threshold=False,
        nn_distance=nn,
        coverage=cov,
        mahalanobis=maha,
    )
    assert res.component_by_name(ComponentName.NN_DISTANCE) is nn
    assert res.component_by_name(ComponentName.COVERAGE) is cov
    assert res.component_by_name(ComponentName.MAHALANOBIS) is maha
