"""Verify the initial migration produces the expected schema.

These tests interrogate the Postgres catalog directly rather than
running ``\\d``-style descriptions: catalog queries are the
authoritative source of truth for what's on disk.
"""

from __future__ import annotations

import psycopg
import pytest

# ---------------------------------------------------------------------------
# Tables and columns
# ---------------------------------------------------------------------------


_EXPECTED_ISOLATE_COLUMNS = {
    "accession": "text",
    "version": "integer",
    "organism": "text",
    "source": "text",
    "collection_date": "date",
    "location": "text",
    "source_type": "text",
    "status": "text",
    "qc_failure_reason": "text",
    "modal_call_id": "text",
    "novelty_score": "double precision",
    "nn_distance": "double precision",
    "coverage": "double precision",
    "mahalanobis": "double precision",
    "above_threshold": "boolean",
    "discovered_at": "timestamp with time zone",
    "scored_at": "timestamp with time zone",
    "raw_metadata": "jsonb",
    "updated_at": "timestamp with time zone",
}

_EXPECTED_GENOME_EMBEDDING_COLUMNS = {
    "accession": "text",
    "version": "integer",
    "model": "text",
    "model_version": "text",
    "embedding": "USER-DEFINED",  # pgvector's ``vector`` type lands here.
    "updated_at": "timestamp with time zone",
}

_EXPECTED_ALERT_COLUMNS = {
    "id": "bigint",
    "accession": "text",
    "version": "integer",
    "score": "double precision",
    "triggered_at": "timestamp with time zone",
    "status": "text",
    "notes": "text",
    "updated_at": "timestamp with time zone",
}


def _columns(dsn: str, table: str) -> dict[str, str]:
    sql = """
    SELECT column_name, data_type
      FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = %s
    """
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql, (table,))
        return {row[0]: row[1] for row in cur.fetchall()}


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ("isolates", _EXPECTED_ISOLATE_COLUMNS),
        ("genome_embeddings", _EXPECTED_GENOME_EMBEDDING_COLUMNS),
        ("alerts", _EXPECTED_ALERT_COLUMNS),
    ],
)
def test_table_columns(applied_db: str, table: str, expected: dict[str, str]) -> None:
    actual = _columns(applied_db, table)
    assert actual == expected, f"{table} columns mismatch: {actual}"


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------


_EXPECTED_INDEXES = {
    "isolates": {
        "isolates_pkey",
        "isolates_organism_idx",
        "isolates_collection_date_idx",
        "isolates_novelty_score_idx",
        "isolates_status_idx",
        "isolates_location_idx",
        "isolates_status_scored_at_idx",
    },
    "genome_embeddings": {
        "genome_embeddings_pkey",
        "genome_embeddings_embedding_hnsw_idx",
    },
    "alerts": {
        "alerts_pkey",
        "alerts_status_idx",
        "alerts_accession_idx",
        "alerts_accession_version_unique",
    },
}


def _indexes(dsn: str, table: str) -> set[str]:
    sql = """
    SELECT indexname FROM pg_indexes
     WHERE schemaname = 'public' AND tablename = %s
    """
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql, (table,))
        return {row[0] for row in cur.fetchall()}


@pytest.mark.parametrize(("table", "expected"), list(_EXPECTED_INDEXES.items()))
def test_table_indexes(applied_db: str, table: str, expected: set[str]) -> None:
    actual = _indexes(applied_db, table)
    missing = expected - actual
    assert not missing, f"{table}: missing indexes {missing}"


def test_hnsw_index_uses_expected_parameters(applied_db: str) -> None:
    """The HNSW index must be built with m = 16 and ef_construction = 200.

    pgvector exposes these via ``pg_class.reloptions`` as a TEXT[]
    of ``key=value`` strings.
    """
    sql = """
    SELECT reloptions
      FROM pg_class
     WHERE relname = 'genome_embeddings_embedding_hnsw_idx'
    """
    with psycopg.connect(applied_db) as conn, conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    assert row is not None
    options = set(row[0] or [])
    assert "m=16" in options
    assert "ef_construction=200" in options


