"""Growlithe — the Discoverer service.

Polls NCBI Pathogen Detection (and, in v2, ENA / GISAID / customer
submissions) and emits one IsolateDiscovered event per new isolate
to the ``kanto.discovered`` Event Hubs topic. Cursors live in Mew
(``discovery_cursors``); previous metadata snapshots live in the
``kanto-metadata-{env}`` Blob container so diffs don't re-read the
full upstream TSV on every poll.

This package is intentionally small. The DataSource adapter
(``growlithe.datasource``) is the place to add a new upstream — see
the package docstring there.
"""

from __future__ import annotations

__all__: list[str] = []
