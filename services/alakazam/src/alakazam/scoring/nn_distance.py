"""NN-distance strategy: mean cosine distance to the k nearest neighbors.

This is the cheap tier-1 component — one HNSW lookup against pgvector,
no Object Storage round-trip, no per-protein math. We compute it for
*every* isolate so the orchestrator can decide whether to fire the
expensive components.

Semantics
---------
* The seed isolate is excluded from its own neighbor set (handled in
  the gateway SQL).
* The seed must have an embedding row in Mew; if it doesn't, that's
  an upstream bug and we surface it via :class:`LookupError`.
* If the table has fewer than ``k`` other isolates (cold-start, dev
  environments), we score with whatever's available and tag the
  diagnostic. A genome scored against zero neighbors gets
  ``score=0.0`` and a ``no_neighbors`` diagnostic — the orchestrator
  treats this as a "no signal" tier-1 pass and skips tier 2.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from alakazam.scoring.strategy import (
    ComponentName,
    ComponentResult,
    SkipReason,
)

if TYPE_CHECKING:
    from alakazam.mew_gateway import AlakazamMewGateway
    from alakazam.scoring.orchestrator import IsolateContext

logger = logging.getLogger(__name__)


class NNDistanceStrategy:
    """Cheap, always-on tier-1 strategy."""

    name = ComponentName.NN_DISTANCE

    def __init__(self, *, gateway: AlakazamMewGateway, k: int) -> None:
        self._gateway = gateway
        self._k = k

    async def compute(self, ctx: IsolateContext) -> ComponentResult:
        start = time.perf_counter()
        try:
            neighbors = await self._gateway.k_nearest(
                accession=ctx.accession,
                k=self._k,
            )
        except Exception:
            logger.exception(
                "alakazam.nn_distance: gateway failure for accession=%s",
                ctx.accession,
            )
            return ComponentResult.skipped(
                self.name,
                SkipReason.COMPUTE_ERROR,
                duration_seconds=time.perf_counter() - start,
            )

        if not neighbors:
            return ComponentResult.computed(
                self.name,
                score=0.0,
                duration_seconds=time.perf_counter() - start,
                diagnostics={"n_neighbors": 0, "no_neighbors": "true"},
            )

        # pgvector's ``<=>`` cosine distance is already in [0, 2]; we
        # take the mean so the metric is directly comparable across
        # different ``k`` values.
        mean_distance = sum(n.distance for n in neighbors) / len(neighbors)
        return ComponentResult.computed(
            self.name,
            score=mean_distance,
            duration_seconds=time.perf_counter() - start,
            diagnostics={
                "n_neighbors": len(neighbors),
                "min_distance": min(n.distance for n in neighbors),
                "max_distance": max(n.distance for n in neighbors),
            },
        )


__all__ = ["NNDistanceStrategy"]
