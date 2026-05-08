"""Smoke tests for ``kanto_commons.testing`` itself.

The fakes and factories must keep working — they're the contract
service tests rely on.
"""

from __future__ import annotations

from kanto_commons.schemas import (
    EmbeddingsReady,
    IsolateDiscovered,
    IsolateScored,
    ProteinsReady,
)
from kanto_commons.testing import (
    FakeObjectStorage,
    FakeStreamingClient,
    bootstrap_schema,
    make_embeddings_ready,
    make_isolate_discovered,
    make_isolate_scored,
    make_proteins_ready,
)


def test_factories_default_to_valid_objects() -> None:
    assert isinstance(make_isolate_discovered(), IsolateDiscovered)
    assert isinstance(make_proteins_ready(), ProteinsReady)
    assert isinstance(make_embeddings_ready(), EmbeddingsReady)
    assert isinstance(make_isolate_scored(), IsolateScored)


def test_factories_accept_overrides() -> None:
    obj = make_isolate_discovered(accession="OVERRIDE", version=99)
    assert obj.accession == "OVERRIDE"
    assert obj.version == 99


def test_fake_object_storage_implements_protocol() -> None:
    """The fake matches the OS protocol the wrapper provides."""
    fake = FakeObjectStorage()
    fake.put_bytes(bucket="b", key="k", data=b"x")
    assert fake.get_bytes(bucket="b", key="k") == b"x"
    assert fake.exists(bucket="b", key="k")
    assert fake.ping() is True


async def test_fake_streaming_client_matches_basic_contract() -> None:
    fake = FakeStreamingClient()
    await fake.produce(topic="t", value=b"x")
    received = [m async for m in fake.consume(topic="t", group="g")]
    assert len(received) == 1
    assert received[0].value == b"x"


def test_bootstrap_schema_function_exists() -> None:
    """The DDL bootstrap is exported and is a callable.

    Actually applying it requires Postgres — the integration-test
    fixture handles that. Unit tests just verify the public API exists.
    """
    assert callable(bootstrap_schema)
