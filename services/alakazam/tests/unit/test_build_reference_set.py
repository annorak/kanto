"""Tests for the reference-set builder helpers."""

from __future__ import annotations

import io

import numpy as np
import pyarrow.parquet as pq
import pytest

from alakazam.reference_set import parse_reference_parquet_bytes
from alakazam.scripts.build_reference_set import (
    build_parquet_bytes,
    md5_of,
    parse_fasta,
    parse_sources_tsv,
)

_DIM = 1152


def test_parse_fasta_simple() -> None:
    text = ">P1 some desc\nACDEFG\n>P2\nGHIK\nLMNP\n"
    rows = list(parse_fasta(text))
    assert rows == [("P1", "ACDEFG"), ("P2", "GHIKLMNP")]


def test_parse_fasta_rejects_orphan_sequence() -> None:
    text = "ACDE\n>P1\nGHIK"
    with pytest.raises(ValueError):
        list(parse_fasta(text))


def test_parse_sources_tsv() -> None:
    text = "protein_id\tsource\nP1\tbusco\nP2\tkegg\n# comment\nP3\tmanual\n"
    sources = parse_sources_tsv(text)
    assert sources == {"P1": "busco", "P2": "kegg", "P3": "manual"}


def test_parse_sources_tsv_rejects_unknown_source() -> None:
    text = "P1\tbogus\n"
    with pytest.raises(ValueError):
        parse_sources_tsv(text)


def test_md5_is_stable() -> None:
    assert md5_of("ACDE") == md5_of("ACDE")
    assert md5_of("ACDE") != md5_of("ACDF")


def test_build_parquet_roundtrips_through_reference_loader() -> None:
    n = 3
    embeddings = np.eye(n, _DIM, dtype=np.float16)
    proteins = [(f"P{i}", md5_of(f"SEQ{i}"), "busco") for i in range(n)]
    raw = build_parquet_bytes(proteins, embeddings)

    # The reference-set parser should accept the builder's output.
    ref = parse_reference_parquet_bytes(raw)
    assert ref.size == n
    assert ref.sources == ("busco", "busco", "busco")

    # Sanity-check the schema directly too.
    table = pq.read_table(io.BytesIO(raw))
    assert set(table.column_names) == {"protein_id", "source", "sequence_md5", "embedding"}


def test_build_parquet_validates_shape() -> None:
    n = 2
    embeddings = np.eye(n, _DIM, dtype=np.float16)
    proteins = [("P", "md5", "busco")]  # length mismatch
    with pytest.raises(ValueError):
        build_parquet_bytes(proteins, embeddings)
