"""Normalize accession identity: reject in-DB version suffix.

Revision ID: 0004
Revises: 0003
Created: 2026-05-28

Background
----------
Pre-fix, Growlithe was emitting the raw NCBI ``target_acc`` (e.g.
``PDT000000123.4``) as the canonical ``accession`` and ``version=4``
side by side. That meant a v=5 event would insert a *new* row keyed by
``PDT000000123.5`` rather than supersede the v=4 row -- collapsing
identity and version into one column.

This migration enforces the new convention: ``accession`` is the
stable base identifier with no version suffix, and ``version`` is the
integer column. We add a CHECK constraint rejecting accession values
matching ``.<digits>`` at end of string on every table that stores an
accession, so any backslide is caught at write time.

``NOT VALID`` + ``VALIDATE CONSTRAINT`` lets a populated DB skip the
table scan up front; the validate step still verifies every row. An
empty DB validates instantly.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_check(table: str, name: str) -> None:
    # ``!~`` is Postgres "does not match"; ``\.[0-9]+$`` is the
    # literal dot followed by one or more digits at end-of-string,
    # which is the NCBI version-suffix shape we forbid in accession.
    op.execute(
        f"ALTER TABLE {table} "
        f"ADD CONSTRAINT {name} "
        f"CHECK (accession !~ '\\.[0-9]+$') NOT VALID"
    )
    op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")


def _drop_check(table: str, name: str) -> None:
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")


def upgrade() -> None:
    _add_check("isolates", "isolates_accession_no_version_check")
    _add_check("genome_embeddings", "genome_embeddings_accession_no_version_check")
    _add_check("alerts", "alerts_accession_no_version_check")


def downgrade() -> None:
    _drop_check("alerts", "alerts_accession_no_version_check")
    _drop_check("genome_embeddings", "genome_embeddings_accession_no_version_check")
    _drop_check("isolates", "isolates_accession_no_version_check")
