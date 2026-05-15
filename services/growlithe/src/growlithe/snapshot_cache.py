"""Cache previous metadata snapshots in Azure Blob.

The cache lets the pipeline diff a new NCBI snapshot against the
previous one *without* re-reading the full upstream TSV every
cycle (the file would be re-read only at first poll after a cold
start). For the typical case — same-PDG-as-last-time — we don't
even fetch the new metadata: we observe that the snapshot id is
unchanged and skip the cycle.

Layout under ``kanto-metadata-{env}``:

``discovery/<source>/<organism>/<snapshot_id>.tsv.gz``
    Snapshot bytes as the source served them. Snapshot IDs are
    upstream-unique (NCBI PDG version, e.g. ``PDG000000001.4703``)
    so we don't need a timestamp suffix.

``discovery/<source>/<organism>/latest.txt``
    Plain-text pointer to the most recently cached snapshot ID.
    Read first; the snapshot file is read second only when the
    upstream ID has changed.
"""

from __future__ import annotations

import gzip
import logging

from kanto_commons.storage import (
    KeyBuilder,
    ObjectNotFoundError,
    ObjectStorage,
)

logger = logging.getLogger(__name__)


class SnapshotCache:
    """Read/write the per-source-organism snapshot cache."""

    def __init__(
        self,
        *,
        storage: ObjectStorage,
        container: str,
    ) -> None:
        self._storage = storage
        self._container = container

    # ---------------- Reads ----------------

    def latest_snapshot_id(self, *, source: str, organism: str) -> str | None:
        """Return the snapshot ID we cached last cycle, or None on cold start."""
        pointer_key = KeyBuilder.metadata_latest_pointer_key(source, organism)
        try:
            raw = self._storage.get_bytes(container=self._container, key=pointer_key)
        except ObjectNotFoundError:
            return None
        return raw.decode("utf-8").strip() or None

    def read_snapshot(
        self,
        *,
        source: str,
        organism: str,
        snapshot_id: str,
    ) -> bytes | None:
        """Return the cached uncompressed TSV bytes for ``snapshot_id``.

        Returns ``None`` rather than raising when the snapshot isn't
        present — that's "first time we've seen this snapshot ID" and
        the caller should diff against an empty baseline.
        """
        key = KeyBuilder.metadata_snapshot_key(source, organism, snapshot_id)
        try:
            gz_bytes = self._storage.get_bytes(container=self._container, key=key)
        except ObjectNotFoundError:
            return None
        try:
            return gzip.decompress(gz_bytes)
        except (gzip.BadGzipFile, OSError) as exc:
            # A corrupted cache entry shouldn't break the cycle: treat
            # as cache-miss so we diff against an empty baseline this
            # round and overwrite on the next write.
            logger.warning(
                "growlithe.cache: corrupt cache entry %s/%s/%s; treating as cold " "start: %s",
                source,
                organism,
                snapshot_id,
                exc,
            )
            return None

    # ---------------- Writes ----------------

    def write_snapshot(
        self,
        *,
        source: str,
        organism: str,
        snapshot_id: str,
        metadata_tsv: bytes,
    ) -> None:
        """Store ``metadata_tsv`` and update the ``latest.txt`` pointer.

        Pointer is written **after** the snapshot blob so a concurrent
        reader never sees a pointer to a snapshot that isn't there
        yet. (We won't have concurrent writers in v1 — there's only
        one Growlithe replica — but the ordering is free.)
        """
        snapshot_key = KeyBuilder.metadata_snapshot_key(source, organism, snapshot_id)
        gz_bytes = gzip.compress(metadata_tsv, compresslevel=6)
        self._storage.put_bytes(
            container=self._container,
            key=snapshot_key,
            data=gz_bytes,
            content_type="application/gzip",
            metadata={"source": source, "organism": organism, "snapshot_id": snapshot_id},
        )
        pointer_key = KeyBuilder.metadata_latest_pointer_key(source, organism)
        self._storage.put_bytes(
            container=self._container,
            key=pointer_key,
            data=snapshot_id.encode("utf-8"),
            content_type="text/plain",
        )


__all__ = ["SnapshotCache"]
