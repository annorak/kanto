"""Embed-one-isolate orchestration.

The Modal function in :mod:`ditto.modal_app` is a thin shell that
calls this module. Splitting lets us unit-test the heavy logic
without a Modal account.

Step-by-step
------------
1. **Idempotency check.** If Mew already has an embedding for this
   accession with the same model_version: re-emit the event (cheap;
   consumers dedupe on accession+version) and return without doing
   the GPU work. This makes the function safely re-invokable.
2. **Fetch FASTA** from Blob Storage.
3. **Parse + truncate** to per-protein records.
4. **Length-group** into batches.
5. **Run inference** batch by batch. On OOM, retry the offending
   batch at size=1; if a singleton still OOMs, that protein is too
   big for the GPU and we raise PermanentError.
6. **Compute aggregate** (length-weighted mean).
7. **Write parquet** to ``kanto-embeddings``.
8. **Upsert aggregate into Mew** + flip status to EMBEDDED in one
   transaction.
9. **Emit EmbeddingsReady**.

Step 9 is last so a crash anywhere earlier leaves the isolate in its
prior state and the next invocation safely re-runs. Re-emission on
the idempotent skip path means there is no "Mew succeeded but event
didn't" recovery gap.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import psycopg
from kanto_commons.mew import (
    EmbeddingRepository,
    EmbeddingRow,
    IsolateRepository,
    IsolateStatus,
    MewClient,
)
from kanto_commons.mew.repositories import (
    EmbeddingNotFoundError,
    IsolateNotFoundError,
)
from kanto_commons.storage import KeyBuilder
from opentelemetry import trace

from ditto.aggregate import length_weighted_mean
from ditto.azure_io import BlobIO
from ditto.batching import Batch, group_by_length
from ditto.config import DittoServiceSettings
from ditto.errors import (
    InferenceOOMError,
    MewWriteError,
    PermanentError,
)
from ditto.event_emit import EventEmitter
from ditto.fasta import ParsedProtein, parse_fasta_bytes
from ditto.parquet_io import ProteinEmbedding, write_parquet_bytes

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer("ditto.pipeline")


@dataclass(frozen=True)
class EmbedResult:
    """Returned to the Modal-function shell; consumed only by smoke tests."""

    accession: str
    version: int
    proteins_embedded: int
    parquet_key: str
    skipped_idempotent: bool


class EmbedPipeline:
    """Owns the embed-one-isolate state machine.

    All external dependencies are injected as Protocols so unit tests
    substitute fakes for every system without touching torch or modal.
    """

    def __init__(
        self,
        *,
        embedder: Any,  # ESMCEmbedder; loose-typed so model.py is lazy.
        blob_io: BlobIO,
        mew: MewClient,
        emitter: EventEmitter,
        key_builder: KeyBuilder,
        settings: DittoServiceSettings,
        isolate_repo: IsolateRepository | None = None,
        embedding_repo: EmbeddingRepository | None = None,
    ) -> None:
        self._embedder = embedder
        self._blob_io = blob_io
        self._mew = mew
        self._emitter = emitter
        self._keys = key_builder
        self._settings = settings
        self._isolate_repo = isolate_repo or IsolateRepository()
        self._embedding_repo = embedding_repo or EmbeddingRepository()

    async def embed_isolate(
        self,
        *,
        accession: str,
        version: int,
        os_key: str,
    ) -> EmbedResult:
        with _tracer.start_as_current_span(
            "ditto.embed_isolate",
            attributes={
                "kanto.accession": accession,
                "kanto.version": version,
                "kanto.os_key": os_key,
            },
        ):
            return await self._embed_isolate_inner(accession, version, os_key)

    # ----------------------------------------------------------------
    # Steps
    # ----------------------------------------------------------------

    async def _embed_isolate_inner(
        self,
        accession: str,
        version: int,
        os_key: str,
    ) -> EmbedResult:
        parquet_key = self._keys.embedding_parquet_key(accession, version)

        # 1. Idempotency. Re-emit so a Modal retry never silently
        #    drops the downstream event.
        if self._settings.skip_if_already_embedded and await self._already_embedded(accession):
            logger.info(
                "ditto.pipeline: skip accession=%s version=%d model_version=%s",
                accession,
                version,
                self._settings.model_version,
            )
            await self._emit_ready(accession, version, parquet_key)
            return EmbedResult(
                accession=accession,
                version=version,
                proteins_embedded=0,
                parquet_key=parquet_key,
                skipped_idempotent=True,
            )

        # 2-3. Fetch + parse FASTA.
        fasta_bytes = self._blob_io.fetch_protein_fasta(
            container=self._keys.proteins_container,
            key=os_key,
        )
        parsed = parse_fasta_bytes(
            fasta_bytes, max_sequence_length=self._settings.max_sequence_length
        )
        n_truncated = sum(1 for p in parsed if p.was_truncated)
        if n_truncated:
            logger.info(
                "ditto.pipeline: accession=%s truncated=%d total=%d",
                accession,
                n_truncated,
                len(parsed),
            )

        # 4-5. Length-grouped batching + inference (with OOM fallback).
        rows = self._run_inference(parsed)

        # 6. Length-weighted aggregate. Biological length (pre-trunc)
        #    is the weight; a truncated long protein still contributed
        #    its full residue count to the genome.
        aggregate = length_weighted_mean(
            np.stack([r.embedding for r in rows], axis=0),
            [r.sequence_length for r in rows],
        )
        logger.info(
            "ditto.pipeline: aggregate accession=%s proteins=%d residues=%d",
            accession,
            len(rows),
            sum(r.sequence_length for r in rows),
        )

        # 7. Parquet write. Bytes are deterministic so overwrite is safe.
        self._blob_io.write_parquet(
            container=self._keys.embeddings_container,
            key=parquet_key,
            data=write_parquet_bytes(rows, embedding_dim=self._settings.embedding_dim),
        )

        # 8. Mew aggregate + status flip in one transaction.
        await self._write_mew(accession, version, aggregate)

        # 9. Event last, so any earlier crash leaves the isolate in a
        #    state the next invocation can recover from.
        await self._emit_ready(accession, version, parquet_key)

        return EmbedResult(
            accession=accession,
            version=version,
            proteins_embedded=len(rows),
            parquet_key=parquet_key,
            skipped_idempotent=False,
        )

    # ----------------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------------

    async def _already_embedded(self, accession: str) -> bool:
        """True iff Mew has an embedding for ``accession`` at the
        current model_version. A model bump invalidates the check."""
        try:
            async with self._mew.connection() as conn:
                row = await self._embedding_repo.get(conn, accession=accession)
        except EmbeddingNotFoundError:
            return False
        return row.model_version == self._settings.model_version

    def _run_inference(self, parsed: Sequence[ParsedProtein]) -> list[ProteinEmbedding]:
        if not parsed:
            raise PermanentError(
                "FASTA parsed to zero proteins -- empty-genome handling "
                "is upstream (Snorlax should not ship protein_count=0)"
            )
        proteins = [p.to_batch_protein() for p in parsed]
        batches = group_by_length(
            proteins,
            target_tokens_per_batch=self._settings.target_tokens_per_batch,
            max_batch_size=self._settings.max_batch_size,
        )
        logger.info(
            "ditto.pipeline.batching: proteins=%d batches=%d tokens=%d max_size=%d",
            len(parsed),
            len(batches),
            self._settings.target_tokens_per_batch,
            self._settings.max_batch_size,
        )

        parsed_by_id = {p.id: p for p in parsed}
        rows: list[ProteinEmbedding] = []
        started = time.monotonic()
        for i, batch in enumerate(batches):
            embeddings = self._embed_with_oom_fallback(batch, i, len(batches))
            for protein, embedding in zip(batch.proteins, embeddings, strict=True):
                pp = parsed_by_id[protein.id]
                rows.append(
                    ProteinEmbedding(
                        protein_id=pp.id,
                        sequence_length=pp.original_length,
                        sequence_md5=pp.sequence_md5,
                        embedding=embedding,
                    )
                )
        logger.info(
            "ditto.pipeline.inference: proteins=%d batches=%d elapsed_s=%.3f",
            len(rows),
            len(batches),
            time.monotonic() - started,
        )
        return rows

    def _embed_with_oom_fallback(
        self, batch: Batch, batch_index: int, batch_total: int
    ) -> np.ndarray:
        """Run one batch; on OOM, retry each protein in a singleton batch."""
        try:
            first: np.ndarray = self._embedder.embed_batch(batch)
            return first
        except InferenceOOMError:
            logger.warning(
                "ditto.pipeline: OOM batch=%d/%d size=%d longest=%d -- "
                "falling back to batch_size=1",
                batch_index + 1,
                batch_total,
                batch.size,
                batch.longest_length,
            )

        singletons: list[np.ndarray] = []
        for protein in batch.proteins:
            try:
                singleton_emb = self._embedder.embed_batch(
                    Batch(proteins=(protein,), longest_length=protein.sequence_length)
                )
            except InferenceOOMError as exc:
                raise PermanentError(
                    f"protein {protein.id!r} (len={protein.sequence_length}) "
                    "OOMs at batch_size=1; bump the GPU tier and re-run"
                ) from exc
            singletons.append(singleton_emb[0])
        return np.stack(singletons, axis=0)

    async def _write_mew(
        self,
        accession: str,
        version: int,
        aggregate: np.ndarray,
    ) -> None:
        try:
            async with self._mew.transaction() as conn:
                await self._embedding_repo.upsert(
                    conn,
                    EmbeddingRow(
                        accession=accession,
                        version=version,
                        model=self._settings.model_id,
                        model_version=self._settings.model_version,
                        # pgvector expects a list/ndarray; the column is
                        # vector(N) (fp32 on the wire), so we promote.
                        embedding=aggregate.astype(np.float32).tolist(),
                    ),
                )
                # update_status (not upsert) preserves Snorlax-written
                # columns. Raises IsolateNotFoundError if the row is
                # absent -- a permanent failure for this accession.
                await self._isolate_repo.update_status(
                    conn,
                    accession=accession,
                    status=IsolateStatus.EMBEDDED,
                )
        except IsolateNotFoundError as exc:
            raise PermanentError(
                f"no isolates row for {accession}; Snorlax did not create it"
            ) from exc
        except psycopg.Error as exc:
            raise MewWriteError(f"mew transaction failed for {accession}@{version}: {exc}") from exc

    async def _emit_ready(self, accession: str, version: int, parquet_key: str) -> None:
        """Emit EmbeddingsReady. Re-raised on failure so Modal's retry
        layer takes over; consumers dedupe on (accession, version)."""
        await self._emitter.emit_embeddings_ready(
            accession=accession,
            version=version,
            model=self._settings.model_id,
            model_version=self._settings.model_version,
            os_key=parquet_key,
        )


__all__ = ["EmbedPipeline", "EmbedResult"]
