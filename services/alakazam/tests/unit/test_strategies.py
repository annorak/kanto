"""Per-strategy unit tests with mocked dependencies."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import pytest
from kanto_commons.mew.models import NeighborRow
from kanto_commons.storage import KeyBuilder
from kanto_commons.testing.fakes import FakeObjectStorage

from alakazam.mew_gateway import SpeciesCentroidRecord
from alakazam.scoring import (
    CoverageStrategy,
    IsolateContext,
    MahalanobisStrategy,
    NNDistanceStrategy,
)
from alakazam.scoring.strategy import ComponentName, SkipReason
from alakazam.species_cache import SpeciesCentroidCache
from tests.conftest import make_protein_parquet_bytes

_DIM = 1152


# ---------------------------------------------------------------------------
# NN-distance
# ---------------------------------------------------------------------------


class _FakeGateway:
    def __init__(self, neighbors: list[NeighborRow] | None = None, raise_: bool = False) -> None:
        self._neighbors = neighbors or []
        self._raise = raise_

    async def k_nearest(self, *, accession: str, k: int) -> list[NeighborRow]:
        _ = (accession, k)
        if self._raise:
            raise RuntimeError("boom")
        return self._neighbors


@pytest.fixture
def ctx() -> IsolateContext:
    return IsolateContext(
        accession="PDT0001.1",
        version=1,
        organism="Salmonella",
        embedding=[0.0] * _DIM,
        now=datetime.now(UTC),
    )


async def test_nn_distance_mean(ctx: IsolateContext) -> None:
    gateway: Any = _FakeGateway(
        neighbors=[
            NeighborRow(accession="A", distance=0.1),
            NeighborRow(accession="B", distance=0.3),
            NeighborRow(accession="C", distance=0.5),
        ]
    )
    strategy = NNDistanceStrategy(gateway=gateway, k=3)
    result = await strategy.compute(ctx)
    assert result.name is ComponentName.NN_DISTANCE
    assert result.score == pytest.approx(0.3)
    assert result.diagnostics["n_neighbors"] == 3


async def test_nn_distance_no_neighbors(ctx: IsolateContext) -> None:
    gateway: Any = _FakeGateway(neighbors=[])
    strategy = NNDistanceStrategy(gateway=gateway, k=10)
    result = await strategy.compute(ctx)
    assert result.was_computed
    assert result.score == 0.0
    assert result.diagnostics["no_neighbors"] == "true"


async def test_nn_distance_gateway_error_skips(ctx: IsolateContext) -> None:
    gateway: Any = _FakeGateway(raise_=True)
    strategy = NNDistanceStrategy(gateway=gateway, k=5)
    result = await strategy.compute(ctx)
    assert not result.was_computed
    assert result.skip_reason is SkipReason.COMPUTE_ERROR


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def _key_builder() -> KeyBuilder:
    return KeyBuilder(
        proteins_container="kanto-proteins",
        embeddings_container="kanto-embeddings",
        metadata_container="kanto-metadata",
    )


async def test_coverage_recognized_fraction(ctx: IsolateContext, small_reference_set: Any) -> None:
    # Make every genome protein identical to reference[0] (after normalization)
    # so cosine sim == 1 against ref[0] and ≈ random against the others.
    ref_row = small_reference_set.matrix[0]
    embeddings = np.tile(ref_row, (4, 1)).astype(np.float16)
    parquet = make_protein_parquet_bytes(embeddings)
    storage = FakeObjectStorage()
    storage.put_bytes(
        container="kanto-embeddings",
        key="PDT0001.1/1.parquet",
        data=parquet,
    )
    strategy = CoverageStrategy(
        reference_set=small_reference_set,
        storage=storage,
        key_builder=_key_builder(),
        embeddings_container="kanto-embeddings",
        match_threshold=0.99,
        max_proteins=100,
    )
    result = await strategy.compute(ctx)
    assert result.was_computed
    # All 4 proteins recognized → coverage score = 1 - 1 = 0.0.
    assert result.score == pytest.approx(0.0, abs=1e-3)
    assert result.diagnostics["n_recognized"] == 4
    assert result.diagnostics["n_proteins"] == 4


async def test_coverage_no_matches_high_score(
    ctx: IsolateContext, small_reference_set: Any
) -> None:
    # Construct embeddings orthogonal to every reference row by zeroing out
    # the first _DIM // 2 dims and putting mass in the second half, then
    # making the reference set inhabit only the first half by zeroing the
    # second half. Easier: use a tiny match_threshold and a high one.
    rng = np.random.default_rng(123)
    embeddings = rng.standard_normal((6, _DIM)).astype(np.float16)
    parquet = make_protein_parquet_bytes(embeddings)
    storage = FakeObjectStorage()
    storage.put_bytes(
        container="kanto-embeddings",
        key="PDT0001.1/1.parquet",
        data=parquet,
    )
    strategy = CoverageStrategy(
        reference_set=small_reference_set,
        storage=storage,
        key_builder=_key_builder(),
        embeddings_container="kanto-embeddings",
        match_threshold=0.999999,  # impossible match
        max_proteins=100,
    )
    result = await strategy.compute(ctx)
    assert result.was_computed
    assert result.score == pytest.approx(1.0)
    assert result.diagnostics["n_recognized"] == 0


async def test_coverage_skips_when_reference_set_missing(ctx: IsolateContext) -> None:
    strategy = CoverageStrategy(
        reference_set=None,
        storage=FakeObjectStorage(),
        key_builder=_key_builder(),
        embeddings_container="kanto-embeddings",
        match_threshold=0.7,
        max_proteins=100,
    )
    result = await strategy.compute(ctx)
    assert not result.was_computed
    assert result.skip_reason is SkipReason.REFERENCE_SET_MISSING


async def test_coverage_skips_when_parquet_missing(
    ctx: IsolateContext, small_reference_set: Any
) -> None:
    strategy = CoverageStrategy(
        reference_set=small_reference_set,
        storage=FakeObjectStorage(),
        key_builder=_key_builder(),
        embeddings_container="kanto-embeddings",
        match_threshold=0.7,
        max_proteins=100,
    )
    result = await strategy.compute(ctx)
    assert not result.was_computed
    assert result.skip_reason is SkipReason.PROTEIN_PARQUET_MISSING


# ---------------------------------------------------------------------------
# Mahalanobis
# ---------------------------------------------------------------------------


def _make_centroid_record(
    *,
    organism: str,
    n_isolates: int,
    centroid: np.ndarray,
    variance: float,
) -> SpeciesCentroidRecord:
    return SpeciesCentroidRecord(
        organism=organism,
        model="esm-c-600m",
        model_version="1.0.0",
        n_isolates=n_isolates,
        centroid=centroid.tolist(),
        covariance_diagonal=[variance] * _DIM,
        regularization=1e-4,
        computed_at=datetime.now(UTC),
    )


class _StubGateway:
    """SpeciesCentroidCache only calls list_species_centroids during refresh."""

    async def list_species_centroids(self) -> list[SpeciesCentroidRecord]:
        return []


def _make_cache_with(rec: SpeciesCentroidRecord) -> SpeciesCentroidCache:
    gateway: Any = _StubGateway()
    cache = SpeciesCentroidCache(gateway=gateway)
    cache.replace({rec.organism: rec})
    return cache


async def test_mahalanobis_zero_when_at_centroid(ctx: IsolateContext) -> None:
    centroid = np.array(ctx.embedding, dtype=np.float32)
    rec = _make_centroid_record(
        organism=ctx.organism,
        n_isolates=100,
        centroid=centroid,
        variance=1.0,
    )
    strategy = MahalanobisStrategy(
        centroid_cache=_make_cache_with(rec),
        min_samples=10,
        regularization=1e-6,
    )
    result = await strategy.compute(ctx)
    assert result.was_computed
    assert result.score == pytest.approx(0.0, abs=1e-3)


async def test_mahalanobis_known_distance() -> None:
    centroid = np.zeros(_DIM, dtype=np.float32)
    ctx = IsolateContext(
        accession="X",
        version=1,
        organism="E. coli",
        embedding=[1.0] * _DIM,
        now=datetime.now(UTC),
    )
    rec = _make_centroid_record(
        organism="E. coli",
        n_isolates=100,
        centroid=centroid,
        variance=1.0,
    )
    strategy = MahalanobisStrategy(
        centroid_cache=_make_cache_with(rec),
        min_samples=10,
        regularization=1e-6,
    )
    result = await strategy.compute(ctx)
    assert result.was_computed
    # delta^2 / sigma^2 = 1.0 / (1 + 1e-4) over _DIM dims, sqrt of sum ≈ sqrt(_DIM).
    assert result.score == pytest.approx(np.sqrt(_DIM), rel=1e-3)


async def test_mahalanobis_skips_when_centroid_missing(ctx: IsolateContext) -> None:
    strategy = MahalanobisStrategy(
        centroid_cache=_make_cache_with(
            _make_centroid_record(
                organism="other-species",
                n_isolates=100,
                centroid=np.zeros(_DIM, dtype=np.float32),
                variance=1.0,
            )
        ),
        min_samples=10,
        regularization=1e-6,
    )
    result = await strategy.compute(ctx)
    assert not result.was_computed
    assert result.skip_reason is SkipReason.CENTROID_MISSING


async def test_mahalanobis_skips_below_min_samples(ctx: IsolateContext) -> None:
    rec = _make_centroid_record(
        organism=ctx.organism,
        n_isolates=3,
        centroid=np.zeros(_DIM, dtype=np.float32),
        variance=1.0,
    )
    strategy = MahalanobisStrategy(
        centroid_cache=_make_cache_with(rec),
        min_samples=10,
        regularization=1e-6,
    )
    result = await strategy.compute(ctx)
    assert not result.was_computed
    assert result.skip_reason is SkipReason.CENTROID_INSUFFICIENT_SAMPLES
