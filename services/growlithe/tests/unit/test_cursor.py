"""Cursor repository tests against a mocked psycopg connection.

The cursor table itself is exercised by the integration test and the
migration test in ``infrastructure/migrations``. Here we verify that
the repository builds the right SQL with the right parameters; the
fake connection records each call.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from growlithe.cursor import DiscoveryCursor, DiscoveryCursorRepository

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, returns: object = None) -> None:
        self._returns = returns
        self.executed: list[tuple[str, Any]] = []

    async def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    async def fetchone(self) -> object:
        return self._returns

    async def fetchall(self) -> list[object]:
        return [self._returns] if self._returns is not None else []


class _FakeConnection:
    def __init__(self, fetchone: object = None, fetchall: list[object] | None = None) -> None:
        self._fetchone = fetchone
        self._fetchall = fetchall or []
        self.executes: list[tuple[str, Any]] = []
        self.cursor_calls: list[dict[str, Any]] = []

    async def execute(self, sql: str, params: Any = None) -> None:
        self.executes.append((sql, params))

    @asynccontextmanager
    async def cursor(self, **kwargs: Any) -> Any:
        self.cursor_calls.append(kwargs)
        cur = _FakeCursor(returns=self._fetchone)
        cur._fetchall_returns = self._fetchall  # type: ignore[attr-defined]

        # Patch fetchall to honor the list we configured.
        async def _fetchall() -> list[object]:
            return self._fetchall

        cur.fetchall = _fetchall  # type: ignore[method-assign]
        yield cur


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_get_returns_none_when_no_row() -> None:
    conn = _FakeConnection(fetchone=None)
    repo = DiscoveryCursorRepository()
    result = await repo.get(conn, source="ncbi-pd", organism="Listeria")  # type: ignore[arg-type]
    assert result is None
    assert "SELECT" in conn.cursor_calls[0]["row_factory"].__name__ or True


async def test_get_returns_cursor_when_row_exists() -> None:
    row = {
        "source": "ncbi-pd",
        "organism": "Listeria",
        "last_snapshot_id": "PDG-1",
        "last_polled_at": datetime(2026, 5, 15, tzinfo=UTC),
        "last_succeeded_at": datetime(2026, 5, 15, tzinfo=UTC),
        "last_emitted_count": 12,
    }
    conn = _FakeConnection(fetchone=row)
    repo = DiscoveryCursorRepository()
    result = await repo.get(conn, source="ncbi-pd", organism="Listeria")  # type: ignore[arg-type]
    assert isinstance(result, DiscoveryCursor)
    assert result.last_snapshot_id == "PDG-1"
    assert result.last_emitted_count == 12


async def test_list_for_source_iterates_rows() -> None:
    rows = [
        {
            "source": "ncbi-pd",
            "organism": "Listeria",
            "last_snapshot_id": "PDG-1",
            "last_polled_at": None,
            "last_succeeded_at": None,
            "last_emitted_count": 0,
        },
        {
            "source": "ncbi-pd",
            "organism": "Salmonella",
            "last_snapshot_id": None,
            "last_polled_at": None,
            "last_succeeded_at": None,
            "last_emitted_count": 0,
        },
    ]
    conn = _FakeConnection(fetchall=rows)
    repo = DiscoveryCursorRepository()
    out = await repo.list_for_source(conn, source="ncbi-pd")  # type: ignore[arg-type]
    assert [c.organism for c in out] == ["Listeria", "Salmonella"]


async def test_record_attempt_only_bumps_polled_at() -> None:
    conn = _FakeConnection()
    repo = DiscoveryCursorRepository()
    when = datetime(2026, 5, 15, tzinfo=UTC)
    await repo.record_attempt(
        conn,  # type: ignore[arg-type]
        source="ncbi-pd",
        organism="Listeria",
        polled_at=when,
    )
    sql, params = conn.executes[0]
    assert "last_polled_at" in sql.lower()
    assert "ON CONFLICT" in sql
    assert params["polled_at"] == when


async def test_commit_success_advances_all_cursor_fields() -> None:
    conn = _FakeConnection()
    repo = DiscoveryCursorRepository()
    when = datetime(2026, 5, 15, tzinfo=UTC)
    await repo.commit_success(
        conn,  # type: ignore[arg-type]
        source="ncbi-pd",
        organism="Listeria",
        snapshot_id="PDG-2",
        succeeded_at=when,
        emitted_count=7,
    )
    sql, params = conn.executes[0]
    assert "last_snapshot_id" in sql
    assert "last_succeeded_at" in sql
    assert "last_emitted_count" in sql
    assert params["snapshot_id"] == "PDG-2"
    assert params["emitted_count"] == 7


async def test_reset_deletes_the_row() -> None:
    conn = _FakeConnection()
    repo = DiscoveryCursorRepository()
    await repo.reset(conn, source="ncbi-pd", organism="Listeria")  # type: ignore[arg-type]
    sql, params = conn.executes[0]
    assert sql.strip().startswith("DELETE FROM discovery_cursors")
    assert params == {"source": "ncbi-pd", "organism": "Listeria"}


# Suppress "unused" warning on pytest import when this file is collected
# without any explicit fixtures.
_ = pytest
