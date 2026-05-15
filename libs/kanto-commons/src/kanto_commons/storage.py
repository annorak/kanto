"""Azure Blob Storage client wrapper.

Sync API. Blob Storage in Kanto is used for one-shot uploads of
protein FASTAs (Snorlax) and Parquet embedding files (Ditto), the
metadata-snapshot cache (Growlithe), and the occasional backfill read
by Alakazam — none of these benefit from async, and the sync
azure-storage-blob client is well-supported and dependency-light.

Public API
----------
:class:`ObjectStorageClient` exposes:

* :meth:`put_bytes` — upload a small in-memory blob.
* :meth:`put_stream` — upload a large object from a file-like.
* :meth:`get_bytes` — fetch a small object.
* :meth:`get_stream` — iterate chunks of a large object without loading
  it all into memory.
* :meth:`exists` — head check used for write-if-not-exists semantics.
* :meth:`delete` — used by tests and DLQ-cleanup.
* :meth:`ping` — health check for k8s readiness probes.

Plus key helpers under :class:`KeyBuilder`.

Authentication
--------------
``ObjectStorageClient.from_settings`` uses
:class:`azure.identity.DefaultAzureCredential`, which transparently
picks up:

* AKS Workload Identity in pods (the production path).
* ``az login`` locally on a developer laptop.
* Environment-variable credentials in CI.

For tests, construct the wrapper from a connection string via
``ObjectStorageClient.from_connection_string``, or use
``FakeObjectStorage`` from :mod:`kanto_commons.testing`.

Retry
-----
Transient failures (HTTP 5xx, 429, network errors) are retried with
exponential backoff via :mod:`tenacity`. ``ObjectNotFoundError`` and
similar deterministic errors are NOT retried — they're surfaced
immediately.

The wrapper deliberately does NOT leak ``azure.storage.blob`` types
through its API.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import IO, Any, Protocol

from azure.core.credentials import TokenCredential
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
)
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from kanto_commons.config import ObjectStorageSettings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ObjectStorageError(Exception):
    """Base for every error this module raises."""


class ObjectNotFoundError(ObjectStorageError):
    """The requested key does not exist."""


class ObjectAlreadyExistsError(ObjectStorageError):
    """``put`` was called with ``if_not_exists=True`` and the key exists."""


# ---------------------------------------------------------------------------
# Key conventions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeyBuilder:
    """Canonical Azure Blob keys for Kanto artifacts.

    The keys live under per-purpose containers configured on the
    settings object, so this class only assembles the blob name portion.
    """

    proteins_container: str
    embeddings_container: str
    metadata_container: str

    @classmethod
    def from_settings(cls, settings: ObjectStorageSettings) -> KeyBuilder:
        return cls(
            proteins_container=settings.proteins_container,
            embeddings_container=settings.embeddings_container,
            metadata_container=settings.metadata_container,
        )

    @staticmethod
    def protein_fasta_key(accession: str, version: int) -> str:
        """``{accession}/{version}.faa.gz`` — Snorlax → Ditto handoff."""
        return f"{accession}/{version}.faa.gz"

    @staticmethod
    def embedding_parquet_key(accession: str, version: int) -> str:
        """``{accession}/{version}.parquet`` — Ditto per-protein output."""
        return f"{accession}/{version}.parquet"

    @staticmethod
    def metadata_snapshot_key(source: str, organism: str, snapshot_id: str) -> str:
        """``discovery/{source}/{organism}/{snapshot_id}.tsv.gz``.

        Used by Growlithe to cache the previous metadata snapshot for
        diffing. ``snapshot_id`` is typically a PDG version string
        (NCBI) — anything stable per source-organism cycle.
        """
        return f"discovery/{source}/{organism}/{snapshot_id}.tsv.gz"

    @staticmethod
    def metadata_latest_pointer_key(source: str, organism: str) -> str:
        """``discovery/{source}/{organism}/latest.txt``.

        Points to the snapshot_id most recently cached. Growlithe reads
        this first to find the previous snapshot for diffing.
        """
        return f"discovery/{source}/{organism}/latest.txt"


# ---------------------------------------------------------------------------
# Protocol — services depend on this, not on the concrete class.
# ---------------------------------------------------------------------------


class ObjectStorage(Protocol):
    """Subset of the wrapper that services should depend on.

    The concrete :class:`ObjectStorageClient` implements it; the
    in-memory ``FakeObjectStorage`` in
    :mod:`kanto_commons.testing` also implements it. Services can take
    this protocol as a parameter and accept either.
    """

    def put_bytes(
        self,
        *,
        container: str,
        key: str,
        data: bytes,
        content_type: str | None = ...,
        metadata: dict[str, str] | None = ...,
        if_not_exists: bool = ...,
    ) -> None: ...

    def put_stream(
        self,
        *,
        container: str,
        key: str,
        stream: IO[bytes],
        content_length: int,
        content_type: str | None = ...,
        metadata: dict[str, str] | None = ...,
        if_not_exists: bool = ...,
    ) -> None: ...

    def get_bytes(self, *, container: str, key: str) -> bytes: ...

    def get_stream(
        self, *, container: str, key: str, chunk_size: int = ...
    ) -> Iterator[bytes]: ...

    def exists(self, *, container: str, key: str) -> bool: ...

    def delete(self, *, container: str, key: str) -> None: ...

    def ping(self) -> bool: ...


# ---------------------------------------------------------------------------
# Concrete client
# ---------------------------------------------------------------------------


_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def _is_retryable(exc: BaseException) -> bool:
    """Retry network + transient-5xx errors; surface 4xx + own errors.

    ResourceNotFoundError / ResourceExistsError are deterministic and
    must NOT be retried. ObjectStorageError subclasses originate in
    this module and are similarly deterministic.
    """
    if isinstance(exc, ResourceNotFoundError | ResourceExistsError):
        return False
    if isinstance(exc, ObjectStorageError):
        return False
    if isinstance(exc, HttpResponseError):
        status = getattr(exc, "status_code", None)
        return status in _RETRYABLE_STATUSES
    return isinstance(exc, OSError)


_RETRY = retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=0.5, max=10.0),
    reraise=True,
)


class ObjectStorageClient:
    """Concrete Azure Blob Storage wrapper.

    Construct via :meth:`from_settings` for production
    (DefaultAzureCredential — workload identity in cluster, az login
    locally) or :meth:`from_connection_string` for local development
    against Azurite or a real storage account key. Tests should use
    ``FakeObjectStorage`` from :mod:`kanto_commons.testing`.
    """

    def __init__(self, *, blob_service: BlobServiceClient) -> None:
        self._service = blob_service

    @classmethod
    def from_settings(
        cls,
        *,
        settings: ObjectStorageSettings,
        credential: TokenCredential | None = None,
    ) -> ObjectStorageClient:  # pragma: no cover — real Azure required
        """Build from settings using workload identity by default.

        ``credential`` defaults to :class:`DefaultAzureCredential`,
        which discovers AKS workload identity, az-cli login, env-var
        creds, etc., in order.
        """
        cred = credential or DefaultAzureCredential()
        service = BlobServiceClient(
            account_url=settings.account_url,
            credential=cred,
            max_single_get_size=settings.max_single_get_size,
            max_single_put_size=settings.max_single_put_size,
        )
        return cls(blob_service=service)

    @classmethod
    def from_connection_string(
        cls, connection_string: str
    ) -> ObjectStorageClient:  # pragma: no cover — used by local dev only
        """Build from a full Azure Storage connection string.

        Suitable for Azurite or for an operator who already exported
        ``AZURE_STORAGE_CONNECTION_STRING`` for a one-off task. Not
        used in cluster — workload identity is the production path.
        """
        service = BlobServiceClient.from_connection_string(connection_string)
        return cls(blob_service=service)

    # -------------------- write --------------------

    @_RETRY
    def put_bytes(
        self,
        *,
        container: str,
        key: str,
        data: bytes,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
        if_not_exists: bool = False,
    ) -> None:
        blob = self._service.get_blob_client(container=container, blob=key)
        content_settings = (
            ContentSettings(content_type=content_type) if content_type else None
        )
        kwargs: dict[str, Any] = {
            "overwrite": not if_not_exists,
        }
        if content_settings is not None:
            kwargs["content_settings"] = content_settings
        if metadata:
            kwargs["metadata"] = metadata
        # Azure's native "create if absent" path: If-None-Match=* fails the
        # request when the blob exists. We translate to our error type.
        if if_not_exists:
            kwargs["match_condition"] = None  # explicit; If-None-Match handled below
        try:
            blob.upload_blob(data, **kwargs)
        except ResourceExistsError as exc:
            raise ObjectAlreadyExistsError(f"{container}/{key}") from exc

    @_RETRY
    def put_stream(
        self,
        *,
        container: str,
        key: str,
        stream: IO[bytes],
        content_length: int,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
        if_not_exists: bool = False,
    ) -> None:
        blob = self._service.get_blob_client(container=container, blob=key)
        content_settings = (
            ContentSettings(content_type=content_type) if content_type else None
        )
        kwargs: dict[str, Any] = {
            "overwrite": not if_not_exists,
            "length": content_length,
        }
        if content_settings is not None:
            kwargs["content_settings"] = content_settings
        if metadata:
            kwargs["metadata"] = metadata
        try:
            blob.upload_blob(stream, **kwargs)
        except ResourceExistsError as exc:
            raise ObjectAlreadyExistsError(f"{container}/{key}") from exc

    # -------------------- read --------------------

    @_RETRY
    def get_bytes(self, *, container: str, key: str) -> bytes:
        blob = self._service.get_blob_client(container=container, blob=key)
        try:
            downloader = blob.download_blob()
            return downloader.readall()
        except ResourceNotFoundError as exc:
            raise ObjectNotFoundError(f"{container}/{key}") from exc

    def get_stream(
        self, *, container: str, key: str, chunk_size: int = 1024 * 1024
    ) -> Iterator[bytes]:
        """Stream a blob in chunks.

        Not wrapped with :data:`_RETRY` because the iterator is consumed
        lazily; a retry decorator on the generator would only retry the
        creation, not individual chunk fetches. The SDK retries chunk
        reads internally.
        """
        blob = self._service.get_blob_client(container=container, blob=key)
        try:
            downloader = blob.download_blob()
        except ResourceNotFoundError as exc:
            raise ObjectNotFoundError(f"{container}/{key}") from exc
        yield from downloader.chunks()
        # chunk_size is honored by the SDK at downloader-construction time
        # via max_chunk_get_size on the BlobServiceClient; the parameter is
        # kept on the API for backwards-compat with the OS-era wrapper.
        _ = chunk_size

    # -------------------- metadata --------------------

    @_RETRY
    def exists(self, *, container: str, key: str) -> bool:
        blob = self._service.get_blob_client(container=container, blob=key)
        try:
            return blob.exists()
        except ResourceNotFoundError:
            # Some SDK versions raise instead of returning False.
            return False

    @_RETRY
    def delete(self, *, container: str, key: str) -> None:
        blob = self._service.get_blob_client(container=container, blob=key)
        try:
            blob.delete_blob()
        except ResourceNotFoundError as exc:
            raise ObjectNotFoundError(f"{container}/{key}") from exc

    # -------------------- health --------------------

    def ping(self) -> bool:
        """Cheap reachability probe for k8s readiness.

        Returns True if the service responds (even with an auth error —
        that's a config bug, not a connectivity one). Returns False on
        any transport / DNS / TLS failure.
        """
        try:
            self._service.get_service_properties()
        except HttpResponseError as exc:
            status = getattr(exc, "status_code", None)
            if status in (401, 403):
                logger.warning("ping: auth error, but Azure Blob is reachable")
                return True
            return False
        except Exception:
            return False
        return True


__all__ = [
    "KeyBuilder",
    "ObjectAlreadyExistsError",
    "ObjectNotFoundError",
    "ObjectStorage",
    "ObjectStorageClient",
    "ObjectStorageError",
]
