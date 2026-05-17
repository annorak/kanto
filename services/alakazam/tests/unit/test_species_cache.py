"""Tests for the in-memory species centroid cache."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pytest

from alakazam.mew_gateway import SpeciesCentroidRecord
from alakazam.species_cache import SpeciesCentroidCache

_DIM = 1152


@dataclass
class _StubGateway:
    rows: list[SpeciesCentroidRecord] = field(default_factory=list)

    async def list_species_centroids(self) -> list[SpeciesCentroidRecord]:
        return list(self.rows)


def _rec(name: str) -> SpeciesCentroidRecord:
    return SpeciesCentroidRecord(
        organism=name,
        model="esm-c-600m",
        model_version="1.0.0",
        n_isolates=50,
        centroid=np.zeros(_DIM, dtype=np.float32).tolist(),
        covariance_diagonal=np.ones(_DIM, dtype=np.float32).tolist(),
        regularization=1e-4,
        computed_at=datetime.now(UTC),
    )


async def test_refresh_loads_from_gateway() -> None:
    gateway: Any = _StubGateway(rows=[_rec("A"), _rec("B")])
    cache = SpeciesCentroidCache(gateway=gateway)
    assert cache.size == 0
    n = await cache.refresh()
    assert n == 2
    assert cache.size == 2
    assert cache.get("A") is not None
    assert cache.get("missing") is None


async def test_refresh_replaces_atomically() -> None:
    gateway: Any = _StubGateway(rows=[_rec("A"), _rec("B")])
    cache = SpeciesCentroidCache(gateway=gateway)
    await cache.refresh()
    gateway.rows = [_rec("B"), _rec("C")]
    await cache.refresh()
    assert cache.get("A") is None
    assert cache.get("B") is not None
    assert cache.get("C") is not None


def test_replace_test_hook() -> None:
    gateway: Any = _StubGateway()
    cache = SpeciesCentroidCache(gateway=gateway)
    rec = _rec("Z")
    cache.replace({rec.organism: rec})
    assert cache.get("Z") is rec
    assert cache.size == 1
    _ = pytest  # quiet
