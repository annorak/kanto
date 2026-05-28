"""Thin async wrapper around the Mew client for Alakazam-specific reads/writes.

Why this layer exists
---------------------
The repositories in :mod:`kanto_commons.mew.repositories` already cover
the generic CRUD over ``isolates`` / ``genome_embeddings`` / ``alerts``,
but they don't know about Alakazam's specific access patterns:

* k-NN against the seed accession's *own* embedding (a one-shot,
  HNSW-driven query — distinct from a generic neighbor query).
* Per-species centroid + diagonal covariance reads, including the
  freshness check on ``computed_at``.
* Score upserts that accept ``None`` for components Alakazam didn't
  compute (tiered scoring), which the generic ``update_scores``
  helper does not support.

Keeping these reads/writes here rather than expanding kanto-commons
keeps the shared repository surface narrow — generalize on the
second consumer, not the first.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from kanto_commons.mew import MewClient
from kanto_commons.mew.models import IsolateStatus, NeighborRow
from kanto_commons.mew.repositories import (
    EmbeddingNotFoundError,
    IsolateNotFoundError,
)
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Read-shaped data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IsolateForScoring:
    """The narrow projection of an isolate Alakazam actually needs.

    The full :class:`kanto_commons.mew.models.IsolateRow` carries a
    dozen fields that the scoring path doesn't touch. We name what we
    use so a future refactor can spot dead reads at the call site.
    """

    accession: str
    version: int
    organism: str
    status: IsolateStatus


@dataclass(frozen=True, slots=True)
class GenomeEmbeddingRecord:
    """The genome-level embedding for one accession."""

    accession: str
    version: int
    model: str
    model_version: str
    # Plain Python list[float] — the pgvector adapter hands these back
    # as numpy arrays, but we materialize to list at the gateway edge
    # so downstream callers don't depend on numpy's array semantics
    # (notably ``__bool__``).
    embedding: list[float]


@dataclass(frozen=True, slots=True)
class SpeciesCentroidRecord:
    """One row from ``species_centroids``."""

    organism: str
    model: str
    model_version: str
    n_isolates: int
    centroid: list[float]
    covariance_diagonal: list[float]
    regularization: float
    computed_at: datetime


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------


class AlakazamMewGateway:
    """Alakazam's typed read/write surface against Mew."""

    def __init__(self, *, mew: MewClient) -> None:
        self._mew = mew

    # -------------------- isolate + embedding reads --------------------

    async def get_isolate(self, accession: str) -> IsolateForScoring:
        """Fetch the narrow isolate projection used by the scorer."""
        sql = "SELECT accession, version, organism, status FROM isolates WHERE accession = %s"
        async with self._mew.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (accession,))
            row = await cur.fetchone()
        if row is None:
            raise IsolateNotFoundError(accession)
        return IsolateForScoring(
            accession=row["accession"],
            version=int(row["version"]),
            organism=row["organism"],
            status=IsolateStatus(row["status"]),
        )

    async def get_genome_embedding(self, accession: str) -> GenomeEmbeddingRecord:
        """Fetch the per-genome embedding for ``accession``."""
        sql = (
            "SELECT accession, version, model, model_version, embedding "
            "FROM genome_embeddings WHERE accession = %s"
        )
        async with self._mew.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (accession,))
            row = await cur.fetchone()
        if row is None:
            raise EmbeddingNotFoundError(accession)
        return GenomeEmbeddingRecord(
            accession=row["accession"],
            version=int(row["version"]),
            model=row["model"],
            model_version=row["model_version"],
            embedding=list(row["embedding"]),
        )

    async def k_nearest(
        self,
        *,
        accession: str,
        k: int,
    ) -> list[NeighborRow]:
        """k-NN against the seed accession's own embedding, excluding the seed.

        ``<=>`` is pgvector's cosine-distance operator and matches the
        HNSW index family declared in migration 0001. We rely on the
        index for the ``ORDER BY``; the planner picks it up because
        the seed embedding is supplied via a CTE, which keeps the
        index-friendly shape ``ORDER BY g.embedding <=> :seed``.
        """
        sql = """
        WITH seed AS (
            SELECT embedding FROM genome_embeddings WHERE accession = %(accession)s
        )
        SELECT g.accession AS accession,
               (g.embedding <=> seed.embedding) AS distance
          FROM genome_embeddings AS g, seed
         WHERE g.accession <> %(accession)s
         ORDER BY g.embedding <=> seed.embedding
         LIMIT %(k)s
        """
        async with self._mew.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, {"accession": accession, "k": k})
            rows = await cur.fetchall()
        return [NeighborRow.model_validate(r) for r in rows]

    # -------------------- species centroids --------------------

    async def get_species_centroid(self, organism: str) -> SpeciesCentroidRecord | None:
        """Return the latest centroid row for ``organism``, or ``None``.

        ``None`` is the signal to Alakazam that the Mahalanobis
        component cannot run for this isolate — score with the other
        components only and record the component as null in Mew.
        """
        sql = """
        SELECT organism, model, model_version, n_isolates,
               centroid, covariance_diagonal, regularization, computed_at
          FROM species_centroids
         WHERE organism = %s
        """
        async with self._mew.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (organism,))
            row = await cur.fetchone()
        if row is None:
            return None
        return SpeciesCentroidRecord(
            organism=row["organism"],
            model=row["model"],
            model_version=row["model_version"],
            n_isolates=int(row["n_isolates"]),
            centroid=list(row["centroid"]),
            covariance_diagonal=list(row["covariance_diagonal"]),
            regularization=float(row["regularization"]),
            computed_at=row["computed_at"],
        )

    async def list_species_centroids(self) -> list[SpeciesCentroidRecord]:
        """Return every centroid row. Used by the in-memory cache refresh."""
        sql = """
        SELECT organism, model, model_version, n_isolates,
               centroid, covariance_diagonal, regularization, computed_at
          FROM species_centroids
        """
        async with self._mew.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql)
            rows = await cur.fetchall()
        return [
            SpeciesCentroidRecord(
                organism=r["organism"],
                model=r["model"],
                model_version=r["model_version"],
                n_isolates=int(r["n_isolates"]),
                centroid=list(r["centroid"]),
                covariance_diagonal=list(r["covariance_diagonal"]),
                regularization=float(r["regularization"]),
                computed_at=r["computed_at"],
            )
            for r in rows
        ]

    async def upsert_species_centroid(self, record: SpeciesCentroidRecord) -> None:
        """Insert or update one species centroid by organism."""
        sql = """
        INSERT INTO species_centroids (
            organism, model, model_version, n_isolates,
            centroid, covariance_diagonal, regularization, computed_at
        ) VALUES (
            %(organism)s, %(model)s, %(model_version)s, %(n_isolates)s,
            %(centroid)s, %(covariance_diagonal)s,
            %(regularization)s, %(computed_at)s
        )
        ON CONFLICT (organism) DO UPDATE SET
            model               = EXCLUDED.model,
            model_version       = EXCLUDED.model_version,
            n_isolates          = EXCLUDED.n_isolates,
            centroid            = EXCLUDED.centroid,
            covariance_diagonal = EXCLUDED.covariance_diagonal,
            regularization      = EXCLUDED.regularization,
            computed_at         = EXCLUDED.computed_at
        """
        async with self._mew.connection() as conn:
            await conn.execute(
                sql,
                {
                    "organism": record.organism,
                    "model": record.model,
                    "model_version": record.model_version,
                    "n_isolates": record.n_isolates,
                    "centroid": record.centroid,
                    "covariance_diagonal": record.covariance_diagonal,
                    "regularization": record.regularization,
                    "computed_at": record.computed_at,
                },
            )

    # -------------------- score writes --------------------

    async def write_score(
        self,
        *,
        accession: str,
        novelty_score: float,
        nn_distance: float | None,
        coverage: float | None,
        mahalanobis: float | None,
        above_threshold: bool,
        scored_at: datetime,
    ) -> None:
        """Upsert the score components onto the isolate row.

        Unlike :meth:`IsolateRepository.update_scores`, this accepts
        ``None`` for the expensive components — tiered scoring writes
        them as SQL NULL so the analyst-facing UI can distinguish
        "didn't run" from "ran and scored zero".
        """
        sql = """
        UPDATE isolates
           SET novelty_score   = %(novelty_score)s,
               nn_distance     = %(nn_distance)s,
               coverage        = %(coverage)s,
               mahalanobis     = %(mahalanobis)s,
               above_threshold = %(above_threshold)s,
               scored_at       = %(scored_at)s,
               status          = %(status)s
         WHERE accession = %(accession)s
        RETURNING accession
        """
        async with self._mew.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                sql,
                {
                    "accession": accession,
                    "novelty_score": novelty_score,
                    "nn_distance": nn_distance,
                    "coverage": coverage,
                    "mahalanobis": mahalanobis,
                    "above_threshold": above_threshold,
                    "scored_at": scored_at,
                    "status": IsolateStatus.SCORED.value,
                },
            )
            row = await cur.fetchone()
        if row is None:
            raise IsolateNotFoundError(accession)

    # -------------------- centroid-job-only reads --------------------

    async def stream_embeddings_by_organism(
        self,
        organism: str,
        *,
        batch_size: int = 1000,
    ) -> AsyncIterator[list[float]]:
        """Yield each embedding for ``organism`` one row at a time.

        Uses a server-side named cursor with ``itersize=batch_size`` so
        Postgres streams rows lazily and memory stays bounded by the
        single-vector width (~9 KB at fp32 * 1152) plus the cursor
        buffer -- not by the species's full embedding cloud.
        """
        sql = """
        SELECT e.embedding
          FROM genome_embeddings AS e
          JOIN isolates AS i ON i.accession = e.accession
         WHERE i.organism = %s
        """
        async with self._mew.connection() as conn:
            conn_typed: psycopg.AsyncConnection[Any] = conn
            async with conn_typed.cursor(name="centroid_stream") as cur:
                cur.itersize = batch_size
                await cur.execute(sql, (organism,))
                async for row in cur:
                    # Named-cursor rows are tuples; column 0 is the embedding.
                    yield list(row[0])

    async def list_organisms_with_embeddings(
        self,
        *,
        min_isolates: int,
    ) -> list[tuple[str, int]]:
        """Return ``[(organism, count)]`` for species above the floor."""
        sql = """
        SELECT i.organism AS organism, COUNT(*) AS n
          FROM genome_embeddings AS e
          JOIN isolates AS i ON i.accession = e.accession
      GROUP BY i.organism
        HAVING COUNT(*) >= %s
      ORDER BY i.organism
        """
        async with self._mew.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (min_isolates,))
            rows = await cur.fetchall()
        return [(r["organism"], int(r["n"])) for r in rows]

    # -------------------- health --------------------

    async def healthy(self) -> bool:
        return await self._mew.healthy()


__all__ = [
    "AlakazamMewGateway",
    "GenomeEmbeddingRecord",
    "IsolateForScoring",
    "SpeciesCentroidRecord",
]
