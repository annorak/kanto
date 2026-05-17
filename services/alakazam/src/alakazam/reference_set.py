"""In-memory reference set of common bacterial proteins.

The coverage strategy needs a small, curated set of proteins that
"any normal bacterium should have most of" — housekeeping genes,
ribosomal proteins, common metabolic enzymes. v1 builds this from
the union of BUSCO bacterial single-copy orthologs and the KEGG core
bacterial gene set; the construction script lives at
:mod:`alakazam.scripts.build_reference_set` and the README documents
the bundle assembly.

Storage layout
--------------
The reference set lives as a single Parquet blob on Object Storage
at a known key (default ``references/common_proteins_v1.parquet``).
Each row carries:

* ``protein_id``    — stable identifier within the bundle (e.g. KEGG
  K-number, BUSCO ortholog id, or a synthetic ``busco:OG…/kegg:K…``).
* ``source``        — ``busco`` / ``kegg`` / ``manual``.
* ``sequence_md5``  — md5 of the residue string the embedding was
  computed against; lets the operator audit drift across rebuilds.
* ``embedding``     — fixed-size list of ``EMBEDDING_DIM`` float16,
  produced by the same ESM C 600M model Ditto uses on the hot path.

Runtime
-------
:class:`ReferenceSet.from_parquet_bytes` is the production loader:
download once at startup, materialize to a contiguous L2-normalized
(R, D) float32 matrix in RAM, and reuse on every coverage call. The
matrix is normalized so cosine similarity reduces to a single matmul
``genome @ ref.T`` with no per-row division on the hot path.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import IO

import numpy as np
import pyarrow.parquet as pq
from kanto_commons.storage import ObjectStorage

logger = logging.getLogger(__name__)


# ESM C 600M outputs 1152-d embeddings; the reference set must match.
# We assert at load time rather than hardcode globally because the
# eventual v2 model swap will change this constant in exactly one place.
_EXPECTED_EMBEDDING_DIM = 1152

_EPS = 1e-12


@dataclass(frozen=True, slots=True)
class ReferenceSet:
    """L2-normalized reference embedding matrix + per-row metadata.

    ``matrix`` shape: ``(R, D)``, dtype float32, rows unit-norm.
    Coverage similarity is a single matmul against this matrix.
    """

    matrix: np.ndarray
    protein_ids: tuple[str, ...]
    sources: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.matrix.ndim != 2:
            raise ValueError(
                f"ReferenceSet matrix must be 2D (R, D); got shape {self.matrix.shape}"
            )
        if self.matrix.dtype != np.float32:
            raise ValueError(f"ReferenceSet matrix must be float32; got {self.matrix.dtype}")
        if self.matrix.shape[0] != len(self.protein_ids):
            raise ValueError(
                f"matrix has {self.matrix.shape[0]} rows but "
                f"{len(self.protein_ids)} protein_ids supplied"
            )
        if len(self.protein_ids) != len(self.sources):
            raise ValueError(
                f"protein_ids and sources length mismatch: "
                f"{len(self.protein_ids)} vs {len(self.sources)}"
            )

    @property
    def size(self) -> int:
        """Number of reference proteins."""
        return int(self.matrix.shape[0])

    @property
    def embedding_dim(self) -> int:
        return int(self.matrix.shape[1])


def load_reference_parquet(
    storage: ObjectStorage,
    *,
    container: str,
    key: str,
) -> ReferenceSet:
    """Production loader: download from OS, build the matrix, return."""
    logger.info("alakazam.reference_set: loading %s/%s", container, key)
    raw = storage.get_bytes(container=container, key=key)
    ref = parse_reference_parquet_bytes(raw)
    logger.info(
        "alakazam.reference_set: loaded %d reference proteins (dim=%d)",
        ref.size,
        ref.embedding_dim,
    )
    return ref


def parse_reference_parquet_bytes(blob: bytes) -> ReferenceSet:
    """Parse the reference parquet from in-memory bytes.

    Split out from :func:`load_reference_parquet` so unit tests can
    exercise the parser without touching Object Storage.
    """
    buffer: IO[bytes] = io.BytesIO(blob)
    table = pq.read_table(buffer)
    return _table_to_reference_set(table)


def _table_to_reference_set(table: object) -> ReferenceSet:
    """Convert an Arrow ``Table`` to a normalized :class:`ReferenceSet`.

    Accepts the parquet schema produced by
    :mod:`alakazam.scripts.build_reference_set`:

      protein_id : string
      source     : string
      sequence_md5 : string (unused at runtime; kept for audit)
      embedding  : fixed_size_list[float16, EMBEDDING_DIM]
    """
    # Local import keeps pyarrow typing out of this module's surface
    # for IDE users that mostly call from the bytes-based path.
    import pyarrow as pa

    if not isinstance(table, pa.Table):
        raise TypeError(f"expected pyarrow.Table; got {type(table).__name__}")

    required = {"protein_id", "source", "embedding"}
    missing = required - set(table.column_names)
    if missing:
        raise ValueError(
            f"reference parquet missing columns: {sorted(missing)}; have {table.column_names}"
        )
    if table.num_rows == 0:
        raise ValueError("reference parquet has zero rows — refusing to load")

    protein_ids = tuple(table.column("protein_id").to_pylist())
    sources = tuple(table.column("source").to_pylist())

    # The embedding column is a FixedSizeListArray; ``.combine_chunks``
    # gives us a contiguous block we can reshape to ``(R, D)`` in one
    # allocation. Cast to fp32 for the matmul — float16 matmul through
    # numpy goes via fp32 internally anyway, so we save a copy.
    embed_col = table.column("embedding").combine_chunks()
    fixed_size_list = embed_col
    if not isinstance(fixed_size_list, pa.FixedSizeListArray):
        raise ValueError(
            f"embedding column must be FixedSizeList; got {type(fixed_size_list).__name__}"
        )
    dim = fixed_size_list.type.list_size
    if dim != _EXPECTED_EMBEDDING_DIM:
        raise ValueError(
            f"reference embedding dim {dim} != expected {_EXPECTED_EMBEDDING_DIM}; "
            f"rebuild with the current model"
        )

    flat = np.asarray(fixed_size_list.values.to_numpy(zero_copy_only=False))
    matrix = flat.reshape(table.num_rows, dim).astype(np.float32, copy=False)
    matrix = _l2_normalize_rows(matrix)
    return ReferenceSet(
        matrix=matrix,
        protein_ids=protein_ids,
        sources=sources,
    )


def from_arrays(
    *,
    embeddings: np.ndarray,
    protein_ids: list[str],
    sources: list[str] | None = None,
) -> ReferenceSet:
    """Construct a :class:`ReferenceSet` directly from arrays.

    Used by tests and by the builder script. Normalizes rows and
    promotes to float32 so the matrix invariants in
    :meth:`ReferenceSet.__post_init__` hold.
    """
    if embeddings.ndim != 2:
        raise ValueError(f"embeddings must be 2D (n, d); got shape {embeddings.shape}")
    n = embeddings.shape[0]
    if len(protein_ids) != n:
        raise ValueError(f"protein_ids has {len(protein_ids)} entries; embeddings has {n} rows")
    if sources is None:
        sources = ["manual"] * n
    if len(sources) != n:
        raise ValueError(f"sources has {len(sources)} entries; embeddings has {n} rows")

    matrix = embeddings.astype(np.float32, copy=False)
    matrix = _l2_normalize_rows(matrix)
    return ReferenceSet(
        matrix=matrix,
        protein_ids=tuple(protein_ids),
        sources=tuple(sources),
    )


def _l2_normalize_rows(matrix: np.ndarray) -> np.ndarray:
    """Return a row-normalized copy of ``matrix``.

    Rows with zero norm get a 1.0 norm so the division yields a zero
    row rather than NaN — a zero-norm reference protein is itself a
    bug, but the scoring path should not crash on it. The caller is
    expected to log + audit such rows separately.
    """
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    safe = np.where(norms < _EPS, 1.0, norms)
    return (matrix / safe).astype(np.float32, copy=False)


__all__ = [
    "ReferenceSet",
    "from_arrays",
    "load_reference_parquet",
    "parse_reference_parquet_bytes",
]
