"""In-memory cache of species centroids.

The Mahalanobis strategy needs a centroid + diagonal covariance per
organism on every tier-2 isolate. Reading these from Mew on each
invocation would be wasted IO — centroids change ~daily (the CronJob
schedule). So we cache them in process and refresh on a timer.

The cache is async-safe via a single :class:`asyncio.Lock`; concurrent
readers are fine, and the refresh swaps the dict atomically.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from alakazam.mew_gateway import SpeciesCentroidRecord

if TYPE_CHECKING:
    from alakazam.mew_gateway import AlakazamMewGateway

logger = logging.getLogger(__name__)


class SpeciesCentroidCache:
    """Thread-safe per-organism centroid cache."""

    def __init__(self, *, gateway: AlakazamMewGateway) -> None:
        self._gateway = gateway
        self._centroids: dict[str, SpeciesCentroidRecord] = {}
        self._refresh_lock = asyncio.Lock()

    def get(self, organism: str) -> SpeciesCentroidRecord | None:
        """Return the cached row for ``organism``, or None.

        Synchronous because the lookup is a plain dict read; the
        Mahalanobis strategy stays on the hot path without an extra
        ``await``.
        """
        return self._centroids.get(organism)

    @property
    def size(self) -> int:
        return len(self._centroids)

    async def refresh(self) -> int:
        """Reload every centroid from Mew. Returns the new size."""
        async with self._refresh_lock:
            rows = await self._gateway.list_species_centroids()
            self._centroids = {row.organism: row for row in rows}
            logger.info("alakazam.species_cache: refreshed %d centroids", len(rows))
            return len(rows)

    def replace(self, rows: dict[str, SpeciesCentroidRecord]) -> None:
        """Test hook: swap the cached dict without going through Mew."""
        self._centroids = dict(rows)


__all__ = ["SpeciesCentroidCache"]
