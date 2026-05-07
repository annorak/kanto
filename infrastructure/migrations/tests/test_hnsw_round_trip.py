"""Insert a representative vector and verify HNSW returns it.

The schema-shape tests in ``test_initial_schema.py`` confirm the index
exists with the right parameters; this file confirms the index is
*usable* — that an INSERT lands on disk and a cosine-distance query
returns the row in the expected order.
"""

from __future__ import annotations

import psycopg
from pgvector.psycopg import register_vector


def _dim_vector(seed: float, dim: int = 1152) -> list[float]:
    """Deterministic non-zero vector. Avoiding the all-zeros vector
    matters because cosine distance is undefined at the origin."""
    return [seed + i * 1e-4 for i in range(dim)]


def test_insert_and_hnsw_search_round_trip(applied_db: str) -> None:
    with psycopg.connect(applied_db, autocommit=True) as conn:
        register_vector(conn)

        # Three isolates with embeddings clustered such that we know
        # the expected nearest-neighbour ordering.
        rows = [
            ("SEED", _dim_vector(0.10)),
            ("CLOSE", _dim_vector(0.10001)),  # near SEED
            ("FAR", _dim_vector(0.90)),  # far from SEED
        ]
        for accession, _vec in rows:
            conn.execute(
                "INSERT INTO isolates (accession, version, organism, source, status) "
                "VALUES (%s, %s, %s, %s, %s)",
                (accession, 1, "Salmonella", "ncbi", "DISCOVERED"),
            )
        for accession, vec in rows:
            conn.execute(
                "INSERT INTO genome_embeddings "
                "(accession, version, model, model_version, embedding) "
                "VALUES (%s, %s, %s, %s, %s)",
                (accession, 1, "esm-c-600m", "1.0.0", vec),
            )

        # Force the planner to choose the HNSW index. With only 3
        # rows it would otherwise prefer a sequential scan.
        conn.execute("SET LOCAL enable_seqscan = off")

        cur = conn.execute(
            """
            SELECT g.accession, g.embedding <=> (
                SELECT embedding FROM genome_embeddings WHERE accession = 'SEED'
            ) AS distance
              FROM genome_embeddings g
             WHERE g.accession <> 'SEED'
             ORDER BY g.embedding <=> (
                SELECT embedding FROM genome_embeddings WHERE accession = 'SEED'
            )
             LIMIT 2
            """
        )
        results = cur.fetchall()

    assert [r[0] for r in results] == ["CLOSE", "FAR"]
    # Distance to CLOSE strictly less than to FAR.
    assert results[0][1] < results[1][1]


def test_explain_uses_hnsw_index(applied_db: str) -> None:
    """``EXPLAIN`` must show the HNSW index is consulted.

    Catches future regressions where a refactor (or a missing
    ``CREATE INDEX``) silently downgrades to seqscan, which works
    correctly but is unusable at scale.

    We open a non-autocommit transaction and run ``SET LOCAL
    enable_seqscan = off`` inside it so the setting actually applies
    to the EXPLAIN that follows. (Under autocommit, every statement
    runs in its own implicit transaction and ``SET LOCAL`` is
    discarded immediately.) We also seed several rows so the
    planner has more than one viable plan to pick from.
    """
    with psycopg.connect(applied_db, autocommit=True) as setup_conn:
        register_vector(setup_conn)
        # Seed a small but >1 batch so the planner takes the index
        # plan seriously. HNSW is built; we just need rows to query.
        for i in range(20):
            acc = f"SEED-{i:02d}"
            setup_conn.execute(
                "INSERT INTO isolates (accession, version, organism, source, status) "
                "VALUES (%s, 1, 'X', 'y', 'DISCOVERED')",
                (acc,),
            )
            setup_conn.execute(
                "INSERT INTO genome_embeddings "
                "(accession, version, model, model_version, embedding) "
                "VALUES (%s, 1, 'm', '1.0', %s)",
                (acc, _dim_vector(0.1 + i * 0.01)),
            )
        setup_conn.execute("ANALYZE genome_embeddings")

    # New connection so we're not stuck inside autocommit semantics.
    with psycopg.connect(applied_db) as conn:
        register_vector(conn)
        with conn.transaction():
            conn.execute("SET LOCAL enable_seqscan = off")
            cur = conn.execute(
                """
                EXPLAIN
                SELECT accession FROM genome_embeddings
                 ORDER BY embedding <=> %s::vector
                 LIMIT 1
                """,
                (_dim_vector(0.1),),
            )
            plan = "\n".join(row[0] for row in cur.fetchall())

    assert "genome_embeddings_embedding_hnsw_idx" in plan, plan
