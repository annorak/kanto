"""Read/write the per-source-organism cursor stored in Mew.

The cursor table is :sql:`discovery_cursors` (migration ``0002``).
There is one row per ``(source, organism)`` and the cursor advances
only after the corresponding poll cycle has emitted its events
successfully. If the cycle fails midway, the cursor stays where it
was and the next cycle re-emits the same isolates — downstream
consumers dedupe on ``(accession, version)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row


@dataclass(frozen=True, slots=True)
class DiscoveryCursor:
    """One row of the ``discovery_cursors`` table."""

    source: str
    organism: str
    last_snapshot_id: str | None
    last_polled_at: datetime | None
    last_succeeded_at: datetime | None
    last_emitted_count: int


class DiscoveryCursorRepository:
    """CRUD over the cursor table.

    Methods take an explicit :class:`psycopg.AsyncConnection` so the
    pipeline can compose multiple cursor updates into a single
    transaction when needed (it currently doesn't, but the door is
    open).
    """

    async def get(
        self,
        conn: psycopg.AsyncConnection[Any],
        *,
        source: str,
        organism: str,
    ) -> DiscoveryCursor | None:
        sql = (
            "SELECT source, organism, last_snapshot_id, last_polled_at, "
            "last_succeeded_at, last_emitted_count "
            "FROM discovery_cursors "
            "WHERE source = %(source)s AND organism = %(organism)s"
        )
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, {"source": source, "organism": organism})
            row = await cur.fetchone()
        return None if row is None else DiscoveryCursor(**row)

    async def list_for_source(
        self,
        conn: psycopg.AsyncConnection[Any],
        *,
        source: str,
    ) -> list[DiscoveryCursor]:
        sql = (
            "SELECT source, organism, last_snapshot_id, last_polled_at, "
            "last_succeeded_at, last_emitted_count "
            "FROM discovery_cursors WHERE source = %(source)s "
            "ORDER BY organism"
        )
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, {"source": source})
            rows = await cur.fetchall()
        return [DiscoveryCursor(**r) for r in rows]

    async def record_attempt(
        self,
        conn: psycopg.AsyncConnection[Any],
        *,
        source: str,
        organism: str,
        polled_at: datetime,
    ) -> None:
        """Bump ``last_polled_at`` (not ``last_succeeded_at``).

        Called at the start of each cycle for diagnostics so oncall
        can tell "we're trying" from "we stopped polling entirely".
        """
        sql = """
        INSERT INTO discovery_cursors (source, organism, last_polled_at)
        VALUES (%(source)s, %(organism)s, %(polled_at)s)
        ON CONFLICT (source, organism) DO UPDATE
            SET last_polled_at = EXCLUDED.last_polled_at
        """
        await conn.execute(
            sql,
            {"source": source, "organism": organism, "polled_at": polled_at},
        )

    async def commit_success(
        self,
        conn: psycopg.AsyncConnection[Any],
        *,
        source: str,
        organism: str,
        snapshot_id: str,
        succeeded_at: datetime,
        emitted_count: int,
    ) -> None:
        """Advance the cursor for a fully-emitted cycle.

        Must be called after **all** :class:`IsolateDiscovered` events
        have been awaited by the streaming producer. If the pipeline
        crashes before this point, the cursor stays put and the next
        cycle re-emits.
        """
        sql = """
        INSERT INTO discovery_cursors
            (source, organism, last_snapshot_id, last_polled_at,
             last_succeeded_at, last_emitted_count)
        VALUES
            (%(source)s, %(organism)s, %(snapshot_id)s,
             %(succeeded_at)s, %(succeeded_at)s, %(emitted_count)s)
        ON CONFLICT (source, organism) DO UPDATE
            SET last_snapshot_id   = EXCLUDED.last_snapshot_id,
                last_polled_at     = EXCLUDED.last_polled_at,
                last_succeeded_at  = EXCLUDED.last_succeeded_at,
                last_emitted_count = EXCLUDED.last_emitted_count
        """
        await conn.execute(
            sql,
            {
                "source": source,
                "organism": organism,
                "snapshot_id": snapshot_id,
                "succeeded_at": succeeded_at,
                "emitted_count": emitted_count,
            },
        )

    async def reset(
        self,
        conn: psycopg.AsyncConnection[Any],
        *,
        source: str,
        organism: str,
    ) -> None:
        """Delete the cursor — operator escape hatch.

        Called from the ``growlithe reset-cursor`` runbook step.
        Resetting a cursor causes the next poll to re-emit every
        isolate the source currently lists; downstream consumers
        will dedupe but cost will spike.
        """
        await conn.execute(
            "DELETE FROM discovery_cursors "
            "WHERE source = %(source)s AND organism = %(organism)s",
            {"source": source, "organism": organism},
        )


__all__ = ["DiscoveryCursor", "DiscoveryCursorRepository"]
