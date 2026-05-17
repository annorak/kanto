"""Pipeline orchestration tests — every Section 7 failure mode plus happy path.

We use the in-memory fakes from kanto-commons for OS and a tiny
``FakeMewClient`` here for Mew. Prodigal is stubbed out by a Fake
runner that produces a canned protein FASTA.
"""

from __future__ import annotations

import gzip
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from kanto_commons import IsolateDiscovered
from kanto_commons.mew import IsolateStatus
from kanto_commons.storage import KeyBuilder
from kanto_commons.testing import FakeObjectStorage

from snorlax.downloader import (
    DownloadedGenome,
    GenomeChecksumError,
    GenomeNotFoundError,
    GenomeTooLargeError,
)
from snorlax.fasta import FastaValidationError
from snorlax.metrics import SnorlaxMetrics
from snorlax.modal_client import (
    ModalSpawnError,
    NoopModalClient,
    SpawnedCall,
)
from snorlax.pipeline import (
    FailureCategory,
    Outcome,
    Pipeline,
)
from snorlax.prodigal import (
    ProdigalNonZeroExitError,
    ProdigalResult,
    ProdigalTimeoutError,
)
from snorlax.uploader import ProteinUploader, UploadError

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


_VALID_PROTEIN_FASTA = ">prot1\nMKVL*\n>prot2\nACDEFGHIK*\n"


@dataclass
class FakeDownloader:
    """Stub returning a canned genome or raising the configured error."""

    fixture_path: Path
    error: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def download(
        self,
        *,
        parent_dir_url: str,
        asm_acc: str,
        dest: Path,
    ) -> DownloadedGenome:
        self.calls.append({"parent_dir_url": parent_dir_url, "asm_acc": asm_acc, "dest": dest})
        if self.error is not None:
            raise self.error
        # Copy fixture bytes to dest so downstream stages see a real file.
        dest.write_bytes(self.fixture_path.read_bytes())
        return DownloadedGenome(
            local_path=dest,
            source_url="https://fake.test/genome.fna.gz",
            size_bytes=dest.stat().st_size,
            md5_hex="deadbeef" * 4,
            checksum_verified=True,
        )


@dataclass
class FakeProdigal:
    """Awaitable stub that writes a canned protein FASTA, or raises."""

    error: Exception | None = None
    protein_body: str = _VALID_PROTEIN_FASTA

    async def run(self, *, genome_fasta_gz: Path, work_dir: Path) -> ProdigalResult:
        work_dir.mkdir(parents=True, exist_ok=True)
        if self.error is not None:
            raise self.error
        out = work_dir / "proteins.faa"
        out.write_text(self.protein_body, encoding="utf-8")
        return ProdigalResult(
            protein_fasta_path=out,
            stderr="",
            duration_seconds=0.01,
        )


@dataclass
class FakeUploader:
    """Either delegates to a real ProteinUploader or raises UploadError."""

    inner: ProteinUploader | None
    error: Exception | None = None

    def upload(
        self,
        *,
        accession: str,
        version: int,
        protein_fasta_path: Path,
        protein_count: int,
        source_md5: str | None,
    ) -> Any:
        if self.error is not None:
            raise self.error
        assert self.inner is not None
        return self.inner.upload(
            accession=accession,
            version=version,
            protein_fasta_path=protein_fasta_path,
            protein_count=protein_count,
            source_md5=source_md5,
        )


@dataclass
class FakeMewConnection:
    parent: FakeMewClient

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def execute(self, sql: str, params: Any | None = None) -> None:
        self.parent.executed.append((sql.strip(), params))

    def cursor(self) -> _FakeMewCursor:
        # Some kanto_commons repository methods use ``conn.cursor() ... await
        # cur.execute(...) ... await cur.fetchone()`` for the SQL that needs
        # RETURNING (e.g. IsolateRepository.update_status -> 0-row detect).
        # The fake cursor records the same way and synthesises a result row
        # when the target accession is known to the parent fake.
        return _FakeMewCursor(self)


