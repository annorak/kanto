"""Tests for the tiered scoring orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from alakazam.scoring import (
    IsolateContext,
    LinearCombiner,
    ScoringOrchestrator,
)
from alakazam.scoring.strategy import (
    ComponentName,
    ComponentResult,
    ScoringStrategy,
    SkipReason,
)

_DIM = 1152


@dataclass
class _ConstStrategy(ScoringStrategy):
    """Test stub: returns a pre-baked :class:`ComponentResult`."""

    _name: ComponentName
    _result: ComponentResult
    calls: int = 0

    @property
    def name(self) -> ComponentName:
        return self._name

    async def compute(self, ctx: IsolateContext) -> ComponentResult:
        self.calls += 1
        return self._result


def _ctx() -> IsolateContext:
    return IsolateContext(
        accession="PDT0001.1",
        version=1,
        organism="Salmonella",
        embedding=[0.0] * _DIM,
        now=datetime.now(UTC),
    )


def _orchestrator(
    *,
    nn_score: float,
    coverage_score: float,
    maha_score: float,
    candidate_threshold: float,
    alert_threshold: float = 0.5,
) -> tuple[ScoringOrchestrator, _ConstStrategy, _ConstStrategy, _ConstStrategy]:
    nn = _ConstStrategy(
        _name=ComponentName.NN_DISTANCE,
        _result=ComponentResult.computed(ComponentName.NN_DISTANCE, nn_score, duration_seconds=0.0),
    )
    cov = _ConstStrategy(
        _name=ComponentName.COVERAGE,
        _result=ComponentResult.computed(
            ComponentName.COVERAGE, coverage_score, duration_seconds=0.0
        ),
    )
    maha = _ConstStrategy(
        _name=ComponentName.MAHALANOBIS,
        _result=ComponentResult.computed(
            ComponentName.MAHALANOBIS, maha_score, duration_seconds=0.0
        ),
    )
    orch = ScoringOrchestrator(
        nn_strategy=nn,
        coverage_strategy=cov,
        mahalanobis_strategy=maha,
        combiner=LinearCombiner(weight_nn=1.0, weight_coverage=2.0, weight_mahalanobis=0.5),
        candidate_threshold=candidate_threshold,
        alert_threshold=alert_threshold,
    )
    return orch, nn, cov, maha


async def test_below_threshold_skips_tier_two() -> None:
    orch, nn, cov, maha = _orchestrator(
        nn_score=0.10,
        coverage_score=0.99,  # never read
        maha_score=99.0,  # never read
        candidate_threshold=0.30,
    )
    result = await orch.score(_ctx())
    assert nn.calls == 1
    assert cov.calls == 0
    assert maha.calls == 0
    assert not result.coverage.was_computed
    assert result.coverage.skip_reason is SkipReason.BELOW_CANDIDATE_THRESHOLD
    # Only NN contributes -> renormalized to NN's raw score.
    assert result.novelty_score == pytest.approx(0.10)


async def test_above_threshold_fires_tier_two_in_parallel() -> None:
    orch, nn, cov, maha = _orchestrator(
        nn_score=0.5,
        coverage_score=0.3,
        maha_score=2.0,
        candidate_threshold=0.30,
        alert_threshold=0.5,
    )
    result = await orch.score(_ctx())
    assert nn.calls == 1
    assert cov.calls == 1
    assert maha.calls == 1
    assert result.coverage.was_computed
    assert result.mahalanobis.was_computed
    # (0.5*1.0 + 0.3*2.0 + 2.0*0.5) / 3.5 = 0.6
    assert result.novelty_score == pytest.approx(0.6)
    assert result.above_threshold is True


async def test_alert_threshold_compares_combined_score() -> None:
    orch, *_ = _orchestrator(
        nn_score=0.10,
        coverage_score=0.0,
        maha_score=0.0,
        candidate_threshold=0.30,
        alert_threshold=0.5,
    )
    result = await orch.score(_ctx())
    assert result.above_threshold is False
    assert result.novelty_score < 0.5


async def test_nn_skip_does_not_promote_to_tier_two() -> None:
    skipped_nn = _ConstStrategy(
        _name=ComponentName.NN_DISTANCE,
        _result=ComponentResult.skipped(ComponentName.NN_DISTANCE, SkipReason.COMPUTE_ERROR),
    )
    cov = _ConstStrategy(
        _name=ComponentName.COVERAGE,
        _result=ComponentResult.computed(ComponentName.COVERAGE, 0.9, duration_seconds=0.0),
    )
    maha = _ConstStrategy(
        _name=ComponentName.MAHALANOBIS,
        _result=ComponentResult.computed(ComponentName.MAHALANOBIS, 5.0, duration_seconds=0.0),
    )
    orch = ScoringOrchestrator(
        nn_strategy=skipped_nn,
        coverage_strategy=cov,
        mahalanobis_strategy=maha,
        combiner=LinearCombiner(weight_nn=1.0, weight_coverage=2.0, weight_mahalanobis=0.5),
        candidate_threshold=0.30,
        alert_threshold=0.5,
    )
    result = await orch.score(_ctx())
    assert cov.calls == 0
    assert maha.calls == 0
    assert result.novelty_score == 0.0
    assert result.above_threshold is False
