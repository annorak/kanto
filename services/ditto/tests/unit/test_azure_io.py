"""Tests for the AzureBlobIO error mapping."""

from __future__ import annotations

import pytest
from kanto_commons.storage import (
    ObjectStorageError,
)
from kanto_commons.testing.fakes import FakeObjectStorage

from ditto.azure_io import AzureBlobIO
from ditto.errors import ParquetWriteError, PermanentError, TransientError


def test_fetch_protein_fasta_returns_bytes() -> None:
    storage = FakeObjectStorage()
    storage.put_bytes(container="proteins", key="a/1.faa.gz", data=b"hello")
    blob = AzureBlobIO(storage=storage)
    assert blob.fetch_protein_fasta(container="proteins", key="a/1.faa.gz") == b"hello"


def test_missing_blob_raises_permanent_error() -> None:
    storage = FakeObjectStorage()
    blob = AzureBlobIO(storage=storage)
    with pytest.raises(PermanentError):
        blob.fetch_protein_fasta(container="proteins", key="missing")


def test_other_storage_errors_are_transient() -> None:
    class FlakyStorage(FakeObjectStorage):
        def get_bytes(self, *, container: str, key: str) -> bytes:
            raise ObjectStorageError("network blip")

    blob = AzureBlobIO(storage=FlakyStorage())
    with pytest.raises(TransientError):
        blob.fetch_protein_fasta(container="proteins", key="a")


def test_write_parquet_round_trips() -> None:
    storage = FakeObjectStorage()
    blob = AzureBlobIO(storage=storage)
    blob.write_parquet(container="emb", key="a/1.parquet", data=b"PAR1...")
    assert storage.get_bytes(container="emb", key="a/1.parquet") == b"PAR1..."


def test_write_parquet_failure_raises_parquet_write_error() -> None:
    class FailingStorage(FakeObjectStorage):
        def put_bytes(self, **kwargs: object) -> None:
            raise ObjectStorageError("blob 503")

    blob = AzureBlobIO(storage=FailingStorage())
    with pytest.raises(ParquetWriteError):
        blob.write_parquet(container="emb", key="a/1.parquet", data=b"x")
