"""Verify the discovery_cursors migration produces the expected schema.

Mirrors the catalog-query style of ``test_initial_schema.py``: we hit
``information_schema`` and ``pg_catalog`` directly because that's the
authoritative description of what's on disk after a migration applies.
"""

from __future__ import annotations

import psycopg
import pytest

_EXPECTED_COLUMNS = {
    "source": "text",
    "organism": "text",
    "last_snapshot_id": "text",
    "last_polled_at": "timestamp with time zone",
    "last_succeeded_at": "timestamp with time zone",
    "last_emitted_count": "integer",
    "updated_at": "timestamp with time zone",
}


pytestmark = pytest.mark.integration


def test_discovery_cursors_table_exists(applied_db: str) -> None:
    with psycopg.connect(applied_db) as conn:
        cur = conn.execute("SELECT to_regclass('public.discovery_cursors') IS NOT NULL")
        row = cur.fetchone()
    assert row is not None
    assert row[0] is True


def test_discovery_cursors_columns_match(applied_db: str) -> None:
    with psycopg.connect(applied_db) as conn:
        cur = conn.execute(
            """
            SELECT column_name, data_type
              FROM information_schema.columns
             WHERE table_name = 'discovery_cursors'
            """
        )
        rows = dict(cur.fetchall())
    assert rows == _EXPECTED_COLUMNS


def test_discovery_cursors_primary_key_is_composite(applied_db: str) -> None:
    with psycopg.connect(applied_db) as conn:
        cur = conn.execute(
            """
            SELECT a.attname
              FROM pg_index i
              JOIN pg_attribute a
                ON a.attrelid = i.indrelid
               AND a.attnum = ANY(i.indkey)
             WHERE i.indrelid = 'discovery_cursors'::regclass
               AND i.indisprimary
             ORDER BY a.attnum
            """
        )
        pk_cols = [row[0] for row in cur.fetchall()]
    assert pk_cols == ["source", "organism"]


def test_discovery_cursors_source_index_exists(applied_db: str) -> None:
    with psycopg.connect(applied_db) as conn:
        cur = conn.execute(
            """
            SELECT indexname
              FROM pg_indexes
             WHERE tablename = 'discovery_cursors'
            """
        )
        names = {row[0] for row in cur.fetchall()}
    assert "discovery_cursors_source_idx" in names


def test_discovery_cursors_updated_at_trigger_fires(applied_db: str) -> None:
    """The shared set_updated_at() trigger must be wired to this table."""
    with psycopg.connect(applied_db, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO discovery_cursors
                (source, organism, last_snapshot_id, last_succeeded_at)
            VALUES
                ('ncbi-pd', 'Salmonella', 'PDG000000004.355', NOW())
            """
        )
        first = conn.execute(
            "SELECT updated_at FROM discovery_cursors "
            "WHERE source='ncbi-pd' AND organism='Salmonella'"
        ).fetchone()
        # NOW() granularity is microseconds; pg_sleep to guarantee a tick.
        conn.execute("SELECT pg_sleep(0.05)")
        conn.execute(
            "UPDATE discovery_cursors SET last_snapshot_id = 'PDG000000004.356' "
            "WHERE source='ncbi-pd' AND organism='Salmonella'"
        )
        second = conn.execute(
            "SELECT updated_at FROM discovery_cursors "
            "WHERE source='ncbi-pd' AND organism='Salmonella'"
        ).fetchone()
    assert first is not None and second is not None
    assert second[0] > first[0]


def test_downgrade_drops_table(clean_db: str) -> None:
    """Apply head, downgrade one, confirm the table is gone, then up again."""
    from kanto_migrations import runner

    runner.upgrade(dsn=clean_db, revision="head")
    runner.downgrade(dsn=clean_db, revision="0001")

    with psycopg.connect(clean_db) as conn:
        row = conn.execute("SELECT to_regclass('public.discovery_cursors')").fetchone()
    assert row is not None
    assert row[0] is None

    # Re-applying should bring the table back so a partial rollback isn't
    # a one-way door.
    runner.upgrade(dsn=clean_db, revision="head")
    with psycopg.connect(clean_db) as conn:
        row = conn.execute("SELECT to_regclass('public.discovery_cursors')").fetchone()
    assert row is not None
    assert row[0] == "discovery_cursors"
