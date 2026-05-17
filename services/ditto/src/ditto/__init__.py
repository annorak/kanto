"""Ditto — the GPU embedder service.

Layout
------
* :mod:`ditto.modal_app` — Modal app + function definition
  (``modal deploy services/ditto/src/ditto/modal_app.py``).
* :mod:`ditto.pipeline` — orchestrates one ``(accession, version)`` end to
  end. Pure Python; no Modal types in its signature.
* :mod:`ditto.model` — ESM C 600M wrapper. Loaded once per worker.
* :mod:`ditto.batching` — length-grouped batching (task §3, mandatory).
* :mod:`ditto.aggregate` — length-weighted mean over per-protein embeddings.
* :mod:`ditto.fasta` — FASTA parsing for the gzipped Snorlax output.
* :mod:`ditto.parquet_io` — Arrow/Parquet schema + writer.
* :mod:`ditto.azure_io` — Blob Storage read/write wrapper.
* :mod:`ditto.event_emit` — EmbeddingsReady producer wrapper.
* :mod:`ditto.config` — service settings (subclass of KantoBaseSettings).
* :mod:`ditto.errors` — exception hierarchy.

The top-level ``__init__`` keeps imports minimal so test runners and
the cost-monitor CLI can import :mod:`ditto._version` without pulling
in torch or modal.
"""

from ditto._version import __version__

__all__ = ["__version__"]
