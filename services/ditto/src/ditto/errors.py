"""Exception hierarchy for Ditto.

The hierarchy mirrors the failure-mode table in task-07 §10:

* :class:`DittoError` — the base. Catch this at the Modal-function boundary
  to map onto Modal's per-invocation retry; everything else is a subclass.
* :class:`TransientError` — retry-eligible (network blip, transient Mew
  error, transient Event Hubs error). The Modal function re-raises so
  Modal's retry layer takes over.
* :class:`PermanentError` — re-running won't help (FASTA is malformed,
  embedding produced NaN, Mew refused the upsert on a deterministic
  constraint). The Modal function re-raises **without** retry intent;
  ``Function`` is configured to fail straight to the dead-letter path.
* :class:`InferenceOOMError` — special case: the GPU ran out of memory.
  The pipeline catches this and retries the offending genome with
  ``batch_size=1`` before declaring permanent failure.

Each subclass carries the (accession, version) where applicable so logs
and metrics stay correlatable across the failure handler boundary.
"""

from __future__ import annotations


class DittoError(Exception):
    """Base for every Ditto-side error."""


class TransientError(DittoError):
    """Re-running the function will probably succeed.

    Modal's retry layer re-invokes the function. Examples: Blob 503,
    Mew pool exhausted, Event Hubs broker rebalance.
    """


class PermanentError(DittoError):
    """Re-running will hit the same problem; route to dead-letter."""


class FastaParseError(PermanentError):
    """The protein FASTA is empty or malformed."""


class InferenceError(PermanentError):
    """ESM C produced NaN, inf, or otherwise unusable output."""


class InferenceOOMError(DittoError):
    """CUDA out-of-memory inside a batched forward pass.

    Not a permanent failure on its own — the pipeline retries the
    offending genome with ``batch_size=1`` (§Common Pitfalls), and only
    promotes to :class:`PermanentError` if that also OOMs.
    """


class ParquetWriteError(TransientError):
    """Blob write of the per-protein parquet failed after retries."""


class MewWriteError(TransientError):
    """Upsert into ``genome_embeddings`` failed after retries."""


class EventEmitError(TransientError):
    """Producing ``EmbeddingsReady`` to ``kanto.embedded`` failed.

    Note: by the time we raise this, the parquet and Mew writes have
    already succeeded. The Modal retry will re-do the (idempotent)
    writes and try the emit again. If retries are exhausted, the
    operator runs the backfill scanner — see README §Recovery.
    """


__all__ = [
    "DittoError",
    "EventEmitError",
    "FastaParseError",
    "InferenceError",
    "InferenceOOMError",
    "MewWriteError",
    "ParquetWriteError",
    "PermanentError",
    "TransientError",
]
