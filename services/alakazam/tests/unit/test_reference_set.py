"""Tests for the reference-set loader + parser."""

from __future__ import annotations

import io

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from alakazam.reference_set import (
    ReferenceSet,
    from_arrays,
    parse_reference_parquet_bytes,
)

_DIM = 1152


def test_from_arrays_normalizes_rows() -> None:
    rng = np.random.default_rng(0)
    embeddings = rng.standard_normal((4, _DIM)).astype(np.float32) * 7.0
    ref = from_arrays(
        embeddings=embeddings,
        protein_ids=["A", "B", "C", "D"],
        sources=["busco", "kegg", "kegg", "manual"],
    )
    norms = np.linalg.norm(ref.matrix, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5)
    assert ref.size == 4
    assert ref.embedding_dim == _DIM
    assert ref.sources == ("busco", "kegg", "kegg", "manual")


def test_from_arrays_rejects_length_mismatch() -> None:
    embeddings = np.zeros((3, _DIM), dtype=np.float32)
    with pytest.raises(ValueError):
        from_arrays(embeddings=embeddings, protein_ids=["A", "B"])


def test_from_arrays_defaults_sources_to_manual() -> None:
    embeddings = np.eye(2, _DIM, dtype=np.float32)
    ref = from_arrays(embeddings=embeddings, protein_ids=["X", "Y"])
    assert ref.sources == ("manual", "manual")


def test_zero_norm_row_does_not_explode() -> None:
    embeddings = np.zeros((2, _DIM), dtype=np.float32)
    embeddings[1, 0] = 1.0
    ref = from_arrays(embeddings=embeddings, protein_ids=["zero", "unit"])
    # Zero row stays zero (no NaN) and unit row stays unit.
    assert np.linalg.norm(ref.matrix[0]) == 0.0
    assert np.isclose(np.linalg.norm(ref.matrix[1]), 1.0)


def test_invalid_matrix_dtype_rejected() -> None:
    matrix = np.eye(2, _DIM, dtype=np.float64)
    with pytest.raises(ValueError):
        ReferenceSet(matrix=matrix, protein_ids=("a", "b"), sources=("busco", "kegg"))


def test_parse_reference_parquet_bytes(reference_parquet_bytes: bytes) -> None:
    ref = parse_reference_parquet_bytes(reference_parquet_bytes)
    assert ref.size == 5
    norms = np.linalg.norm(ref.matrix, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-3)
    assert "busco" in ref.sources


def test_parse_rejects_zero_rows() -> None:
    schema = pa.schema(
        [
            pa.field("protein_id", pa.string()),
            pa.field("source", pa.string()),
            pa.field("sequence_md5", pa.string()),
            pa.field(
                "embedding",
                pa.list_(pa.field("item", pa.float16(), nullable=False), _DIM),
                nullable=False,
            ),
        ]
    )
    table = pa.Table.from_arrays(
        [
            pa.array([], type=pa.string()),
            pa.array([], type=pa.string()),
            pa.array([], type=pa.string()),
            pa.FixedSizeListArray.from_arrays(pa.array([], type=pa.float16()), _DIM),
        ],
        schema=schema,
    )
    buf = io.BytesIO()
    pq.write_table(table, buf)
    with pytest.raises(ValueError):
        parse_reference_parquet_bytes(buf.getvalue())


def test_parse_rejects_wrong_dim() -> None:
    bad_dim = 64
    schema = pa.schema(
        [
            pa.field("protein_id", pa.string()),
            pa.field("source", pa.string()),
            pa.field("sequence_md5", pa.string()),
            pa.field(
                "embedding",
                pa.list_(pa.field("item", pa.float16(), nullable=False), bad_dim),
                nullable=False,
            ),
        ]
    )
    flat = pa.array(np.zeros(bad_dim, dtype=np.float16), type=pa.float16())
    embed_col = pa.FixedSizeListArray.from_arrays(flat, bad_dim)
    table = pa.Table.from_arrays(
        [
            pa.array(["A"], type=pa.string()),
            pa.array(["busco"], type=pa.string()),
            pa.array(["md5"], type=pa.string()),
            embed_col,
        ],
        schema=schema,
    )
    buf = io.BytesIO()
    pq.write_table(table, buf)
    with pytest.raises(ValueError, match=r"dim 64"):
        parse_reference_parquet_bytes(buf.getvalue())
