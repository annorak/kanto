"""Unit tests for the Object Storage wrapper.

Two surfaces under test:

* The in-memory ``FakeObjectStorage`` (services use this in their
  unit tests so we owe them confidence it behaves like the real
  thing).
* ``ObjectStorageClient``: we mock the underlying Azure SDK and
  assert the wrapper translates ``ResourceNotFoundError`` →
  ``ObjectNotFoundError``, honors ``if_not_exists`` via the SDK's
  native overwrite=False path, and routes calls through retry.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
)
from kanto_commons.config import ObjectStorageSettings
from kanto_commons.storage import (
    KeyBuilder,
    ObjectAlreadyExistsError,
    ObjectNotFoundError,
    ObjectStorageClient,
)
from kanto_commons.testing import FakeObjectStorage


# ---------------------------------------------------------------------------
# KeyBuilder
# ---------------------------------------------------------------------------


def test_key_builder_protein_fasta() -> None:
    assert KeyBuilder.protein_fasta_key("PDT001", 2) == "PDT001/2.faa.gz"


def test_key_builder_embedding_parquet() -> None:
    assert KeyBuilder.embedding_parquet_key("PDT001", 2) == "PDT001/2.parquet"


def test_key_builder_metadata_snapshot_key() -> None:
    assert (
        KeyBuilder.metadata_snapshot_key("ncbi-pd", "Salmonella", "PDG000000004.355")
        == "discovery/ncbi-pd/Salmonella/PDG000000004.355.tsv.gz"
    )


def test_key_builder_metadata_latest_pointer_key() -> None:
    assert (
        KeyBuilder.metadata_latest_pointer_key("ncbi-pd", "Listeria")
        == "discovery/ncbi-pd/Listeria/latest.txt"
    )


def test_key_builder_from_settings() -> None:
    settings = ObjectStorageSettings(  # type: ignore[call-arg]
        account_url="https://kantodevdata1234.blob.core.windows.net",
    )
    kb = KeyBuilder.from_settings(settings)
    assert kb.proteins_container == "kanto-proteins"
    assert kb.embeddings_container == "kanto-embeddings"
    assert kb.metadata_container == "kanto-metadata"


# ---------------------------------------------------------------------------
# Fake — round-trips, exists, delete, metadata, idempotency
# ---------------------------------------------------------------------------


def test_fake_put_then_get_returns_same_bytes() -> None:
    os = FakeObjectStorage()
    os.put_bytes(container="c", key="k", data=b"hello")
    assert os.get_bytes(container="c", key="k") == b"hello"


def test_fake_get_missing_raises_not_found() -> None:
    os = FakeObjectStorage()
    with pytest.raises(ObjectNotFoundError):
        os.get_bytes(container="c", key="missing")


def test_fake_exists_true_and_false() -> None:
    os = FakeObjectStorage()
    os.put_bytes(container="c", key="k", data=b"x")
    assert os.exists(container="c", key="k")
    assert not os.exists(container="c", key="other")


def test_fake_delete_removes_object() -> None:
    os = FakeObjectStorage()
    os.put_bytes(container="c", key="k", data=b"x")
    os.delete(container="c", key="k")
    assert not os.exists(container="c", key="k")


def test_fake_delete_missing_raises() -> None:
    os = FakeObjectStorage()
    with pytest.raises(ObjectNotFoundError):
        os.delete(container="c", key="missing")


def test_fake_if_not_exists_blocks_overwrite() -> None:
    os = FakeObjectStorage()
    os.put_bytes(container="c", key="k", data=b"first")
    with pytest.raises(ObjectAlreadyExistsError):
        os.put_bytes(container="c", key="k", data=b"second", if_not_exists=True)
    # Default behaviour overwrites without complaint.
    os.put_bytes(container="c", key="k", data=b"second")
    assert os.get_bytes(container="c", key="k") == b"second"


def test_fake_metadata_round_trip() -> None:
    os = FakeObjectStorage()
    os.put_bytes(container="c", key="k", data=b"x", metadata={"author": "snorlax"})
    assert os.get_metadata(container="c", key="k") == {"author": "snorlax"}


def test_fake_stream_chunks() -> None:
    os = FakeObjectStorage()
    os.put_bytes(container="c", key="k", data=b"abcdefghij")
    chunks = list(os.get_stream(container="c", key="k", chunk_size=3))
    assert chunks == [b"abc", b"def", b"ghi", b"j"]


def test_fake_put_stream_round_trip() -> None:
    os = FakeObjectStorage()
    src = io.BytesIO(b"streamed-content")
    os.put_stream(container="c", key="k", stream=src, content_length=16)
    assert os.get_bytes(container="c", key="k") == b"streamed-content"


def test_fake_all_keys_sorted() -> None:
    os = FakeObjectStorage()
    os.put_bytes(container="c", key="z", data=b"z")
    os.put_bytes(container="c", key="a", data=b"a")
    assert os.all_keys("c") == ["a", "z"]


def test_fake_ping_always_true() -> None:
    os = FakeObjectStorage()
    assert os.ping() is True


def test_fake_reset() -> None:
    os = FakeObjectStorage()
    os.put_bytes(container="c", key="k", data=b"x")
    os.reset()
    assert not os.exists(container="c", key="k")


# ---------------------------------------------------------------------------
# Real wrapper, with the Azure SDK mocked.
# ---------------------------------------------------------------------------


def _mk_http_error(status: int, message: str = "mocked") -> HttpResponseError:
    response = MagicMock()
    response.status_code = status
    response.reason = message
    response.headers = {}
    err = HttpResponseError(response=response)
    err.status_code = status  # set explicitly; SDK sets it from response normally
    return err


@pytest.fixture
def mock_service() -> Iterator[MagicMock]:
    """Return a MagicMock that pretends to be a BlobServiceClient.

    Each call to ``get_blob_client`` returns a fresh MagicMock so tests
    that interact with one blob don't accidentally observe state from
    another.
    """
    service = MagicMock()
    service.get_blob_client.side_effect = lambda **_: MagicMock()
    yield service


def _wrap(service: MagicMock) -> ObjectStorageClient:
    return ObjectStorageClient(blob_service=service)


def test_wrapper_get_404_translates_to_not_found(mock_service: MagicMock) -> None:
    blob = MagicMock()
    blob.download_blob.side_effect = ResourceNotFoundError("missing")
    mock_service.get_blob_client.side_effect = lambda **_: blob
    with pytest.raises(ObjectNotFoundError):
        _wrap(mock_service).get_bytes(container="c", key="k")


def test_wrapper_get_returns_bytes(mock_service: MagicMock) -> None:
    blob = MagicMock()
    blob.download_blob.return_value.readall.return_value = b"payload"
    mock_service.get_blob_client.side_effect = lambda **_: blob
    assert _wrap(mock_service).get_bytes(container="c", key="k") == b"payload"


def test_wrapper_put_routes_metadata(mock_service: MagicMock) -> None:
    blob = MagicMock()
    mock_service.get_blob_client.side_effect = lambda **_: blob
    _wrap(mock_service).put_bytes(
        container="c",
        key="k",
        data=b"payload",
        content_type="application/octet-stream",
        metadata={"author": "snorlax"},
    )
    args = blob.upload_blob.call_args
    assert args.args[0] == b"payload"
    assert args.kwargs["overwrite"] is True
    assert args.kwargs["metadata"] == {"author": "snorlax"}
    # ContentSettings is opaque; just confirm we passed *something* through.
    assert "content_settings" in args.kwargs


def test_wrapper_if_not_exists_skips_when_present(mock_service: MagicMock) -> None:
    blob = MagicMock()
    blob.upload_blob.side_effect = ResourceExistsError("exists")
    mock_service.get_blob_client.side_effect = lambda **_: blob
    with pytest.raises(ObjectAlreadyExistsError):
        _wrap(mock_service).put_bytes(
            container="c", key="k", data=b"x", if_not_exists=True
        )
    args = blob.upload_blob.call_args
    assert args.kwargs["overwrite"] is False


def test_wrapper_exists_true(mock_service: MagicMock) -> None:
    blob = MagicMock()
    blob.exists.return_value = True
    mock_service.get_blob_client.side_effect = lambda **_: blob
    assert _wrap(mock_service).exists(container="c", key="k") is True


def test_wrapper_exists_404_returns_false(mock_service: MagicMock) -> None:
    blob = MagicMock()
    blob.exists.side_effect = ResourceNotFoundError("nope")
    mock_service.get_blob_client.side_effect = lambda **_: blob
    assert _wrap(mock_service).exists(container="c", key="k") is False


def test_wrapper_exists_500_propagates_after_retry(mock_service: MagicMock) -> None:
    blob = MagicMock()
    blob.exists.side_effect = _mk_http_error(500)
    mock_service.get_blob_client.side_effect = lambda **_: blob
    with pytest.raises(HttpResponseError):
        _wrap(mock_service).exists(container="c", key="k")
    # tenacity retries 5 attempts by default.
    assert blob.exists.call_count == 5


def test_wrapper_get_retries_on_503_then_succeeds(mock_service: MagicMock) -> None:
    blob = MagicMock()
    ok = MagicMock()
    ok.readall.return_value = b"ok"
    blob.download_blob.side_effect = [
        _mk_http_error(503),
        _mk_http_error(503),
        ok,
    ]
    mock_service.get_blob_client.side_effect = lambda **_: blob
    assert _wrap(mock_service).get_bytes(container="c", key="k") == b"ok"
    assert blob.download_blob.call_count == 3


def test_wrapper_delete_404_translates(mock_service: MagicMock) -> None:
    blob = MagicMock()
    blob.delete_blob.side_effect = ResourceNotFoundError("missing")
    mock_service.get_blob_client.side_effect = lambda **_: blob
    with pytest.raises(ObjectNotFoundError):
        _wrap(mock_service).delete(container="c", key="k")


def test_wrapper_ping_treats_auth_error_as_reachable(mock_service: MagicMock) -> None:
    mock_service.get_service_properties.side_effect = _mk_http_error(401)
    assert _wrap(mock_service).ping() is True


def test_wrapper_ping_returns_false_on_connection_failure(
    mock_service: MagicMock,
) -> None:
    mock_service.get_service_properties.side_effect = ConnectionError("boom")
    assert _wrap(mock_service).ping() is False


def test_wrapper_ping_returns_false_on_5xx(mock_service: MagicMock) -> None:
    mock_service.get_service_properties.side_effect = _mk_http_error(500)
    assert _wrap(mock_service).ping() is False


def test_wrapper_put_stream_routes_arguments(mock_service: MagicMock) -> None:
    blob = MagicMock()
    mock_service.get_blob_client.side_effect = lambda **_: blob
    src = io.BytesIO(b"streamed-bytes")
    _wrap(mock_service).put_stream(
        container="c",
        key="k",
        stream=src,
        content_length=14,
        content_type="application/x-gzip",
        metadata={"author": "snorlax"},
    )
    args = blob.upload_blob.call_args
    assert args.kwargs["length"] == 14
    assert args.kwargs["overwrite"] is True
    assert args.kwargs["metadata"] == {"author": "snorlax"}


def test_wrapper_put_stream_if_not_exists_translates(mock_service: MagicMock) -> None:
    blob = MagicMock()
    blob.upload_blob.side_effect = ResourceExistsError("exists")
    mock_service.get_blob_client.side_effect = lambda **_: blob
    src = io.BytesIO(b"data")
    with pytest.raises(ObjectAlreadyExistsError):
        _wrap(mock_service).put_stream(
            container="c",
            key="k",
            stream=src,
            content_length=4,
            if_not_exists=True,
        )


def test_wrapper_get_stream_yields_chunks(mock_service: MagicMock) -> None:
    blob = MagicMock()
    downloader = MagicMock()
    downloader.chunks.return_value = iter([b"abc", b"def"])
    blob.download_blob.return_value = downloader
    mock_service.get_blob_client.side_effect = lambda **_: blob
    chunks = list(_wrap(mock_service).get_stream(container="c", key="k"))
    assert chunks == [b"abc", b"def"]


def test_wrapper_get_stream_404_translates(mock_service: MagicMock) -> None:
    blob = MagicMock()
    blob.download_blob.side_effect = ResourceNotFoundError("missing")
    mock_service.get_blob_client.side_effect = lambda **_: blob
    with pytest.raises(ObjectNotFoundError):
        list(_wrap(mock_service).get_stream(container="c", key="k"))
