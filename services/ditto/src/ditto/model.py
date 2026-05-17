"""ESM C 600M inference wrapper.

Loaded once per Modal worker at ``@modal.enter()`` time so the ~30s
weight load is paid per cold start, not per invocation.

Why we bypass ``ESMC.logits()``
-------------------------------
The published example loops ``client.logits()`` per protein, which
serialises forward passes -- defeating batching. The ``ESMC`` module
exposes ``forward(sequence_tokens=...)`` taking a padded ``(B, L)``
tensor and returning ``ESMCOutput.embeddings`` of shape
``(B, L, d_model)``. We use that directly so length-grouped batches
actually run as batches on the GPU.

Mean-pool semantics
-------------------
We mean over non-padding token positions only:
``embeddings.sum(dim=1) / lengths_with_specials.unsqueeze(1)``. ESM
C's BOS/EOS tokens sit at positions 0 and L-1 of the un-padded slice
and are included in the mean, matching the reference behavior of
``client.logits()`` for ``mean_embedding``.

Precision
---------
bf16 throughout. bf16 has fp32's exponent range so attention scores
don't overflow without an autocast block. Final embeddings are cast
to fp16 before leaving the GPU to match the parquet schema.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from ditto.batching import Batch
from ditto.errors import InferenceError, InferenceOOMError

logger = logging.getLogger(__name__)


class ESMCEmbedder:
    """Wraps a loaded ESM C model + tokenizer for batched inference."""

    def __init__(
        self,
        *,
        model: Any,
        tokenizer: Any,
        device: str,
        embedding_dim: int,
    ) -> None:
        # model/tokenizer typed as Any so this module's stubs don't
        # require torch/esm at import time (matters on macOS dev boxes).
        self._model = model
        self._tokenizer = tokenizer
        self._device = device
        self._embedding_dim = embedding_dim

    @property
    def embedding_dim(self) -> int:
        return self._embedding_dim

    def embed_batch(self, batch: Batch) -> np.ndarray:
        """Run one batched forward pass and mean-pool to per-protein vectors.

        Returns a ``(batch_size, embedding_dim)`` float16 ndarray in
        the same protein order as ``batch.proteins``.

        Raises
        ------
        InferenceOOMError
            CUDA OOM. The pipeline retries the offending batch at
            ``batch_size=1`` per task-07 section 10.
        InferenceError
            NaN/inf in output (rare; usually pathological residues).
        """
        # Lazy torch import: tests can import this module without torch.
        import torch

        sequences = [p.sequence for p in batch.proteins]
        lengths = batch.sequence_lengths

        try:
            encoded = self._tokenizer(
                sequences,
                padding=True,
                return_tensors="pt",
                add_special_tokens=True,
            )
            input_ids = encoded["input_ids"].to(self._device)

            with torch.no_grad():
                output = self._model.forward(sequence_tokens=input_ids)

            hidden = output.embeddings  # (B, L, d_model)
            if hidden is None:
                raise InferenceError("ESMCOutput.embeddings is None")

            # Length-aware mean pool. Special-token-aware: BOS + EOS
            # add 2 positions. We build the mask from `lengths` rather
            # than scanning input_ids for the pad token because the pad
            # token id is not stable across model revisions.
            seq_len_padded = hidden.shape[1]
            lengths_with_specials = torch.tensor(
                [length + 2 for length in lengths],
                dtype=torch.long,
                device=self._device,
            )
            position_index = torch.arange(seq_len_padded, device=self._device).unsqueeze(0)
            mask = (position_index < lengths_with_specials.unsqueeze(1)).to(hidden.dtype)
            summed = (hidden * mask.unsqueeze(-1)).sum(dim=1)
            mean = summed / lengths_with_specials.unsqueeze(1).to(hidden.dtype)

            result: np.ndarray = mean.detach().to(dtype=torch.float16).cpu().numpy()
        except torch.cuda.OutOfMemoryError as exc:
            # Free the cache so the fallback batch_size=1 retry has room.
            torch.cuda.empty_cache()
            raise InferenceOOMError(
                f"CUDA OOM at batch_size={batch.size}, longest={batch.longest_length}"
            ) from exc

        if not np.all(np.isfinite(result)):
            raise InferenceError(
                f"non-finite values in batch (size={batch.size}, "
                f"longest={batch.longest_length})"
            )
        if result.shape != (batch.size, self._embedding_dim):
            raise InferenceError(
                f"unexpected output shape {result.shape}; expected "
                f"({batch.size}, {self._embedding_dim})"
            )
        return result


def load_embedder(
    *,
    model_id: str,
    embedding_dim: int,
    device: str = "cuda",
) -> ESMCEmbedder:
    """Load ESM C 600M and prepare it for inference.

    Pulls the weights via the ``esm`` SDK's factory, moves to the
    device, switches to ``eval`` mode, casts to bfloat16, and wraps.
    Invoked once per Modal worker from ``@modal.enter()``.
    """
    import torch
    from esm.models.esmc import ESMC

    logger.info("ditto.model: loading ESM C %s on %s", model_id, device)
    client = ESMC.from_pretrained(model_id).to(device)
    client.eval()
    # bf16: same exponent range as fp32, so attention scores don't
    # overflow without an autocast block.
    client.to(dtype=torch.bfloat16)
    return ESMCEmbedder(
        model=client,
        tokenizer=client.tokenizer,
        device=device,
        embedding_dim=embedding_dim,
    )


__all__ = ["ESMCEmbedder", "load_embedder"]
