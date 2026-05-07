"""Local-only seed data for Mew.

The CLI entry point lives at ``scripts/seed-mew.py``; this module
holds the data construction and insert logic so tests can import it
directly. The CLI is responsible for the local-environment guard.

Idempotency
-----------
Each insert uses ``ON CONFLICT DO NOTHING`` keyed on the table's
natural unique constraint. Running :func:`seed` twice does not
double the row count.
"""

from __future__ import annotations

import logging
import random
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
from pgvector.psycopg import register_vector

log = logging.getLogger(__name__)

EMBEDDING_DIM = 1152
SEED = 42
N_ISOLATES = 24
N_ALERTS = 6


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------


def isolate_rows() -> list[dict[str, Any]]:
    """Deterministic mix of statuses, organisms, and dates."""
    rng = random.Random(SEED)
    organisms = ["Salmonella enterica", "Listeria monocytogenes", "E. coli", "Campylobacter"]
    sources = ["ncbi-pd", "ncbi-pd", "internal-upload"]
    statuses = ["DISCOVERED", "EMBEDDED", "SCORED", "ALERTED", "QC_FAILED"]
    locations = ["USA:CA", "USA:NY", "USA:TX", None, "Canada:ON"]
    rows: list[dict[str, Any]] = []
    for i in range(N_ISOLATES):
        status = statuses[i % len(statuses)]
        organism = organisms[i % len(organisms)]
        novelty = round(rng.uniform(1.0, 6.0), 3) if status in {"SCORED", "ALERTED"} else None
        rows.append(
            {
                "accession": f"PDT-LOCAL-{i:04d}",
                "version": 1,
                "organism": organism,
                "source": sources[i % len(sources)],
                "collection_date": date(2026, 1, 1) + timedelta(days=i),
                "location": locations[i % len(locations)],
                "source_type": "clinical" if i % 2 == 0 else "food",
                "status": status,
                "qc_failure_reason": "low coverage" if status == "QC_FAILED" else None,
                "novelty_score": novelty,
                "above_threshold": (novelty is not None and novelty > 4.0),
                "discovered_at": datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=i),
                "scored_at": (
                    datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=i, minutes=10)
                    if novelty is not None
                    else None
                ),
            }
        )
    return rows


def embedding_rows(isolates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One embedding per non-QC-failed isolate, deterministic from ``SEED``."""
    rng = random.Random(SEED + 1)
    rows: list[dict[str, Any]] = []
    for iso in isolates:
        # Skip QC-failed isolates: by design, no embedding is produced
        # for them. This makes the seeded data faithful to the
        # pipeline's actual semantics.
        if iso["status"] == "QC_FAILED":
            continue
        vector = [rng.uniform(-1.0, 1.0) for _ in range(EMBEDDING_DIM)]
        rows.append(
            {
                "accession": iso["accession"],
                "version": 1,
                "model": "esm-c-600m",
                "model_version": "1.0.0",
                "embedding": vector,
            }
        )
    return rows


def alert_rows(isolates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One alert per ALERTED isolate, alternating workflow states."""
    statuses = ["OPEN", "ACKNOWLEDGED", "CLOSED", "DISMISSED"]
    alerted = [iso for iso in isolates if iso["status"] == "ALERTED"][:N_ALERTS]
    rows: list[dict[str, Any]] = []
    for i, iso in enumerate(alerted):
        rows.append(
            {
                "accession": iso["accession"],
                "version": 1,
                "score": iso["novelty_score"] or 4.5,
                "status": statuses[i % len(statuses)],
                "notes": f"seeded alert #{i}",
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Insert
# ---------------------------------------------------------------------------


_ISOLATE_INSERT = """
INSERT INTO isolates (
    accession, version, organism, source, collection_date, location,
    source_type, status, qc_failure_reason, novelty_score, above_threshold,
    discovered_at, scored_at
) VALUES (
    %(accession)s, %(version)s, %(organism)s, %(source)s, %(collection_date)s,
    %(location)s, %(source_type)s, %(status)s, %(qc_failure_reason)s,
    %(novelty_score)s, %(above_threshold)s, %(discovered_at)s, %(scored_at)s
)
ON CONFLICT (accession) DO NOTHING
"""

_EMBEDDING_INSERT = """
INSERT INTO genome_embeddings (accession, version, model, model_version, embedding)
VALUES (%(accession)s, %(version)s, %(model)s, %(model_version)s, %(embedding)s)
ON CONFLICT (accession) DO NOTHING
"""

_ALERT_INSERT = """
INSERT INTO alerts (accession, version, score, status, notes)
VALUES (%(accession)s, %(version)s, %(score)s, %(status)s, %(notes)s)
ON CONFLICT (accession, version) DO NOTHING
"""


def seed(dsn: str) -> dict[str, int]:
    """Apply the seed data to ``dsn``. Returns per-table input counts.

    Idempotent: ``ON CONFLICT DO NOTHING`` skips duplicates so the
    second run is a no-op at the row level (constraint checks still
    fire — that's intentional, it's how we know the seed is sane).
    """
    isolates = isolate_rows()
    embeddings = embedding_rows(isolates)
    alerts = alert_rows(isolates)

    with psycopg.connect(dsn, autocommit=False) as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            cur.executemany(_ISOLATE_INSERT, isolates)
            cur.executemany(_EMBEDDING_INSERT, embeddings)
            cur.executemany(_ALERT_INSERT, alerts)
        conn.commit()

    counts = {
        "isolates": len(isolates),
        "genome_embeddings": len(embeddings),
        "alerts": len(alerts),
    }
    log.info("Seeded Mew: %s", counts)
    return counts


__all__ = [
    "EMBEDDING_DIM",
    "N_ALERTS",
    "N_ISOLATES",
    "alert_rows",
    "embedding_rows",
    "isolate_rows",
    "seed",
]
