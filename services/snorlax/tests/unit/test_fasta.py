"""FASTA validation and helper tests."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from snorlax.fasta import (
    FastaValidationError,
    count_proteins,
    validate_genome_fasta,
    validate_protein_fasta,
)


def _write(tmp_path: Path, name: str, body: str, *, gzipped: bool) -> Path:
    p = tmp_path / name
    if gzipped:
        with gzip.open(p, "wt", encoding="utf-8") as fp:
            fp.write(body)
    else:
        p.write_text(body, encoding="utf-8")
    return p


def test_validate_genome_fasta_plain(tmp_path: Path) -> None:
    body = ">chr1\nACGTACGT\nACGT\n>chr2\nACGTACGT\n"
    path = _write(tmp_path, "ok.fna", body, gzipped=False)
    stats = validate_genome_fasta(path)
    assert stats.record_count == 2
    assert stats.total_residues == 20


def test_validate_genome_fasta_gzipped(tmp_path: Path) -> None:
    body = ">chr1\nACGTNNNN\n"
    path = _write(tmp_path, "ok.fna.gz", body, gzipped=True)
    stats = validate_genome_fasta(path)
    assert stats.record_count == 1
    assert stats.total_residues == 8


def test_validate_genome_fasta_rejects_html(tmp_path: Path) -> None:
    """An HTML error page is the classic "we got the wrong thing" failure."""
    path = _write(tmp_path, "html.fna", "<html><body>503</body></html>\n", gzipped=False)
    with pytest.raises(FastaValidationError):
        validate_genome_fasta(path)


def test_validate_genome_fasta_rejects_bad_alphabet(tmp_path: Path) -> None:
    path = _write(tmp_path, "bad.fna", ">x\nACGZQQQ\n", gzipped=False)
    with pytest.raises(FastaValidationError):
        validate_genome_fasta(path)


def test_validate_genome_fasta_rejects_empty(tmp_path: Path) -> None:
    path = _write(tmp_path, "empty.fna", "", gzipped=False)
    with pytest.raises(FastaValidationError):
        validate_genome_fasta(path)


def test_validate_protein_fasta_ok(tmp_path: Path) -> None:
    body = ">prot1 example\nMKVL*\n>prot2\nACDEFGHIKLMNPQRSTVWY\n"
    path = _write(tmp_path, "p.faa", body, gzipped=False)
    stats = validate_protein_fasta(path)
    assert stats.record_count == 2


def test_validate_protein_fasta_rejects_digits(tmp_path: Path) -> None:
    path = _write(tmp_path, "bad.faa", ">p\nMKV123\n", gzipped=False)
    with pytest.raises(FastaValidationError):
        validate_protein_fasta(path)


def test_count_proteins(tmp_path: Path) -> None:
    body = ">a\nMKVL\n>b\nMM\n>c\nA*\n"
    path = _write(tmp_path, "x.faa", body, gzipped=False)
    assert count_proteins(path) == 3
