"""Mahalanobis strategy: standardized distance from the species centroid.

For each isolate of organism ``X``, we look up the precomputed
centroid + diagonal covariance for ``X`` from Mew, and compute the
Mahalanobis distance from the isolate's embedding to the centroid
under that diagonal covariance:

    delta_i = (x_i - mu_i)
    d^2     = sum_i (delta_i^2 / (sigma_i^2 + eps))
    d       = sqrt(d^2)

Two design notes:

* **Diagonal covariance only.** v1 stores per-dimension variances,
  not the full 1152x1152 covariance matrix; rationale in the README's
  "diagonal vs full covariance" subsection. A full matrix is a v2
  change and lives behind a second-row column rather than overwriting
  this one.

* **Regularization is read from the row.** The centroid job picks an
  epsilon and writes it alongside the variances; this strategy uses
  whatever the row carries so a compute-job tweak doesn't require a
  service redeploy.

If the centroid is missing or was computed from too few isolates,
the strategy skips with the appropriate :class:`SkipReason`.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import numpy as np

from alakazam.scoring.strategy import (
    ComponentName,
    ComponentResult,
    SkipReason,
)

if TYPE_CHECKING:
    from alakazam.scoring.orchestrator import IsolateContext
    from alakazam.species_cache import SpeciesCentroidCache


logger = logging.getLogger(__name__)


class MahalanobisStrategy:
    """Tier-2: Mahalanobis distance from the species centroid."""

    name = ComponentName.MAHALANOBIS

    def __init__(
        self,
        *,
        centroid_cache: SpeciesCentroidCache,
        min_samples: int,
        regularization: float,
    ) -> None:
        self._cache = centroid_cache
        self._min_samples = min_samples
        # Backstop regularization used when the cached row's own
        # epsilon is somehow zero. The row CHECK constraint forbids
        # this, but defending here means a bad seed doesn't blow up
        # the scoring path.
        self._regularization = regularization

    async def compute(self, ctx: IsolateContext) -> ComponentResult:
        start = time.perf_counter()
        centroid = self._cache.get(ctx.organism)
        if centroid is None:
            return ComponentResult.skipped(
                self.name,
                SkipReason.CENTROID_MISSING,
                duration_seconds=time.perf_counter() - start,
                diagnostics={"organism": ctx.organism},
            )
        if centroid.n_isolates < self._min_samples:
            return ComponentResult.skipped(
                self.name,
                SkipReason.CENTROID_INSUFFICIENT_SAMPLES,
                duration_seconds=time.perf_counter() - start,
                diagnostics={
                    "organism": ctx.organism,
                    "n_isolates": centroid.n_isolates,
                    "min_samples": self._min_samples,
                },
            )

        try:
            embedding = np.asarray(ctx.embedding, dtype=np.float32)
            mu = np.asarray(centroid.centroid, dtype=np.float32)
            variances = np.asarray(centroid.covariance_diagonal, dtype=np.float32)
            eps = max(float(centroid.regularization), self._regularization)
            denom = variances + eps
            delta = embedding - mu
            d2 = float(np.sum((delta * delta) / denom))
            score = float(np.sqrt(d2))
        except Exception:
            logger.exception(
                "alakazam.mahalanobis: compute failure for %s (organism=%s)",
                ctx.accession,
                ctx.organism,
            )
            return ComponentResult.skipped(
                self.name,
                SkipReason.COMPUTE_ERROR,
                duration_seconds=time.perf_counter() - start,
                diagnostics={"organism": ctx.organism},
            )

        return ComponentResult.computed(
            self.name,
            score=score,
            duration_seconds=time.perf_counter() - start,
            diagnostics={
                "organism": ctx.organism,
                "n_isolates": centroid.n_isolates,
                "regularization": eps,
                "centroid_age_seconds": (
                    float((ctx.now - centroid.computed_at).total_seconds())
                    if ctx.now is not None
                    else -1.0
                ),
            },
        )


__all__ = ["MahalanobisStrategy"]
