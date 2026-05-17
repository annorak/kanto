"""Verify the species_centroids migration produces the expected schema.

Mirrors the catalog-query style of ``test_initial_schema.py`` and
``test_discovery_cursors_schema.py``.
"""

from __future__ import annotations

import psycopg
import pytest
from kanto_migrations import runner

_EXPECTED_COLUMNS = {
    "organism": "text",
    "model": "text",
    "model_version": "text",
    "n_isolates": "integer",
    "centroid": "USER-DEFINED",
    "covariance_diagonal": "USER-DEFINED",
    "regularization": "double precision",
    "computed_at": "timestamp with time zone",
    "updated_at": "timestamp with time zone",
}


pytestmark = pytest.mark.integration


def test_table_exists(applied_db: str) -> None:
    with psycopg.connect(applied_db) as conn:
        cur = conn.execute("SELECT to_regclass('public.species_centroids') IS NOT NULL")
        row = cur.fetchone()
    assert row is not None
    assert row[0] is True


def test_columns_match(applied_db: str) -> None:
    with psycopg.connect(applied_db) as conn:
        cur = conn.execute(
            """
            SELECT column_name, data_type
              FROM information_schema.columns
             WHERE table_name = 'species_centroids'
            """
        )
        rows = dict(cur.fetchall())
    assert rows == _EXPECTED_COLUMNS


def test_primary_key_is_organism(applied_db: str) -> None:
    with psycopg.connect(applied_db) as conn:
        cur = conn.execute(
            """
            SELECT a.attname
              FROM pg_index i
              JOIN pg_attribute a
                ON a.attrelid = i.indrelid
               AND a.attnum = ANY(i.indkey)
             WHERE i.indrelid = 'species_centroids'::regclass
               AND i.indisprimary
            """
        )
        pk_cols = [row[0] for row in cur.fetchall()]
    assert pk_cols == ["organism"]


def test_model_filter_index_exists(applied_db: str) -> None:
    with psycopg.connect(applied_db) as conn:
        cur = conn.execute(
            """
            SELECT indexname
              FROM pg_indexes
             WHERE tablename = 'species_centroids'
            """
        )
        names = {row[0] for row in cur.fetchall()}
    assert "species_centroids_model_idx" in names


def test_check_constraint_rejects_zero_isolates(applied_db: str) -> None:
    """The n_isolates > 0 CHECK must surface as a write-time error."""
    sql = """
    INSERT INTO species_centroids
        (organism, model, model_version, n_isolates,
         centroid, covariance_diagonal, regularization, computed_at)
    VALUES (%s, %s, %s, %s, %s::vector, %s::vector, %s, NOW())
    """
    centroid = "[" + ",".join(["0.0"] * 1152) + "]"
    variances = "[" + ",".join(["1.0"] * 1152) + "]"
    with (
        psycopg.connect(applied_db, autocommit=True) as conn,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        conn.execute(
            sql,
            ("X", "esm-c-600m", "1.0.0", 0, centroid, variances, 1e-4),
        )


def test_check_constraint_rejects_zero_regularization(applied_db: str) -> None:
    sql = """
    INSERT INTO species_centroids
        (organism, model, model_version, n_isolates,
         centroid, covariance_diagonal, regularization, computed_at)
    VALUES (%s, %s, %s, %s, %s::vector, %s::vector, %s, NOW())
    """
    centroid = "[" + ",".join(["0.0"] * 1152) + "]"
    variances = "[" + ",".join(["1.0"] * 1152) + "]"
    with (
        psycopg.connect(applied_db, autocommit=True) as conn,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        conn.execute(
            sql,
            ("X", "esm-c-600m", "1.0.0", 5, centroid, variances, 0.0),
        )


def test_updated_at_trigger_fires(applied_db: str) -> None:
    centroid = "[" + ",".join(["0.0"] * 1152) + "]"
    variances = "[" + ",".join(["1.0"] * 1152) + "]"
    with psycopg.connect(applied_db, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO species_centroids
                (organism, model, model_version, n_isolates,
                 centroid, covariance_diagonal, regularization, computed_at)
            VALUES (%s, %s, %s, %s, %s::vector, %s::vector, %s, NOW())
            """,
            ("Salmonella", "esm-c-600m", "1.0.0", 10, centroid, variances, 1e-4),
        )
        first = conn.execute(
            "SELECT updated_at FROM species_centroids WHERE organism='Salmonella'"
        ).fetchone()
        conn.execute("SELECT pg_sleep(0.05)")
        conn.execute("UPDATE species_centroids SET n_isolates = 99 WHERE organism='Salmonella'")
        second = conn.execute(
            "SELECT updated_at FROM species_centroids WHERE organism='Salmonella'"
        ).fetchone()
    assert first is not None and second is not None
    assert second[0] > first[0]


def test_downgrade_drops_table(clean_db: str) -> None:
    runner.upgrade(dsn=clean_db, revision="head")
    runner.downgrade(dsn=clean_db, revision="0002")
    with psycopg.connect(clean_db) as conn:
        row = conn.execute("SELECT to_regclass('public.species_centroids')").fetchone()
    assert row is not None
    assert row[0] is None
    # Re-upgrade brings the table back.
    runner.upgrade(dsn=clean_db, revision="head")
    with psycopg.connect(clean_db) as conn:
        row = conn.execute("SELECT to_regclass('public.species_centroids')").fetchone()
    assert row is not None
    assert row[0] == "species_centroids"