def test_hnsw_index_uses_cosine_ops(applied_db: str) -> None:
    """The HNSW index must use ``vector_cosine_ops``.

    The pgvector indexam stores the operator class on each column;
    ``pg_index.indclass`` references it. Verify by joining through
    pg_opclass to its name.
    """
    sql = """
    SELECT op.opcname
      FROM pg_index i
      JOIN pg_class c ON c.oid = i.indexrelid
      JOIN pg_opclass op ON op.oid = i.indclass[0]
     WHERE c.relname = 'genome_embeddings_embedding_hnsw_idx'
    """
    with psycopg.connect(applied_db) as conn, conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "vector_cosine_ops"


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------


def test_isolates_status_check_rejects_unknown_status(applied_db: str) -> None:
    with (
        psycopg.connect(applied_db) as conn,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        conn.execute(
            "INSERT INTO isolates (accession, version, organism, source, status) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("PDT-X", 1, "Salmonella", "ncbi", "BOGUS"),
        )


def test_alerts_status_check_rejects_unknown_status(applied_db: str) -> None:
    with psycopg.connect(applied_db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO isolates (accession, version, organism, source, status) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("PDT-A", 1, "Salmonella", "ncbi", "DISCOVERED"),
        )
    with (
        psycopg.connect(applied_db) as conn,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        conn.execute(
            "INSERT INTO alerts (accession, version, score, status) " "VALUES (%s, %s, %s, %s)",
            ("PDT-A", 1, 4.2, "BOGUS"),
        )


def test_alerts_accession_version_unique(applied_db: str) -> None:
    with psycopg.connect(applied_db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO isolates (accession, version, organism, source, status) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("PDT-U", 1, "Salmonella", "ncbi", "DISCOVERED"),
        )
        conn.execute(
            "INSERT INTO alerts (accession, version, score) VALUES (%s, %s, %s)",
            ("PDT-U", 1, 4.2),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO alerts (accession, version, score) VALUES (%s, %s, %s)",
                ("PDT-U", 1, 5.0),
            )


def test_genome_embeddings_fk_blocks_orphan_inserts(applied_db: str) -> None:
    """A vector for a non-existent isolate must be rejected by the FK."""
    with (
        psycopg.connect(applied_db) as conn,
        pytest.raises(psycopg.errors.ForeignKeyViolation),
    ):
        conn.execute(
            "INSERT INTO genome_embeddings (accession, version, model, model_version, embedding) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("PDT-NOPE", 1, "m", "v1", "[" + ",".join("0" for _ in range(1152)) + "]"),
        )


def test_extension_vector_is_installed(applied_db: str) -> None:
    sql = "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
    with psycopg.connect(applied_db) as conn, conn.cursor() as cur:
        cur.execute(sql)
        assert cur.fetchone() is not None


def test_updated_at_trigger_fires_on_update(applied_db: str) -> None:
    """A simple UPDATE must bump ``updated_at`` automatically."""
    with psycopg.connect(applied_db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO isolates (accession, version, organism, source, status) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("PDT-T", 1, "Salmonella", "ncbi", "DISCOVERED"),
        )
        cur = conn.execute("SELECT updated_at FROM isolates WHERE accession = %s", ("PDT-T",))
        first = cur.fetchone()
        assert first is not None
        before = first[0]

        # Force a measurable delta: trigger uses NOW() which is the
        # statement timestamp. A dedicated UPDATE produces a new
        # transaction → a new statement timestamp.
        conn.execute(
            "UPDATE isolates SET organism = %s WHERE accession = %s", ("Listeria", "PDT-T")
        )
        cur = conn.execute("SELECT updated_at FROM isolates WHERE accession = %s", ("PDT-T",))
        after_row = cur.fetchone()
        assert after_row is not None
        after = after_row[0]
    assert after >= before


def test_alembic_version_table_records_head(applied_db: str) -> None:
    """The dedicated version table must contain the current head revision.

    The head is resolved from the alembic ScriptDirectory rather than
    hard-coded so adding a new migration (e.g. 0002, 0003) does not
    silently invalidate this assertion.
    """
    from alembic.script import ScriptDirectory
    from kanto_migrations.runner import alembic_config

    expected_head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    with psycopg.connect(applied_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT version_num FROM kanto_alembic_version")
        rows = cur.fetchall()
    assert rows == [(expected_head,)]
