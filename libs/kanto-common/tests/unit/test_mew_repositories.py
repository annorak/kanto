"""Unit tests for Mew repositories using a mocked AsyncConnection.

These tests don't require Docker — they verify:

* Methods produce parameterized SQL (no f-string interpolation of values).
* Parameters land in the right slots.
* Row-mapping turns dict_row results into the typed pydantic models.
* Error paths raise the right exception types.

Integration tests against a real Postgres + pgvector container live
in :mod:`tests.integration.test_mew_repositories_integration` and
require Docker to run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from kanto_common.mew import (
    AlertRepository,
    AlertRow,
    AlertStatus,
    EmbeddingRepository,
    EmbeddingRow,
    IsolateRepository,
    IsolateRow,
    IsolateStatus,
    NeighborRow,
)
from kanto_common.mew.repositories import (
    AlertNotFoundError,
    EmbeddingNotFoundError,
    IsolateNotFoundError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_conn(
    fetchone: dict[str, Any] | None = None,
    fetchall: list[dict[str, Any]] | None = None,
) -> tuple[MagicMock, AsyncMock]:
    """Return ``(conn, execute_mock)``.

    The cursor's ``__aenter__`` returns a cursor whose ``fetchone`` /
    ``fetchall`` are pre-stubbed. ``conn.execute`` is a separate
    AsyncMock — we return it so tests can assert call arguments.
    """
    cursor = MagicMock()
    cursor.execute = AsyncMock()
    cursor.fetchone = AsyncMock(return_value=fetchone)
    cursor.fetchall = AsyncMock(return_value=fetchall or [])

    cursor_ctx = MagicMock()
    cursor_ctx.__aenter__ = AsyncMock(return_value=cursor)
    cursor_ctx.__aexit__ = AsyncMock(return_value=None)

    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.cursor = MagicMock(return_value=cursor_ctx)
    return conn, conn.execute


def _make_isolate_row(**overrides: Any) -> IsolateRow:
    fields: dict[str, Any] = dict(
        accession="PDT001",
        version=1,
        organism="Salmonella",
        source="ncbi-pd",
        status=IsolateStatus.DISCOVERED,
        discovered_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    fields.update(overrides)
    return IsolateRow(**fields)


# ---------------------------------------------------------------------------
# IsolateRepository
# ---------------------------------------------------------------------------


async def test_isolate_upsert_passes_all_columns() -> None:
    conn, execute = _mock_conn()
    repo = IsolateRepository()
    row = _make_isolate_row()
    await repo.upsert(conn, row)
    execute.assert_called()
    sql, params = execute.call_args.args
    # Critical invariants: parameterized, not interpolated.
    assert "%(accession)s" in sql
    assert "INSERT INTO isolates" in sql
    assert "ON CONFLICT (accession)" in sql
    assert params["accession"] == "PDT001"
    assert params["status"] == "DISCOVERED"


async def test_isolate_update_status_only_status_required() -> None:
    conn, execute = _mock_conn()
    repo = IsolateRepository()
    await repo.update_status(
        conn, accession="PDT001", status=IsolateStatus.PROTEINS_READY
    )
    sql, params = execute.call_args.args
    assert "UPDATE isolates" in sql
    assert params["status"] == "PROTEINS_READY"
    assert params["modal_call_id"] is None


async def test_isolate_update_scores_writes_scored_status() -> None:
    conn, execute = _mock_conn()
    repo = IsolateRepository()
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    await repo.update_scores(
        conn,
        accession="PDT001",
        novelty_score=4.2,
        nn_distance=0.3,
        coverage=0.8,
        mahalanobis=2.1,
        above_threshold=True,
        scored_at=ts,
    )
    _sql, params = execute.call_args.args
    assert params["status"] == "SCORED"
    assert params["scored_at"] == ts
    assert params["above_threshold"] is True


async def test_isolate_get_returns_typed_row() -> None:
    conn, _ = _mock_conn(
        fetchone={
            "accession": "PDT001",
            "version": 1,
            "organism": "Salmonella",
            "source": "ncbi-pd",
            "collection_date": None,
            "location": None,
            "source_type": None,
            "status": "DISCOVERED",
            "qc_failure_reason": None,
            "modal_call_id": None,
            "novelty_score": None,
            "nn_distance": None,
            "coverage": None,
            "mahalanobis": None,
            "above_threshold": None,
            "discovered_at": None,
            "scored_at": None,
            "raw_metadata": None,
        }
    )
    repo = IsolateRepository()
    row = await repo.get(conn, "PDT001")
    assert isinstance(row, IsolateRow)
    assert row.status is IsolateStatus.DISCOVERED


async def test_isolate_get_missing_raises_not_found() -> None:
    conn, _ = _mock_conn(fetchone=None)
    repo = IsolateRepository()
    with pytest.raises(IsolateNotFoundError):
        await repo.get(conn, "PDT-MISSING")


async def test_isolate_find_assembles_filters() -> None:
    conn, _ = _mock_conn(fetchall=[])
    repo = IsolateRepository()
    await repo.find(
        conn,
        organism="Salmonella",
        status=IsolateStatus.SCORED,
        min_score=2.0,
        max_score=5.0,
        discovered_after=datetime(2026, 1, 1, tzinfo=UTC),
        limit=50,
    )
    cursor = conn.cursor.return_value.__aenter__.return_value
    sql = cursor.execute.call_args.args[0]
    params = cursor.execute.call_args.args[1]
    assert "organism = %(organism)s" in sql
    assert "status = %(status)s" in sql
    assert "novelty_score >= %(min_score)s" in sql
    assert "novelty_score <= %(max_score)s" in sql
    assert "discovered_at >= %(discovered_after)s" in sql
    assert params["organism"] == "Salmonella"
    assert params["limit"] == 50


async def test_isolate_find_no_filters_emits_no_where() -> None:
    conn, _ = _mock_conn(fetchall=[])
    repo = IsolateRepository()
    await repo.find(conn)
    cursor = conn.cursor.return_value.__aenter__.return_value
    sql = cursor.execute.call_args.args[0]
    assert "WHERE" not in sql


# ---------------------------------------------------------------------------
# EmbeddingRepository
# ---------------------------------------------------------------------------


async def test_embedding_upsert_params() -> None:
    conn, execute = _mock_conn()
    repo = EmbeddingRepository()
    await repo.upsert(
        conn,
        EmbeddingRow(
            accession="PDT001",
            version=1,
            model="esm-c-600m",
            model_version="1.0.0",
            embedding=[0.1] * 1152,
        ),
    )
    sql, params = execute.call_args.args
    assert "INSERT INTO genome_embeddings" in sql
    assert "ON CONFLICT (accession)" in sql
    assert params["embedding"] == [0.1] * 1152


async def test_embedding_get_missing_raises() -> None:
    conn, _ = _mock_conn(fetchone=None)
    repo = EmbeddingRepository()
    with pytest.raises(EmbeddingNotFoundError):
        await repo.get(conn, "PDT-MISSING")


async def test_embedding_get_serializes_pgvector_array() -> None:
    conn, _ = _mock_conn(
        fetchone={
            "accession": "PDT001",
            "version": 1,
            "model": "esm-c-600m",
            "model_version": "1.0.0",
            "embedding": [0.1, 0.2, 0.3],  # mock returns list directly
        }
    )
    repo = EmbeddingRepository()
    row = await repo.get(conn, "PDT001")
    assert row.embedding == [0.1, 0.2, 0.3]


async def test_embedding_k_nearest_uses_pgvector_distance_op() -> None:
    conn, _ = _mock_conn(
        fetchall=[
            {"accession": "PDT002", "distance": 0.1},
            {"accession": "PDT003", "distance": 0.2},
        ]
    )
    repo = EmbeddingRepository()
    rows = await repo.k_nearest(conn, accession="PDT001", k=2)
    cursor = conn.cursor.return_value.__aenter__.return_value
    sql = cursor.execute.call_args.args[0]
    assert "<=>" in sql
    assert rows == [
        NeighborRow(accession="PDT002", distance=0.1),
        NeighborRow(accession="PDT003", distance=0.2),
    ]


# ---------------------------------------------------------------------------
# AlertRepository
# ---------------------------------------------------------------------------


async def test_alert_create_returns_alert_row() -> None:
    conn, _ = _mock_conn(
        fetchone={
            "id": 7,
            "accession": "PDT001",
            "version": 1,
            "score": 4.2,
            "triggered_at": datetime(2026, 1, 1, tzinfo=UTC),
            "status": "OPEN",
            "notes": None,
        }
    )
    repo = AlertRepository()
    alert = await repo.create(conn, accession="PDT001", version=1, score=4.2)
    assert isinstance(alert, AlertRow)
    assert alert.id == 7
    assert alert.status is AlertStatus.OPEN


async def test_alert_update_status_returns_updated_row() -> None:
    conn, _ = _mock_conn(
        fetchone={
            "id": 7,
            "accession": "PDT001",
            "version": 1,
            "score": 4.2,
            "triggered_at": datetime(2026, 1, 1, tzinfo=UTC),
            "status": "ACKNOWLEDGED",
            "notes": "ack",
        }
    )
    repo = AlertRepository()
    updated = await repo.update_status(
        conn, alert_id=7, status=AlertStatus.ACKNOWLEDGED, notes="ack"
    )
    assert updated.status is AlertStatus.ACKNOWLEDGED


async def test_alert_update_status_missing_raises() -> None:
    conn, _ = _mock_conn(fetchone=None)
    repo = AlertRepository()
    with pytest.raises(AlertNotFoundError):
        await repo.update_status(conn, alert_id=999, status=AlertStatus.CLOSED)


async def test_alert_list_open_filters_by_status() -> None:
    conn, _ = _mock_conn(fetchall=[])
    repo = AlertRepository()
    await repo.list_open(conn, limit=25)
    cursor = conn.cursor.return_value.__aenter__.return_value
    sql, params = cursor.execute.call_args.args
    assert "status = %(status)s" in sql
    assert params["status"] == "OPEN"
    assert params["limit"] == 25
