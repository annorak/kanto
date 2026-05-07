"""Verify ``alembic downgrade base`` reverses the schema cleanly."""

from __future__ import annotations

import psycopg
from kanto_migrations import runner


def _table_exists(dsn: str, name: str) -> bool:
    sql = """
    SELECT 1 FROM information_schema.tables
     WHERE table_schema = 'public' AND table_name = %s
    """
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql, (name,))
        return cur.fetchone() is not None


def _function_exists(dsn: str, name: str) -> bool:
    sql = """
    SELECT 1 FROM pg_proc p
      JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public' AND p.proname = %s
    """
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql, (name,))
        return cur.fetchone() is not None


def _extension_exists(dsn: str, name: str) -> bool:
    sql = "SELECT 1 FROM pg_extension WHERE extname = %s"
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql, (name,))
        return cur.fetchone() is not None


def test_downgrade_base_removes_all_objects(clean_db: str) -> None:
    """``upgrade head`` then ``downgrade base`` returns to a clean baseline."""
    runner.upgrade(dsn=clean_db)
    assert _table_exists(clean_db, "isolates")

    runner.downgrade(dsn=clean_db, revision="base")
    assert not _table_exists(clean_db, "isolates")
    assert not _table_exists(clean_db, "genome_embeddings")
    assert not _table_exists(clean_db, "alerts")
    assert not _function_exists(clean_db, "set_updated_at")
    assert not _extension_exists(clean_db, "vector")


def test_upgrade_downgrade_upgrade_is_idempotent(clean_db: str) -> None:
    """Re-applying after a downgrade reproduces the same schema.

    The cycle catches migrations whose down step leaves residue that
    poisons the next upgrade — a classic Alembic footgun.
    """
    runner.upgrade(dsn=clean_db)
    runner.downgrade(dsn=clean_db, revision="base")
    runner.upgrade(dsn=clean_db)
    assert _table_exists(clean_db, "isolates")
    assert _table_exists(clean_db, "genome_embeddings")
    assert _table_exists(clean_db, "alerts")
    assert _function_exists(clean_db, "set_updated_at")
    assert _extension_exists(clean_db, "vector")
