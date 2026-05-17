"""Per-protein embeddings parquet writer.

Schema (task-07 §5):

    protein_id        : string         — Prodigal identifier
    sequence_length   : int32          — pre-truncation residue count
    sequence_md5      : string (hex)   — MD5 of the pre-truncation sequence
    embedding         : fixed_size_list[float16, EMBEDDING_DIM]

We write Apache Parquet with Snappy compression (§5). A fixed-size
list is the right Arrow type for embeddings — it stores no per-row
length metadata, which matters when you have 12k rows of 1152-float
vectors.

Compression and row-group sizing
--------------------------------
Snappy is the §5 requirement: fast, deterministic, widely supported.
The row group is sized so a full bacterial genome (~12k proteins) is
one row group: this avoids the multi-row-group seek overhead that
Alakazam would otherwise pay when it reads the full file.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

# We size row groups slightly above the largest reasonable bacterial
# proteome (~12k proteins). Real input is single-row-group for the
# whole file; this constant is the cap, not the target.
_ROW_GROUP_SIZE = 16_384

_PROTEIN_ID = pa.field("protein_id", pa.string(), nullable=False)
_SEQ_LEN = pa.field("sequence_length", pa.int32(), nullable=False)
_SEQ_MD5 = pa.field("sequence_md5", pa.string(), nullable=False)


def embedding_field(dim: int) -> pa.Field:
    """Schema field for the per-protein embedding column.

    The fixed-size list dim is taken from settings rather than hard-
    coded so a future model swap doesn't require a parquet-writer
    edit.
    """
    return pa.field(
        "embedding",
        pa.list_(pa.field("item", pa.float16(), nullable=False), dim),
        nullable=False,
    )


def schema_for(embedding_dim: int) -> pa.Schema:
    """Full Arrow schema for the per-protein parquet."""
    return pa.schema([_PROTEIN_ID, _SEQ_LEN, _SEQ_MD5, embedding_field(embedding_dim)])


@dataclass(frozen=True)
class ProteinEmbedding:
    """One row of the per-protein parquet, pre-Arrow.

    Holding this as a frozen dataclass (rather than building the Arrow
    table directly column-by-column) lets the inference loop emit rows
    incrementally without holding two copies of the embedding tensor
    in memory.
    """

    protein_id: str
    sequence_length: int
    sequence_md5: str
    # 1D ndarray of length embedding_dim, dtype float16.
    embedding: np.ndarray


def build_table(
    rows: Sequence[ProteinEmbedding],
    *,
    embedding_dim: int,
) -> pa.Table:
    """Assemble an Arrow table from per-protein embedding rows.

    Validates shape and dtype eagerly so a misshapen embedding fails
    at this boundary, not deep in the parquet writer.
    """
    if not rows:
        raise ValueError("cannot build parquet for an empty genome — handle upstream")

    # Stack into a single (n, dim) ndarray so the Arrow conversion is
    # one allocation, not n allocations of length dim. Validate shape
    # first to surface a useful error.
    for row in rows:
        if row.embedding.shape != (embedding_dim,):
            raise ValueError(
                f"protein {row.protein_id!r}: embedding shape "
                f"{row.embedding.shape} != ({embedding_dim},)"
            )
        if row.embedding.dtype != np.float16:
            raise ValueError(
                f"protein {row.protein_id!r}: embedding dtype " f"{row.embedding.dtype} != float16"
            )

    stacked = np.stack([r.embedding for r in rows], axis=0)
    # Flatten to a single contiguous buffer; FixedSizeListArray.from_arrays
    # takes the flat float16 array and the fixed size.
    flat = pa.array(stacked.reshape(-1), type=pa.float16())
    embeddings = pa.FixedSizeListArray.from_arrays(flat, embedding_dim)

    return pa.Table.from_arrays(
        [
            pa.array([r.protein_id for r in rows], type=pa.string()),
            pa.array([r.sequence_length for r in rows], type=pa.int32()),
            pa.array([r.sequence_md5 for r in rows], type=pa.string()),
            embeddings,
        ],
        schema=schema_for(embedding_dim),
    )


def write_parquet_bytes(
    rows: Sequence[ProteinEmbedding],
    *,
    embedding_dim: int,
) -> bytes:
    """Serialise rows to an in-memory parquet blob.

    The output is what gets uploaded to Blob Storage. For a 12k-protein
    bacterial genome at 1152 fp16 dims the resulting file is roughly
    28 MB compressed — small enough to live in memory comfortably and
    bounded so the Modal worker doesn't spill to its tiny tmpfs.
    """
    table = build_table(rows, embedding_dim=embedding_dim)
    buffer = io.BytesIO()
    pq.write_table(
        table,
        buffer,
        compression="snappy",
        row_group_size=_ROW_GROUP_SIZE,
        # use_dictionary=False on the embedding column would be ideal
        # but pyarrow's writer already disables dictionary encoding for
        # fixed_size_list children by default.
    )
    return buffer.getvalue()


__all__ = [
    "ProteinEmbedding",
    "build_table",
    "embedding_field",
    "schema_for",
    "write_parquet_bytes",
]
