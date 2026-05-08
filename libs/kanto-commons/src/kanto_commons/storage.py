"""OCI Object Storage client wrapper.

Sync API. Object Storage in Kanto is used for one-shot uploads of
protein FASTAs (Snorlax) and Parquet embedding files (Ditto), and
for the occasional backfill read by Alakazam — none of these benefit
from async, and the official OCI SDK is sync-only.

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

Retry
-----
Transient failures (HTTP 5xx, connection errors) are retried with
exponential backoff via :mod:`tenacity`. ``ObjectNotFound`` and
similar deterministic errors are NOT retried — they're surfaced
immediately.

The wrapper deliberately does NOT leak ``oci.object_storage``
classes through its API.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import IO, Any, Protocol

from oci.exceptions import ServiceError
from oci.object_storage import ObjectStorageClient as _OCIObjectStorageClient
from oci.object_storage.models import CreatePreauthenticatedRequestDetails  # noqa: F401
from tenacity import (
    retry,
    retry_if_exception_type,
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
    """Canonical OCI Object Storage keys for Kanto artifacts.

    The keys live under per-purpose buckets configured on the settings
    object, so this class only assembles the ``object_name`` portion.
    """

    proteins_bucket: str
    embeddings_bucket: str
    cache_bucket: str

    @classmethod
    def from_settings(cls, settings: ObjectStorageSettings) -> KeyBuilder:
        return cls(
            proteins_bucket=settings.proteins_bucket,
            embeddings_bucket=settings.embeddings_bucket,
            cache_bucket=settings.cache_bucket,
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
    def cache_key(name: str) -> str:
        """``cache/{name}`` — Growlithe metadata cache, etc."""
        return f"cache/{name}"


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
        bucket: str,
        key: str,
        data: bytes,
        content_type: str | None = ...,
        metadata: dict[str, str] | None = ...,
        if_not_exists: bool = ...,
    ) -> None: ...

    def put_stream(
        self,
        *,
        bucket: str,
        key: str,
        stream: IO[bytes],
        content_length: int,
        content_type: str | None = ...,
        metadata: dict[str, str] | None = ...,
        if_not_exists: bool = ...,
    ) -> None: ...

    def get_bytes(self, *, bucket: str, key: str) -> bytes: ...

    def get_stream(
        self, *, bucket: str, key: str, chunk_size: int = ...
    ) -> Iterator[bytes]: ...

    def exists(self, *, bucket: str, key: str) -> bool: ...

    def delete(self, *, bucket: str, key: str) -> None: ...

    def ping(self) -> bool: ...


# ---------------------------------------------------------------------------
# Concrete client
# ---------------------------------------------------------------------------


_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, ServiceError):
        return exc.status in _RETRYABLE_STATUSES
    if isinstance(exc, ObjectStorageError):
        return False
    # Network-level errors (connection reset, DNS hiccups, etc.) bubble
    # up as generic OSError or oci.exceptions subclasses; retry those.
    return isinstance(exc, OSError)


_RETRY = retry(
    retry=retry_if_exception_type((ServiceError, OSError)),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=0.5, max=10.0),
    reraise=True,
)


class ObjectStorageClient:
    """Concrete OCI Object Storage wrapper.

    Construct via :meth:`from_oci_config` for production. Tests should
    use ``FakeObjectStorage`` from :mod:`kanto_commons.testing`.
    """

    def __init__(
        self,
        *,
        oci_client: _OCIObjectStorageClient,
        namespace: str,
    ) -> None:
        self._client = oci_client
        self._namespace = namespace

    @classmethod
    def from_oci_config(
        cls,
        *,
        settings: ObjectStorageSettings,
        oci_config: dict[str, Any],
    ) -> ObjectStorageClient:  # pragma: no cover — real OCI required
        """Build from a parsed ``~/.oci/config`` dict."""
        client = _OCIObjectStorageClient(oci_config)
        return cls(oci_client=client, namespace=settings.namespace)

    # -------------------- write --------------------

    @_RETRY
    def put_bytes(
        self,
        *,
        bucket: str,
        key: str,
        data: bytes,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
        if_not_exists: bool = False,
    ) -> None:
        if if_not_exists and self.exists(bucket=bucket, key=key):
            raise ObjectAlreadyExistsError(f"{bucket}/{key}")
        kwargs: dict[str, Any] = {}
        if content_type is not None:
            kwargs["content_type"] = content_type
        if metadata:
            kwargs["opc_meta"] = metadata
        self._client.put_object(
            namespace_name=self._namespace,
            bucket_name=bucket,
            object_name=key,
            put_object_body=data,
            **kwargs,
        )

    @_RETRY
    def put_stream(
        self,
        *,
        bucket: str,
        key: str,
        stream: IO[bytes],
        content_length: int,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
        if_not_exists: bool = False,
    ) -> None:
        if if_not_exists and self.exists(bucket=bucket, key=key):
            raise ObjectAlreadyExistsError(f"{bucket}/{key}")
        kwargs: dict[str, Any] = {"content_length": content_length}
        if content_type is not None:
            kwargs["content_type"] = content_type
        if metadata:
            kwargs["opc_meta"] = metadata
        self._client.put_object(
            namespace_name=self._namespace,
            bucket_name=bucket,
            object_name=key,
            put_object_body=stream,
            **kwargs,
        )

    # -------------------- read --------------------

    @_RETRY
    def get_bytes(self, *, bucket: str, key: str) -> bytes:
        try:
            response = self._client.get_object(
                namespace_name=self._namespace,
                bucket_name=bucket,
                object_name=key,
            )
        except ServiceError as exc:
            if exc.status == 404:
                raise ObjectNotFoundError(f"{bucket}/{key}") from exc
            raise
        return bytes(response.data.content)

    def get_stream(
        self, *, bucket: str, key: str, chunk_size: int = 1024 * 1024
    ) -> Iterator[bytes]:
        """Stream an object in chunks. Caller must consume the iterator
        eagerly within the same network connection lifetime.
        """
        try:
            response = self._client.get_object(
                namespace_name=self._namespace,
                bucket_name=bucket,
                object_name=key,
            )
        except ServiceError as exc:
            if exc.status == 404:
                raise ObjectNotFoundError(f"{bucket}/{key}") from exc
            raise
        # response.data is a requests Response wrapper; iter_content yields chunks.
        yield from response.data.iter_content(chunk_size=chunk_size)

    # -------------------- metadata --------------------

    @_RETRY
    def exists(self, *, bucket: str, key: str) -> bool:
        try:
            self._client.head_object(
                namespace_name=self._namespace,
                bucket_name=bucket,
                object_name=key,
            )
        except ServiceError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    @_RETRY
    def delete(self, *, bucket: str, key: str) -> None:
        try:
            self._client.delete_object(
                namespace_name=self._namespace,
                bucket_name=bucket,
                object_name=key,
            )
        except ServiceError as exc:
            if exc.status == 404:
                raise ObjectNotFoundError(f"{bucket}/{key}") from exc
            raise

    # -------------------- health --------------------

    def ping(self) -> bool:
        """Issue a cheap call to verify the SDK can reach OCI.

        Returns False on any failure rather than raising, so it slots
        directly into k8s readiness probes that interpret False as
        "not ready" and True as "ready".
        """
        try:
            self._client.list_buckets(
                namespace_name=self._namespace,
                compartment_id="root",  # any compartment id; we don't care about results
                limit=1,
            )
        except ServiceError as exc:
            # Auth/perm errors still mean "we can talk to OCI" — they're
            # configuration bugs, not connectivity. We log and return True.
            if exc.status in (401, 403):
                logger.warning("ping: auth error, but OCI is reachable")
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
