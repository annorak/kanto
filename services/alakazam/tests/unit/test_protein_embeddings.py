"""Tests for the per-protein parquet reader used by the coverage strategy."""

from __future__ import annotations

import numpy as np
import pytest
from kanto_commons.testing.fakes import FakeObjectStorage

from alakazam.protein_embeddings import (
    ProteinEmbeddingsNotFoundError,
    load_protein_embeddings,
    parse_protein_embedding_bytes,
)
from tests.conftest import make_protein_parquet_bytes

_DIM = 1152


def test_parse_normalizes_rows() -> None:
    rng = np.random.default_rng(7)
    embeddings = rng.standard_normal((4, _DIM)).astype(np.float16) * 3.0
    parquet = make_protein_parquet_bytes(embeddings)
    proteins = parse_protein_embedding_bytes(parquet, max_proteins=10)
    assert proteins.n_proteins == 4
    norms = np.linalg.norm(proteins.matrix, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-3)
    assert proteins.truncated is False


def test_parse_truncates_when_over_cap() -> None:
    embeddings = np.eye(8, _DIM, dtype=np.float16)
    parquet = make_protein_parquet_bytes(embeddings)
    proteins = parse_protein_embedding_bytes(parquet, max_proteins=3)
    assert proteins.n_proteins == 3
    assert proteins.truncated is True


def test_parse_zero_rows_rejected() -> None:
    # Build an empty parquet by passing zero-row embeddings via build helper.
    embeddings = np.zeros((0, _DIM), dtype=np.float16)
    # The helper requires non-empty; build directly.
    import io

    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = pa.schema(
        [
            pa.field("protein_id", pa.string(), nullable=False),
            pa.field("sequence_length", pa.int32(), nullable=False),
            pa.field("sequence_md5", pa.string(), nullable=False),
            pa.field(
                "embedding",
                pa.list_(pa.field("item", pa.float16(), nullable=False), _DIM),
                nullable=False,
            ),
        ]
    )
    flat = pa.array(np.zeros(0, dtype=np.float16), type=pa.float16())
    embed_col = pa.FixedSizeListArray.from_arrays(flat, _DIM)
    table = pa.Table.from_arrays(
        [
            pa.array([], type=pa.string()),
            pa.array([], type=pa.int32()),
            pa.array([], type=pa.string()),
            embed_col,
        ],
        schema=schema,
    )
    buf = io.BytesIO()
    pq.write_table(table, buf)
    with pytest.raises(ValueError):
        parse_protein_embedding_bytes(buf.getvalue(), max_proteins=10)
    _ = embeddings  # quiet linter


def test_load_through_object_storage_returns_matrix() -> None:
    embeddings = np.eye(3, _DIM, dtype=np.float16)
    parquet = make_protein_parquet_bytes(embeddings)
    storage = FakeObjectStorage()
    storage.put_bytes(container="kanto-embeddings", key="PDT.1/1.parquet", data=parquet)
    proteins = load_protein_embeddings(
        storage,
        container="kanto-embeddings",
        key="PDT.1/1.parquet",
        max_proteins=10,
    )
    assert proteins.n_proteins == 3


def test_load_missing_object_raises_not_found() -> None:
    storage = FakeObjectStorage()
    with pytest.raises(ProteinEmbeddingsNotFoundError):
        load_protein_embeddings(
            storage,
            container="kanto-embeddings",
            key="missing/1.parquet",
            max_proteins=10,
        )


def test_max_proteins_below_one_rejected() -> None:
    embeddings = np.eye(1, _DIM, dtype=np.float16)
    parquet = make_protein_parquet_bytes(embeddings)
    with pytest.raises(ValueError):
        parse_protein_embedding_bytes(parquet, max_proteins=0)
