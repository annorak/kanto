"""Modal client tests — both the no-op path and the factory."""

from __future__ import annotations

from datetime import UTC, datetime

from kanto_commons import ProteinsReady

from snorlax.modal_client import (
    NoopModalClient,
    SpawnedCall,
    _idempotency_key_for,
    build_modal_client,
)


def _payload(*, accession: str = "PDT0001", version: int = 1) -> ProteinsReady:
    return ProteinsReady(
        accession=accession,
        version=version,
        os_key=f"{accession}/{version}.faa.gz",
        protein_count=5,
        produced_at=datetime.now(UTC),
    )


def test_noop_spawn_returns_deterministic_call_id() -> None:
    client = NoopModalClient()
    p = _payload()
    a = client.spawn(payload=p)
    b = client.spawn(payload=p)
    assert isinstance(a, SpawnedCall)
    assert a.call_id == b.call_id
    assert a.idempotency_key == b.idempotency_key
    assert len(client.calls) == 2


def test_idempotency_key_distinct_per_isolate() -> None:
    a = _idempotency_key_for(_payload(accession="A", version=1))
    b = _idempotency_key_for(_payload(accession="A", version=2))
    c = _idempotency_key_for(_payload(accession="B", version=1))
    assert a != b
    assert a != c
    assert b != c


def test_build_modal_client_disabled() -> None:
    client = build_modal_client(
        enabled=False,
        function_ref="x/y",
        environment="main",
        max_attempts=1,
    )
    assert isinstance(client, NoopModalClient)
    assert client.healthy() is True
