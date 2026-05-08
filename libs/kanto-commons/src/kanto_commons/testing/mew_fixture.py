"""Pytest fixtures spinning up a Postgres + pgvector container for Mew tests.

The fixtures are session-scoped — one container per test process —
because container start/stop is the slow part. Repository tests share
the same container and clean up between tests via TRUNCATE.

Schema is applied by running ``alembic upgrade head`` against the
container, so these tests exercise the same DDL path the operator
runs against dev and prod. The Alembic runner is imported lazily
inside :func:`bootstrap_schema` so kanto-commons doesn't take a
hard runtime dependency on kanto-migrations.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from psycopg_pool import AsyncConnectionPool
from testcontainers.postgres import PostgresContainer

from kanto_commons.config import MewSettings


def bootstrap_schema(dsn: str) -> None:
    """Apply all Alembic migrations to ``head`` against ``dsn``.

    The import is lazy so kanto-commons stays installable in environments
    where kanto-migrations isn't on the path (e.g. minimal service
    images). Tests that need this fixture must have kanto-migrations as
    a dev dependency in their package's pyproject.
    """
    from kanto_migrations import runner

    runner.upgrade(dsn=dsn)


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

    The ``configure`` callback registers the pgvector adapter on every
    new connection. Without it, vector reads come back as the literal
    string ``"[0.1,0.2,...]"`` and ``EmbeddingRow.model_validate``
    explodes — the same wiring :class:`MewClient.from_settings` relies
    on at runtime.
    """
    # Imported here to avoid a top-level import cycle: client.py imports
    # config which is consumed by the fixtures above.
    from kanto_commons.mew.client import _configure_connection

    dsn = mew_settings.dsn()
    pool = AsyncConnectionPool(
        dsn,
        min_size=mew_settings.pool_min_size,
        max_size=mew_settings.pool_max_size,
        configure=_configure_connection,
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
