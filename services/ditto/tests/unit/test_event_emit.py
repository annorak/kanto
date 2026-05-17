"""Tests for the EventEmitter wrapper.

We don't spin up a real producer here — that needs a Kafka/Event Hubs
testcontainer, which is overkill for a wrapper this thin. The
:mod:`tests/integration` directory is where end-to-end producer tests
live.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from kanto_commons.streaming import StreamingError

from ditto.errors import EventEmitError
from ditto.event_emit import StreamingEventEmitter


@dataclass
class FakeProducer:
    sent: list[object] = field(default_factory=list)
    raise_on_send: Exception | None = None

    async def send(
        self,
        event: object,
        *,
        topic: str | None = None,
        key: str | None = None,
    ) -> None:
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.sent.append(event)


async def test_emit_sends_a_typed_event() -> None:
    fake = FakeProducer()
    emitter = StreamingEventEmitter(producer=fake)  # type: ignore[arg-type]
    await emitter.emit_embeddings_ready(
        accession="PDT001",
        version=1,
        model="esmc_600m",
        model_version="2024-12",
        os_key="PDT001/1.parquet",
    )
    assert len(fake.sent) == 1
    event = fake.sent[0]
    assert event.accession == "PDT001"  # type: ignore[attr-defined]
    assert event.version == 1  # type: ignore[attr-defined]


async def test_emit_failure_wraps_in_event_emit_error() -> None:
    fake = FakeProducer(raise_on_send=StreamingError("broker rebalance"))
    emitter = StreamingEventEmitter(producer=fake)  # type: ignore[arg-type]
    with pytest.raises(EventEmitError):
        await emitter.emit_embeddings_ready(
            accession="PDT001",
            version=1,
            model="esmc_600m",
            model_version="2024-12",
            os_key="PDT001/1.parquet",
        )
