"""End-to-end pipeline integration test against a real Postgres.

Uses ``kanto_commons.testing.mew_pool`` (a session-scoped fixture
that brings up ``pgvector/pgvector:pg16`` via testcontainers and
applies all kanto-migrations). The discovery_cursors table is
required for cursor updates to land; the existing fixture runs
``alembic upgrade head`` which now includes migration ``0002``.

Skipped automatically when Docker isn't available.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import respx

# pytest_asyncio's typed wrappers are not strictly necessary here; the
# tests are plain ``async def`` and ``asyncio_mode=auto`` in the
# package pyproject picks them up.
from kanto_commons.mew.client import MewClient
from kanto_commons.testing import FakeObjectStorage, FakeStreamingClient
from psycopg_pool import AsyncConnectionPool

from growlithe.cursor import DiscoveryCursorRepository
from growlithe.datasource.ncbi import NCBIPathogenDetection
from growlithe.metrics import GrowlitheMetrics
from growlithe.pipeline import Pipeline
from growlithe.snapshot_cache import SnapshotCache

pytestmark = pytest.mark.integration


# Reuse the session-scoped fixtures from kanto-commons.
pytest_plugins = ["kanto_commons.testing.mew_fixture"]


_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "ncbi"
_BASE = "https://example.test/pathogen/Results/"


async def _build_ncbi(timeout: float = 5.0) -> NCBIPathogenDetection:
    return NCBIPathogenDetection.from_config(
        organisms=["Listeria"],
        base_url=_BASE,
        timeout_seconds=timeout,
        max_attempts=3,
    )


def _serve_fixtures() -> respx.MockRouter:
    """Route every NCBI request the pipeline will make to fixture bytes."""
    metadata = (_FIXTURE_DIR / "Listeria_PDG000000001.4703.metadata.tsv").read_bytes()
    exceptions = (_FIXTURE_DIR / "Listeria_PDG000000001.4703.exceptions.tsv").read_bytes()
    listing = (_FIXTURE_DIR / "listing" / "Listeria_latest_kmer_Metadata.html").read_bytes()
    router = respx.mock(base_url=_BASE, assert_all_called=False)
    router.get("Listeria/latest_kmer/Metadata/").respond(200, content=listing)
    router.get("Listeria/latest_kmer/Metadata/PDG000000001.4703.metadata.tsv").respond(
        200, content=metadata
    )
    router.get("Listeria/latest_kmer/Metadata/PDG000000001.4703.exceptions.tsv").respond(
        200, content=exceptions
    )
    return router


async def _build_pipeline(
    mew_pool: AsyncConnectionPool,
) -> tuple[Pipeline, FakeStreamingClient, FakeObjectStorage, NCBIPathogenDetection]:
    storage = FakeObjectStorage()
    streamer = FakeStreamingClient()

    async def _emit(event: object) -> None:
        # The streaming fake's produce takes (topic, value, ...). We
        # serialize like the real producer would but skip the envelope
        # wrap here — the integration test asserts on the count of
        # messages, not the envelope shape.
        from kanto_commons import IsolateDiscovered

        assert isinstance(event, IsolateDiscovered)
        await streamer.produce(
            topic="kanto.discovered",
            key=event.accession.encode("utf-8").decode(),
            value=event.model_dump_json().encode("utf-8"),
        )

    cache = SnapshotCache(storage=storage, container="kanto-metadata-test")
    ncbi = await _build_ncbi()
    pipeline = Pipeline(
        source=ncbi,
        cache=cache,
        mew=MewClient(pool=mew_pool),
        cursors=DiscoveryCursorRepository(),
        emit=_emit,
        metrics=GrowlitheMetrics.build(),
        max_concurrent=4,
    )
    return pipeline, streamer, storage, ncbi


async def test_full_cycle_against_real_postgres(
    mew_pool: AsyncConnectionPool,
) -> None:
    """One full cycle from empty Mew through the testcontainers schema.

    After the cycle: 8 events on the stream (10 rows - 2 QC), and the
    discovery_cursors row records the snapshot ID.
    """
    pipeline, streamer, storage, ncbi = await _build_pipeline(mew_pool)
    try:
        with _serve_fixtures():
            result = await pipeline.run_once()

        assert result.total_emitted == 8
        assert not result.errors

        async with mew_pool.connection() as conn:
            cur = await conn.execute(
                "SELECT source, organism, last_snapshot_id, last_emitted_count "
                "FROM discovery_cursors"
            )
            row = await cur.fetchone()
        assert row == ("ncbi-pd", "Listeria", "PDG000000001.4703", 8)

        # Snapshot is cached so the next cycle can diff against it.
        assert any("PDG000000001.4703" in k for k in storage.all_keys("kanto-metadata-test"))
        # 8 IsolateDiscovered events on the stream.
        assert len(streamer.messages_in("kanto.discovered")) == 8
    finally:
        ncbi.close()


async def test_idempotency_same_snapshot_emits_nothing(
    mew_pool: AsyncConnectionPool,
) -> None:
    """Running the pipeline twice against the same upstream is a no-op."""
    pipeline, streamer, _, ncbi = await _build_pipeline(mew_pool)
    try:
        with _serve_fixtures():
            await pipeline.run_once()
            second = await pipeline.run_once()

        assert second.total_emitted == 0
        assert second.per_organism[0].skipped_unchanged is True
        # The first cycle emitted 8; the second emitted 0; total stays 8.
        assert len(streamer.messages_in("kanto.discovered")) == 8
    finally:
        ncbi.close()
