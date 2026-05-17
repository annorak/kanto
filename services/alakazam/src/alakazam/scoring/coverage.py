"""Coverage strategy: fraction of genome proteins missing a reference match.

A "normal" bacterium carries most of the housekeeping / ribosomal /
core-metabolic proteins in the reference bundle. A genome with many
proteins that cosine-similar to *nothing* in the reference set is
suspicious — either it's truly novel biology, an engineered design,
or a contaminated assembly. The coverage component reduces this to a
single number: the fraction of genome proteins *without* a confident
reference match, so higher means more novel.

Compute path
------------
1. Load the genome's per-protein parquet (one OS read, ~28 MB).
2. Matmul ``proteins @ reference.T`` to get an ``(n_proteins,
   n_reference)`` similarity matrix. Rows are already L2-normalized
   on both sides, so dot product == cosine similarity.
3. For each protein, take the max similarity to any reference. If
   that max is ``>= coverage_match_threshold``, the protein is
   "recognized".
4. Score = ``1 - recognized_fraction``.

This is the expensive tier-2 component — it fires only when
NN-distance has already flagged the isolate as a candidate. The OS
read is the dominant cost; the matmul itself is ~5 ms for a
4k-protein genome against a 3k-protein reference set at fp32.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import numpy as np
from kanto_commons.storage import KeyBuilder, ObjectStorage

from alakazam.protein_embeddings import (
    ProteinEmbeddingsNotFoundError,
    load_protein_embeddings,
)
from alakazam.reference_set import ReferenceSet
from alakazam.scoring.strategy import (
    ComponentName,
    ComponentResult,
    SkipReason,
)

if TYPE_CHECKING:
    from alakazam.scoring.orchestrator import IsolateContext

logger = logging.getLogger(__name__)


class CoverageStrategy:
    """Tier-2: fraction of unmatched proteins against the reference set."""

    name = ComponentName.COVERAGE

    def __init__(
        self,
        *,
        reference_set: ReferenceSet | None,
        storage: ObjectStorage,
        key_builder: KeyBuilder,
        embeddings_container: str,
        match_threshold: float,
        max_proteins: int,
    ) -> None:
        self._reference = reference_set
        self._storage = storage
        self._key_builder = key_builder
        self._embeddings_container = embeddings_container
        self._match_threshold = match_threshold
        self._max_proteins = max_proteins

    async def compute(self, ctx: IsolateContext) -> ComponentResult:
        start = time.perf_counter()

        if self._reference is None:
            return ComponentResult.skipped(
                self.name,
                SkipReason.REFERENCE_SET_MISSING,
                duration_seconds=time.perf_counter() - start,
            )

        key = self._key_builder.embedding_parquet_key(ctx.accession, ctx.version)
        try:
            # OS read is sync; the consumer awaits it on the default
            # executor to avoid blocking the event loop on a slow
            # download. The Object Storage SDK is sync-only by design.
            import asyncio

            proteins = await asyncio.to_thread(
                load_protein_embeddings,
                self._storage,
                container=self._embeddings_container,
                key=key,
                max_proteins=self._max_proteins,
            )
        except ProteinEmbeddingsNotFoundError:
            logger.warning(
                "alakazam.coverage: per-protein parquet missing for %s/%d (%s)",
                ctx.accession,
                ctx.version,
                key,
            )
            return ComponentResult.skipped(
                self.name,
                SkipReason.PROTEIN_PARQUET_MISSING,
                duration_seconds=time.perf_counter() - start,
                diagnostics={"os_key": key},
            )
        except Exception:
            logger.exception(
                "alakazam.coverage: load failure for %s/%d (%s)",
                ctx.accession,
                ctx.version,
                key,
            )
            return ComponentResult.skipped(
                self.name,
                SkipReason.COMPUTE_ERROR,
                duration_seconds=time.perf_counter() - start,
                diagnostics={"os_key": key},
            )

        # Both matrices are row-normalized → dot product == cosine.
        # Use ``axis=1`` max so we get one similarity per genome protein.
        # Cast to fp32 explicitly for parity with the reference set
        # (which is fp32 after :func:`_l2_normalize_rows`).
        similarities = proteins.matrix @ self._reference.matrix.T
        max_per_protein = similarities.max(axis=1)
        recognized = int(np.sum(max_per_protein >= self._match_threshold))
        total = int(proteins.n_proteins)
        recognized_fraction = recognized / total if total > 0 else 0.0
        score = 1.0 - recognized_fraction
        return ComponentResult.computed(
            self.name,
            score=score,
            duration_seconds=time.perf_counter() - start,
            diagnostics={
                "n_proteins": total,
                "n_recognized": recognized,
                "recognized_fraction": recognized_fraction,
                "match_threshold": self._match_threshold,
                "reference_size": self._reference.size,
                "truncated": "true" if proteins.truncated else "false",
            },
        )


__all__ = ["CoverageStrategy"]
