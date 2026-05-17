"""Synthetic benchmark — runs the full pipeline path with fakes and prints
the per-stage timing breakdown. Useful for sanity-checking that the in-
process stages (parse, batch, aggregate, parquet) are bounded as expected
even when real cross-cloud measurements aren't available.

Marked ``slow`` so it doesn't run by default; invoke explicitly with::

    pytest tests/integration/test_synthetic_benchmark.py -m slow -s
"""

from __future__ import annotations

import time

import numpy as np
import pytest


@pytest.mark.slow
def test_synthetic_pipeline_breakdown(capsys: pytest.CaptureFixture[str]) -> None:
    """Drive the pipeline with fake everything; print per-stage timings.

    Inputs sized to a typical bacterial proteome (~3,000 proteins,
    average length ~300 residues, max ~1,500). The synthetic fake
    embedder skips the real ESM C forward; this measures *only* the
    in-process orchestration overhead.
    """
    from kanto_commons.storage import KeyBuilder

    from ditto.aggregate import length_weighted_mean
    from ditto.batching import group_by_length
    from ditto.config import DittoServiceSettings
    from ditto.fasta import parse_fasta_bytes
    from ditto.parquet_io import ProteinEmbedding, write_parquet_bytes

    # Deterministic synthetic genome. Real bacterial proteomes are
    # heavily skewed — mode ~150 residues, long tail to ~2000. Use an
    # exponential to match. Without the skew, naive batching looks
    # artificially competitive (the naive-vs-grouped efficiency ratio
    # depends on the *variance* of length, not the mean).
    rng = np.random.default_rng(42)
    n = 3000
    raw = rng.exponential(scale=300.0, size=n)
    lengths = np.clip(raw.astype(int), 30, 2000).tolist()
    seqs = {f"contig_{i}": "M" * lengths[i] for i in range(n)}

    fasta = "\n".join(f">{pid}\n{seq}" for pid, seq in seqs.items()).encode("ascii")

    settings = DittoServiceSettings(
        embedding_dim=1152,
        max_sequence_length=2048,
        target_tokens_per_batch=16384,
        max_batch_size=64,
    )

    # ---- parse + truncate ----
    t0 = time.perf_counter()
    parsed = parse_fasta_bytes(fasta, max_sequence_length=settings.max_sequence_length)
    t_parse = time.perf_counter() - t0

    # ---- batch ----
    t0 = time.perf_counter()
    proteins = [p.to_batch_protein() for p in parsed]
    batches = group_by_length(
        proteins,
        target_tokens_per_batch=settings.target_tokens_per_batch,
        max_batch_size=settings.max_batch_size,
    )
    t_batch = time.perf_counter() - t0

    # ---- "inference" (synthetic random embeddings) ----
    t0 = time.perf_counter()
    rows: list[ProteinEmbedding] = []
    parsed_by_id = {p.id: p for p in parsed}
    for batch in batches:
        # Synthetic per-batch embedding tensor.
        emb = rng.standard_normal((batch.size, settings.embedding_dim)).astype(np.float16)
        for i, protein in enumerate(batch.proteins):
            pp = parsed_by_id[protein.id]
            rows.append(
                ProteinEmbedding(
                    protein_id=pp.id,
                    sequence_length=pp.original_length,
                    sequence_md5=pp.sequence_md5,
                    embedding=emb[i],
                )
            )
    t_inference_synthetic = time.perf_counter() - t0

    # ---- aggregate ----
    t0 = time.perf_counter()
    all_emb = np.stack([r.embedding for r in rows], axis=0)
    aggregate = length_weighted_mean(all_emb, [r.sequence_length for r in rows])
    t_agg = time.perf_counter() - t0
    assert aggregate.shape == (settings.embedding_dim,)

    # ---- parquet build + serialise ----
    t0 = time.perf_counter()
    parquet_bytes = write_parquet_bytes(rows, embedding_dim=settings.embedding_dim)
    t_parquet = time.perf_counter() - t0

    # ---- batch composition stats ----
    naive_one_batch = len(proteins) * max(p.sequence_length for p in proteins) if proteins else 0
    grouped_padded = sum(b.padded_tokens for b in batches)

    # ---- print ----
    keys = KeyBuilder(
        proteins_container="kanto-proteins",
        embeddings_container="kanto-embeddings",
        metadata_container="kanto-metadata",
    )
    out = [
        "",
        "Synthetic Ditto pipeline benchmark (no GPU, no cloud)",
        f"  proteins:               {n}",
        f"  total residues:         {sum(lengths):,}",
        f"  longest protein:        {max(lengths)}",
        f"  number of batches:      {len(batches)}",
        f"  parquet bytes:          {len(parquet_bytes):,}",
        f"  embedding parquet key:  {keys.embedding_parquet_key('PDT001', 1)}",
        "",
        "Padded-tokens efficiency (lower = better):",
        f"  length-grouped:         {grouped_padded:,}",
        f"  naive (one big batch):  {naive_one_batch:,}",
        f"  ratio (naive/grouped):  {naive_one_batch / max(grouped_padded, 1):.2f}x",
        "",
        "Per-stage in-process timings (single thread, dev box):",
        f"  parse_fasta:            {t_parse * 1000:8.2f} ms",
        f"  group_by_length:        {t_batch * 1000:8.2f} ms",
        f"  inference (synthetic):  {t_inference_synthetic * 1000:8.2f} ms",
        f"  aggregate:              {t_agg * 1000:8.2f} ms",
        f"  parquet_build:          {t_parquet * 1000:8.2f} ms",
        "",
        "Notes:",
        "  - Real inference dominates total time; this only measures Python overhead.",
        "  - Cross-cloud blob/mew/event timings require operator-run "
        "`ditto-benchmark --mode modal`.",
        "",
    ]
    print("\n".join(out))

    # Sanity-check assertions so this also functions as a regression test.
    assert t_parse < 1.0, f"parse_fasta took {t_parse:.3f}s for 3k proteins"
    assert t_batch < 1.0, f"group_by_length took {t_batch:.3f}s"
    assert t_agg < 1.0, f"aggregate took {t_agg:.3f}s"
    assert t_parquet < 5.0, f"parquet_build took {t_parquet:.3f}s"
    # The big payoff: length-grouped should be at least 4x more
    # token-efficient than the naive one-batch case.
    assert naive_one_batch / max(grouped_padded, 1) >= 4.0
