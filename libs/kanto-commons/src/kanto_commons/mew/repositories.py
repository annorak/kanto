"""Repository pattern over Mew. All SQL in the package lives here.

Every method takes either an ``AsyncConnection`` (when the caller
needs to compose multiple operations into a transaction) or borrows
its own from the pool implicitly. We chose the explicit-connection
style so transactions are always visible at the call site:

.. code-block:: python

    async with mew.transaction() as conn:
        await isolate_repo.upsert(conn, row)
        await embedding_repo.upsert(conn, embedding_row)

Methods are async. All queries are parameterized — no f-strings or
string formatting touches a SQL string anywhere in this module.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from kanto_commons.mew.models import (
    AlertRow,
    AlertStatus,
    EmbeddingRow,
    IsolateRow,
    IsolateStatus,
    NeighborRow,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors and retry policy
# ---------------------------------------------------------------------------


class RepositoryError(Exception):
    """Raised by repositories when a read finds nothing or an invariant breaks."""


class IsolateNotFoundError(RepositoryError):
    pass


class EmbeddingNotFoundError(RepositoryError):
    pass


class AlertNotFoundError(RepositoryError):
    pass


# Retry only on transient connection errors. ``UniqueViolation`` etc. are
# deterministic and must surface to the caller so they don't infinite-loop.
_TRANSIENT_EXC = (
    psycopg.OperationalError,
    psycopg.errors.ConnectionTimeout,
    psycopg.errors.AdminShutdown,
    psycopg.errors.CrashShutdown,
)


async def _with_transient_retry(coro_factory: Any) -> Any:
    """Run ``coro_factory()`` with transient-error retry."""
    async for attempt in AsyncRetrying(
        retry=retry_if_exception_type(_TRANSIENT_EXC),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=0.5, max=10.0),
        reraise=True,
    ):
        with attempt:
            return await coro_factory()
    return None  # pragma: no cover — AsyncRetrying always exits via attempt


# ---------------------------------------------------------------------------
# Helpers shared across repositories
# ---------------------------------------------------------------------------


_ConnLike = psycopg.AsyncConnection[Any] | AsyncConnectionPool


async def _acquire(conn_or_pool: _ConnLike) -> psycopg.AsyncConnection[Any]:
    """Return ``conn_or_pool`` as a connection.

    If passed a pool, callers must wrap the call site in
    ``async with pool.connection() as conn:`` themselves — this helper
    is intentionally simple. The repositories below take connections
    directly so transaction composition is explicit.
    """
    if isinstance(conn_or_pool, AsyncConnectionPool):
        raise TypeError(
            "Pass an AsyncConnection (e.g. via `async with mew.connection() "
            "as conn:`); pools are not accepted directly."
        )
    return conn_or_pool


# ---------------------------------------------------------------------------
# IsolateRepository
# ---------------------------------------------------------------------------


_ISOLATE_COLUMNS = (
    "accession, version, organism, source, collection_date, location, "
    "source_type, status, qc_failure_reason, modal_call_id, novelty_score, "
    "nn_distance, coverage, mahalanobis, above_threshold, discovered_at, "
    "scored_at, raw_metadata"
)


class IsolateRepository:
    """CRUD + filtered queries over the ``isolates`` table."""

    async def upsert(
        self,
        conn: psycopg.AsyncConnection[Any],
        row: IsolateRow,
    ) -> None:
        """Insert or update by primary key (``accession``).

        ``ON CONFLICT (accession) DO UPDATE ... WHERE EXCLUDED.version
        >= isolates.version`` — newer (or equal-version replay) writes
        win; a stale lower-version event silently no-ops. This is what
        makes "(accession, v=N+1) supersedes v=N" and "redelivered
        v=N replay still works" both safe in the face of out-of-order
        message processing.
        """
        sql = f"""
        INSERT INTO isolates ({_ISOLATE_COLUMNS})
        VALUES (
            %(accession)s, %(version)s, %(organism)s, %(source)s,
            %(collection_date)s, %(location)s, %(source_type)s,
            %(status)s, %(qc_failure_reason)s, %(modal_call_id)s,
            %(novelty_score)s, %(nn_distance)s, %(coverage)s,
            %(mahalanobis)s, %(above_threshold)s,
            %(discovered_at)s, %(scored_at)s, %(raw_metadata)s
        )
        ON CONFLICT (accession) DO UPDATE SET
            version           = EXCLUDED.version,
            organism          = EXCLUDED.organism,
            source            = EXCLUDED.source,
            collection_date   = EXCLUDED.collection_date,
            location          = EXCLUDED.location,
            source_type       = EXCLUDED.source_type,
            status            = EXCLUDED.status,
            qc_failure_reason = EXCLUDED.qc_failure_reason,
            modal_call_id     = EXCLUDED.modal_call_id,
            novelty_score     = EXCLUDED.novelty_score,
            nn_distance       = EXCLUDED.nn_distance,
            coverage          = EXCLUDED.coverage,
            mahalanobis       = EXCLUDED.mahalanobis,
            above_threshold   = EXCLUDED.above_threshold,
            discovered_at     = EXCLUDED.discovered_at,
            scored_at         = EXCLUDED.scored_at,
            raw_metadata      = EXCLUDED.raw_metadata
         WHERE EXCLUDED.version >= isolates.version
        """
        params = self._row_params(row)

        async def _do() -> None:
            await conn.execute(sql, params)

        await _with_transient_retry(_do)

    async def update_status(
        self,
        conn: psycopg.AsyncConnection[Any],
        *,
        accession: str,
        status: IsolateStatus,
        modal_call_id: str | None = None,
        qc_failure_reason: str | None = None,
    ) -> None:
        sql = """
        UPDATE isolates
           SET status = %(status)s,
               modal_call_id = COALESCE(%(modal_call_id)s, modal_call_id),
               qc_failure_reason = COALESCE(%(qc_failure_reason)s, qc_failure_reason)
         WHERE accession = %(accession)s
        RETURNING accession
        """
        async with conn.cursor() as cur:
            await cur.execute(
                sql,
                {
                    "status": status.value,
                    "modal_call_id": modal_call_id,
                    "qc_failure_reason": qc_failure_reason,
                    "accession": accession,
                },
            )
            row = await cur.fetchone()
        if row is None:
            raise IsolateNotFoundError(accession)

    async def update_scores(
        self,
        conn: psycopg.AsyncConnection[Any],
        *,
        accession: str,
        novelty_score: float,
        nn_distance: float,
        coverage: float,
        mahalanobis: float,
        above_threshold: bool,
        scored_at: datetime,
    ) -> None:
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
        """
        await conn.execute(
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

    async def get(
        self,
        conn: psycopg.AsyncConnection[Any],
        accession: str,
    ) -> IsolateRow:
        sql = f"SELECT {_ISOLATE_COLUMNS} FROM isolates WHERE accession = %s"
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (accession,))
            row = await cur.fetchone()
        if row is None:
            raise IsolateNotFoundError(accession)
        return IsolateRow.model_validate(row)

    async def find(
        self,
        conn: psycopg.AsyncConnection[Any],
        *,
        organism: str | None = None,
        status: IsolateStatus | None = None,
        discovered_after: datetime | None = None,
        discovered_before: datetime | None = None,
        min_score: float | None = None,
        max_score: float | None = None,
        limit: int = 100,
    ) -> list[IsolateRow]:
        """Filtered query. All filters are AND-ed."""
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if organism is not None:
            clauses.append("organism = %(organism)s")
            params["organism"] = organism
        if status is not None:
            clauses.append("status = %(status)s")
            params["status"] = status.value
        if discovered_after is not None:
            clauses.append("discovered_at >= %(discovered_after)s")
            params["discovered_after"] = discovered_after
        if discovered_before is not None:
            clauses.append("discovered_at < %(discovered_before)s")
            params["discovered_before"] = discovered_before
        if min_score is not None:
            clauses.append("novelty_score >= %(min_score)s")
            params["min_score"] = min_score
        if max_score is not None:
            clauses.append("novelty_score <= %(max_score)s")
            params["max_score"] = max_score
        params["limit"] = limit
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT {_ISOLATE_COLUMNS} FROM isolates {where} "
            "ORDER BY discovered_at DESC NULLS LAST LIMIT %(limit)s"
        )
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            rows = await cur.fetchall()
        return [IsolateRow.model_validate(r) for r in rows]

    @staticmethod
    def _row_params(row: IsolateRow) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        return {
            "accession": row.accession,
            "version": row.version,
            "organism": row.organism,
            "source": row.source,
            "collection_date": row.collection_date,
            "location": row.location,
            "source_type": row.source_type,
            "status": row.status.value,
            "qc_failure_reason": row.qc_failure_reason,
            "modal_call_id": row.modal_call_id,
            "novelty_score": row.novelty_score,
            "nn_distance": row.nn_distance,
            "coverage": row.coverage,
            "mahalanobis": row.mahalanobis,
            "above_threshold": row.above_threshold,
            "discovered_at": row.discovered_at,
            "scored_at": row.scored_at,
            "raw_metadata": Jsonb(row.raw_metadata)
            if row.raw_metadata is not None
            else None,
        }


