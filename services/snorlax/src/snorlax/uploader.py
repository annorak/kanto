"""Protein FASTA upload to Azure Blob Storage.

This module is a thin wrapper over :class:`kanto_commons.storage.ObjectStorage`
that handles the Snorlax-specific concerns:

* gzip compression of the Prodigal output (Prodigal writes plain text).
* The canonical OS key (``{accession}/{version}.faa.gz``) from
  :class:`kanto_commons.storage.KeyBuilder`.
* Blob metadata: source accession, schema version, produced timestamp.
* Idempotent overwrite semantics — the blob name is content-addressed
  on ``(accession, version)``, so retries of a successful upload are
  no-ops.

The upload is wrapped in a retry decorator at the kanto-commons layer;
we surface :class:`UploadError` here for the pipeline to dispatch on.
"""

from __future__ import annotations

import gzip
import io
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from kanto_commons.storage import (
    KeyBuilder,
    ObjectStorage,
    ObjectStorageError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UploadError(Exception):
    """Raised when the protein FASTA cannot be uploaded.

    The pipeline retries with backoff; on persistent failure the
    message is routed to the DLQ.
    """


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UploadedProtein:
    """Result of a successful upload."""

    container: str
    key: str
    size_compressed_bytes: int
    size_uncompressed_bytes: int


# ---------------------------------------------------------------------------
# Uploader
# ---------------------------------------------------------------------------


class ProteinUploader:
    """Compresses Prodigal output and stores it on Azure Blob."""

    # Content type per RFC 6713; ``application/gzip`` is the canonical
    # value for ``*.gz`` payloads regardless of the inner format.
    _CONTENT_TYPE = "application/gzip"

    def __init__(
        self,
        *,
        storage: ObjectStorage,
        key_builder: KeyBuilder,
    ) -> None:
        self._storage = storage
        self._key_builder = key_builder

    def upload(
        self,
        *,
        accession: str,
        version: int,
        protein_fasta_path: Path,
        protein_count: int,
        source_md5: str | None,
    ) -> UploadedProtein:
        """Compress ``protein_fasta_path`` and upload it.

        ``source_md5`` is the genome's md5 from NCBI (when known) — we
        stash it on the blob's user metadata for traceability so that
        operators can map an OS object back to the exact NCBI bytes it
        came from.

        Raises :class:`UploadError` on any storage-side failure. The
        underlying kanto-commons client retries transient errors
        internally; what surfaces here is final.
        """
        uncompressed_size = protein_fasta_path.stat().st_size
        compressed = _gzip_bytes(protein_fasta_path)
        compressed_size = len(compressed)

        key = self._key_builder.protein_fasta_key(accession, version)
        metadata = {
            "accession": accession,
            "version": str(version),
            "protein_count": str(protein_count),
            "schema_version": "1",
            "produced_at": datetime.now(UTC).isoformat(),
        }
        if source_md5 is not None:
            metadata["source_genome_md5"] = source_md5

        try:
            self._storage.put_bytes(
                container=self._key_builder.proteins_container,
                key=key,
                data=compressed,
                content_type=self._CONTENT_TYPE,
                metadata=metadata,
                # The blob is content-addressed on (accession, version)
                # so re-uploading the same bytes is a no-op for our
                # purposes; we let the storage client overwrite rather
                # than fail-on-exists. This is what "idempotent on
                # (accession, version)" means in the design doc.
                if_not_exists=False,
            )
        except ObjectStorageError as exc:
            raise UploadError(f"upload failed for {key}: {exc}") from exc

        logger.info(
            "snorlax.uploader: uploaded %s (%d B -> %d B)",
            key,
            uncompressed_size,
            compressed_size,
        )
        return UploadedProtein(
            container=self._key_builder.proteins_container,
            key=key,
            size_compressed_bytes=compressed_size,
            size_uncompressed_bytes=uncompressed_size,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gzip_bytes(path: Path) -> bytes:
    """Read ``path`` and return a gzip-compressed buffer of its contents.

    Prodigal output is small (a few hundred KB up to ~2 MB), so we
    materialize in memory — simpler than streaming and well within
    pod memory limits. If genomes ever grow beyond that we'd switch
    to a streamed upload, but that's not in scope for v1.
    """
    buf = io.BytesIO()
    with path.open("rb") as src, gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        # mtime=0 makes the compressed output deterministic; identical
        # input bytes produce identical gzipped output, which keeps the
        # idempotent overwrite truly idempotent at the blob-content
        # level too (not just at the key level).
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            gz.write(chunk)
    return buf.getvalue()


__all__ = ["ProteinUploader", "UploadError", "UploadedProtein"]
