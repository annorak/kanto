"""Integration tests against a real Postgres + pgvector container.

Marked ``integration`` so the default unit-test run skips them.
Run them explicitly with::

    uv run pytest -m integration

These tests require Docker. They auto-skip if the docker daemon
isn't reachable so contributors without Docker can still run the
non-integration suite.
"""

from __future__ import annotations

import socket
from datetime import UTC, datetime

import pytest

# Skip the entire module if testcontainers/docker isn't available.
docker = pytest.importorskip("docker")

try:
    _client = docker.from_env()
    _client.ping()
except Exception as _exc:
    pytest.skip(
        f"Docker daemon not available; skipping Mew integration tests: {_exc}",
        allow_module_level=True,
    )


from kanto_commons.mew import (  # noqa: E402
    AlertRepository,
    AlertStatus,
    EmbeddingRepository,
    EmbeddingRow,
    IsolateRepository,
    IsolateRow,
    IsolateStatus,
)
from kanto_commons.mew.repositories import (  # noqa: E402
    AlertNotFoundError,
    EmbeddingNotFoundError,
    IsolateNotFoundError,
)
from psycopg_pool import AsyncConnectionPool  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture
def isolate_repo() -> IsolateRepository:
    return IsolateRepository()


@pytest.fixture
def embedding_repo() -> EmbeddingRepository:
    return EmbeddingRepository()


@pytest.fixture
def alert_repo() -> AlertRepository:
    return AlertRepository()