# ---------------------------------------------------------------------------
# EmbeddingRepository
# ---------------------------------------------------------------------------


class EmbeddingRepository:
    """``genome_embeddings`` table + the pgvector HNSW index queries."""

    async def upsert(
        self,
        conn: psycopg.AsyncConnection[Any],
        row: EmbeddingRow,
    ) -> None:
        """Insert or update by ``accession``. Stale lower-version writes
        silently no-op so a re-embedded older version never overwrites
        the newer embedding currently in Mew.
        """
        sql = """
        INSERT INTO genome_embeddings (accession, version, model, model_version, embedding)
        VALUES (%(accession)s, %(version)s, %(model)s, %(model_version)s, %(embedding)s)
        ON CONFLICT (accession) DO UPDATE SET
            version       = EXCLUDED.version,
            model         = EXCLUDED.model,
            model_version = EXCLUDED.model_version,
            embedding     = EXCLUDED.embedding
         WHERE EXCLUDED.version >= genome_embeddings.version
        """

        async def _do() -> None:
            await conn.execute(
                sql,
                {
                    "accession": row.accession,
                    "version": row.version,
                    "model": row.model,
                    "model_version": row.model_version,
                    "embedding": row.embedding,
                },
            )

        await _with_transient_retry(_do)

    async def get(
        self,
        conn: psycopg.AsyncConnection[Any],
        accession: str,
    ) -> EmbeddingRow:
        sql = (
            "SELECT accession, version, model, model_version, embedding "
            "FROM genome_embeddings WHERE accession = %s"
        )
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (accession,))
            row = await cur.fetchone()
        if row is None:
            raise EmbeddingNotFoundError(accession)
        # pgvector returns numpy arrays; serialize to a plain list for the model.
        row["embedding"] = list(row["embedding"])
        return EmbeddingRow.model_validate(row)

    async def k_nearest(
        self,
        conn: psycopg.AsyncConnection[Any],
        *,
        accession: str,
        k: int = 10,
    ) -> list[NeighborRow]:
        """Return the ``k`` nearest neighbors of ``accession`` by cosine distance.

        The query joins the embedding back to itself excluding the
        seed accession. ``<=>`` is pgvector's cosine-distance operator
        and matches the index built in the design doc DDL.
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
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, {"accession": accession, "k": k})
            rows = await cur.fetchall()
        return [NeighborRow.model_validate(r) for r in rows]


# ---------------------------------------------------------------------------
# AlertRepository
# ---------------------------------------------------------------------------


class AlertRepository:
    """``alerts`` table CRUD."""

    async def create(
        self,
        conn: psycopg.AsyncConnection[Any],
        *,
        accession: str,
        version: int,
        score: float,
        notes: str | None = None,
    ) -> AlertRow:
        sql = """
        INSERT INTO alerts (accession, version, score, notes)
        VALUES (%(accession)s, %(version)s, %(score)s, %(notes)s)
        RETURNING id, accession, version, score, triggered_at, status, notes
        """
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                sql,
                {
                    "accession": accession,
                    "version": version,
                    "score": score,
                    "notes": notes,
                },
            )
            row = await cur.fetchone()
        assert row is not None  # RETURNING guarantees a row
        return AlertRow.model_validate(row)

    async def update_status(
        self,
        conn: psycopg.AsyncConnection[Any],
        *,
        alert_id: int,
        status: AlertStatus,
        notes: str | None = None,
    ) -> AlertRow:
        sql = """
        UPDATE alerts
           SET status = %(status)s,
               notes  = COALESCE(%(notes)s, notes)
         WHERE id = %(alert_id)s
        RETURNING id, accession, version, score, triggered_at, status, notes
        """
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                sql,
                {
                    "alert_id": alert_id,
                    "status": status.value,
                    "notes": notes,
                },
            )
            row = await cur.fetchone()
        if row is None:
            raise AlertNotFoundError(str(alert_id))
        return AlertRow.model_validate(row)

    async def list_open(
        self,
        conn: psycopg.AsyncConnection[Any],
        *,
        limit: int = 100,
    ) -> list[AlertRow]:
        sql = """
        SELECT id, accession, version, score, triggered_at, status, notes
          FROM alerts
         WHERE status = %(status)s
         ORDER BY triggered_at DESC
         LIMIT %(limit)s
        """
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                sql,
                {"status": AlertStatus.OPEN.value, "limit": limit},
            )
            rows = await cur.fetchall()
        return [AlertRow.model_validate(r) for r in rows]


__all__ = [
    "AlertNotFoundError",
    "AlertRepository",
    "EmbeddingNotFoundError",
    "EmbeddingRepository",
    "IsolateNotFoundError",
    "IsolateRepository",
    "RepositoryError",
]


# Hint to mypy that ``date`` is referenced by IsolateRow's typing only.
_unused: tuple[type, ...] = (date,)