@dataclass
class _FakeMewCursor:
    conn: FakeMewConnection
    _last_returning_row: tuple[Any, ...] | None = None

    async def __aenter__(self) -> _FakeMewCursor:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def execute(self, sql: str, params: Any | None = None) -> None:
        await self.conn.execute(sql, params)
        if "RETURNING accession" in sql and isinstance(params, dict):
            acc = params.get("accession")
            if acc and acc in self.conn.parent.isolates:
                self._last_returning_row = (acc,)
            else:
                self._last_returning_row = None

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self._last_returning_row


class FakeMewClient:
    """Records SQL executions and tracks status transitions per accession."""

    def __init__(self, *, fail: bool = False) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.fail = fail
        # Snapshot of isolates: accession -> status string + last reason.
        self.isolates: dict[str, dict[str, Any]] = {}

    def connection(self) -> FakeMewConnection:
        if self.fail:
            raise RuntimeError("mew unavailable")
        # We override execute to interpret the canonical statements
        # produced by IsolateRepository — keep them in sync with mew/
        # repositories.py.
        return FakeMewConnection(self)

    async def close(self) -> None:
        return None

    async def healthy(self) -> bool:
        return not self.fail


class _RecordingConnection(FakeMewConnection):
    """Connection that interprets the SQL it sees to update ``parent.isolates``."""

    async def execute(self, sql: str, params: Any | None = None) -> None:
        self.parent.executed.append((sql.strip(), params))
        if not isinstance(params, dict):
            return
        accession = params.get("accession")
        if not accession:
            return
        row = self.parent.isolates.setdefault(accession, {})
        # UPSERT carries every column.
        for key in (
            "version",
            "organism",
            "source",
            "status",
            "qc_failure_reason",
            "modal_call_id",
        ):
            if key in params and params[key] is not None:
                row[key] = params[key]
        if "status" in params and params["status"] is not None:
            row["status"] = params["status"]


def _make_recording_client() -> FakeMewClient:
    client = FakeMewClient()

    def _connection() -> _RecordingConnection:
        return _RecordingConnection(client)

    # Replace the bound method with our recording variant.
    client.connection = _connection  # type: ignore[method-assign]
    return client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def event() -> IsolateDiscovered:
    return IsolateDiscovered(
        accession="PDT000000001.1",
        version=1,
        organism="Listeria",
        source="ncbi-pd",
        ftp_path="https://example.test/genomes/all/GCA/000/123/456/",
        discovered_at=datetime.now(UTC),
        metadata={"asm_acc": "GCA_000123456.1", "snapshot_id": "PDG-test"},
    )


@pytest.fixture
def synthetic_genome(fixtures_dir: Path) -> Path:
    return fixtures_dir / "synthetic_genomic.fna.gz"


def _build_pipeline(
    *,
    downloader: Any,
    prodigal: Any,
    uploader: Any,
    modal_client: Any,
    mew: Any,
    work_dir: Path,
    storage: FakeObjectStorage | None = None,
) -> Pipeline:
    key_builder = KeyBuilder(
        proteins_container="kanto-proteins-test",
        embeddings_container="kanto-embeddings-test",
        metadata_container="kanto-metadata-test",
    )
    _ = storage  # carried by uploader; signature kept for clarity
    metrics = SnorlaxMetrics.build(in_flight_callback=lambda: 0)
    return Pipeline(
        downloader=downloader,
        prodigal=prodigal,
        uploader=uploader,
        modal_client=modal_client,
        mew=mew,
        key_builder=key_builder,
        metrics=metrics,
        work_dir=work_dir,
        mew_max_attempts=2,
    )


