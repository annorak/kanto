"""End-to-end pipeline tests against a real Postgres+pgvector container.

Uses the session-scoped fixtures from ``kanto_commons.testing.mew_fixture``,
the same ones the Mew integration tests in kanto-commons rely on. Each
test gets a fresh TRUNCATEd schema so they don't bleed.
"""

from __future__ import annotations

import gzip
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from kanto_commons import IsolateDiscovered
from kanto_commons.mew import IsolateRepository, IsolateStatus, MewClient
from kanto_commons.storage import KeyBuilder
from kanto_commons.testing import FakeObjectStorage
from psycopg_pool import AsyncConnectionPool

from snorlax.downloader import GenomeNotFoundError
from snorlax.metrics import SnorlaxMetrics
from snorlax.modal_client import NoopModalClient
from snorlax.pipeline import FailureCategory, Outcome, Pipeline
from snorlax.prodigal import ProdigalResult
from snorlax.uploader import ProteinUploader

pytestmark = pytest.mark.integration

pytest_plugins = ["kanto_commons.testing.mew_fixture"]


# ---------------------------------------------------------------------------
# Local doubles (re-used in spirit from test_pipeline.py)
# ---------------------------------------------------------------------------


class _FakeDownloader:
    def __init__(self, *, fixture_path: Path, error: Exception | None = None) -> None:
        self._fixture_path = fixture_path
        self._error = error

    def download(self, *, parent_dir_url: str, asm_acc: str, dest: Path) -> Any:
        _ = (parent_dir_url, asm_acc)
        if self._error is not None:
            raise self._error
        dest.write_bytes(self._fixture_path.read_bytes())
        from snorlax.downloader import DownloadedGenome

        return DownloadedGenome(
            local_path=dest,
            source_url="https://fake.test/g.fna.gz",
            size_bytes=dest.stat().st_size,
            md5_hex="abc123" * 5 + "ab",
            checksum_verified=True,
        )


class _FakeProdigal:
    async def run(self, *, genome_fasta_gz: Path, work_dir: Path) -> ProdigalResult:
        _ = genome_fasta_gz
        work_dir.mkdir(parents=True, exist_ok=True)
        out = work_dir / "proteins.faa"
        out.write_text(
            ">prot1 # 1 # 12 # 1 # ID=1_1;\nMKVLAQ*\n>prot2 # 13 # 24 # 1 # ID=1_2;\nMACDEF*\n",
            encoding="utf-8",
        )
        return ProdigalResult(
            protein_fasta_path=out,
            stderr="",
            duration_seconds=0.01,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    accession: str = "PDT_INT_001",
    asm_acc: str = "GCA_000123456.1",
) -> IsolateDiscovered:
    return IsolateDiscovered(
        accession=accession,
        version=1,
        organism="Listeria",
        source="ncbi-pd",
        ftp_path="https://example.test/genomes/all/GCA/000/123/456/",
        discovered_at=datetime.now(UTC),
        metadata={"asm_acc": asm_acc, "snapshot_id": "PDG-int"},
    )


def _build_pipeline(
    *,
    mew: MewClient,
    fixture_path: Path,
    work_dir: Path,
    downloader_error: Exception | None = None,
) -> tuple[Pipeline, FakeObjectStorage, NoopModalClient]:
    storage = FakeObjectStorage()
    kb = KeyBuilder(
        proteins_container="kanto-proteins-int",
        embeddings_container="kanto-embeddings-int",
        metadata_container="kanto-metadata-int",
    )
    uploader = ProteinUploader(storage=storage, key_builder=kb)
    modal = NoopModalClient()
    metrics = SnorlaxMetrics.build(in_flight_callback=lambda: 0)
    pipeline = Pipeline(
        downloader=_FakeDownloader(fixture_path=fixture_path, error=downloader_error),
        prodigal=_FakeProdigal(),
        uploader=uploader,
        modal_client=modal,
        mew=mew,
        key_builder=kb,
        metrics=metrics,
        work_dir=work_dir,
        mew_max_attempts=2,
    )
    return pipeline, storage, modal


async def _read_isolate(mew: MewClient, accession: str) -> Any:
    async with mew.connection() as conn:
        repo = IsolateRepository()
        return await repo.get(conn, accession)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_writes_row_and_uploads(
    mew_pool: AsyncConnectionPool,
    fixtures_dir: Path,
    tmp_work_dir: Path,
) -> None:
    mew = MewClient(pool=mew_pool)
    pipeline, storage, modal = _build_pipeline(
        mew=mew,
        fixture_path=fixtures_dir / "synthetic_genomic.fna.gz",
        work_dir=tmp_work_dir,
    )
    event = _make_event()

    result = await pipeline.process(event)

    assert result.outcome is Outcome.SUCCESS
    row = await _read_isolate(mew, event.accession)
    assert row.status is IsolateStatus.PROTEINS_READY
    assert row.modal_call_id is not None
    assert row.modal_call_id == result.modal_call_id
    assert storage.all_keys("kanto-proteins-int") == [f"{event.accession}/1.faa.gz"]
    assert len(modal.calls) == 1


@pytest.mark.asyncio
async def test_404_persists_qc_failed(
    mew_pool: AsyncConnectionPool,
    fixtures_dir: Path,
    tmp_work_dir: Path,
) -> None:
    mew = MewClient(pool=mew_pool)
    pipeline, _storage, _modal = _build_pipeline(
        mew=mew,
        fixture_path=fixtures_dir / "synthetic_genomic.fna.gz",
        work_dir=tmp_work_dir,
        downloader_error=GenomeNotFoundError("gone"),
    )
    event = _make_event(accession="PDT_INT_002")

    result = await pipeline.process(event)

    assert result.outcome is Outcome.PERMANENT_FAILURE
    assert result.category is FailureCategory.NOT_FOUND
    row = await _read_isolate(mew, event.accession)
    assert row.status is IsolateStatus.QC_FAILED
    assert row.qc_failure_reason is not None
    assert "not_found" in row.qc_failure_reason


@pytest.mark.asyncio
async def test_idempotent_replay(
    mew_pool: AsyncConnectionPool,
    fixtures_dir: Path,
    tmp_work_dir: Path,
) -> None:
    """Processing the same event twice produces the same OS object and Mew row."""
    mew = MewClient(pool=mew_pool)
    pipeline, storage, modal = _build_pipeline(
        mew=mew,
        fixture_path=fixtures_dir / "synthetic_genomic.fna.gz",
        work_dir=tmp_work_dir,
    )
    event = _make_event(accession="PDT_INT_003")

    r1 = await pipeline.process(event)
    r2 = await pipeline.process(event)
    assert r1.outcome is Outcome.SUCCESS
    assert r2.outcome is Outcome.SUCCESS
    # Same OS key — content-addressed on (accession, version).
    assert storage.all_keys("kanto-proteins-int") == [f"{event.accession}/1.faa.gz"]
    # Modal idempotency keys match across invocations.
    assert modal.calls[0].idempotency_key == modal.calls[1].idempotency_key
    # Stored protein bytes still parse as a FASTA.
    blob = storage.get_bytes(container="kanto-proteins-int", key=f"{event.accession}/1.faa.gz")
    assert gzip.decompress(blob).startswith(b">prot1")
