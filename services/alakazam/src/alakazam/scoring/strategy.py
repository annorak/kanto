"""Strategy interface for scoring components.

Each strategy answers one question of the form "how novel is this
isolate, by my particular metric?" — returning a floating-point
score in ``[0, +∞)`` where higher means more novel, plus optional
diagnostic numbers.

The interface is deliberately tight (one async method, a small data
class) so future v2 components (engineered-sequence detection, for
example) drop in without ceremony. See the README's "extending
the scorer" section for the receipe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from alakazam.scoring.orchestrator import IsolateContext


class ComponentName(StrEnum):
    """Canonical names for the v1 scoring components.

    Names appear in metrics labels, log fields, and Mew column names;
    keep them stable across versions or the dashboards break.
    """

    NN_DISTANCE = "nn_distance"
    COVERAGE = "coverage"
    MAHALANOBIS = "mahalanobis"


class SkipReason(StrEnum):
    """Why a strategy declined to compute its score.

    A skipped component contributes nothing to the combined score and
    is recorded as NULL in Mew. The reason is logged + metric'd so the
    operator can spot a strategy that's silently skipping every call.
    """

    BELOW_CANDIDATE_THRESHOLD = "below_candidate_threshold"
    REFERENCE_SET_MISSING = "reference_set_missing"
    PROTEIN_PARQUET_MISSING = "protein_parquet_missing"
    CENTROID_MISSING = "centroid_missing"
    CENTROID_INSUFFICIENT_SAMPLES = "centroid_insufficient_samples"
    COMPUTE_ERROR = "compute_error"


@dataclass(frozen=True, slots=True)
class ComponentResult:
    """One strategy's contribution to the final score.

    A non-``None`` ``score`` means the strategy ran and produced a
    number; ``None`` means it deliberately skipped (with a populated
    ``skip_reason``). The combiner ignores skipped components.
    ``diagnostics`` carries strategy-specific extras (NN list,
    coverage counts, etc.) for the README runbook + metrics.
    """

    name: ComponentName
    score: float | None
    skip_reason: SkipReason | None = None
    duration_seconds: float = 0.0
    diagnostics: dict[str, float | int | str] = field(default_factory=dict)

    @classmethod
    def computed(
        cls,
        name: ComponentName,
        score: float,
        *,
        duration_seconds: float,
        diagnostics: dict[str, float | int | str] | None = None,
    ) -> ComponentResult:
        return cls(
            name=name,
            score=float(score),
            skip_reason=None,
            duration_seconds=duration_seconds,
            diagnostics=diagnostics or {},
        )

    @classmethod
    def skipped(
        cls,
        name: ComponentName,
        reason: SkipReason,
        *,
        duration_seconds: float = 0.0,
        diagnostics: dict[str, float | int | str] | None = None,
    ) -> ComponentResult:
        return cls(
            name=name,
            score=None,
            skip_reason=reason,
            duration_seconds=duration_seconds,
            diagnostics=diagnostics or {},
        )

    @property
    def was_computed(self) -> bool:
        return self.score is not None


class ScoringStrategy(Protocol):
    """Async interface every scoring component implements.

    Concrete strategies are constructed once at startup with their
    dependencies (Mew gateway, reference set, OS client, settings) and
    invoked per-isolate. They must be safe to call concurrently from
    multiple coroutines on the same instance.
    """

    @property
    def name(self) -> ComponentName: ...

    async def compute(self, ctx: IsolateContext) -> ComponentResult: ...


__all__ = [
    "ComponentName",
    "ComponentResult",
    "ScoringStrategy",
    "SkipReason",
]