def _real_uploader() -> tuple[ProteinUploader, FakeObjectStorage]:
    storage = FakeObjectStorage()
    kb = KeyBuilder(
        proteins_container="kanto-proteins-test",
        embeddings_container="kanto-embeddings-test",
        metadata_container="kanto-metadata-test",
    )
    return ProteinUploader(storage=storage, key_builder=kb), storage


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_end_to_end(
    event: IsolateDiscovered,
    synthetic_genome: Path,
    tmp_work_dir: Path,
) -> None:
    uploader, storage = _real_uploader()
    mew = _make_recording_client()
    modal = NoopModalClient()
    pipeline = _build_pipeline(
        downloader=FakeDownloader(fixture_path=synthetic_genome),
        prodigal=FakeProdigal(),
        uploader=uploader,
        modal_client=modal,
        mew=mew,
        work_dir=tmp_work_dir,
        storage=storage,
    )

    result = await pipeline.process(event)

    assert result.outcome is Outcome.SUCCESS
    assert result.modal_call_id is not None
    assert result.os_key == "PDT000000001.1/1.faa.gz"
    assert result.protein_count == 2

    # Protein FASTA landed in fake OS.
    blob = storage.get_bytes(container="kanto-proteins-test", key=result.os_key)
    assert gzip.decompress(blob).startswith(b">prot1")

    # Mew row went DISCOVERED → PROTEINS_READY → PROTEINS_READY+modal_call_id.
    row = mew.isolates[event.accession]
    assert row["status"] == IsolateStatus.PROTEINS_READY.value
    assert row["modal_call_id"] == result.modal_call_id

    # Exactly one Modal spawn.
    assert len(modal.calls) == 1


# ---------------------------------------------------------------------------
# Section 7 — NCBI 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ncbi_404_is_permanent_qc_failed(
    event: IsolateDiscovered,
    synthetic_genome: Path,
    tmp_work_dir: Path,
) -> None:
    mew = _make_recording_client()
    pipeline = _build_pipeline(
        downloader=FakeDownloader(
            fixture_path=synthetic_genome,
            error=GenomeNotFoundError("gone"),
        ),
        prodigal=FakeProdigal(),
        uploader=FakeUploader(inner=None),
        modal_client=NoopModalClient(),
        mew=mew,
        work_dir=tmp_work_dir,
    )

    result = await pipeline.process(event)

    assert result.outcome is Outcome.PERMANENT_FAILURE
    assert result.category is FailureCategory.NOT_FOUND
    assert mew.isolates[event.accession]["status"] == IsolateStatus.QC_FAILED.value
    assert "not_found" in mew.isolates[event.accession]["qc_failure_reason"]


# ---------------------------------------------------------------------------
# Section 7 — Validation failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_genome_validation_failure_is_qc_failed(
    event: IsolateDiscovered,
    tmp_work_dir: Path,
    tmp_path: Path,
) -> None:
    """The downloaded bytes are HTML — validate_genome_fasta rejects."""
    bad = tmp_path / "html.fna.gz"
    with gzip.open(bad, "wt", encoding="utf-8") as fp:
        fp.write("<html>503 Service Unavailable</html>\n")

    mew = _make_recording_client()
    pipeline = _build_pipeline(
        downloader=FakeDownloader(fixture_path=bad),
        prodigal=FakeProdigal(),
        uploader=FakeUploader(inner=None),
        modal_client=NoopModalClient(),
        mew=mew,
        work_dir=tmp_work_dir,
    )

    result = await pipeline.process(event)

    assert result.outcome is Outcome.PERMANENT_FAILURE
    assert result.category is FailureCategory.VALIDATION_FAILED
    assert mew.isolates[event.accession]["status"] == IsolateStatus.QC_FAILED.value


# ---------------------------------------------------------------------------
# Section 7 — Prodigal failure modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prodigal_failure_routes_to_dlq(
    event: IsolateDiscovered,
    synthetic_genome: Path,
    tmp_work_dir: Path,
) -> None:
    mew = _make_recording_client()
    pipeline = _build_pipeline(
        downloader=FakeDownloader(fixture_path=synthetic_genome),
        prodigal=FakeProdigal(
            error=ProdigalNonZeroExitError(exit_code=1, stderr="bad input"),
        ),
        uploader=FakeUploader(inner=None),
        modal_client=NoopModalClient(),
        mew=mew,
        work_dir=tmp_work_dir,
    )

    result = await pipeline.process(event)

    assert result.outcome is Outcome.DLQ
    assert result.category is FailureCategory.PRODIGAL_FAILED
    assert mew.isolates[event.accession]["status"] == IsolateStatus.QC_FAILED.value


