"""Real-NCBI smoke test for the download + Prodigal + OS upload portion.

Runs against the live ``ftp.ncbi.nlm.nih.gov`` HTTPS endpoint. Marks
the test ``real_ncbi`` so the regular unit/integration runs don't hit
the public infrastructure. Activate with::

    pytest -m real_ncbi tests/integration/test_real_ncbi_smoke.py

Requires:

* Internet egress to ncbi.nlm.nih.gov.
* A Prodigal binary on ``$PATH`` (or ``$SNORLAX_PRODIGAL_BIN``).
  Skipped automatically if Prodigal isn't available locally — operator
  is expected to run the smoke test inside the Snorlax container image
  in CI before declaring a release ready.

The Modal portion is mocked via :class:`NoopModalClient` until Task 7
ships, per task-06 §6.
"""

from __future__ import annotations

import gzip
import os
import shutil
from pathlib import Path

import pytest
from kanto_commons.storage import KeyBuilder
from kanto_commons.testing import FakeObjectStorage

from snorlax.downloader import GenomeDownloader
from snorlax.fasta import (
    count_proteins,
    validate_genome_fasta,
    validate_protein_fasta,
)
from snorlax.modal_client import NoopModalClient
from snorlax.prodigal import ProdigalRunner
from snorlax.uploader import ProteinUploader

pytestmark = pytest.mark.real_ncbi


# A small, stable reference genome from NCBI GenBank — Pseudomonas
# aeruginosa PAO1 reference genome (GCA_000006765.1) is well-known
# and ~6 MB compressed. The accession has been published for a
# decade-plus; it's a low-risk pick for a smoke test.
_TARGET_ACCESSION = "GCA_000006765.1"
_TARGET_PARENT_URL = "https://ftp.ncbi.nlm.nih.gov/genomes/all/GCA/000/006/765/"


def _prodigal_binary() -> str | None:
    explicit = os.environ.get("SNORLAX_PRODIGAL_BIN")
    if explicit:
        return explicit
    return shutil.which("prodigal")


@pytest.mark.slow
@pytest.mark.asyncio
async def test_smoke_download_prodigal_upload(tmp_path: Path) -> None:
    """Download a real genome, run Prodigal, upload to a fake OS bucket.

    Modal is mocked (NoopModalClient). End-to-end coverage of the
    "download + Prodigal + OS upload" portion is the explicit task-06
    DoD item; the Modal portion will join once Task 7 deploys Ditto.
    """
    binary = _prodigal_binary()
    if binary is None:
        pytest.skip(
            "prodigal not on PATH; install bioconda/prodigal or set "
            "SNORLAX_PRODIGAL_BIN to enable the smoke test"
        )

    work = tmp_path / "snorlax-smoke"
    work.mkdir()

    downloader = GenomeDownloader.from_settings(
        timeout_seconds=300.0,
        max_attempts=3,
        chunk_bytes=1024 * 1024,
        max_bytes=200 * 1024 * 1024,
        min_bytes=200 * 1024,  # default
    )
    try:
        downloaded = downloader.download(
            parent_dir_url=_TARGET_PARENT_URL,
            asm_acc=_TARGET_ACCESSION,
            dest=work / "genome.fna.gz",
        )
    finally:
        downloader.close()

    assert downloaded.size_bytes > 1_000_000
    assert downloaded.checksum_verified is True  # NCBI publishes MD5

    stats = validate_genome_fasta(downloaded.local_path)
    assert stats.record_count >= 1
    assert stats.total_residues >= 5_000_000  # P. aeruginosa is ~6.3 Mbp

    prodigal = ProdigalRunner(
        binary=binary,
        mode="single",
        timeout_seconds=300.0,
    )
    result = await prodigal.run(
        genome_fasta_gz=downloaded.local_path,
        work_dir=work / "prodigal",
    )
    proteins = validate_protein_fasta(result.protein_fasta_path)
    # PAO1 has ~5,700 annotated CDS; Prodigal lands near that number.
    assert proteins.record_count > 4_000
    assert count_proteins(result.protein_fasta_path) == proteins.record_count

    # Upload to a fake OS so the smoke test doesn't need Azure creds.
    storage = FakeObjectStorage()
    key_builder = KeyBuilder(
        proteins_container="kanto-proteins-smoke",
        embeddings_container="kanto-embeddings-smoke",
        metadata_container="kanto-metadata-smoke",
    )
    uploader = ProteinUploader(storage=storage, key_builder=key_builder)
    uploaded = uploader.upload(
        accession="SMOKE_PA01",
        version=1,
        protein_fasta_path=result.protein_fasta_path,
        protein_count=proteins.record_count,
        source_md5=downloaded.md5_hex,
    )
    blob = storage.get_bytes(container=uploaded.container, key=uploaded.key)
    # Decompress to verify gzip integrity end-to-end.
    body = gzip.decompress(blob)
    assert body.startswith(b">")

    # Modal portion: mock to confirm the spawn would happen with the
    # right payload. Replace with the real client once Task 7 ships.
    modal = NoopModalClient()
    from datetime import UTC, datetime

    from kanto_commons import ProteinsReady

    payload = ProteinsReady(
        accession="SMOKE_PA01",
        version=1,
        os_key=uploaded.key,
        protein_count=proteins.record_count,
        produced_at=datetime.now(UTC),
    )
    spawned = modal.spawn(payload=payload)
    assert spawned.call_id.startswith("noop-")
