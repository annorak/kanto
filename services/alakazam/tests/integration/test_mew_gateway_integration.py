"""Integration tests: AlakazamMewGateway against a real Postgres + pgvector.

Uses the session-scoped ``mew_container`` / ``mew_pool`` fixtures from
:mod:`kanto_commons.testing.mew_fixture`. Each test seeds its own
fixtures via the kanto-commons repositories, then exercises the
gateway end-to-end.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import psycopg
import pytest
import pytest_asyncio
from kanto_commons.mew.client import MewClient
from kanto_commons.mew.models import (
    EmbeddingRow,
    IsolateRow,
    IsolateStatus,
)
from kanto_commons.mew.repositories import (
    EmbeddingNotFoundError,
    EmbeddingRepository,
    IsolateNotFoundError,
    IsolateRepository,
)
from psycopg_pool import AsyncConnectionPool

from alakazam.mew_gateway import AlakazamMewGateway, SpeciesCentroidRecord

# Pull the testcontainer fixtures into this test module's discovery.
pytest_plugins = ["kanto_commons.testing.mew_fixture"]


pytestmark = pytest.mark.integration


_DIM = 1152


@pytest_asyncio.fixture
async def mew_client(mew_pool: AsyncConnectionPool) -> Any:
    return MewClient(pool=mew_pool)


def _seed_embedding_vec(seed: int) -> list[float]:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(_DIM).astype(np.float32)
    return v.tolist()


async def _seed_isolate(
    pool: AsyncConnectionPool,
    *,
    accession: str,
    organism: str,
    version: int = 1,
    with_embedding: bool = True,
) -> None:
    row = IsolateRow(
        accession=accession,
        version=version,
        organism=organism,
        source="ncbi-pd",
        status=IsolateStatus.EMBEDDED,
        discovered_at=datetime.now(UTC),
    )
    async with pool.connection() as conn:
        await IsolateRepository().upsert(conn, row)
        if with_embedding:
            await EmbeddingRepository().upsert(
                conn,
                EmbeddingRow(
                    accession=accession,
                    version=version,
                    model="esm-c-600m",
                    model_version="1.0.0",
                    embedding=_seed_embedding_vec(seed=hash(accession) & 0xFFFF),
                ),
            )


async def test_get_isolate_and_embedding_round_trip(
    mew_pool: AsyncConnectionPool,
    mew_client: MewClient,
) -> None:
    await _seed_isolate(mew_pool, accession="PDT0001", organism="Salmonella")
    gateway = AlakazamMewGateway(mew=mew_client)
    iso = await gateway.get_isolate("PDT0001")
    assert iso.organism == "Salmonella"
    assert iso.status is IsolateStatus.EMBEDDED

    emb = await gateway.get_genome_embedding("PDT0001")
    assert emb.accession == "PDT0001"
    assert len(emb.embedding) == _DIM


async def test_k_nearest_excludes_seed(
    mew_pool: AsyncConnectionPool,
    mew_client: MewClient,
) -> None:
    await _seed_isolate(mew_pool, accession="PDT0001", organism="Salmonella")
    await _seed_isolate(mew_pool, accession="PDT0002", organism="Salmonella")
    await _seed_isolate(mew_pool, accession="PDT0003", organism="Listeria")
    gateway = AlakazamMewGateway(mew=mew_client)
    neighbors = await gateway.k_nearest(accession="PDT0001", k=5)
    accessions = {n.accession for n in neighbors}
    assert "PDT0001" not in accessions
    assert {"PDT0002", "PDT0003"}.issubset(accessions)
    assert all(n.distance >= 0.0 for n in neighbors)


async def test_get_isolate_missing_raises(mew_client: MewClient) -> None:
    gateway = AlakazamMewGateway(mew=mew_client)
    with pytest.raises(IsolateNotFoundError):
        await gateway.get_isolate("missing")


async def test_get_embedding_missing_raises(
    mew_pool: AsyncConnectionPool,
    mew_client: MewClient,
) -> None:
    await _seed_isolate(
        mew_pool,
        accession="PDT0001",
        organism="X",
        with_embedding=False,
    )
    gateway = AlakazamMewGateway(mew=mew_client)
    with pytest.raises(EmbeddingNotFoundError):
        await gateway.get_genome_embedding("PDT0001")


async def test_write_score_with_nulls_for_skipped_components(
    mew_pool: AsyncConnectionPool,
    mew_client: MewClient,
) -> None:
    await _seed_isolate(mew_pool, accession="PDT0001", organism="Salmonella")
    gateway = AlakazamMewGateway(mew=mew_client)
    await gateway.write_score(
        accession="PDT0001",
        novelty_score=0.42,
        nn_distance=0.4,
        coverage=None,
        mahalanobis=None,
        above_threshold=False,
        scored_at=datetime.now(UTC),
    )
    async with mew_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT novelty_score, coverage, mahalanobis, status "
            "FROM isolates WHERE accession = %s",
            ("PDT0001",),
        )
        row: Any = await cur.fetchone()
    assert row is not None
    assert row[0] == pytest.approx(0.42)
    assert row[1] is None
    assert row[2] is None
    assert row[3] == "SCORED"


async def test_write_score_missing_isolate(mew_client: MewClient) -> None:
    gateway = AlakazamMewGateway(mew=mew_client)
    with pytest.raises(IsolateNotFoundError):
        await gateway.write_score(
            accession="missing",
            novelty_score=0.1,
            nn_distance=0.1,
            coverage=None,
            mahalanobis=None,
            above_threshold=False,
            scored_at=datetime.now(UTC),
        )


async def test_species_centroid_round_trip(
    mew_pool: AsyncConnectionPool,
    mew_client: MewClient,
) -> None:
    gateway = AlakazamMewGateway(mew=mew_client)
    rec = SpeciesCentroidRecord(
        organism="Salmonella",
        model="esm-c-600m",
        model_version="1.0.0",
        n_isolates=42,
        centroid=_seed_embedding_vec(1),
        covariance_diagonal=[1.0] * _DIM,
        regularization=1e-4,
        computed_at=datetime.now(UTC),
    )
    await gateway.upsert_species_centroid(rec)
    fetched = await gateway.get_species_centroid("Salmonella")
    assert fetched is not None
    assert fetched.n_isolates == 42
    assert len(fetched.centroid) == _DIM
    assert len(fetched.covariance_diagonal) == _DIM
    assert fetched.regularization == pytest.approx(1e-4)

    # Upsert again with new stats; the row should be updated, not duplicated.
    rec_v2 = SpeciesCentroidRecord(
        organism="Salmonella",
        model="esm-c-600m",
        model_version="1.0.0",
        n_isolates=99,
        centroid=_seed_embedding_vec(2),
        covariance_diagonal=[2.0] * _DIM,
        regularization=2e-4,
        computed_at=datetime.now(UTC),
    )
    await gateway.upsert_species_centroid(rec_v2)
    fetched2 = await gateway.get_species_centroid("Salmonella")
    assert fetched2 is not None
    assert fetched2.n_isolates == 99


async def test_list_organisms_with_embeddings_filters_by_min(
    mew_pool: AsyncConnectionPool,
    mew_client: MewClient,
) -> None:
    for i in range(3):
        await _seed_isolate(mew_pool, accession=f"PDT{i:05d}", organism="Salmonella")
    await _seed_isolate(mew_pool, accession="PDT99999", organism="Listeria", with_embedding=True)
    gateway = AlakazamMewGateway(mew=mew_client)
    listings = await gateway.list_organisms_with_embeddings(min_isolates=2)
    by_org = dict(listings)
    assert by_org.get("Salmonella") == 3
    assert "Listeria" not in by_org


async def test_stream_embeddings_returns_all_for_organism(
    mew_pool: AsyncConnectionPool,
    mew_client: MewClient,
) -> None:
    for i in range(4):
        await _seed_isolate(mew_pool, accession=f"PDT0{i}", organism="Salmonella")
    gateway = AlakazamMewGateway(mew=mew_client)
    rows = [r async for r in gateway.stream_embeddings_by_organism("Salmonella")]
    assert len(rows) == 4
    assert all(len(r) == _DIM for r in rows)


# Quiet unused-import linter; psycopg is here so type-checkers see the dep.
_ = psycopg
