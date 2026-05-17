"""Read a genome's per-protein embeddings from Object Storage.

The coverage strategy is the only place in v1's hot path that touches
the per-protein parquet Ditto writes. We pull the full file into
memory and materialize it as an L2-normalized ``(n_proteins, dim)``
float32 matrix so the subsequent matmul against the reference set is
one BLAS call.

Bounded reads
-------------
A normal bacterial genome has ~4k proteins (~28 MB compressed) — small
enough to load whole. Pathological inputs (mismatched concatenations,
metagenomic mixes) could push past that; the caller can cap
``max_proteins`` so a runaway genome doesn't sink the consumer's RAM
budget.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import IO

import numpy as np
import pyarrow.parquet as pq
from kanto_commons.storage import ObjectNotFoundError, ObjectStorage

logger = logging.getLogger(__name__)


_EPS = 1e-12


@dataclass(frozen=True, slots=True)
class ProteinEmbeddings:
    """Row-normalized per-protein embedding matrix for one genome.

    ``matrix`` is ``(n_proteins, dim)`` float32 with each row L2-unit
    so cosine similarity reduces to ``matrix @ ref.T``.
    """

    matrix: np.ndarray
    protein_ids: tuple[str, ...]
    truncated: bool

    @property
    def n_proteins(self) -> int:
        return int(self.matrix.shape[0])


class ProteinEmbeddingsNotFoundError(LookupError):
    """The per-protein parquet for ``accession`` does not exist in OS."""


def load_protein_embeddings(
    storage: ObjectStorage,
    *,
    container: str,
    key: str,
    max_proteins: int,
) -> ProteinEmbeddings:
    """Fetch + parse the genome's per-protein embedding parquet."""
    try:
        raw = storage.get_bytes(container=container, key=key)
    except ObjectNotFoundError as exc:
        raise ProteinEmbeddingsNotFoundError(f"{container}/{key}") from exc

    return parse_protein_embedding_bytes(raw, max_proteins=max_proteins)


def parse_protein_embedding_bytes(
    blob: bytes,
    *,
    max_proteins: int,
) -> ProteinEmbeddings:
    """Parse the per-protein parquet from in-memory bytes.

    The schema matches what :mod:`ditto.parquet_io` writes:

      protein_id      : string
      sequence_length : int32   (ignored here)
      sequence_md5    : string  (ignored here)
      embedding       : fixed_size_list[float16, EMBEDDING_DIM]
    """
    if max_proteins < 1:
        raise ValueError("max_proteins must be >= 1")

    import pyarrow as pa

    buffer: IO[bytes] = io.BytesIO(blob)
    table = pq.read_table(buffer, columns=["protein_id", "embedding"])
    if table.num_rows == 0:
        raise ValueError("per-protein parquet has zero rows")

    truncated = False
    if table.num_rows > max_proteins:
        logger.warning(
            "alakazam.protein_embeddings: truncating %d -> %d proteins",
            table.num_rows,
            max_proteins,
        )
        table = table.slice(0, max_proteins)
        truncated = True

    protein_ids = tuple(table.column("protein_id").to_pylist())

    embed_col = table.column("embedding").combine_chunks()
    if not isinstance(embed_col, pa.FixedSizeListArray):
        raise ValueError(f"embedding column must be FixedSizeList; got {type(embed_col).__name__}")
    dim = embed_col.type.list_size
    flat = np.asarray(embed_col.values.to_numpy(zero_copy_only=False))
    matrix = flat.reshape(table.num_rows, dim).astype(np.float32, copy=False)
    matrix = _l2_normalize_rows(matrix)
    return ProteinEmbeddings(
        matrix=matrix,
        protein_ids=protein_ids,
        truncated=truncated,
    )


def _l2_normalize_rows(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    safe = np.where(norms < _EPS, 1.0, norms)
    return (matrix / safe).astype(np.float32, copy=False)


__all__ = [
    "ProteinEmbeddings",
    "ProteinEmbeddingsNotFoundError",
    "load_protein_embeddings",
    "parse_protein_embedding_bytes",
]
