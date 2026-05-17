"""Add species_centroids for Alakazam's Mahalanobis component.

Revision ID: 0003
Revises: 0002
Created: 2026-05-16

The Mahalanobis component of Alakazam's novelty score needs, for every
organism, the mean and covariance of its existing embedding cloud.
Recomputing these on every isolate is intractable (a single species can
hold hundreds of thousands of embeddings), so Alakazam ships a daily
CronJob that recomputes them from the current population and writes
them here. The scoring path then reads the latest row per organism on
startup and refreshes periodically.

Design choices baked into this schema:

* ``organism`` is the primary key (one row per species). The hot read
  pattern is "give me the centroid for organism X"; a composite key
  with a computed_at would make that read awkward without buying
  anything — history is reproducible from the source isolates anyway.
* ``centroid`` is a ``vector(1152)`` so it sits alongside
  ``genome_embeddings.embedding`` in pgvector land. Same dtype, same
  index family, same operators.
* ``covariance_diagonal`` stores only the per-dimension variance, not
  the full 1152x1152 covariance matrix. Two reasons: (1) the full
  matrix is ~5 MB per species in float32 — workable but heavy for a
  hot-path read; (2) on 1152-d ESM embeddings the cross-dimensional
  covariance is small and a diagonal approximation is a well-known
  workable simplification. The README documents the tradeoff; if we
  ever need the full matrix we add a sibling column rather than break
  the existing one.
* Variance values are regularized at compute time (``var = var +
  eps``) so the Mahalanobis denominator is never zero. The schema
  enforces ``> 0`` via a CHECK so a buggy compute job can't silently
  poison the scoring path.
* ``n_isolates`` is the population that fed the centroid. Useful both
  for monitoring drift (a species with 2 isolates shouldn't drive
  scoring decisions on its own) and for a per-species ``min_samples``
  guard at score time.
* ``model`` + ``model_version`` are first-class columns so a model
  rollout doesn't silently score against centroids computed under an
  older model — the scoring path can match on these.
* ``computed_at`` records when the row was last refreshed. The
  CronJob runs daily; this lets oncall spot a stuck job.
* ``updated_at`` is maintained by the shared trigger function from
  migration 0001.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE species_centroids (
            organism             TEXT PRIMARY KEY,
            model                TEXT NOT NULL,
            model_version        TEXT NOT NULL,
            n_isolates           INTEGER NOT NULL,
            centroid             vector(1152) NOT NULL,
            covariance_diagonal  vector(1152) NOT NULL,
            regularization       DOUBLE PRECISION NOT NULL,
            computed_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT species_centroids_n_isolates_positive
                CHECK (n_isolates > 0),
            CONSTRAINT species_centroids_regularization_positive
                CHECK (regularization > 0)
        )
        """
    )
    op.execute(
        """
        CREATE TRIGGER species_centroids_set_updated_at
            BEFORE UPDATE ON species_centroids
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )
    # Operator-facing filter for "show me every centroid computed under
    # this model" — useful during model rollouts and for the cleanup
    # job that drops stale-model rows after a successful migration.
    op.execute(
        "CREATE INDEX species_centroids_model_idx ON species_centroids (model, model_version)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS species_centroids")
