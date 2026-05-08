"""Initial Mew schema: isolates, genome_embeddings, alerts.

Revision ID: 0001
Revises:
Created: 2026-05-03

This migration creates the v1 Mew schema documented in
``docs/design.md`` §7. It is the first migration in the chain — there
is no down-revision.

Why every piece exists, in one place so reviewers don't have to chase
context across multiple files:

* The ``vector`` extension is the prerequisite for the ``vector(N)``
  column type and the HNSW index. Note: the extension's name is
  ``vector``, not ``pgvector`` — that's a common pitfall.

* ``isolates`` is keyed on ``accession`` (the NCBI PDT identifier)
  because everything downstream is identified by it. Idempotent
  upserts hinge on this PK. ``status`` is TEXT + CHECK rather than a
  Postgres ENUM type because adding a new status value to an ENUM
  cannot run inside a transaction; a CHECK constraint can be replaced
  in a single migration.

* ``genome_embeddings`` is one row per genome (not per protein) —
  Ditto aggregates per-protein embeddings into a single 1,152-d
  genome vector before insert. The FK to ``isolates`` is ``ON DELETE
  CASCADE`` because deleting an isolate (e.g. for retraction) must
  also remove its embedding.

* The HNSW index uses ``m = 16`` and ``ef_construction = 200``. These
  are the current production-recommended values for ~1-10M-scale
  cosine search on pgvector 0.7+. ``m = 16`` keeps memory in line
  (~5GB index at 2M genomes per design doc §7); ``ef_construction =
  200`` significantly improves recall over the 64 default at a one-
  time build cost. ``ef_search`` is set per-query at query time, not
  here. Built without ``CONCURRENTLY`` because the table is empty at
  apply time; subsequent index changes on populated tables must use
  CONCURRENTLY in their own transactionless migration (see
  README.md).

* B-tree indexes are placed on the columns analysts will filter the
  Chatot UI by: organism, collection_date, novelty_score, status,
  location. Each is a simple single-column B-tree.

* A composite ``(status, scored_at DESC)`` index serves the API's
  hottest query: "give me flagged isolates from the last week". This
  is materially faster than two single-column indexes for that
  predicate shape.

* ``alerts.id`` is BIGSERIAL because alert volume is small (low
  thousands per year) and BIGSERIAL is sequential and easy to read in
  logs. A unique ``(accession, version)`` constraint enforces
  idempotency — re-firing the alerter for the same scored isolate
  version doesn't create duplicate alerts.

* Every table has ``updated_at TIMESTAMPTZ`` maintained by the shared
  ``set_updated_at()`` trigger function. Doing this at the database
  level ensures the column is correct regardless of which service
  performs the write, and is reversible (drops cleanly in
  ``downgrade()``).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Status values (kept as Python tuples so the CHECK lists below stay
# in sync with the ``IsolateStatus`` / ``AlertStatus`` enums in
# kanto-commons). Future status additions: add a new migration that
# DROPs and re-creates the CHECK with the expanded set.
# ---------------------------------------------------------------------------

_ISOLATE_STATUSES = (
    "DISCOVERED",
    "PROTEINS_READY",
    "EMBEDDED",
    "SCORED",
    "ALERTED",
    "QC_FAILED",
)
_ALERT_STATUSES = ("OPEN", "ACKNOWLEDGED", "CLOSED", "DISMISSED")


def _status_check_clause(column: str, statuses: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{s}'" for s in statuses)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    # -- Extension ---------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # -- updated_at trigger function (one function, three triggers) ------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )

    # -- isolates ---------------------------------------------------------
    op.execute(
        f"""
        CREATE TABLE isolates (
            accession         TEXT PRIMARY KEY,
            version           INTEGER NOT NULL,
            organism          TEXT NOT NULL,
            source            TEXT NOT NULL,
            collection_date   DATE,
            location          TEXT,
            source_type       TEXT,
            status            TEXT NOT NULL,
            qc_failure_reason TEXT,
            modal_call_id     TEXT,
            novelty_score     DOUBLE PRECISION,
            nn_distance       DOUBLE PRECISION,
            coverage          DOUBLE PRECISION,
            mahalanobis       DOUBLE PRECISION,
            above_threshold   BOOLEAN,
            discovered_at     TIMESTAMPTZ,
            scored_at         TIMESTAMPTZ,
            raw_metadata      JSONB,
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT isolates_status_check
                CHECK ({_status_check_clause("status", _ISOLATE_STATUSES)})
        )
        """
    )
    op.execute(
        """
        CREATE TRIGGER isolates_set_updated_at
            BEFORE UPDATE ON isolates
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )

    # Filter indexes on columns the Chatot UI / Mew API filters by.
    op.execute("CREATE INDEX isolates_organism_idx ON isolates (organism)")
    op.execute("CREATE INDEX isolates_collection_date_idx ON isolates (collection_date)")
    op.execute("CREATE INDEX isolates_novelty_score_idx ON isolates (novelty_score)")
    op.execute("CREATE INDEX isolates_status_idx ON isolates (status)")
    op.execute("CREATE INDEX isolates_location_idx ON isolates (location)")
    # Composite for "flagged isolates from the last week" — the API's
    # hottest predicate. DESC on scored_at lets the planner walk
    # backwards from the most recent rows without an extra sort.
    op.execute("CREATE INDEX isolates_status_scored_at_idx " "ON isolates (status, scored_at DESC)")

    # -- genome_embeddings ------------------------------------------------
    op.execute(
        """
        CREATE TABLE genome_embeddings (
            accession      TEXT PRIMARY KEY
                              REFERENCES isolates(accession) ON DELETE CASCADE,
            version        INTEGER NOT NULL,
            model          TEXT NOT NULL,
            model_version  TEXT NOT NULL,
            embedding      vector(1152) NOT NULL,
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE TRIGGER genome_embeddings_set_updated_at
            BEFORE UPDATE ON genome_embeddings
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )
    # HNSW index. Empty table → CONCURRENTLY not required.
    # See README.md for the procedure when re-indexing on a populated table.
    op.execute(
        """
        CREATE INDEX genome_embeddings_embedding_hnsw_idx
            ON genome_embeddings
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 200)
        """
    )

    # -- alerts -----------------------------------------------------------
    op.execute(
        f"""
        CREATE TABLE alerts (
            id            BIGSERIAL PRIMARY KEY,
            accession     TEXT NOT NULL
                              REFERENCES isolates(accession) ON DELETE CASCADE,
            version       INTEGER NOT NULL,
            score         DOUBLE PRECISION NOT NULL,
            triggered_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            status        TEXT NOT NULL DEFAULT 'OPEN',
            notes         TEXT,
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT alerts_status_check
                CHECK ({_status_check_clause("status", _ALERT_STATUSES)}),
            CONSTRAINT alerts_accession_version_unique
                UNIQUE (accession, version)
        )
        """
    )
    op.execute(
        """
        CREATE TRIGGER alerts_set_updated_at
            BEFORE UPDATE ON alerts
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )
    op.execute("CREATE INDEX alerts_status_idx ON alerts (status)")
    op.execute("CREATE INDEX alerts_accession_idx ON alerts (accession)")


def downgrade() -> None:
    """Reverse the migration. Drops in dependency order.

    The ``vector`` extension is dropped only if it is otherwise unused
    (Postgres allows ``DROP EXTENSION`` to fail if other objects
    depend on it; here we drop tables first, so it should always
    succeed).
    """
    # alerts depends on isolates → drop first.
    op.execute("DROP TABLE IF EXISTS alerts")
    # genome_embeddings depends on isolates → drop next.
    op.execute("DROP TABLE IF EXISTS genome_embeddings")
    op.execute("DROP TABLE IF EXISTS isolates")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
    # Drop the extension last. CASCADE not used: if some other migration
    # in a parallel branch added an object that depends on the
    # extension, we want this to fail loudly rather than silently
    # destroy that object.
    op.execute("DROP EXTENSION IF EXISTS vector")
