"""Linear combiner: weighted sum of computed strategy outputs.

Each strategy supplies a non-negative ``score`` (or skips). The
combiner takes the weighted sum of the computed components and
divides by the sum of the *weights of components that contributed*.
This means a tier-1-only result (NN distance alone) is on the same
scale as a tier-2 result (NN + coverage + Mahalanobis), so the
``alert_threshold`` is a single number rather than three.

Worked example
--------------
Weights ``(nn=1.0, coverage=2.0, maha=0.5)``.

* Tier 1 only:
    score(NN=0.4) -> combined = 0.4 * 1.0 / 1.0 = 0.40
* Tier 2 fired:
    score(NN=0.5, COV=0.3, MAHA=2.0) -> combined =
      (0.5*1.0 + 0.3*2.0 + 2.0*0.5) / (1.0 + 2.0 + 0.5)
      = 2.1 / 3.5 = 0.60

Per-component knobs live in ``alakazam.config.AlakazamServiceSettings``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from alakazam.scoring.strategy import ComponentName, ComponentResult


@dataclass(frozen=True, slots=True)
class ScoringResult:
    """The combiner's output: final score + every component's result."""

    novelty_score: float
    above_threshold: bool
    nn_distance: ComponentResult
    coverage: ComponentResult
    mahalanobis: ComponentResult

    def component_by_name(self, name: ComponentName) -> ComponentResult:
        if name is ComponentName.NN_DISTANCE:
            return self.nn_distance
        if name is ComponentName.COVERAGE:
            return self.coverage
        if name is ComponentName.MAHALANOBIS:
            return self.mahalanobis
        raise ValueError(f"unknown component name {name!r}")  # pragma: no cover


class ScoreCombiner(Protocol):
    """How a set of :class:`ComponentResult` becomes a single score."""

    def combine(
        self,
        *,
        nn: ComponentResult,
        coverage: ComponentResult,
        mahalanobis: ComponentResult,
    ) -> float: ...


@dataclass(frozen=True, slots=True)
class LinearCombiner:
    """Weighted mean of the computed components.

    A ``weight_*`` of zero means "ignore this component even if it
    ran"; useful for A/B-style operator experiments without redeploys.
    """

    weight_nn: float
    weight_coverage: float
    weight_mahalanobis: float

    def __post_init__(self) -> None:
        if self.weight_nn < 0 or self.weight_coverage < 0 or self.weight_mahalanobis < 0:
            raise ValueError("combiner weights must be non-negative")
        if (self.weight_nn + self.weight_coverage + self.weight_mahalanobis) <= 0:
            raise ValueError("at least one combiner weight must be > 0")

    def combine(
        self,
        *,
        nn: ComponentResult,
        coverage: ComponentResult,
        mahalanobis: ComponentResult,
    ) -> float:
        numerator = 0.0
        denominator = 0.0
        for result, weight in (
            (nn, self.weight_nn),
            (coverage, self.weight_coverage),
            (mahalanobis, self.weight_mahalanobis),
        ):
            if result.was_computed and weight > 0:
                # mypy: result.score is float when was_computed is True
                assert result.score is not None
                numerator += weight * result.score
                denominator += weight
        if denominator == 0.0:
            # All contributing components either skipped or were
            # zero-weighted. Return 0 rather than dividing — the score
            # is meaningless here and the orchestrator will tag the
            # diagnostics so oncall can see what happened.
            return 0.0
        return numerator / denominator


__all__ = ["LinearCombiner", "ScoreCombiner", "ScoringResult"]
