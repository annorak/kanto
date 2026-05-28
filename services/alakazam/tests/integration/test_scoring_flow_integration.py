"""Integration test: end-to-end scoring flow against real Mew + fake OS/producer.

We compose Pipeline + ScoringOrchestrator + real AlakazamMewGateway
against the testcontainer Mew, with the per-protein parquet served
from a :class:`FakeObjectStorage` and the IsolateScored output
captured by a stub producer. This exercises the integration the unit
suite cannot: the SQL writes, pgvector reads, the HNSW path, and
the gateway/strategy boundary all together.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pytest
import pytest_asyncio
from kanto_commons import IsolateScored
from kanto_commons.mew.client import MewClient
from kanto_commons.mew.models import EmbeddingRow, IsolateRow, IsolateStatus
from kanto_commons.mew.repositories import EmbeddingRepository, IsolateRepository
from kanto_commons.storage import KeyBuilder
from kanto_commons.testing.factories import make_embeddings_ready
from kanto_commons.testing.fakes import FakeObjectStorage
from psycopg_pool import AsyncConnectionPool

from alakazam.metrics import AlakazamMetrics
from alakazam.mew_gateway import AlakazamMewGateway, SpeciesCentroidRecord
from alakazam.pipeline import Outcome, Pipeline
from alakazam.reference_set import from_arrays
from alakazam.scoring import (
    CoverageStrategy,
    LinearCombiner,
    MahalanobisStrategy,
    NNDistanceStrategy,
    ScoringOrchestrator,
)
from alakazam.species_cache import SpeciesCentroidCache
from tests.conftest import make_protein_parquet_bytes

pytest_plugins = ["kanto_commons.testing.mew_fixture"]

pytestmark = pytest.mark.integration

_DIM = 1152


@pytest_asyncio.fixture
async def mew_client(mew_pool: AsyncConnectionPool) -> Any:
    return MewClient(pool=mew_pool)


@dataclass
class _StubProducer:
    sent: list[IsolateScored] = field(default_factory=list)

    async def send(self, event: IsolateScored) -> None:
        self.sent.append(event)


def _key_builder() -> KeyBuilder:
    return KeyBuilder(
        proteins_container="kanto-proteins",
        embeddings_container="kanto-embeddings",
        metadata_container="kanto-metadata",
    )


async def _seed_isolate_with_embedding(
    pool: AsyncConnectionPool,
    *,
    accession: str,
    organism: str,
    embedding: np.ndarray,
) -> None:
    async with pool.connection() as conn:
        await IsolateRepository().upsert(
            conn,
            IsolateRow(
                accession=accession,
                version=1,
                organism=organism,
                source="ncbi-pd",
                status=IsolateStatus.EMBEDDED,
                discovered_at=datetime.now(UTC),
            ),
        )
        await EmbeddingRepository().upsert(
            conn,
            EmbeddingRow(
                accession=accession,
                version=1,
                model="esm-c-600m",
                model_version="1.0.0",
                embedding=embedding.astype(np.float32).tolist(),
            ),
        )


async def test_full_scoring_flow_tier_two(
    mew_pool: AsyncConnectionPool,
    mew_client: MewClient,
) -> None:
    # Seed the seed isolate plus a few neighbors so k-NN has data.
    seed_vec = np.random.default_rng(1).standard_normal(_DIM).astype(np.float32)
    seed_vec /= np.linalg.norm(seed_vec)
    await _seed_isolate_with_embedding(
        mew_pool,
        accession="PDT0001",
        organism="Salmonella",
        embedding=seed_vec,
    )
    rng = np.random.default_rng(2)
    for i in range(5):
        # Move neighbors away from seed by random noise so cosine distance is high.
        neigh = rng.standard_normal(_DIM).astype(np.float32)
        await _seed_isolate_with_embedding(
            mew_pool,
            accession=f"PDT_neigh_{i:02d}",
            organism="Salmonella",
            embedding=neigh,
        )

    # Reference set: small, random; we just want coverage to run.
    ref = from_arrays(
        embeddings=rng.standard_normal((4, _DIM)).astype(np.float32),
        protein_ids=["R0", "R1", "R2", "R3"],
        sources=["busco"] * 4,
    )

    # Per-protein parquet for the seed isolate; landed in the fake OS.
    storage = FakeObjectStorage()
    proteins = rng.standard_normal((6, _DIM)).astype(np.float16)
    parquet = make_protein_parquet_bytes(proteins)
    storage.put_bytes(
        container="kanto-embeddings",
        key="PDT0001.1/1.parquet",
        data=parquet,
    )

    # Species centroid (write to Mew so the cache picks it up).
    gateway = AlakazamMewGateway(mew=mew_client)
    await gateway.upsert_species_centroid(
        SpeciesCentroidRecord(
            organism="Salmonella",
            model="esm-c-600m",
            model_version="1.0.0",
            n_isolates=50,
            centroid=np.zeros(_DIM, dtype=np.float32).tolist(),
            covariance_diagonal=[1.0] * _DIM,
            regularization=1e-4,
            computed_at=datetime.now(UTC),
        )
    )

    cache = SpeciesCentroidCache(gateway=gateway)
    await cache.refresh()
    assert cache.size == 1

    metrics = AlakazamMetrics.build()
    orchestrator = ScoringOrchestrator(
        nn_strategy=NNDistanceStrategy(gateway=gateway, k=5),
        coverage_strategy=CoverageStrategy(
            reference_set=ref,
            storage=storage,
            key_builder=_key_builder(),
            embeddings_container="kanto-embeddings",
            match_threshold=0.6,
            max_proteins=100,
        ),
        mahalanobis_strategy=MahalanobisStrategy(
            centroid_cache=cache, min_samples=10, regularization=1e-6
        ),
        combiner=LinearCombiner(weight_nn=1.0, weight_coverage=2.0, weight_mahalanobis=0.5),
        # Aggressively low threshold so this seed is guaranteed to be a candidate.
        candidate_threshold=0.0,
        alert_threshold=0.1,
    )
    producer: Any = _StubProducer()
    pipeline = Pipeline(
        gateway=gateway,
        orchestrator=orchestrator,
        producer=producer,
        metrics=metrics,
        candidate_threshold=0.0,
    )

    result = await pipeline.process(make_embeddings_ready(accession="PDT0001"))
    assert result.outcome is Outcome.SUCCESS
    assert result.was_candidate
    assert result.score is not None
    assert result.score.coverage.was_computed
    assert result.score.mahalanobis.was_computed

    # The score was persisted to Mew with the SCORED status.
    async with mew_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT status, novelty_score, coverage, mahalanobis "
            "FROM isolates WHERE accession = %s",
            ("PDT0001",),
        )
        row: Any = await cur.fetchone()
    assert row is not None
    assert row[0] == "SCORED"
    assert row[1] is not None
    assert row[2] is not None
    assert row[3] is not None

    assert len(producer.sent) == 1
    assert producer.sent[0].accession == "PDT0001"


async def test_full_scoring_flow_tier_one_only(
    mew_pool: AsyncConnectionPool,
    mew_client: MewClient,
) -> None:
    # Cold start: a single isolate in the table — no neighbors, so the
    # NN score is 0 and tier 2 is skipped.
    seed_vec = np.random.default_rng(99).standard_normal(_DIM).astype(np.float32)
    await _seed_isolate_with_embedding(
        mew_pool,
        accession="PDT0099",
        organism="Listeria",
        embedding=seed_vec,
    )

    gateway = AlakazamMewGateway(mew=mew_client)
    cache = SpeciesCentroidCache(gateway=gateway)
    await cache.refresh()

    orchestrator = ScoringOrchestrator(
        nn_strategy=NNDistanceStrategy(gateway=gateway, k=10),
        coverage_strategy=CoverageStrategy(
            reference_set=None,
            storage=FakeObjectStorage(),
            key_builder=_key_builder(),
            embeddings_container="kanto-embeddings",
            match_threshold=0.6,
            max_proteins=100,
        ),
        mahalanobis_strategy=MahalanobisStrategy(
            centroid_cache=cache, min_samples=10, regularization=1e-6
        ),
        combiner=LinearCombiner(weight_nn=1.0, weight_coverage=2.0, weight_mahalanobis=0.5),
        candidate_threshold=0.3,
        alert_threshold=0.5,
    )
    producer: Any = _StubProducer()
    pipeline = Pipeline(
        gateway=gateway,
        orchestrator=orchestrator,
        producer=producer,
        metrics=AlakazamMetrics.build(),
        candidate_threshold=0.3,
    )

    result = await pipeline.process(make_embeddings_ready(accession="PDT0099"))
    assert result.outcome is Outcome.SUCCESS
    assert not result.was_candidate

    async with mew_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT coverage, mahalanobis FROM isolates WHERE accession = %s",
            ("PDT0099",),
        )
        row: Any = await cur.fetchone()
    assert row is not None
    assert row[0] is None
    assert row[1] is None