@pytest.mark.asyncio
async def test_prodigal_timeout_routes_to_dlq(
    event: IsolateDiscovered,
    synthetic_genome: Path,
    tmp_work_dir: Path,
) -> None:
    pipeline = _build_pipeline(
        downloader=FakeDownloader(fixture_path=synthetic_genome),
        prodigal=FakeProdigal(error=ProdigalTimeoutError("hung")),
        uploader=FakeUploader(inner=None),
        modal_client=NoopModalClient(),
        mew=_make_recording_client(),
        work_dir=tmp_work_dir,
    )

    result = await pipeline.process(event)
    assert result.outcome is Outcome.DLQ
    assert result.category is FailureCategory.PRODIGAL_TIMEOUT


# ---------------------------------------------------------------------------
# Section 7 — OS upload failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_failure_routes_to_dlq(
    event: IsolateDiscovered,
    synthetic_genome: Path,
    tmp_work_dir: Path,
) -> None:
    pipeline = _build_pipeline(
        downloader=FakeDownloader(fixture_path=synthetic_genome),
        prodigal=FakeProdigal(),
        uploader=FakeUploader(inner=None, error=UploadError("blob 500")),
        modal_client=NoopModalClient(),
        mew=_make_recording_client(),
        work_dir=tmp_work_dir,
    )

    result = await pipeline.process(event)
    assert result.outcome is Outcome.DLQ
    assert result.category is FailureCategory.UPLOAD_FAILED


# ---------------------------------------------------------------------------
# Section 7 — Modal spawn failure
# ---------------------------------------------------------------------------


class _FailingModalClient:
    """ModalClient that always raises ModalSpawnError."""

    def spawn(self, *, payload: Any) -> SpawnedCall:
        raise ModalSpawnError("modal: queue down")

    def healthy(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_modal_spawn_failure_routes_to_dlq_after_upload(
    event: IsolateDiscovered,
    synthetic_genome: Path,
    tmp_work_dir: Path,
) -> None:
    uploader, storage = _real_uploader()
    pipeline = _build_pipeline(
        downloader=FakeDownloader(fixture_path=synthetic_genome),
        prodigal=FakeProdigal(),
        uploader=uploader,
        modal_client=_FailingModalClient(),
        mew=_make_recording_client(),
        work_dir=tmp_work_dir,
        storage=storage,
    )

    result = await pipeline.process(event)
    assert result.outcome is Outcome.DLQ
    assert result.category is FailureCategory.MODAL_SPAWN_FAILED
    # Protein FASTA is in OS — operator can manually re-spawn.
    assert storage.all_keys("kanto-proteins-test") == ["PDT000000001.1/1.faa.gz"]


# ---------------------------------------------------------------------------
# Section 7 — Mew unreachable on entry (transient)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mew_down_yields_transient_retry(
    event: IsolateDiscovered,
    synthetic_genome: Path,
    tmp_work_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mew = FakeMewClient(fail=True)
    pipeline = _build_pipeline(
        downloader=FakeDownloader(fixture_path=synthetic_genome),
        prodigal=FakeProdigal(),
        uploader=FakeUploader(inner=None),
        modal_client=NoopModalClient(),
        mew=mew,
        work_dir=tmp_work_dir,
    )
    # The pipeline's retry sleeps; skip the wait so the test stays quick.
    import asyncio as _asyncio

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(_asyncio, "sleep", _no_sleep)

    result = await pipeline.process(event)
    assert result.outcome is Outcome.TRANSIENT_RETRY
    assert result.category is FailureCategory.MEW_UPDATE_FAILED


# ---------------------------------------------------------------------------
# Section 7 — Missing asm_acc → permanent failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_asm_acc_is_validation_failed(
    synthetic_genome: Path,
    tmp_work_dir: Path,
) -> None:
    event = IsolateDiscovered(
        accession="PDT000000002.1",
        version=1,
        organism="Listeria",
        source="ncbi-pd",
        ftp_path="https://example.test/x/",
        discovered_at=datetime.now(UTC),
        metadata={},  # no asm_acc — contract violation
    )
    mew = _make_recording_client()
    pipeline = _build_pipeline(
        downloader=FakeDownloader(fixture_path=synthetic_genome),
        prodigal=FakeProdigal(),
        uploader=FakeUploader(inner=None),
        modal_client=NoopModalClient(),
        mew=mew,
        work_dir=tmp_work_dir,
    )
    result = await pipeline.process(event)
    assert result.outcome is Outcome.PERMANENT_FAILURE
    assert result.category is FailureCategory.VALIDATION_FAILED


