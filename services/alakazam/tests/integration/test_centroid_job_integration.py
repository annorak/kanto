"""Integration test: the centroid recomputation job end-to-end against Mew."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import pytest
import pytest_asyncio
from kanto_commons.mew.client import MewClient
from kanto_commons.mew.models import EmbeddingRow, IsolateRow, IsolateStatus
from kanto_commons.mew.repositories import EmbeddingRepository, IsolateRepository
from psycopg_pool import AsyncConnectionPool

from alakazam.config import AlakazamServiceSettings, AlakazamSettings
from alakazam.mew_gateway import AlakazamMewGateway
from alakazam.scripts.species_centroids_job import run

pytest_plugins = ["kanto_commons.testing.mew_fixture"]

pytestmark = pytest.mark.integration

_DIM = 1152


@pytest_asyncio.fixture
async def mew_client(mew_pool: AsyncConnectionPool) -> Any:
    return MewClient(pool=mew_pool)


async def _seed_population(
    pool: AsyncConnectionPool,
    *,
    organism: str,
    n: int,
) -> np.ndarray:
    """Insert ``n`` isolates with random embeddings for ``organism``."""
    rng = np.random.default_rng(seed=hash(organism) & 0xFFFF)
    rows = rng.standard_normal((n, _DIM)).astype(np.float32)
    async with pool.connection() as conn:
        for i, vec in enumerate(rows):
            accession = f"PDT_{organism}_{i:04d}"
            await IsolateRepository().upsert(
                conn,
                IsolateRow(
                    accession=accession,
                    version=1,
                    organism=organism,
                    source="ncbi-pd",
                    status=IsolateStatus.EMBEDDED,
                    discovered_at=datetime.now(UTC),
                ),
            )
            await EmbeddingRepository().upsert(
                conn,
                EmbeddingRow(
                    accession=accession,
                    version=1,
                    model="esm-c-600m",
                    model_version="1.0.0",
                    embedding=vec.tolist(),
                ),
            )
    return rows


async def test_centroid_job_writes_correct_centroid(
    mew_pool: AsyncConnectionPool,
    mew_client: MewClient,
    mew_settings: Any,
) -> None:
    rows = await _seed_population(mew_pool, organism="Salmonella", n=30)

    # Build a minimal AlakazamSettings rooted on the testcontainer's Mew.
    # We need the centroid job's run() to load *its* MewClient against the
    # same DSN, so monkey-patch ``MewClient.from_settings`` into the
    # process-wide pool. Easier path: call _compute_one_centroid +
    # upsert_species_centroid directly via the gateway, which the unit
    # test covers; here we exercise the full ``run()`` loop by using a
    # custom AlakazamSettings whose mew.dsn() returns the container DSN.
    settings = AlakazamSettings(
        mew=mew_settings,
        object_storage=_dummy_os_settings(),
        streaming=_dummy_streaming_settings(),
        tracing=_dummy_tracing_settings(),
        alakazam=AlakazamServiceSettings(centroid_min_isolates=10),
    )

    await run(settings, dry_run=False, organisms=["Salmonella"])

    gateway = AlakazamMewGateway(mew=mew_client)
    rec = await gateway.get_species_centroid("Salmonella")
    assert rec is not None
    assert rec.n_isolates == 30
    centroid_np = np.asarray(rec.centroid, dtype=np.float32)
    expected_mean = rows.mean(axis=0)
    assert np.allclose(centroid_np, expected_mean, atol=1e-4)
    var_np = np.asarray(rec.covariance_diagonal, dtype=np.float32)
    expected_var = rows.var(axis=0)
    assert np.allclose(var_np, expected_var, atol=1e-4)


async def test_centroid_job_dry_run_does_not_write(
    mew_pool: AsyncConnectionPool,
    mew_client: MewClient,
    mew_settings: Any,
) -> None:
    await _seed_population(mew_pool, organism="Listeria", n=15)
    settings = AlakazamSettings(
        mew=mew_settings,
        object_storage=_dummy_os_settings(),
        streaming=_dummy_streaming_settings(),
        tracing=_dummy_tracing_settings(),
        alakazam=AlakazamServiceSettings(centroid_min_isolates=10),
    )
    outcomes = await run(settings, dry_run=True, organisms=["Listeria"])
    assert outcomes == {"Listeria": 15}
    gateway = AlakazamMewGateway(mew=mew_client)
    rec = await gateway.get_species_centroid("Listeria")
    assert rec is None


# ---------------------------------------------------------------------------
# Minimal dummy settings sections so we can build AlakazamSettings without
# the live env vars the production loader expects.
# ---------------------------------------------------------------------------


def _dummy_os_settings() -> Any:
    from kanto_commons.config import ObjectStorageSettings

    return ObjectStorageSettings(account_url="https://example.blob.core.windows.net")


def _dummy_streaming_settings() -> Any:
    from kanto_commons.config import StreamingSettings

    return StreamingSettings(bootstrap_servers="localhost:9092")


def _dummy_tracing_settings() -> Any:
    from kanto_commons.config import TracingSettings

    return TracingSettings()
