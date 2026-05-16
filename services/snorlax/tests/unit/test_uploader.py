"""ProteinUploader tests against FakeObjectStorage."""

from __future__ import annotations

import gzip
from pathlib import Path

from kanto_commons.storage import KeyBuilder
from kanto_commons.testing import FakeObjectStorage

from snorlax.uploader import ProteinUploader


def _make_uploader() -> tuple[ProteinUploader, FakeObjectStorage]:
    storage = FakeObjectStorage()
    key_builder = KeyBuilder(
        proteins_container="kanto-proteins-test",
        embeddings_container="kanto-embeddings-test",
        metadata_container="kanto-metadata-test",
    )
    uploader = ProteinUploader(storage=storage, key_builder=key_builder)
    return uploader, storage


def test_uploader_compresses_and_writes(tmp_path: Path) -> None:
    src = tmp_path / "proteins.faa"
    body = ">prot1\nMKVLAQ*\n>prot2\nMACDEF*\n"
    src.write_text(body, encoding="utf-8")

    uploader, storage = _make_uploader()
    result = uploader.upload(
        accession="PDT0001",
        version=2,
        protein_fasta_path=src,
        protein_count=2,
        source_md5="cafebabe" * 4,
    )

    assert result.key == "PDT0001/2.faa.gz"
    assert result.container == "kanto-proteins-test"
    stored = storage.get_bytes(container=result.container, key=result.key)
    assert gzip.decompress(stored).decode("utf-8") == body

    metadata = storage.get_metadata(container=result.container, key=result.key)
    assert metadata["accession"] == "PDT0001"
    assert metadata["version"] == "2"
    assert metadata["protein_count"] == "2"
    assert metadata["schema_version"] == "1"
    assert metadata["source_genome_md5"] == "cafebabe" * 4


def test_uploader_idempotent(tmp_path: Path) -> None:
    """Uploading the same bytes twice produces the same blob, no error."""
    src = tmp_path / "p.faa"
    src.write_text(">p\nMA*\n", encoding="utf-8")
    uploader, storage = _make_uploader()

    r1 = uploader.upload(
        accession="X",
        version=1,
        protein_fasta_path=src,
        protein_count=1,
        source_md5=None,
    )
    r2 = uploader.upload(
        accession="X",
        version=1,
        protein_fasta_path=src,
        protein_count=1,
        source_md5=None,
    )
    assert r1.key == r2.key
    # ``put_bytes`` overwrites; only one object remains.
    assert storage.all_keys("kanto-proteins-test") == ["X/1.faa.gz"]
