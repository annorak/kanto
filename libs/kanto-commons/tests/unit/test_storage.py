"""Unit tests for the Object Storage wrapper.

Two surfaces under test:

* The in-memory ``FakeObjectStorage`` (services use this in their
  unit tests so we owe them confidence it behaves like the real
  thing).
* ``ObjectStorageClient``: we mock the underlying OCI SDK and
  assert the wrapper translates 404 → ``ObjectNotFoundError``,
  honors ``if_not_exists``, and routes calls through retry.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from kanto_commons.config import ObjectStorageSettings
from kanto_commons.storage import (
    KeyBuilder,
    ObjectAlreadyExistsError,
    ObjectNotFoundError,
    ObjectStorageClient,
)
from kanto_commons.testing import FakeObjectStorage
from oci.exceptions import ServiceError

# ---------------------------------------------------------------------------
# KeyBuilder
# ---------------------------------------------------------------------------


def test_key_builder_protein_fasta() -> None:
    assert KeyBuilder.protein_fasta_key("PDT001", 2) == "PDT001/2.faa.gz"


def test_key_builder_embedding_parquet() -> None:
    assert KeyBuilder.embedding_parquet_key("PDT001", 2) == "PDT001/2.parquet"


def test_key_builder_cache_key() -> None:
    assert KeyBuilder.cache_key("ncbi-listing.tsv") == "cache/ncbi-listing.tsv"


def test_key_builder_from_settings() -> None:
    settings = ObjectStorageSettings(  # type: ignore[call-arg]
        namespace="ns",
        region="r",
    )
    kb = KeyBuilder.from_settings(settings)
    assert kb.proteins_bucket == "kanto-proteins"
    assert kb.embeddings_bucket == "kanto-embeddings"
    assert kb.cache_bucket == "kanto-cache"


# ---------------------------------------------------------------------------
# Fake — round-trips, exists, delete, metadata, idempotency
# ---------------------------------------------------------------------------


def test_fake_put_then_get_returns_same_bytes() -> None:
    os = FakeObjectStorage()
    os.put_bytes(bucket="b", key="k", data=b"hello")
    assert os.get_bytes(bucket="b", key="k") == b"hello"


def test_fake_get_missing_raises_not_found() -> None:
    os = FakeObjectStorage()
    with pytest.raises(ObjectNotFoundError):
        os.get_bytes(bucket="b", key="missing")


def test_fake_exists_true_and_false() -> None:
    os = FakeObjectStorage()
    os.put_bytes(bucket="b", key="k", data=b"x")
    assert os.exists(bucket="b", key="k")
    assert not os.exists(bucket="b", key="other")


def test_fake_delete_removes_object() -> None:
    os = FakeObjectStorage()
    os.put_bytes(bucket="b", key="k", data=b"x")
    os.delete(bucket="b", key="k")
    assert not os.exists(bucket="b", key="k")


def test_fake_delete_missing_raises() -> None:
    os = FakeObjectStorage()
    with pytest.raises(ObjectNotFoundError):
        os.delete(bucket="b", key="missing")


def test_fake_if_not_exists_blocks_overwrite() -> None:
    os = FakeObjectStorage()
    os.put_bytes(bucket="b", key="k", data=b"first")
    with pytest.raises(ObjectAlreadyExistsError):
        os.put_bytes(bucket="b", key="k", data=b"second", if_not_exists=True)
    # Default behaviour overwrites without complaint.
    os.put_bytes(bucket="b", key="k", data=b"second")
    assert os.get_bytes(bucket="b", key="k") == b"second"


def test_fake_metadata_round_trip() -> None:
    os = FakeObjectStorage()
    os.put_bytes(bucket="b", key="k", data=b"x", metadata={"author": "snorlax"})
    assert os.get_metadata(bucket="b", key="k") == {"author": "snorlax"}


def test_fake_stream_chunks() -> None:
    os = FakeObjectStorage()
    os.put_bytes(bucket="b", key="k", data=b"abcdefghij")
    chunks = list(os.get_stream(bucket="b", key="k", chunk_size=3))
    assert chunks == [b"abc", b"def", b"ghi", b"j"]


def test_fake_put_stream_round_trip() -> None:
    os = FakeObjectStorage()
    src = io.BytesIO(b"streamed-content")
    os.put_stream(bucket="b", key="k", stream=src, content_length=16)
    assert os.get_bytes(bucket="b", key="k") == b"streamed-content"


def test_fake_all_keys_sorted() -> None:
    os = FakeObjectStorage()
    os.put_bytes(bucket="b", key="z", data=b"z")
    os.put_bytes(bucket="b", key="a", data=b"a")
    assert os.all_keys("b") == ["a", "z"]


def test_fake_ping_always_true() -> None:
    os = FakeObjectStorage()
    assert os.ping() is True


def test_fake_reset() -> None:
    os = FakeObjectStorage()
    os.put_bytes(bucket="b", key="k", data=b"x")
    os.reset()
    assert not os.exists(bucket="b", key="k")


# ---------------------------------------------------------------------------
# Real wrapper, with the OCI SDK mocked.
# ---------------------------------------------------------------------------


def _mk_service_error(status: int, code: str = "Error") -> ServiceError:
    return ServiceError(
        status=status,
        code=code,
        headers={},
        message="mocked",
    )


@pytest.fixture
def mock_oci() -> Iterator[MagicMock]:
    yield MagicMock()


def test_wrapper_get_404_translates_to_not_found(mock_oci: MagicMock) -> None:
    mock_oci.get_object.side_effect = _mk_service_error(404, "ObjectNotFound")
    client = ObjectStorageClient(oci_client=mock_oci, namespace="ns")
    with pytest.raises(ObjectNotFoundError):
        client.get_bytes(bucket="b", key="k")


def test_wrapper_get_returns_response_bytes(mock_oci: MagicMock) -> None:
    response = MagicMock()
    response.data.content = b"payload"
    mock_oci.get_object.return_value = response
    client = ObjectStorageClient(oci_client=mock_oci, namespace="ns")
    assert client.get_bytes(bucket="b", key="k") == b"payload"


def test_wrapper_put_routes_metadata(mock_oci: MagicMock) -> None:
    client = ObjectStorageClient(oci_client=mock_oci, namespace="ns")
    client.put_bytes(
        bucket="b",
        key="k",
        data=b"payload",
        content_type="application/octet-stream",
        metadata={"author": "snorlax"},
    )
    args = mock_oci.put_object.call_args.kwargs
    assert args["bucket_name"] == "b"
    assert args["object_name"] == "k"
    assert args["put_object_body"] == b"payload"
    assert args["content_type"] == "application/octet-stream"
    assert args["opc_meta"] == {"author": "snorlax"}


def test_wrapper_if_not_exists_skips_when_present(mock_oci: MagicMock) -> None:
    # head_object returns successfully (object exists).
    mock_oci.head_object.return_value = MagicMock()
    client = ObjectStorageClient(oci_client=mock_oci, namespace="ns")
    with pytest.raises(ObjectAlreadyExistsError):
        client.put_bytes(bucket="b", key="k", data=b"x", if_not_exists=True)
    mock_oci.put_object.assert_not_called()


def test_wrapper_exists_404_returns_false(mock_oci: MagicMock) -> None:
    mock_oci.head_object.side_effect = _mk_service_error(404)
    client = ObjectStorageClient(oci_client=mock_oci, namespace="ns")
    assert client.exists(bucket="b", key="k") is False


def test_wrapper_exists_500_propagates_after_retry(mock_oci: MagicMock) -> None:
    mock_oci.head_object.side_effect = _mk_service_error(500)
    client = ObjectStorageClient(oci_client=mock_oci, namespace="ns")
    with pytest.raises(ServiceError):
        client.exists(bucket="b", key="k")
    # tenacity retries 5 times by default in our config.
    assert mock_oci.head_object.call_count == 5


def test_wrapper_get_retries_on_500_then_succeeds(mock_oci: MagicMock) -> None:
    response = MagicMock()
    response.data.content = b"ok"
    mock_oci.get_object.side_effect = [
        _mk_service_error(503),
        _mk_service_error(503),
        response,
    ]
    client = ObjectStorageClient(oci_client=mock_oci, namespace="ns")
    assert client.get_bytes(bucket="b", key="k") == b"ok"
    assert mock_oci.get_object.call_count == 3


def test_wrapper_delete_404_translates(mock_oci: MagicMock) -> None:
    mock_oci.delete_object.side_effect = _mk_service_error(404)
    client = ObjectStorageClient(oci_client=mock_oci, namespace="ns")
    with pytest.raises(ObjectNotFoundError):
        client.delete(bucket="b", key="k")


def test_wrapper_ping_treats_auth_error_as_reachable(mock_oci: MagicMock) -> None:
    mock_oci.list_buckets.side_effect = _mk_service_error(401)
    client = ObjectStorageClient(oci_client=mock_oci, namespace="ns")
    # Auth error means we reached OCI; we just don't have list-buckets perm.
    assert client.ping() is True


def test_wrapper_ping_returns_false_on_connection_failure(mock_oci: MagicMock) -> None:
    mock_oci.list_buckets.side_effect = ConnectionError("boom")
    client = ObjectStorageClient(oci_client=mock_oci, namespace="ns")
    assert client.ping() is False


def test_wrapper_ping_returns_false_on_5xx(mock_oci: MagicMock) -> None:
    mock_oci.list_buckets.side_effect = _mk_service_error(500)
    client = ObjectStorageClient(oci_client=mock_oci, namespace="ns")
    assert client.ping() is False


def test_wrapper_put_stream_routes_arguments(mock_oci: MagicMock) -> None:
    client = ObjectStorageClient(oci_client=mock_oci, namespace="ns")
    src = io.BytesIO(b"streamed-bytes")
    client.put_stream(
        bucket="b",
        key="k",
        stream=src,
        content_length=14,
        content_type="application/x-gzip",
        metadata={"author": "snorlax"},
    )
    args = mock_oci.put_object.call_args.kwargs
    assert args["content_length"] == 14
    assert args["content_type"] == "application/x-gzip"
    assert args["opc_meta"] == {"author": "snorlax"}


def test_wrapper_put_stream_if_not_exists_skips(mock_oci: MagicMock) -> None:
    mock_oci.head_object.return_value = MagicMock()
    client = ObjectStorageClient(oci_client=mock_oci, namespace="ns")
    src = io.BytesIO(b"data")
    with pytest.raises(ObjectAlreadyExistsError):
        client.put_stream(
            bucket="b", key="k", stream=src, content_length=4, if_not_exists=True
        )


def test_wrapper_get_stream_yields_chunks(mock_oci: MagicMock) -> None:
    response = MagicMock()
    response.data.iter_content = MagicMock(return_value=iter([b"abc", b"def"]))
    mock_oci.get_object.return_value = response
    client = ObjectStorageClient(oci_client=mock_oci, namespace="ns")
    chunks = list(client.get_stream(bucket="b", key="k", chunk_size=3))
    assert chunks == [b"abc", b"def"]


def test_wrapper_get_stream_404_translates(mock_oci: MagicMock) -> None:
    mock_oci.get_object.side_effect = _mk_service_error(404)
    client = ObjectStorageClient(oci_client=mock_oci, namespace="ns")
    with pytest.raises(ObjectNotFoundError):
        list(client.get_stream(bucket="b", key="k"))


def test_wrapper_put_bytes_if_not_exists_skips(mock_oci: MagicMock) -> None:
    mock_oci.head_object.return_value = MagicMock()
    client = ObjectStorageClient(oci_client=mock_oci, namespace="ns")
    with pytest.raises(ObjectAlreadyExistsError):
        client.put_bytes(bucket="b", key="k", data=b"x", if_not_exists=True)
    mock_oci.put_object.assert_not_called()
