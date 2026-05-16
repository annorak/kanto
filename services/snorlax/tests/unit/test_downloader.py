"""GenomeDownloader tests with respx-mocked NCBI."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest
import respx

from snorlax.downloader import (
    GenomeChecksumError,
    GenomeDownloader,
    GenomeNotFoundError,
    GenomeTooLargeError,
    _find_assembly_subdir,
    _find_genomic_filename,
    _parse_assembly_accession,
    _parse_md5_for,
)

_BASE = "https://example.test/genomes/all/GCA/000/123/456/"
_ASM_ACC = "GCA_000123456.1"
_ASM_DIR = "GCA_000123456.1_KantoTestAssembly"
_ASM_URL = f"{_BASE}{_ASM_DIR}/"
_GENOME_NAME = f"{_ASM_DIR}_genomic.fna.gz"
_GENOME_URL = f"{_ASM_URL}{_GENOME_NAME}"


def _load(filename: str, fixtures_dir: Path) -> bytes:
    return (fixtures_dir / filename).read_bytes()


@pytest.fixture
def downloader() -> GenomeDownloader:
    # Bypass the from_settings path so tests don't open a real client
    # against the internet; build the httpx.Client inline.
    client = httpx.Client(timeout=httpx.Timeout(5.0))
    return GenomeDownloader(
        client=client,
        max_attempts=3,
        chunk_bytes=8192,
        max_bytes=10 * 1024 * 1024,
        min_bytes=1_000,  # small enough that our 9 KB fixture passes
    )


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------


def test_parse_assembly_accession_ok() -> None:
    numeric, version = _parse_assembly_accession("GCA_000123456.2")
    assert numeric == "000123456"
    assert version == "2"


def test_parse_assembly_accession_bad_prefix() -> None:
    with pytest.raises(GenomeNotFoundError):
        _parse_assembly_accession("GCF_000123456.1")


def test_parse_assembly_accession_missing_version() -> None:
    with pytest.raises(GenomeNotFoundError):
        _parse_assembly_accession("GCA_000123456")


def test_find_assembly_subdir(fixtures_dir: Path) -> None:
    html = _load("parent_listing.html", fixtures_dir).decode("utf-8")
    name = _find_assembly_subdir(html, numeric="000123456", version="1")
    assert name == "GCA_000123456.1_KantoTestAssembly"


def test_find_assembly_subdir_picks_correct_version(fixtures_dir: Path) -> None:
    html = _load("parent_listing.html", fixtures_dir).decode("utf-8")
    name = _find_assembly_subdir(html, numeric="000123456", version="2")
    assert name == "GCA_000123456.2_KantoTestAssemblyV2"


def test_find_genomic_filename(fixtures_dir: Path) -> None:
    html = _load("assembly_listing.html", fixtures_dir).decode("utf-8")
    assert _find_genomic_filename(html) == _GENOME_NAME


def test_parse_md5_present(fixtures_dir: Path) -> None:
    text = _load("md5checksums.txt", fixtures_dir).decode("utf-8")
    md5 = _parse_md5_for(text, _GENOME_NAME)
    assert md5 is not None
    assert len(md5) == 32


def test_parse_md5_missing() -> None:
    text = "deadbeef  ./other.txt\n"
    assert _parse_md5_for(text, "ours.txt") is None


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_download_happy_path(
    downloader: GenomeDownloader,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    genome_bytes = _load("synthetic_genomic.fna.gz", fixtures_dir)
    md5_text = _load("md5checksums.txt", fixtures_dir).decode("utf-8")

    with respx.mock(assert_all_called=False) as router:
        router.get(_BASE).respond(200, content=_load("parent_listing.html", fixtures_dir))
        router.get(_ASM_URL).respond(200, content=_load("assembly_listing.html", fixtures_dir))
        router.get(f"{_ASM_URL}md5checksums.txt").respond(200, content=md5_text)
        router.get(_GENOME_URL).respond(200, content=genome_bytes)

        dest = tmp_path / "genome.fna.gz"
        result = downloader.download(
            parent_dir_url=_BASE,
            asm_acc=_ASM_ACC,
            dest=dest,
        )

    assert result.local_path == dest
    assert dest.read_bytes() == genome_bytes
    assert result.checksum_verified is True
    assert result.md5_hex == hashlib.md5(genome_bytes).hexdigest()


def test_download_succeeds_without_md5(
    downloader: GenomeDownloader,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """Missing md5checksums.txt is best-effort; the download still works."""
    genome_bytes = _load("synthetic_genomic.fna.gz", fixtures_dir)

    with respx.mock(assert_all_called=False) as router:
        router.get(_BASE).respond(200, content=_load("parent_listing.html", fixtures_dir))
        router.get(_ASM_URL).respond(200, content=_load("assembly_listing.html", fixtures_dir))
        router.get(f"{_ASM_URL}md5checksums.txt").respond(404)
        router.get(_GENOME_URL).respond(200, content=genome_bytes)

        result = downloader.download(
            parent_dir_url=_BASE,
            asm_acc=_ASM_ACC,
            dest=tmp_path / "g.fna.gz",
        )

    assert result.checksum_verified is False


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_download_404_parent(
    downloader: GenomeDownloader,
    tmp_path: Path,
) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(_BASE).respond(404)
        with pytest.raises(GenomeNotFoundError):
            downloader.download(
                parent_dir_url=_BASE,
                asm_acc=_ASM_ACC,
                dest=tmp_path / "g.fna.gz",
            )


def test_download_assembly_dir_not_listed(
    downloader: GenomeDownloader,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """Parent dir exists but doesn't list our accession version."""
    with respx.mock(assert_all_called=False) as router:
        router.get(_BASE).respond(200, content=_load("parent_listing.html", fixtures_dir))
        with pytest.raises(GenomeNotFoundError):
            downloader.download(
                parent_dir_url=_BASE,
                asm_acc="GCA_000123456.99",  # not in fixture
                dest=tmp_path / "g.fna.gz",
            )