def _make_isolate(accession: str = "PDT001", **overrides: object) -> IsolateRow:
    fields: dict[str, object] = dict(
        accession=accession,
        version=1,
        organism="Salmonella",
        source="ncbi-pd",
        status=IsolateStatus.DISCOVERED,
        discovered_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    fields.update(overrides)
    return IsolateRow(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# IsolateRepository
# ---------------------------------------------------------------------------


async def test_isolate_upsert_then_get_round_trip(
    mew_pool: AsyncConnectionPool,
    isolate_repo: IsolateRepository,
) -> None:
    async with mew_pool.connection() as conn:
        await isolate_repo.upsert(conn, _make_isolate())
        fetched = await isolate_repo.get(conn, "PDT001")
    assert fetched.organism == "Salmonella"
    assert fetched.status is IsolateStatus.DISCOVERED


async def test_isolate_upsert_is_idempotent(
    mew_pool: AsyncConnectionPool,
    isolate_repo: IsolateRepository,
) -> None:
    async with mew_pool.connection() as conn:
        await isolate_repo.upsert(conn, _make_isolate(version=1))
        await isolate_repo.upsert(conn, _make_isolate(version=2))
        row = await isolate_repo.get(conn, "PDT001")
    assert row.version == 2


async def test_isolate_update_status(
    mew_pool: AsyncConnectionPool,
    isolate_repo: IsolateRepository,
) -> None:
    async with mew_pool.connection() as conn:
        await isolate_repo.upsert(conn, _make_isolate())
        await isolate_repo.update_status(
            conn,
            accession="PDT001",
            status=IsolateStatus.PROTEINS_READY,
            modal_call_id="modal-abc",
        )
        row = await isolate_repo.get(conn, "PDT001")
    assert row.status is IsolateStatus.PROTEINS_READY
    assert row.modal_call_id == "modal-abc"


async def test_isolate_update_scores(
    mew_pool: AsyncConnectionPool,
    isolate_repo: IsolateRepository,
) -> None:
    ts = datetime(2026, 1, 2, tzinfo=UTC)
    async with mew_pool.connection() as conn:
        await isolate_repo.upsert(conn, _make_isolate())
        await isolate_repo.update_scores(
            conn,
            accession="PDT001",
            novelty_score=4.2,
            nn_distance=0.3,
            coverage=0.85,
            mahalanobis=2.1,
            above_threshold=True,
            scored_at=ts,
        )
        row = await isolate_repo.get(conn, "PDT001")
    assert row.novelty_score == pytest.approx(4.2)
    assert row.status is IsolateStatus.SCORED
    assert row.scored_at == ts


async def test_isolate_get_missing(
    mew_pool: AsyncConnectionPool,
    isolate_repo: IsolateRepository,
) -> None:
    async with mew_pool.connection() as conn:
        with pytest.raises(IsolateNotFoundError):
            await isolate_repo.get(conn, "PDT-NOPE")


async def test_isolate_find_filters(
    mew_pool: AsyncConnectionPool,
    isolate_repo: IsolateRepository,
) -> None:
    async with mew_pool.connection() as conn:
        await isolate_repo.upsert(conn, _make_isolate("PDT-A", organism="Salmonella"))
        await isolate_repo.upsert(conn, _make_isolate("PDT-B", organism="Listeria"))
        salmonella = await isolate_repo.find(conn, organism="Salmonella")
    assert {r.accession for r in salmonella} == {"PDT-A"}


# ---------------------------------------------------------------------------
# EmbeddingRepository
# ---------------------------------------------------------------------------


def _vec(seed: float, dim: int = 1152) -> list[float]:
    return [seed + i * 1e-4 for i in range(dim)]


async def test_embedding_upsert_then_get(
    mew_pool: AsyncConnectionPool,
    isolate_repo: IsolateRepository,
    embedding_repo: EmbeddingRepository,
) -> None:
    async with mew_pool.connection() as conn:
        await isolate_repo.upsert(conn, _make_isolate())
        await embedding_repo.upsert(
            conn,
            EmbeddingRow(
                accession="PDT001",
                version=1,
                model="esm-c-600m",
                model_version="1.0.0",
                embedding=_vec(0.1),
            ),
        )
        row = await embedding_repo.get(conn, "PDT001")
    assert len(row.embedding) == 1152
    assert row.model == "esm-c-600m"


async def test_embedding_upsert_is_idempotent(
    mew_pool: AsyncConnectionPool,
    isolate_repo: IsolateRepository,
    embedding_repo: EmbeddingRepository,
) -> None:
    async with mew_pool.connection() as conn:
        await isolate_repo.upsert(conn, _make_isolate())
        await embedding_repo.upsert(
            conn,
            EmbeddingRow(
                accession="PDT001",
                version=1,
                model="m",
                model_version="1.0",
                embedding=_vec(0.1),
            ),
        )
        await embedding_repo.upsert(
            conn,
            EmbeddingRow(
                accession="PDT001",
                version=2,
                model="m",
                model_version="2.0",
                embedding=_vec(0.2),
            ),
        )
        row = await embedding_repo.get(conn, "PDT001")
    assert row.version == 2
    assert row.model_version == "2.0"


async def test_embedding_get_missing(
    mew_pool: AsyncConnectionPool,
    embedding_repo: EmbeddingRepository,
) -> None:
    async with mew_pool.connection() as conn:
        with pytest.raises(EmbeddingNotFoundError):
            await embedding_repo.get(conn, "PDT-MISSING")


async def test_embedding_k_nearest_returns_ordered_neighbors(
    mew_pool: AsyncConnectionPool,
    isolate_repo: IsolateRepository,
    embedding_repo: EmbeddingRepository,
) -> None:
    async with mew_pool.connection() as conn:
        # Three isolates: seed and two others.
        for acc, seed in [("SEED", 0.1), ("CLOSE", 0.10001), ("FAR", 0.9)]:
            await isolate_repo.upsert(conn, _make_isolate(acc))
            await embedding_repo.upsert(
                conn,
                EmbeddingRow(
                    accession=acc,
                    version=1,
                    model="m",
                    model_version="1.0",
                    embedding=_vec(seed),
                ),
            )
        neighbors = await embedding_repo.k_nearest(conn, accession="SEED", k=2)
    accs = [n.accession for n in neighbors]
    assert accs == ["CLOSE", "FAR"]
    assert neighbors[0].distance < neighbors[1].distance


# ---------------------------------------------------------------------------
# AlertRepository
# ---------------------------------------------------------------------------


async def test_alert_create_and_list_open(
    mew_pool: AsyncConnectionPool,
    isolate_repo: IsolateRepository,
    alert_repo: AlertRepository,
) -> None:
    async with mew_pool.connection() as conn:
        await isolate_repo.upsert(conn, _make_isolate())
        alert = await alert_repo.create(conn, accession="PDT001", version=1, score=4.2)
        opens = await alert_repo.list_open(conn)
    assert alert.id > 0
    assert any(a.id == alert.id for a in opens)


async def test_alert_update_status(
    mew_pool: AsyncConnectionPool,
    isolate_repo: IsolateRepository,
    alert_repo: AlertRepository,
) -> None:
    async with mew_pool.connection() as conn:
        await isolate_repo.upsert(conn, _make_isolate())
        alert = await alert_repo.create(conn, accession="PDT001", version=1, score=4.2)
        updated = await alert_repo.update_status(
            conn, alert_id=alert.id, status=AlertStatus.CLOSED, notes="reviewed"
        )
    assert updated.status is AlertStatus.CLOSED
    assert updated.notes == "reviewed"


async def test_alert_update_missing(
    mew_pool: AsyncConnectionPool,
    alert_repo: AlertRepository,
) -> None:
    async with mew_pool.connection() as conn:
        with pytest.raises(AlertNotFoundError):
            await alert_repo.update_status(
                conn, alert_id=99999, status=AlertStatus.CLOSED
            )


# ---------------------------------------------------------------------------
# Transaction composition
# ---------------------------------------------------------------------------


async def test_transaction_rolls_back_on_exception(
    mew_pool: AsyncConnectionPool,
    isolate_repo: IsolateRepository,
) -> None:
    """An exception inside the transaction must roll back the whole block."""
    with pytest.raises(RuntimeError):
        async with mew_pool.connection() as conn, conn.transaction():
            await isolate_repo.upsert(conn, _make_isolate("PDT-TX"))
            raise RuntimeError("boom")

    async with mew_pool.connection() as conn:
        with pytest.raises(IsolateNotFoundError):
            await isolate_repo.get(conn, "PDT-TX")


# Sanity: socket import used to verify docker reachability before
# entering the suite.
_unused = socket
