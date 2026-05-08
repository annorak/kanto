"""Mew connection-pool wrapper.

Owns a single :class:`psycopg_pool.AsyncConnectionPool`. Repositories
borrow connections from the pool via :meth:`MewClient.connection` or
participate in a multi-statement transaction via
:meth:`MewClient.transaction`.

pgvector adapter registration runs once per connection via the
configure callback. Without this, writing a Python ``list[float]`` to
a ``vector(N)`` column round-trips as ``str`` and the query silently
inserts garbage.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from pgvector.psycopg import register_vector_async
from psycopg_pool import AsyncConnectionPool

from kanto_commons.config import MewSettings


async def _configure_connection(conn: psycopg.AsyncConnection[Any]) -> None:
    """Per-connection setup: register the pgvector adapter."""
    await register_vector_async(conn)


class MewClient:
    """Async Postgres connection pool with pgvector wired in."""

    def __init__(self, *, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    @classmethod
    async def from_settings(  # pragma: no cover — real DB required
        cls, settings: MewSettings
    ) -> MewClient:
        pool = AsyncConnectionPool(
            settings.dsn(),
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
            configure=_configure_connection,
            open=False,
        )
        await pool.open()
        return cls(pool=pool)

    @property
    def pool(self) -> AsyncConnectionPool:
        return self._pool

    async def close(self) -> None:
        await self._pool.close()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[psycopg.AsyncConnection[Any]]:
        """Borrow one connection from the pool. Autocommit ON."""
        async with self._pool.connection() as conn:
            yield conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[psycopg.AsyncConnection[Any]]:
        """Wrap a block of repository calls in a single transaction.

        Commits on clean exit, rolls back on any exception::

            async with client.transaction() as conn:
                await isolate_repo.upsert(conn, row1)
                await embedding_repo.upsert(conn, row2)
        """
        async with self._pool.connection() as conn, conn.transaction():
            yield conn

    async def healthy(self) -> bool:
        """Cheap probe used by k8s readiness."""
        try:
            async with self._pool.connection() as conn:
                cur = await conn.execute("SELECT 1")
                row = await cur.fetchone()
                return row is not None and row[0] == 1
        except Exception:
            return False
