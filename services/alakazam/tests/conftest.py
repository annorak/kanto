"""Shared fixtures for Alakazam tests.

Synthetic numerical data only: the reference set, per-protein
embeddings, and centroid records are all constructed in-memory so
unit tests run with no I/O at all. Integration tests bring up a
testcontainers Postgres+pgvector via the fixtures in
:mod:`kanto_commons.testing.mew_fixture`.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import UTC, datetime

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from alakazam.reference_set import ReferenceSet, from_arrays

_DIM = 1152
_RNG = np.random.default_rng(seed=42)


@pytest.fixture
def random_unit_vector() -> Iterator[np.ndarray]:
    """One length-_DIM unit-norm float32 vector."""
    v = _RNG.standard_normal(_DIM).astype(np.float32)
    v /= np.linalg.norm(v)
    yield v


@pytest.fixture
def small_reference_set() -> ReferenceSet:
    """A 5-protein reference set with synthetic embeddings."""
    n = 5
    embeddings = _RNG.standard_normal((n, _DIM)).astype(np.float32)
    return from_arrays(
        embeddings=embeddings,
        protein_ids=[f"REF{i:03d}" for i in range(n)],
        sources=["busco", "kegg", "kegg", "manual", "busco"],
    )


@pytest.fixture
def reference_parquet_bytes(small_reference_set: ReferenceSet) -> bytes:
    """A parquet blob conforming to the reference-set schema."""
    n = small_reference_set.size
    embeddings_f16 = small_reference_set.matrix.astype(np.float16, copy=False)
    flat = pa.array(embeddings_f16.reshape(-1), type=pa.float16())
    embed_col = pa.FixedSizeListArray.from_arrays(flat, _DIM)
    schema = pa.schema(
        [
            pa.field("protein_id", pa.string(), nullable=False),
            pa.field("source", pa.string(), nullable=False),
            pa.field("sequence_md5", pa.string(), nullable=False),
            pa.field(
                "embedding",
                pa.list_(pa.field("item", pa.float16(), nullable=False), _DIM),
                nullable=False,
            ),
        ]
    )
    table = pa.Table.from_arrays(
        [
            pa.array(list(small_reference_set.protein_ids), type=pa.string()),
            pa.array(list(small_reference_set.sources), type=pa.string()),
            pa.array([f"md5{i:04d}" for i in range(n)], type=pa.string()),
            embed_col,
        ],
        schema=schema,
    )
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")
    return buf.getvalue()


def make_protein_parquet_bytes(
    embeddings: np.ndarray,
    *,
    protein_ids: list[str] | None = None,
) -> bytes:
    """Build a per-protein parquet blob in the Ditto schema."""
    n, dim = embeddings.shape
    if protein_ids is None:
        protein_ids = [f"P{i:05d}" for i in range(n)]
    if embeddings.dtype != np.float16:
        embeddings = embeddings.astype(np.float16, copy=False)
    flat = pa.array(embeddings.reshape(-1), type=pa.float16())
    embed_col = pa.FixedSizeListArray.from_arrays(flat, dim)
    schema = pa.schema(
        [
            pa.field("protein_id", pa.string(), nullable=False),
            pa.field("sequence_length", pa.int32(), nullable=False),
            pa.field("sequence_md5", pa.string(), nullable=False),
            pa.field(
                "embedding",
                pa.list_(pa.field("item", pa.float16(), nullable=False), dim),
                nullable=False,
            ),
        ]
    )
    table = pa.Table.from_arrays(
        [
            pa.array(protein_ids, type=pa.string()),
            pa.array([100 + i for i in range(n)], type=pa.int32()),
            pa.array([f"md5{i:04d}" for i in range(n)], type=pa.string()),
            embed_col,
        ],
        schema=schema,
    )
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")
    return buf.getvalue()


def utcnow() -> datetime:
    return datetime.now(UTC)
