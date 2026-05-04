"""Pytest fixtures spinning up a Postgres + pgvector container for Mew tests.

The fixtures are session-scoped — one container per test process —
because container start/stop is the slow part. Repository tests share
the same container and clean up between tests via TRUNCATE.

The DDL bootstrap below is **temporary**. Task 3 of the Kanto plan
introduces Alembic; once it lands, this module's ``bootstrap_schema``
will be removed and the fixture will run ``alembic upgrade head``
against the container instead.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import psycopg
import pytest
import pytest_asyncio
from psycopg_pool import AsyncConnectionPool
from testcontainers.postgres import PostgresContainer

from kanto_common.config import MewSettings

# Hard-coded DDL matching docs/design.md §7. Replaced by Alembic in Task 3.
_BOOTSTRAP_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS isolates (
    accession         TEXT PRIMARY KEY,
    version           INT NOT NULL,
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
    raw_metadata      JSONB
);

CREATE TABLE IF NOT EXISTS genome_embeddings (
    accession     TEXT PRIMARY KEY REFERENCES isolates(accession),
    version       INT NOT NULL,
    model         TEXT NOT NULL,
    model_version TEXT NOT NULL,
    embedding     vector(1152) NOT NULL
);

CREATE INDEX IF NOT EXISTS genome_embeddings_hnsw_idx
    ON genome_embeddings USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS alerts (
    id              BIGSERIAL PRIMARY KEY,
    accession       TEXT REFERENCES isolates(accession),
    version         INT NOT NULL,
    score           DOUBLE PRECISION NOT NULL,
    triggered_at    TIMESTAMPTZ DEFAULT NOW(),
    status          TEXT DEFAULT 'OPEN',
    notes           TEXT
);
"""


def bootstrap_schema(dsn: str) -> None:
    """Apply the design-doc DDL synchronously, once per fresh container."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        for statement in _split_sql(_BOOTSTRAP_SQL):
            conn.execute(statement)


def _split_sql(sql: str) -> list[str]:
    """Split a multi-statement SQL blob on ``;`` while skipping empties.

    Adequate for our tiny bootstrap DDL — not a general-purpose parser.
    """
    return [s.strip() for s in sql.split(";") if s.strip()]


@pytest.fixture(scope="session")
def mew_container() -> Iterator[PostgresContainer]:
    """Start one ``pgvector/pgvector:pg16`` container for the test session."""
    container = PostgresContainer(
        image="pgvector/pgvector:pg16",
        username="kanto",
        password="kanto-test",
        dbname="mew",
    )
    with container:
        # testcontainers exposes a sync DSN we can use immediately.
        dsn = container.get_connection_url(driver=None)
        # Some testcontainers versions return SQLAlchemy-style URIs.
        # Normalize back to the libpq form expected by psycopg.
        if dsn.startswith("postgresql+psycopg2://"):
            dsn = dsn.replace("postgresql+psycopg2://", "postgresql://", 1)
        bootstrap_schema(dsn)
        # Stash the normalized DSN on the container for downstream fixtures.
        container.kanto_dsn = dsn
        yield container


@pytest.fixture(scope="session")
def mew_settings(mew_container: PostgresContainer) -> MewSettings:
    """Settings object pointing at the testcontainer DSN."""
    return MewSettings(
        host=mew_container.get_container_host_ip(),
        port=int(mew_container.get_exposed_port(5432)),
        database="mew",
        user="kanto",
        password="kanto-test",  # type: ignore[arg-type]
        sslmode="disable",
    )


@pytest_asyncio.fixture
async def mew_pool(mew_settings: MewSettings) -> AsyncIterator[AsyncConnectionPool]:
    """Per-test async connection pool with TRUNCATE cleanup before yield.

    Test isolation is via TRUNCATE rather than container restart for
    speed. Tests that need full schema reset can call
    :func:`bootstrap_schema` themselves.
    """
    dsn = mew_settings.dsn()
    pool = AsyncConnectionPool(
        dsn,
        min_size=mew_settings.pool_min_size,
        max_size=mew_settings.pool_max_size,
        open=False,
    )
    await pool.open()
    try:
        # Clean state before each test.
        async with pool.connection() as conn:
            await conn.execute(
                "TRUNCATE alerts, genome_embeddings, isolates RESTART IDENTITY CASCADE"
            )
        yield pool
    finally:
        await pool.close()
