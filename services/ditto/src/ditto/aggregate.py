"""Per-genome aggregate: length-weighted mean of per-protein embeddings.

Task-07 §4 defines the v1 aggregate as

    aggregate = sum_i (length_i * embedding_i) / sum_i (length_i)

The biological intuition is that a 1500-residue protein contributes
more to the genome representation than a 60-residue one. v2 may
revisit this; this module is deliberately a pure function so the
swap is local.

Precision notes (§Common Pitfalls)
----------------------------------
We accumulate the sum in **float32** and only cast the final result
to **float16**. The alternative (accumulate in float16) saves a few
KB and is faster, but on a 12k-protein bacterial genome with
embeddings in the ±0.1 range the partial sums hit ~1200, which leaves
only ~3 bits of significand for the next term — visible rounding
artifacts in the aggregate. The README §Decision notes records this
choice.

Numerical edge cases
~~~~~~~~~~~~~~~~~~~~
* Zero proteins: caller's responsibility — the pipeline emits a zero
  vector and tags the row, rather than dividing by zero here. This
  function raises ValueError so the bug is loud.
* A single protein: aggregate == that protein's embedding (modulo the
  fp32→fp16 cast), regardless of its length.
* All-zero weights: ValueError. Should not happen — Snorlax guarantees
  every protein it emits has length >= 1.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np


def length_weighted_mean(
    embeddings: np.ndarray,
    lengths: Sequence[int],
) -> np.ndarray:
    """Compute the per-genome length-weighted-mean aggregate.

    Parameters
    ----------
    embeddings:
        Shape ``(n_proteins, embedding_dim)``. Any float dtype; we
        upcast internally so callers don't have to.
    lengths:
        Sequence of length ``n_proteins`` giving each protein's
        sequence length (post-truncation count is fine — what matters
        is the weight, and §4 says length).

    Returns
    -------
    np.ndarray
        Shape ``(embedding_dim,)``, dtype ``float16``. The dtype is
        deliberate: the parquet column for the genome-aggregate row
        in Mew is fp32 (pgvector standard), but Ditto returns fp16 so
        the EmbeddingRepository upsert path can promote at one site.

    Raises
    ------
    ValueError
        If shapes mismatch, if there are zero proteins, or if the
        total weight is zero.
    """
    if embeddings.ndim != 2:
        raise ValueError(f"embeddings must be 2D (n, d); got shape {embeddings.shape}")
    n, _ = embeddings.shape
    if n == 0:
        raise ValueError("cannot aggregate over zero proteins")
    if len(lengths) != n:
        raise ValueError(f"lengths has {len(lengths)} entries; embeddings has {n} rows")

    # Promote to fp32 BEFORE multiplication so the per-protein products
    # are computed at fp32 precision (§Common Pitfalls). Use np.asarray
    # so a list of ints lands as a column vector for broadcasting.
    weights = np.asarray(lengths, dtype=np.float32).reshape(-1, 1)
    total_weight = float(weights.sum())
    if total_weight <= 0:
        raise ValueError("total weight is zero; check that protein lengths are positive")

    weighted = embeddings.astype(np.float32) * weights
    summed = weighted.sum(axis=0)
    mean = summed / total_weight
    result: np.ndarray = mean.astype(np.float16)
    return result


__all__ = ["length_weighted_mean"]
