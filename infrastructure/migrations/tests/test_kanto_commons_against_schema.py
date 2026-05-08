"""Run every kanto-commons Mew repository method against the migrated schema.

Section 7 of Task 3 requires that the runtime client agree with the
migration — adding tables but forgetting a column, or shifting a type,
should fail loudly here. These tests instantiate the production
repositories and invoke each public method end-to-end.

The kanto-commons package has its own integration tests that exercise
the same surface area; the duplication here is intentional. Those
tests live next to the client code (so kanto-commons contributors run
them); these live next to the migration (so migration contributors
run them) — and a regression in either place blocks the change at
the right CI gate.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import psycopg
import pytest
import pytest_asyncio
from kanto_commons.mew import (
    AlertRepository,
    AlertStatus,
    EmbeddingRepository,
    EmbeddingRow,
    IsolateRepository,
    IsolateRow,
    IsolateStatus,
)
from kanto_commons.mew.repositories import (
    AlertNotFoundError,
    EmbeddingNotFoundError,
    IsolateNotFoundError,
)
from psycopg_pool import AsyncConnectionPool


def _make_isolate(
    accession: str = "PDT001",
    version: int = 1,
    organism: str = "Salmonella",
    status: IsolateStatus = IsolateStatus.DISCOVERED,
    discovered_at: datetime | None = None,
    collection_date: date | None = None,
) -> IsolateRow:
    return IsolateRow(
        accession=accession,
        version=version,
        organism=organism,
        source="ncbi-pd",
        status=status,
        discovered_at=discovered_at or datetime(2026, 1, 1, tzinfo=UTC),
        collection_date=collection_date,
    )


def _vec(seed: float, dim: int = 1152) -> list[float]:
    return [seed + i * 1e-4 for i in range(dim)]


@pytest_asyncio.fixture
async def pool(applied_db: str) -> AsyncIterator[AsyncConnectionPool]:
    """Async pool wired against the migrated container.

    The pool is opened with the pgvector adapter pre-registered so the
    repository's vector reads/writes round-trip correctly.
    """
    from kanto_commons.mew.client import _configure_connection

    pool = AsyncConnectionPool(
        applied_db,
        min_size=1,
        max_size=4,
        configure=_configure_connection,
        open=False,
    )
    await pool.open()
    try:
        # Each test starts with a clean table set. The migrated schema
        # is shared across this module's tests so we TRUNCATE rather
        # than re-apply migrations on every test.
        with psycopg.connect(applied_db, autocommit=True) as sync_conn:
            sync_conn.execute(
                "TRUNCATE alerts, genome_embeddings, isolates RESTART IDENTITY CASCADE"
            )
        yield pool
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# IsolateRepository
# ---------------------------------------------------------------------------


async def test_isolate_repository_full_flow(pool: AsyncConnectionPool) -> None:
    repo = IsolateRepository()
    async with pool.connection() as conn:
        await repo.upsert(conn, _make_isolate())
        fetched = await repo.get(conn, "PDT001")
        assert fetched.organism == "Salmonella"
        assert fetched.status is IsolateStatus.DISCOVERED

        # update_status path.
        await repo.update_status(
            conn,
            accession="PDT001",
            status=IsolateStatus.PROTEINS_READY,
            modal_call_id="modal-abc",
        )
        after = await repo.get(conn, "PDT001")
        assert after.status is IsolateStatus.PROTEINS_READY
        assert after.modal_call_id == "modal-abc"

        # update_scores path.
        ts = datetime(2026, 2, 1, tzinfo=UTC)
        await repo.update_scores(
            conn,
            accession="PDT001",
            novelty_score=4.2,
            nn_distance=0.3,
            coverage=0.85,
            mahalanobis=2.1,
            above_threshold=True,
            scored_at=ts,
        )
        scored = await repo.get(conn, "PDT001")
        assert scored.novelty_score == pytest.approx(4.2)
        assert scored.status is IsolateStatus.SCORED


async def test_isolate_get_missing_raises(pool: AsyncConnectionPool) -> None:
    repo = IsolateRepository()
    async with pool.connection() as conn:
        with pytest.raises(IsolateNotFoundError):
            await repo.get(conn, "PDT-MISSING")


async def test_isolate_find_filters(pool: AsyncConnectionPool) -> None:
    repo = IsolateRepository()
    async with pool.connection() as conn:
        await repo.upsert(conn, _make_isolate("PDT-A", organism="Salmonella"))
        await repo.upsert(conn, _make_isolate("PDT-B", organism="Listeria"))
        salmonella = await repo.find(conn, organism="Salmonella")
    assert {r.accession for r in salmonella} == {"PDT-A"}


# ---------------------------------------------------------------------------
# EmbeddingRepository
# ---------------------------------------------------------------------------


async def test_embedding_repository_round_trip(pool: AsyncConnectionPool) -> None:
    isolates = IsolateRepository()
    embeddings = EmbeddingRepository()
    async with pool.connection() as conn:
        await isolates.upsert(conn, _make_isolate())
        await embeddings.upsert(
            conn,
            EmbeddingRow(
                accession="PDT001",
                version=1,
                model="esm-c-600m",
                model_version="1.0.0",
                embedding=_vec(0.1),
            ),
        )
        row = await embeddings.get(conn, "PDT001")
    assert len(row.embedding) == 1152
    assert row.model_version == "1.0.0"


async def test_embedding_get_missing_raises(pool: AsyncConnectionPool) -> None:
    embeddings = EmbeddingRepository()
    async with pool.connection() as conn:
        with pytest.raises(EmbeddingNotFoundError):
            await embeddings.get(conn, "PDT-MISSING")


async def test_embedding_k_nearest_uses_hnsw(pool: AsyncConnectionPool) -> None:
    isolates = IsolateRepository()
    embeddings = EmbeddingRepository()
    async with pool.connection() as conn:
        for acc, seed in [("SEED", 0.10), ("CLOSE", 0.10001), ("FAR", 0.90)]:
            await isolates.upsert(conn, _make_isolate(acc))
            await embeddings.upsert(
                conn,
                EmbeddingRow(
                    accession=acc,
                    version=1,
                    model="m",
                    model_version="1",
                    embedding=_vec(seed),
                ),
            )
        neighbors = await embeddings.k_nearest(conn, accession="SEED", k=2)
    assert [n.accession for n in neighbors] == ["CLOSE", "FAR"]
    assert neighbors[0].distance < neighbors[1].distance


# ---------------------------------------------------------------------------
# AlertRepository
# ---------------------------------------------------------------------------


async def test_alert_repository_full_flow(pool: AsyncConnectionPool) -> None:
    isolates = IsolateRepository()
    alerts = AlertRepository()
    async with pool.connection() as conn:
        await isolates.upsert(conn, _make_isolate())
        created = await alerts.create(conn, accession="PDT001", version=1, score=4.2, notes="auto")
        assert created.id > 0
        assert created.status is AlertStatus.OPEN

        opens = await alerts.list_open(conn)
        assert any(a.id == created.id for a in opens)

        closed = await alerts.update_status(
            conn, alert_id=created.id, status=AlertStatus.CLOSED, notes="reviewed"
        )
        assert closed.status is AlertStatus.CLOSED
        assert closed.notes == "reviewed"


async def test_alert_update_missing_raises(pool: AsyncConnectionPool) -> None:
    alerts = AlertRepository()
    async with pool.connection() as conn:
        with pytest.raises(AlertNotFoundError):
            await alerts.update_status(conn, alert_id=99999, status=AlertStatus.CLOSED)


# ---------------------------------------------------------------------------
# Transaction composition
# ---------------------------------------------------------------------------


async def test_transaction_rolls_back_on_exception(pool: AsyncConnectionPool) -> None:
    isolates = IsolateRepository()
    with pytest.raises(RuntimeError):
        async with pool.connection() as conn, conn.transaction():
            await isolates.upsert(conn, _make_isolate("PDT-TX"))
            raise RuntimeError("boom")

    async with pool.connection() as conn:
        with pytest.raises(IsolateNotFoundError):
            await isolates.get(conn, "PDT-TX")
