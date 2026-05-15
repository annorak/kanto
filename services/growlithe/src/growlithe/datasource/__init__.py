"""DataSource adapter interface.

This package contains the :class:`DataSource` protocol every upstream
source plugs into, plus the v1 NCBI Pathogen Detection implementation
in :mod:`growlithe.datasource.ncbi`.

Adding a new source
-------------------

A new source (ENA, GISAID, customer upload, etc.) is added by:

1. Implementing :class:`DataSource` in a new module under this
   package. The protocol is intentionally small: a snapshot probe
   (cheap, just the upstream version), a metadata fetch (a TSV-like
   byte stream), an exceptions fetch (optional), and a parser
   (rows → :class:`IsolateDiscovered`).
2. Registering the implementation in
   :func:`growlithe.datasource.factory.build_data_sources` so the
   service loop picks it up.

The adapter is intentionally **pure**: it does not emit events,
update cursors, or write to Object Storage. Those are the pipeline's
job. Keeping the adapter pure keeps it testable and makes the
plugin contract small.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    # Imported only for the type alias used in ``ParsedSnapshot.events``.
    # Avoids a hard runtime dependency loop between this protocol and the
    # NCBI adapter that builds the events.
    from kanto_commons import IsolateDiscovered


@dataclass(frozen=True, slots=True)
class SnapshotRef:
    """Pointer to a single upstream snapshot.

    ``snapshot_id`` is the source-specific identifier (NCBI: PDG
    version, e.g. ``PDG000000001.4703``). Growlithe stores it in
    ``discovery_cursors.last_snapshot_id`` and uses it to decide
    whether a new poll cycle has anything to do.

    ``metadata_url`` and ``exceptions_url`` are opaque URLs the
    adapter knows how to fetch; the pipeline never parses them.
    """

    organism: str
    snapshot_id: str
    metadata_url: str
    exceptions_url: str | None = None


@dataclass(frozen=True, slots=True)
class FetchedSnapshot:
    """One snapshot, with its bytes loaded into memory.

    ``metadata_tsv_gz`` is the raw gzipped TSV. We store it gzipped
    so the snapshot-cache write is a byte-for-byte copy of what NCBI
    serves (after our HTTPS GET decompresses transport encoding).
    """

    ref: SnapshotRef
    metadata_tsv: bytes  # uncompressed
    exceptions_tsv: bytes | None  # uncompressed, or None if no exceptions file


@dataclass(frozen=True, slots=True)
class ParsedSnapshot:
    """Parser output for one snapshot.

    Carries both the events to emit and per-cycle stats the pipeline
    surfaces as metrics. Stats live alongside events so the protocol
    doesn't grow side-channel attributes on :class:`FetchedSnapshot`.
    """

    events: list[IsolateDiscovered]
    qc_filtered: int = 0
    missing_required: int = 0
    bad_version: int = 0


class DataSource(Protocol):
    """One upstream source of isolate metadata.

    The protocol is sync because each method is one HTTP round-trip;
    Growlithe overlaps organisms via asyncio + threads when needed.

    Methods
    -------
    name
        Short identifier — used in logs, metrics labels, and as the
        ``source`` field on emitted events.
    list_current_snapshots
        For each tracked organism, return the most recent snapshot
        the source has published. Cheap operation (one HEAD/listing
        per organism); used to decide whether to do anything.
    fetch_snapshot
        Pull the full metadata (and exceptions) for one snapshot.
        This is the expensive call, hundreds of MB for big organisms.
    parse_rows
        Turn the metadata bytes into IsolateDiscovered events. Pure
        function — no I/O. The optional ``exceptions_tsv`` on
        :class:`FetchedSnapshot` filters QC-failed rows; the optional
        ``previous_metadata_tsv`` suppresses already-emitted
        accessions when diffing.
    """

    @property
    def name(self) -> str: ...

    def list_current_snapshots(self) -> list[SnapshotRef]: ...

    def fetch_snapshot(self, ref: SnapshotRef) -> FetchedSnapshot: ...

    def parse_rows(
        self,
        snapshot: FetchedSnapshot,
        *,
        previous_metadata_tsv: bytes | None,
    ) -> ParsedSnapshot: ...


__all__ = [
    "DataSource",
    "FetchedSnapshot",
    "ParsedSnapshot",
    "SnapshotRef",
]
