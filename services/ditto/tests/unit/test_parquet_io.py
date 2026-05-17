"""Tests for the per-protein parquet writer."""

from __future__ import annotations

import io

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ditto.parquet_io import (
    ProteinEmbedding,
    build_table,
    schema_for,
    write_parquet_bytes,
)


def _row(pid: str, length: int, dim: int = 8) -> ProteinEmbedding:
    rng = np.random.default_rng(seed=hash(pid) % (2**32))
    return ProteinEmbedding(
        protein_id=pid,
        sequence_length=length,
        sequence_md5="0" * 32,
        embedding=rng.uniform(-1, 1, size=dim).astype(np.float16),
    )


def test_schema_has_expected_columns() -> None:
    schema = schema_for(1152)
    assert schema.field("protein_id").type == pa.string()
    assert schema.field("sequence_length").type == pa.int32()
    assert schema.field("sequence_md5").type == pa.string()
    embedding_t = schema.field("embedding").type
    assert pa.types.is_fixed_size_list(embedding_t)
    assert embedding_t.list_size == 1152
    assert embedding_t.value_type == pa.float16()


def test_build_table_round_trip() -> None:
    rows = [_row(f"p{i}", 100 + i) for i in range(5)]
    table = build_table(rows, embedding_dim=8)

    assert table.num_rows == 5
    ids = table.column("protein_id").to_pylist()
    assert ids == [r.protein_id for r in rows]

    # FixedSizeList round-trips back to a list-of-lists where each inner
    # list has the right dim.
    embeddings = table.column("embedding").to_pylist()
    assert all(len(e) == 8 for e in embeddings)


def test_write_parquet_round_trip_via_pyarrow() -> None:
    rows = [_row(f"p{i}", 100 + i) for i in range(3)]
    blob = write_parquet_bytes(rows, embedding_dim=8)
    table = pq.read_table(io.BytesIO(blob))
    assert table.num_rows == 3
    assert set(table.column_names) == {
        "protein_id",
        "sequence_length",
        "sequence_md5",
        "embedding",
    }


def test_compression_is_snappy() -> None:
    rows = [_row(f"p{i}", 100) for i in range(3)]
    blob = write_parquet_bytes(rows, embedding_dim=8)
    pq_file = pq.ParquetFile(io.BytesIO(blob))
    # Each column chunk reports its compression — embedding column most.
    rg = pq_file.metadata.row_group(0)
    compressions = {rg.column(i).compression.lower() for i in range(rg.num_columns)}
    assert "snappy" in compressions


def test_misshapen_embedding_rejected() -> None:
    bad = ProteinEmbedding(
        protein_id="bad",
        sequence_length=100,
        sequence_md5="x" * 32,
        embedding=np.zeros(7, dtype=np.float16),  # expected 8
    )
    with pytest.raises(ValueError, match="embedding shape"):
        build_table([bad], embedding_dim=8)


def test_wrong_dtype_rejected() -> None:
    bad = ProteinEmbedding(
        protein_id="bad",
        sequence_length=100,
        sequence_md5="x" * 32,
        embedding=np.zeros(8, dtype=np.float32),
    )
    with pytest.raises(ValueError, match="embedding dtype"):
        build_table([bad], embedding_dim=8)


def test_empty_rows_rejected() -> None:
    with pytest.raises(ValueError, match="empty genome"):
        build_table([], embedding_dim=8)
