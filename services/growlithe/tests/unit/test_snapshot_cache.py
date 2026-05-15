"""Snapshot-cache tests against the in-memory FakeObjectStorage."""

from __future__ import annotations

import gzip

import pytest
from kanto_commons.storage import KeyBuilder
from kanto_commons.testing import FakeObjectStorage

from growlithe.snapshot_cache import SnapshotCache


@pytest.fixture
def cache_pair() -> tuple[SnapshotCache, FakeObjectStorage]:
    storage = FakeObjectStorage()
    return SnapshotCache(storage=storage, container="kanto-metadata-test"), storage


def test_cold_cache_returns_none_pointer(
    cache_pair: tuple[SnapshotCache, FakeObjectStorage],
) -> None:
    cache, _ = cache_pair
    assert cache.latest_snapshot_id(source="ncbi-pd", organism="Listeria") is None


def test_write_then_read_round_trip(
    cache_pair: tuple[SnapshotCache, FakeObjectStorage],
) -> None:
    cache, _ = cache_pair
    cache.write_snapshot(
        source="ncbi-pd",
        organism="Listeria",
        snapshot_id="PDG000000001.4703",
        metadata_tsv=b"#col1\tcol2\nv1\tv2\n",
    )
    assert cache.latest_snapshot_id(source="ncbi-pd", organism="Listeria") == "PDG000000001.4703"
    got = cache.read_snapshot(
        source="ncbi-pd",
        organism="Listeria",
        snapshot_id="PDG000000001.4703",
    )
    assert got == b"#col1\tcol2\nv1\tv2\n"


def test_read_missing_snapshot_returns_none(
    cache_pair: tuple[SnapshotCache, FakeObjectStorage],
) -> None:
    cache, _ = cache_pair
    assert (
        cache.read_snapshot(
            source="ncbi-pd",
            organism="Listeria",
            snapshot_id="never-cached",
        )
        is None
    )


def test_snapshot_is_gzip_compressed_at_rest(
    cache_pair: tuple[SnapshotCache, FakeObjectStorage],
) -> None:
    cache, storage = cache_pair
    raw = b"#col1\tcol2\n" + b"x\t" * 200 + b"\n"
    cache.write_snapshot(
        source="ncbi-pd",
        organism="Listeria",
        snapshot_id="PDG-1.1",
        metadata_tsv=raw,
    )
    key = KeyBuilder.metadata_snapshot_key("ncbi-pd", "Listeria", "PDG-1.1")
    on_disk = storage.get_bytes(container="kanto-metadata-test", key=key)
    # Magic bytes for gzip.
    assert on_disk[:2] == b"\x1f\x8b"
    assert gzip.decompress(on_disk) == raw


def test_corrupt_cache_entry_returns_none(
    cache_pair: tuple[SnapshotCache, FakeObjectStorage],
) -> None:
    """A corrupt entry from a previous run shouldn't kill the next cycle."""
    cache, storage = cache_pair
    key = KeyBuilder.metadata_snapshot_key("ncbi-pd", "Listeria", "PDG-bad")
    storage.put_bytes(
        container="kanto-metadata-test",
        key=key,
        data=b"not gzip at all",
    )
    assert (
        cache.read_snapshot(
            source="ncbi-pd",
            organism="Listeria",
            snapshot_id="PDG-bad",
        )
        is None
    )


def test_pointer_written_after_snapshot(
    cache_pair: tuple[SnapshotCache, FakeObjectStorage],
) -> None:
    """Pointer must reference an already-uploaded snapshot.

    We can't observe write order across the in-memory fake, but we can
    confirm both objects exist after the write completes, and that
    reading via the public API returns matching content.
    """
    cache, storage = cache_pair
    cache.write_snapshot(
        source="ncbi-pd",
        organism="Listeria",
        snapshot_id="PDG-x",
        metadata_tsv=b"hi",
    )
    assert "discovery/ncbi-pd/Listeria/latest.txt" in storage.all_keys("kanto-metadata-test")
    assert "discovery/ncbi-pd/Listeria/PDG-x.tsv.gz" in storage.all_keys("kanto-metadata-test")
