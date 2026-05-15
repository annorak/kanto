"""Shared fixtures for migration tests.

These tests stand up a fresh ``pgvector/pgvector:pg16`` container per
session and apply migrations against it. The container is reused
across tests to keep wall-clock time down; tests that mutate state
clean up after themselves with ``DROP/TRUNCATE`` or use the
``clean_db`` fixture.

All fixtures auto-skip when Docker isn't reachable so contributors
without a local Docker daemon can still run the rest of the test
suite.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import psycopg
import pytest

# Skip the entire suite if Docker isn't available. We import inside the
# try so that the ImportError path is covered even on CI runners that
# happen to have docker-py installed but no daemon.
docker = pytest.importorskip("docker")

try:
    _client = docker.from_env()
    _client.ping()
except Exception as _exc:  # pragma: no cover — exercised only on no-docker CI
    pytest.skip(
        f"Docker daemon not available; skipping migration tests: {_exc}",
        allow_module_level=True,
    )


from kanto_migrations import runner  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402


@pytest.fixture(scope="session")
def pgvector_container() -> Iterator[PostgresContainer]:
    """Start one ``pgvector/pgvector:pg16`` container per test session.

    ``with container:`` handles teardown deterministically even if a
    test crashes mid-suite.
    """
    container = PostgresContainer(
        image="pgvector/pgvector:pg16",
        username="kanto",
        password="kanto-test",
        dbname="mew",
    )
    with container:
        dsn = container.get_connection_url(driver=None)
        if dsn.startswith("postgresql+psycopg2://"):
            dsn = dsn.replace("postgresql+psycopg2://", "postgresql://", 1)
        container.kanto_dsn = dsn  # type: ignore[attr-defined]
        yield container


@pytest.fixture
def clean_db(pgvector_container: PostgresContainer) -> Iterator[str]:
    """Yield a fresh empty schema, restored on test exit.

    Drops any kanto-owned objects from a previous run, then yields
    the DSN. Restoring on exit keeps every test independent without
    paying the cost of re-creating the container.

    The ``DROP EXTENSION`` line is intentional: the migration creates
    it, so a clean baseline must remove it.
    """
    dsn: str = pgvector_container.kanto_dsn  # type: ignore[attr-defined]
    _wipe_schema(dsn)
    try:
        yield dsn
    finally:
        _wipe_schema(dsn)


def _wipe_schema(dsn: str) -> None:
    """Drop all kanto-owned schema objects, leaving an empty database."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        # Tables drop in dependency order; cascades pick up indexes,
        # triggers, sequences. Alembic's version table is dropped
        # explicitly so no migration appears to be applied.
        conn.execute("DROP TABLE IF EXISTS discovery_cursors CASCADE")
        conn.execute("DROP TABLE IF EXISTS alerts CASCADE")
        conn.execute("DROP TABLE IF EXISTS genome_embeddings CASCADE")
        conn.execute("DROP TABLE IF EXISTS isolates CASCADE")
        conn.execute("DROP FUNCTION IF EXISTS set_updated_at() CASCADE")
        conn.execute("DROP TABLE IF EXISTS kanto_alembic_version CASCADE")
        conn.execute("DROP EXTENSION IF EXISTS vector CASCADE")


@pytest.fixture
def applied_db(clean_db: str) -> Iterator[str]:
    """Yield a DSN with all migrations applied to ``head``.

    Most tests want this — a fully-migrated schema they can read
    from. Tests that exercise the migration pipeline itself (apply,
    downgrade, re-apply) use ``clean_db`` instead and call
    :func:`kanto_migrations.runner.upgrade` themselves.
    """
    runner.upgrade(dsn=clean_db)
    try:
        yield clean_db
    finally:
        # Clear the override so subsequent tests start with an empty
        # env var. ``clean_db``'s teardown handles schema cleanup.
        os.environ.pop("KANTO_MEW_DSN_OVERRIDE", None)
