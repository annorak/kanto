"""Add discovery_cursors for Growlithe.

Revision ID: 0002
Revises: 0001
Created: 2026-05-15

Growlithe polls upstream data sources (NCBI Pathogen Detection in v1,
ENA / GISAID / customer uploads later) on a schedule. The cursor table
records, per ``(source, organism)``, the upstream snapshot Growlithe
has fully processed and emitted events for. On restart, Growlithe
reads this row to decide which snapshots are new and which are
already-emitted noise.

Why a dedicated table and not a column on ``isolates``:
* The cursor is per (source, organism), not per accession. Putting it
  on the isolate table would be a coordination headache and a
  hot-row problem under concurrent polls.
* The cursor's lifecycle is independent of any individual isolate. An
  isolate can be retracted from NCBI; we don't want to invalidate the
  cursor when that happens.
* Future sources (ENA, GISAID, customer uploads) will reuse the same
  table without a schema change.

Why fields exist as they do:
* ``source`` and ``organism`` together form the primary key. Both are
  TEXT because their values are upstream-defined strings.
* ``last_snapshot_id`` is the source-specific identifier for the
  most-recently-processed batch (NCBI: PDG version string, e.g.
  ``PDG000000004.355``). Future sources may use a date stamp, a
  sequence number, etc.
* ``last_polled_at`` records the last *attempted* poll's timestamp;
  ``last_succeeded_at`` records the last *successful* one. Splitting
  them lets oncall distinguish "Growlithe is running but every poll
  is failing" from "Growlithe stopped polling entirely".
* ``last_emitted_count`` is the number of IsolateDiscovered events
  emitted in the most recent successful cycle. Useful for dashboards
  and for spotting regressions ("we usually see 200/cycle, today 0").
* ``updated_at`` is maintained by the shared ``set_updated_at()``
  trigger function declared in the 0001 migration.

The downgrade path drops the table without preserving cursor state —
if you're rolling back, you're rebuilding the cursor anyway.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE discovery_cursors (
            source              TEXT NOT NULL,
            organism            TEXT NOT NULL,
            last_snapshot_id    TEXT,
            last_polled_at      TIMESTAMPTZ,
            last_succeeded_at   TIMESTAMPTZ,
            last_emitted_count  INTEGER NOT NULL DEFAULT 0,
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (source, organism)
        )
        """
    )
    op.execute(
        """
        CREATE TRIGGER discovery_cursors_set_updated_at
            BEFORE UPDATE ON discovery_cursors
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )
    # Filter index for "show me every cursor for this source" — Growlithe
    # iterates the cursors for its configured source on startup.
    op.execute("CREATE INDEX discovery_cursors_source_idx ON discovery_cursors (source)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS discovery_cursors")
