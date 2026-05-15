"""NCBI :class:`DataSource` tests against committed fixtures.

The adapter does network I/O via :class:`httpx.Client`; we replace
that with the ``respx`` mock transport so tests are deterministic
and don't reach the real NCBI FTP.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
import respx

from growlithe.datasource.ncbi import (
    GENBANK_ASSEMBLY_BASE,
    SOURCE_ID,
    NCBIPathogenDetection,
    NoSnapshotAvailableError,
    _genbank_assembly_dir_url,
    _is_transient,
)

_BASE = "https://example.test/pathogen/Results/"


@pytest.fixture
def ncbi(ncbi_fixture_dir: Path) -> NCBIPathogenDetection:
    """Adapter wired to a respx-controlled httpx.Client at ``_BASE``."""
    client = httpx.Client(timeout=httpx.Timeout(5.0))
    return NCBIPathogenDetection(
        organisms=["Listeria"],
        base_url=_BASE,
        client=client,
        max_attempts=3,
    )


def _read(ncbi_fixture_dir: Path, name: str) -> bytes:
    return (ncbi_fixture_dir / name).read_bytes()


@respx.mock
def test_lists_current_snapshot_from_listing(
    ncbi: NCBIPathogenDetection, ncbi_fixture_dir: Path
) -> None:
    listing_html = _read(ncbi_fixture_dir, "listing/Listeria_latest_kmer_Metadata.html")
    respx.get(_BASE + "Listeria/latest_kmer/Metadata/").mock(
        return_value=httpx.Response(200, content=listing_html)
    )
    refs = ncbi.list_current_snapshots()
    assert len(refs) == 1
    ref = refs[0]
    assert ref.organism == "Listeria"
    assert ref.snapshot_id == "PDG000000001.4703"
    assert ref.metadata_url.endswith("PDG000000001.4703.metadata.tsv")
    assert (ref.exceptions_url or "").endswith("PDG000000001.4703.exceptions.tsv")


@respx.mock
def test_list_snapshot_returns_empty_when_no_metadata_file(
    ncbi: NCBIPathogenDetection,
) -> None:
    """A directory listing without a metadata TSV is logged-and-skipped."""
    respx.get(_BASE + "Listeria/latest_kmer/Metadata/").mock(
        return_value=httpx.Response(
            200,
            text="""<html><body><pre><a href="/parent/">..</a></pre></body></html>""",
        )
    )
    assert ncbi.list_current_snapshots() == []


@respx.mock
def test_fetch_snapshot_reads_metadata_and_exceptions(
    ncbi: NCBIPathogenDetection, ncbi_fixture_dir: Path
) -> None:
    metadata = _read(ncbi_fixture_dir, "Listeria_PDG000000001.4703.metadata.tsv")
    exceptions = _read(ncbi_fixture_dir, "Listeria_PDG000000001.4703.exceptions.tsv")
    respx.get(re.compile(r".*\.metadata\.tsv$")).mock(
        return_value=httpx.Response(200, content=metadata)
    )
    respx.get(re.compile(r".*\.exceptions\.tsv$")).mock(
        return_value=httpx.Response(200, content=exceptions)
    )

    from growlithe.datasource import SnapshotRef

    ref = SnapshotRef(
        organism="Listeria",
        snapshot_id="PDG000000001.4703",
        metadata_url=_BASE + "Listeria/latest_kmer/Metadata/PDG000000001.4703.metadata.tsv",
        exceptions_url=_BASE + "Listeria/latest_kmer/Metadata/PDG000000001.4703.exceptions.tsv",
    )
    snap = ncbi.fetch_snapshot(ref)
    assert snap.metadata_tsv == metadata
    assert snap.exceptions_tsv == exceptions


@respx.mock
def test_fetch_snapshot_treats_missing_exceptions_as_none(
    ncbi: NCBIPathogenDetection, ncbi_fixture_dir: Path
) -> None:
    metadata = _read(ncbi_fixture_dir, "Listeria_PDG000000001.4703.metadata.tsv")
    respx.get(re.compile(r".*\.metadata\.tsv$")).mock(
        return_value=httpx.Response(200, content=metadata)
    )
    respx.get(re.compile(r".*\.exceptions\.tsv$")).mock(return_value=httpx.Response(404))

    from growlithe.datasource import SnapshotRef

    ref = SnapshotRef(
        organism="Listeria",
        snapshot_id="PDG000000001.4703",
        metadata_url=_BASE + "Listeria/latest_kmer/Metadata/PDG000000001.4703.metadata.tsv",
        exceptions_url=_BASE + "Listeria/latest_kmer/Metadata/PDG000000001.4703.exceptions.tsv",
    )
    snap = ncbi.fetch_snapshot(ref)
    assert snap.exceptions_tsv is None


@respx.mock
def test_transient_5xx_is_retried_then_succeeds(
    ncbi: NCBIPathogenDetection, ncbi_fixture_dir: Path
) -> None:
    listing_html = _read(ncbi_fixture_dir, "listing/Listeria_latest_kmer_Metadata.html")
    route = respx.get(_BASE + "Listeria/latest_kmer/Metadata/").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, content=listing_html),
        ]
    )
    refs = ncbi.list_current_snapshots()
    assert route.call_count == 3
    assert len(refs) == 1


def test_parse_rows_emits_diff_only(ncbi: NCBIPathogenDetection, ncbi_fixture_dir: Path) -> None:
    from growlithe.datasource import FetchedSnapshot, SnapshotRef

    metadata = _read(ncbi_fixture_dir, "Listeria_PDG000000001.4703.metadata.tsv")
    prev_metadata = _read(ncbi_fixture_dir, "Listeria_PDG000000001.4702.metadata.tsv")
    exceptions = _read(ncbi_fixture_dir, "Listeria_PDG000000001.4703.exceptions.tsv")
    ref = SnapshotRef(
        organism="Listeria",
        snapshot_id="PDG000000001.4703",
        metadata_url=_BASE + "x",
        exceptions_url=_BASE + "y",
    )
    snap = FetchedSnapshot(ref=ref, metadata_tsv=metadata, exceptions_tsv=exceptions)
    parsed = ncbi.parse_rows(snap, previous_metadata_tsv=prev_metadata)

    accs = sorted(e.accession for e in parsed.events)
    # 10 total in fixture - 5 previously seen - 2 QC = 3 new
    assert accs == ["PDT000000016.4", "PDT000000019.11", "PDT000000021.3"]
    assert parsed.qc_filtered == 2
    for event in parsed.events:
        assert event.source == SOURCE_ID
        assert event.organism == "Listeria"
        assert event.metadata["snapshot_id"] == "PDG000000001.4703"
        assert event.metadata["asm_acc"].startswith("GCA_")
        assert event.ftp_path.startswith(GENBANK_ASSEMBLY_BASE)


def test_parse_rows_cold_start_emits_everything(
    ncbi: NCBIPathogenDetection, ncbi_fixture_dir: Path
) -> None:
    """No previous snapshot → every non-filtered isolate is new."""
    from growlithe.datasource import FetchedSnapshot, SnapshotRef

    metadata = _read(ncbi_fixture_dir, "Listeria_PDG000000001.4703.metadata.tsv")
    exceptions = _read(ncbi_fixture_dir, "Listeria_PDG000000001.4703.exceptions.tsv")
    ref = SnapshotRef(
        organism="Listeria",
        snapshot_id="PDG000000001.4703",
        metadata_url="",
        exceptions_url=None,
    )
    snap = FetchedSnapshot(ref=ref, metadata_tsv=metadata, exceptions_tsv=exceptions)
    parsed = ncbi.parse_rows(snap, previous_metadata_tsv=None)
    assert len(parsed.events) == 8


# ---------------------------------------------------------------------------
# URL builders + transient classification
# ---------------------------------------------------------------------------


def test_genbank_assembly_dir_url_splits_numeric() -> None:
    assert _genbank_assembly_dir_url("GCA_004104335.1") == GENBANK_ASSEMBLY_BASE + "004/104/335/"


def test_genbank_assembly_dir_url_fallback_on_unexpected_format() -> None:
    assert _genbank_assembly_dir_url("ENA_FOOBAR.1").startswith(GENBANK_ASSEMBLY_BASE)


def test_is_transient_classification() -> None:
    assert _is_transient(httpx.ConnectError("boom"))
    assert _is_transient(httpx.ReadTimeout("slow"))
    fake_resp = httpx.Response(503, request=httpx.Request("GET", "https://x"))
    assert _is_transient(
        httpx.HTTPStatusError("5xx", request=fake_resp.request, response=fake_resp)
    )
    fake_resp_400 = httpx.Response(400, request=httpx.Request("GET", "https://x"))
    assert not _is_transient(
        httpx.HTTPStatusError("4xx", request=fake_resp_400.request, response=fake_resp_400)
    )
    assert not _is_transient(NoSnapshotAvailableError("missing"))
