"""Tests for the FASTA parser + truncation policy."""

from __future__ import annotations

import gzip

import pytest

from ditto.errors import FastaParseError
from ditto.fasta import parse_fasta_bytes


def _fasta(records: dict[str, str], *, gzipped: bool) -> bytes:
    text = "\n".join(f">{pid}\n{seq}" for pid, seq in records.items()) + "\n"
    raw = text.encode("ascii")
    return gzip.compress(raw) if gzipped else raw


@pytest.mark.parametrize("gzipped", [True, False])
def test_parse_short_protein(gzipped: bool) -> None:
    data = _fasta({"contig_1_1": "MAGI"}, gzipped=gzipped)
    proteins = parse_fasta_bytes(data, max_sequence_length=100)
    assert len(proteins) == 1
    assert proteins[0].id == "contig_1_1"
    assert proteins[0].sequence_truncated == "MAGI"
    assert proteins[0].original_length == 4
    assert proteins[0].truncated_length == 4
    # MD5 of MAGI
    assert len(proteins[0].sequence_md5) == 32


def test_truncation_policy() -> None:
    data = _fasta({"long": "A" * 3000}, gzipped=True)
    proteins = parse_fasta_bytes(data, max_sequence_length=2048)
    assert len(proteins) == 1
    assert proteins[0].truncated_length == 2048
    assert proteins[0].original_length == 3000
    assert proteins[0].was_truncated is True


def test_short_protein_not_truncated() -> None:
    data = _fasta({"short": "MAGI"}, gzipped=True)
    [p] = parse_fasta_bytes(data, max_sequence_length=2048)
    assert p.was_truncated is False


def test_truncation_does_not_affect_md5() -> None:
    """MD5 is on the pre-truncation sequence."""
    data = _fasta({"long": "A" * 3000}, gzipped=False)
    proteins = parse_fasta_bytes(data, max_sequence_length=100)
    # MD5 of 3000 A's, not 100 A's
    import hashlib

    expected = hashlib.md5(("A" * 3000).encode()).hexdigest()
    assert proteins[0].sequence_md5 == expected


def test_multiple_proteins_preserve_order() -> None:
    records = {f"p{i}": "MAG" for i in range(5)}
    data = _fasta(records, gzipped=True)
    proteins = parse_fasta_bytes(data, max_sequence_length=100)
    assert [p.id for p in proteins] == [f"p{i}" for i in range(5)]


def test_prodigal_style_header_keeps_only_first_token() -> None:
    """Real Prodigal headers look like '>contig_1_3 # 12 # 456 # +1 # ...'."""
    data = b">contig_1_3 # 12 # 456 # +1 # bla\nMAG\n"
    proteins = parse_fasta_bytes(data, max_sequence_length=100)
    assert proteins[0].id == "contig_1_3"


def test_empty_blob_raises() -> None:
    with pytest.raises(FastaParseError, match="empty"):
        parse_fasta_bytes(b"", max_sequence_length=100)


def test_no_records_raises() -> None:
    # Decompressable but only blank lines
    data = b"\n\n\n"
    with pytest.raises(FastaParseError, match="no protein"):
        parse_fasta_bytes(data, max_sequence_length=100)


def test_sequence_before_header_raises() -> None:
    data = b"MAG\n>p1\nGGG\n"
    with pytest.raises(FastaParseError, match="before any header"):
        parse_fasta_bytes(data, max_sequence_length=100)


def test_empty_header_raises() -> None:
    data = b">\nMAG\n"
    with pytest.raises(FastaParseError, match="without identifier"):
        parse_fasta_bytes(data, max_sequence_length=100)


def test_empty_sequence_raises() -> None:
    data = b">p1\n\n>p2\nMAG\n"
    with pytest.raises(FastaParseError, match="empty sequence"):
        parse_fasta_bytes(data, max_sequence_length=100)


def test_corrupt_gzip_raises() -> None:
    # Magic bytes say gzip, but body is garbage
    data = b"\x1f\x8bnotgzip"
    with pytest.raises(FastaParseError, match="decompress"):
        parse_fasta_bytes(data, max_sequence_length=100)