def test_download_md5_mismatch(
    downloader: GenomeDownloader,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """A wrong md5 is treated as transient — retries exhaust and raise."""
    genome_bytes = _load("synthetic_genomic.fna.gz", fixtures_dir)
    bad_md5_text = f"00000000000000000000000000000000  ./{_GENOME_NAME}\n"
    with respx.mock(assert_all_called=False) as router:
        router.get(_BASE).respond(200, content=_load("parent_listing.html", fixtures_dir))
        router.get(_ASM_URL).respond(200, content=_load("assembly_listing.html", fixtures_dir))
        router.get(f"{_ASM_URL}md5checksums.txt").respond(200, content=bad_md5_text)
        router.get(_GENOME_URL).respond(200, content=genome_bytes)

        with pytest.raises(GenomeChecksumError):
            downloader.download(
                parent_dir_url=_BASE,
                asm_acc=_ASM_ACC,
                dest=tmp_path / "g.fna.gz",
            )


def test_download_too_large(
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """A body larger than max_bytes blows up mid-stream."""
    client = httpx.Client(timeout=httpx.Timeout(5.0))
    small_cap = GenomeDownloader(
        client=client,
        max_attempts=2,
        chunk_bytes=4096,
        max_bytes=1024,  # genome fixture is ~9 KB
        min_bytes=1,
    )
    genome_bytes = _load("synthetic_genomic.fna.gz", fixtures_dir)
    with respx.mock(assert_all_called=False) as router:
        router.get(_BASE).respond(200, content=_load("parent_listing.html", fixtures_dir))
        router.get(_ASM_URL).respond(200, content=_load("assembly_listing.html", fixtures_dir))
        router.get(f"{_ASM_URL}md5checksums.txt").respond(404)
        router.get(_GENOME_URL).respond(200, content=genome_bytes)

        with pytest.raises(GenomeTooLargeError):
            small_cap.download(
                parent_dir_url=_BASE,
                asm_acc=_ASM_ACC,
                dest=tmp_path / "g.fna.gz",
            )
