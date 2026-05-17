"""Scoring strategies + combiner for Alakazam.

The public surface of this subpackage is intentionally narrow: callers
import the Strategy interfaces, the concrete strategies, the combiner,
and the orchestrator. Everything else is an implementation detail.

See the module docstrings for the algorithmic details; see the
:mod:`alakazam` README for the tiered-scoring tradeoff and the per-
component tuning levers.
"""

from __future__ import annotations

from alakazam.scoring.combiner import LinearCombiner, ScoreCombiner, ScoringResult
from alakazam.scoring.coverage import CoverageStrategy
from alakazam.scoring.mahalanobis import MahalanobisStrategy
from alakazam.scoring.nn_distance import NNDistanceStrategy
from alakazam.scoring.orchestrator import IsolateContext, ScoringOrchestrator
from alakazam.scoring.strategy import (
    ComponentName,
    ComponentResult,
    ScoringStrategy,
    SkipReason,
)

__all__ = [
    "ComponentName",
    "ComponentResult",
    "CoverageStrategy",
    "IsolateContext",
    "LinearCombiner",
    "MahalanobisStrategy",
    "NNDistanceStrategy",
    "ScoreCombiner",
    "ScoringOrchestrator",
    "ScoringResult",
    "ScoringStrategy",
    "SkipReason",
]
