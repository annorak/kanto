"""End-to-end smoke tests against real services.

Gated by the @pytest.mark.integration / @pytest.mark.gpu markers so a
``pytest`` from the package root only runs the unit tests by default.

Two flavours:

1. **In-process ESM C** (``-m gpu``): loads ESM C 600M on the local
   CUDA device, runs a real forward pass on a 5-protein FASTA, asserts
   the embeddings are 1152-dim float16 and finite. This is the only
   test that exercises the actual ML stack — keep it small.

2. **Modal-deployed** (``-m integration``): invokes the deployed
   ``kanto-ditto/embed`` function against the dev Modal workspace +
   dev Azure. Requires that the operator already ran ``modal deploy``
   and exported KANTO_DITTO_*_DEV vars.
"""

from __future__ import annotations

import os

import pytest


@pytest.mark.gpu
def test_real_esmc_produces_finite_embeddings() -> None:  # pragma: no cover — needs CUDA
    """Sanity-check that the real ESM C model produces expected output.

    Skip when torch+CUDA aren't available; this test is gated behind
    the ``gpu`` marker so default ``pytest`` runs do not hit it. The
    nested import is guarded so dev boxes without torch (notably
    macOS x86_64, where torch has no wheel) skip cleanly rather than
    erroring at import.
    """
    try:
        import numpy as np
        import torch
    except ImportError:
        pytest.skip("torch not installed (install with `uv sync --extra gpu`)")

    if not torch.cuda.is_available():
        pytest.skip("CUDA not available")

    from ditto.batching import Protein, group_by_length
    from ditto.model import load_embedder

    embedder = load_embedder(model_id="esmc_600m", embedding_dim=1152)
    proteins = [
        Protein(id="p1", sequence="MAGI" * 25, sequence_length=100, original_length=100),
        Protein(id="p2", sequence="GGGT" * 25, sequence_length=100, original_length=100),
    ]
    batches = group_by_length(proteins, target_tokens_per_batch=8000, max_batch_size=16)
    assert len(batches) == 1
    out = embedder.embed_batch(batches[0])
    assert out.embeddings.shape == (2, 1152)
    assert out.embeddings.dtype == np.float16
    assert np.all(np.isfinite(out.embeddings))


@pytest.mark.integration
def test_modal_embed_smoke() -> None:  # pragma: no cover — needs Modal + Azure dev
    """Invoke the deployed Ditto function against dev Azure.

    Operator preconditions:

    * ``modal deploy services/ditto/src/ditto/modal_app.py`` succeeded
      against the ``dev`` Modal env.
    * ``KANTO_DITTO_SMOKE_ACCESSION``, ``_VERSION``, ``_OS_KEY`` set
      to a known protein FASTA already in dev Blob Storage.
    """
    accession = os.environ.get("KANTO_DITTO_SMOKE_ACCESSION")
    version_str = os.environ.get("KANTO_DITTO_SMOKE_VERSION")
    os_key = os.environ.get("KANTO_DITTO_SMOKE_OS_KEY")
    if not (accession and version_str and os_key):
        pytest.skip("set KANTO_DITTO_SMOKE_ACCESSION / _VERSION / _OS_KEY to run")

    import modal

    cls = modal.Cls.from_name("kanto-ditto", "DittoEmbedder")
    result = cls().embed.remote(
        accession=accession,
        version=int(version_str),
        os_key=os_key,
    )
    assert result["accession"] == accession
    assert result["proteins_embedded"] >= 1
    assert result["parquet_key"].endswith(".parquet")
