"""Tests for the centroid recomputation job's math."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pytest

from alakazam.mew_gateway import SpeciesCentroidRecord
from alakazam.scripts.species_centroids_job import _compute_one_centroid

_DIM = 1152


@dataclass
class _StubGateway:
    embeddings: dict[str, list[list[float]]] = field(default_factory=dict)
    upserts: list[SpeciesCentroidRecord] = field(default_factory=list)

    async def stream_embeddings_by_organism(self, organism: str) -> AsyncIterator[list[float]]:
        for row in self.embeddings.get(organism, []):
            yield list(row)

    async def upsert_species_centroid(self, record: SpeciesCentroidRecord) -> None:
        self.upserts.append(record)


async def test_compute_centroid_basic() -> None:
    """Welford streaming results match numpy's batch mean/var."""
    rng = np.random.default_rng(0)
    rows = rng.standard_normal((25, _DIM)).astype(np.float32)
    gateway: Any = _StubGateway(embeddings={"Salmonella": rows.tolist()})
    record = await _compute_one_centroid(
        gateway=gateway,
        organism="Salmonella",
        expected_count=25,
        regularization=1e-4,
    )
    assert record.n_isolates == 25
    assert record.organism == "Salmonella"
    centroid_np = np.asarray(record.centroid, dtype=np.float32)
    var_np = np.asarray(record.covariance_diagonal, dtype=np.float32)
    assert np.allclose(centroid_np, rows.mean(axis=0), atol=1e-5)
    assert np.allclose(var_np, rows.var(axis=0), atol=1e-5)


async def test_streaming_centroid_matches_naive_on_small_fixture() -> None:
    """A tiny deterministic fixture exercises Welford end-to-end with
    an exact baseline we can compute by hand: 4 rows in 3 dims.
    """
    rows = np.array(
        [
            [1.0, 2.0, 3.0],
            [4.0, 5.0, 6.0],
            [7.0, 8.0, 9.0],
            [2.0, 3.0, 4.0],
        ],
        dtype=np.float32,
    )
    gateway: Any = _StubGateway(embeddings={"S": rows.tolist()})
    record = await _compute_one_centroid(
        gateway=gateway,
        organism="S",
        expected_count=4,
        regularization=1e-4,
    )
    expected_mean = rows.mean(axis=0)
    expected_var = rows.var(axis=0)  # population variance (ddof=0)
    assert np.allclose(record.centroid, expected_mean, atol=1e-6)
    assert np.allclose(record.covariance_diagonal, expected_var, atol=1e-6)
    assert record.n_isolates == 4


async def test_compute_centroid_too_few_samples() -> None:
    gateway: Any = _StubGateway(embeddings={"X": [[0.0] * _DIM]})
    from alakazam.scripts.species_centroids_job import _InsufficientDataError

    with pytest.raises(_InsufficientDataError):
        await _compute_one_centroid(
            gateway=gateway,
            organism="X",
            expected_count=1,
            regularization=1e-4,
        )


async def test_compute_centroid_no_embeddings() -> None:
    gateway: Any = _StubGateway(embeddings={})
    from alakazam.scripts.species_centroids_job import _InsufficientDataError

    with pytest.raises(_InsufficientDataError):
        await _compute_one_centroid(
            gateway=gateway,
            organism="Missing",
            expected_count=10,
            regularization=1e-4,
        )
