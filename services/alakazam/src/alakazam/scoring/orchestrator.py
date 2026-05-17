"""Tiered scoring orchestrator.

Owns the per-isolate workflow:

1. Hydrate the :class:`IsolateContext` (organism, embedding,
   timestamp).
2. Run the cheap :class:`NNDistanceStrategy` for every isolate.
3. If ``nn_distance >= candidate_threshold``, fan out to the expensive
   :class:`CoverageStrategy` and :class:`MahalanobisStrategy` in
   parallel.
4. Combine + threshold via the :class:`ScoreCombiner`.

The orchestrator is purely functional in the sense that it doesn't
own retry or DLQ — that's the consumer's job. It does own the
"did the expensive tier fire" diagnostic that the metrics exporter
turns into a Prometheus gauge.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from alakazam.scoring.combiner import ScoreCombiner, ScoringResult
from alakazam.scoring.strategy import (
    ComponentName,
    ComponentResult,
    ScoringStrategy,
    SkipReason,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IsolateContext:
    """Everything a strategy needs to score one isolate.

    Hydrated by :class:`ScoringOrchestrator.score` and passed through
    to each :class:`ScoringStrategy`. Keeping it a frozen dataclass
    means strategies can't mutate fields out from under each other in
    the tier-2 parallel fan-out.
    """

    accession: str
    version: int
    organism: str
    embedding: list[float]
    now: datetime | None = None


class ScoringOrchestrator:
    """Three-strategy, two-tier scorer."""

    def __init__(
        self,
        *,
        nn_strategy: ScoringStrategy,
        coverage_strategy: ScoringStrategy,
        mahalanobis_strategy: ScoringStrategy,
        combiner: ScoreCombiner,
        candidate_threshold: float,
        alert_threshold: float,
    ) -> None:
        self._nn_strategy = nn_strategy
        self._coverage_strategy = coverage_strategy
        self._mahalanobis_strategy = mahalanobis_strategy
        self._combiner = combiner
        self._candidate_threshold = candidate_threshold
        self._alert_threshold = alert_threshold

    async def score(self, ctx: IsolateContext) -> ScoringResult:
        """Run the tiered scoring pipeline on one isolate."""
        nn_result = await self._nn_strategy.compute(ctx)

        # Tier-1 promotion: only flag the candidate when the cheap
        # component both ran *and* exceeded the threshold. A skipped
        # tier-1 (compute error, no neighbors) means we have no signal
        # to base the promotion on — default to tier-1-only.
        is_candidate = (
            nn_result.was_computed and (nn_result.score or 0.0) >= self._candidate_threshold
        )

        if is_candidate:
            coverage_result, mahalanobis_result = await asyncio.gather(
                self._coverage_strategy.compute(ctx),
                self._mahalanobis_strategy.compute(ctx),
            )
        else:
            coverage_result = ComponentResult.skipped(
                ComponentName.COVERAGE,
                SkipReason.BELOW_CANDIDATE_THRESHOLD,
            )
            mahalanobis_result = ComponentResult.skipped(
                ComponentName.MAHALANOBIS,
                SkipReason.BELOW_CANDIDATE_THRESHOLD,
            )

        combined = self._combiner.combine(
            nn=nn_result,
            coverage=coverage_result,
            mahalanobis=mahalanobis_result,
        )
        return ScoringResult(
            novelty_score=combined,
            above_threshold=combined >= self._alert_threshold,
            nn_distance=nn_result,
            coverage=coverage_result,
            mahalanobis=mahalanobis_result,
        )


__all__ = ["IsolateContext", "ScoringOrchestrator"]