# ---------------------------------------------------------------------------
# Section 7 — Download too large is permanent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_too_large_is_permanent(
    event: IsolateDiscovered,
    synthetic_genome: Path,
    tmp_work_dir: Path,
) -> None:
    pipeline = _build_pipeline(
        downloader=FakeDownloader(
            fixture_path=synthetic_genome,
            error=GenomeTooLargeError("12 GB"),
        ),
        prodigal=FakeProdigal(),
        uploader=FakeUploader(inner=None),
        modal_client=NoopModalClient(),
        mew=_make_recording_client(),
        work_dir=tmp_work_dir,
    )
    result = await pipeline.process(event)
    assert result.outcome is Outcome.PERMANENT_FAILURE
    assert result.category is FailureCategory.DOWNLOAD_TOO_LARGE


# ---------------------------------------------------------------------------
# Section 7 — Transient download exhaustion → TRANSIENT_RETRY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_download_exhaustion_yields_transient_retry(
    event: IsolateDiscovered,
    synthetic_genome: Path,
    tmp_work_dir: Path,
) -> None:
    pipeline = _build_pipeline(
        downloader=FakeDownloader(
            fixture_path=synthetic_genome,
            error=GenomeChecksumError("hash mismatch"),
        ),
        prodigal=FakeProdigal(),
        uploader=FakeUploader(inner=None),
        modal_client=NoopModalClient(),
        mew=_make_recording_client(),
        work_dir=tmp_work_dir,
    )
    result = await pipeline.process(event)
    assert result.outcome is Outcome.TRANSIENT_RETRY
    assert result.category is FailureCategory.DOWNLOAD_TRANSIENT_EXHAUSTED


# ---------------------------------------------------------------------------
# Sanity — FastaValidationError raised by *Prodigal output* maps to DLQ
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_garbage_protein_fasta_routes_to_dlq(
    event: IsolateDiscovered,
    synthetic_genome: Path,
    tmp_work_dir: Path,
) -> None:
    """Prodigal exits 0 but emits non-protein content — DLQ."""
    pipeline = _build_pipeline(
        downloader=FakeDownloader(fixture_path=synthetic_genome),
        prodigal=FakeProdigal(protein_body=">x\n12345\n"),  # digits not valid
        uploader=FakeUploader(inner=None),
        modal_client=NoopModalClient(),
        mew=_make_recording_client(),
        work_dir=tmp_work_dir,
    )
    result = await pipeline.process(event)
    assert result.outcome is Outcome.DLQ
    assert result.category is FailureCategory.PRODIGAL_FAILED


# ---------------------------------------------------------------------------
# In-flight gauge advances during processing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_flight_returns_to_zero(
    event: IsolateDiscovered,
    synthetic_genome: Path,
    tmp_work_dir: Path,
) -> None:
    uploader, storage = _real_uploader()
    pipeline = _build_pipeline(
        downloader=FakeDownloader(fixture_path=synthetic_genome),
        prodigal=FakeProdigal(),
        uploader=uploader,
        modal_client=NoopModalClient(),
        mew=_make_recording_client(),
        work_dir=tmp_work_dir,
        storage=storage,
    )
    assert pipeline.in_flight == 0
    await pipeline.process(event)
    assert pipeline.in_flight == 0


# Keep a unused-import suppressor for FastaValidationError so mypy/linters
# don't object on this test file alone.
_ = FastaValidationError
