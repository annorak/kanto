"""Blob Storage read/write for Ditto.

Thin wrapper over :class:`kanto_commons.storage.ObjectStorageClient`.
We keep a wrapper rather than passing the client around bare so that:

1. The pipeline depends on this module's narrow Protocol (just the
   two methods Ditto uses), not the full storage Protocol — easier
   to mock in tests.
2. Cross-cloud-egress logging is centralised here. The benchmark
   script (see ``ditto/scripts/benchmark.py``) reads timing from
   structured log fields emitted here.
3. The error-mapping (ObjectNotFoundError → PermanentError, network
   → TransientError) happens at one site.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol

from kanto_commons.storage import (
    ObjectNotFoundError,
    ObjectStorage,
    ObjectStorageError,
)

from ditto.errors import ParquetWriteError, PermanentError, TransientError

logger = logging.getLogger(__name__)


class BlobIO(Protocol):
    """Narrow surface the pipeline depends on."""

    def fetch_protein_fasta(self, *, container: str, key: str) -> bytes: ...

    def write_parquet(
        self,
        *,
        container: str,
        key: str,
        data: bytes,
    ) -> None: ...


class AzureBlobIO:
    """Blob IO operations Ditto uses, with timing logs for the benchmark.

    Wraps the shared :class:`ObjectStorage` protocol; tests inject
    :class:`kanto_commons.testing.FakeObjectStorage` here.
    """

    def __init__(self, *, storage: ObjectStorage) -> None:
        self._storage = storage

    def fetch_protein_fasta(self, *, container: str, key: str) -> bytes:
        """Read the (gzipped) FASTA blob.

        Maps the deterministic ``ObjectNotFoundError`` to
        :class:`PermanentError` because there's no point in Modal
        retrying — the file isn't going to materialise. Other storage
        errors are treated as transient.
        """
        started = time.monotonic()
        try:
            data = self._storage.get_bytes(container=container, key=key)
        except ObjectNotFoundError as exc:
            raise PermanentError(
                f"protein FASTA not found at {container}/{key}: the upstream "
                "Snorlax invocation likely never completed the upload"
            ) from exc
        except ObjectStorageError as exc:
            raise TransientError(f"blob read failed for {container}/{key}: {exc}") from exc
        elapsed = time.monotonic() - started
        logger.info(
            "ditto.azure_io.fetch_protein_fasta container=%s key=%s " "bytes=%d elapsed_s=%.3f",
            container,
            key,
            len(data),
            elapsed,
        )
        return data

    def write_parquet(
        self,
        *,
        container: str,
        key: str,
        data: bytes,
    ) -> None:
        """Upload the per-protein parquet.

        Overwrite semantics: idempotent re-runs replace the file with
        a byte-identical copy (deterministic batch order from
        :mod:`ditto.batching` + deterministic mean-pool). The
        upload is bounded by the underlying tenacity retry inside
        kanto_commons.storage; if it still fails we wrap in
        :class:`ParquetWriteError` (a :class:`TransientError` subclass)
        so Modal's outer retry layer takes over.
        """
        started = time.monotonic()
        try:
            self._storage.put_bytes(
                container=container,
                key=key,
                data=data,
                content_type="application/vnd.apache.parquet",
                metadata={"producer": "ditto"},
                if_not_exists=False,
            )
        except ObjectStorageError as exc:
            raise ParquetWriteError(f"parquet write failed for {container}/{key}: {exc}") from exc
        elapsed = time.monotonic() - started
        logger.info(
            "ditto.azure_io.write_parquet container=%s key=%s " "bytes=%d elapsed_s=%.3f",
            container,
            key,
            len(data),
            elapsed,
        )


__all__ = ["AzureBlobIO", "BlobIO"]
