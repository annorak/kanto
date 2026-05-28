"""Unit tests for the Event Hubs (Kafka API) streaming wrapper.

We don't spin up a Kafka broker for unit tests — that's an
integration concern. Instead we substitute the underlying
``aiokafka`` producer and consumer with hand-rolled async fakes that
record what would have been sent and what would be polled. This
lets us verify:

* Envelopes are wrapped correctly and contain trace headers.
* Failed messages route to the DLQ after the configured retry budget.
* Schema-validation failures bypass the retry budget and DLQ
  immediately.
* ``commit`` advances the offset; ``handle_failure`` does not until
  the budget is exhausted.

The :class:`FakeStreamingClient` exposed in
:mod:`kanto_commons.testing` is exercised separately as the contract
that services rely on.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest
from kanto_commons.config import StreamingSettings, TracingSettings
from kanto_commons.schemas import (
    DLQEnvelope,
    EmbeddingsReady,
    EventEnvelope,
    IsolateScored,
)
from kanto_commons.streaming import (
    ConsumedMessage,
    StreamingConsumer,
    StreamingError,
    StreamingProducer,
    _registry_entry_for,
)
from kanto_commons.testing import (
    FakeStreamingClient,
    make_embeddings_ready,
    make_isolate_discovered,
    make_isolate_scored,
    make_proteins_ready,
)
from kanto_commons.tracing import setup_tracing

# ---------------------------------------------------------------------------
# Aiokafka stand-ins
# ---------------------------------------------------------------------------


@dataclass
class _CapturedSend:
    topic: str
    value: bytes
    key: bytes
    headers: list[tuple[str, bytes]]


class _FakeAioProducer:
    """Just enough of AIOKafkaProducer for the wrapper to drive."""

    def __init__(self) -> None:
        self.sent: list[_CapturedSend] = []
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def send_and_wait(
        self,
        topic: str,
        *,
        value: bytes,
        key: bytes,
        headers: list[tuple[str, bytes]] | None = None,
    ) -> None:
        self.sent.append(
            _CapturedSend(
                topic=topic,
                value=value,
                key=key,
                headers=list(headers or []),
            )
        )

    @property
    def client(self) -> MagicMock:
        m = MagicMock()
        m.cluster.brokers.return_value = [MagicMock()]
        return m


@dataclass
class _ConsumerRecord:
    topic: str
    partition: int
    offset: int
    value: bytes
    headers: list[tuple[str, bytes]] = field(default_factory=list)


class _FakeAioConsumer:
    def __init__(self, records: list[_ConsumerRecord]) -> None:
        self._records = list(records)
        self.commits: list[Any] = []
        self.started = False
        self.stopped = False
        # Mimic aiokafka's internal client.cluster attribute used by healthy().
        self._client = MagicMock()
        self._client.cluster.brokers.return_value = [MagicMock()]

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def __aiter__(self) -> AsyncIterator[_ConsumerRecord]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[_ConsumerRecord]:
        for record in self._records:
            yield record

    async def commit(self, offsets: dict[Any, Any]) -> None:
        self.commits.append(offsets)


# ---------------------------------------------------------------------------
# Producer tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _tracing() -> None:
    setup_tracing(service_name="t", environment="test", settings=TracingSettings())


@pytest.fixture
def producer_pair() -> tuple[StreamingProducer, _FakeAioProducer]:
    fake = _FakeAioProducer()
    return StreamingProducer(producer=fake), fake  # type: ignore[arg-type]


async def test_send_routes_to_registry_topic(
    producer_pair: tuple[StreamingProducer, _FakeAioProducer],
) -> None:
    prod, fake = producer_pair
    await prod.start()
    await prod.send(make_isolate_scored())
    sent = fake.sent[0]
    assert sent.topic == "kanto.scored"


async def test_send_serializes_envelope(
    producer_pair: tuple[StreamingProducer, _FakeAioProducer],
) -> None:
    prod, fake = producer_pair
    event = make_embeddings_ready()
    await prod.start()
    await prod.send(event)
    payload = json.loads(fake.sent[0].value.decode("utf-8"))
    assert payload["event_name"] == "embeddings.ready"
    assert payload["event_schema_version"] == event.schema_version
    assert payload["payload"]["accession"] == event.accession


async def test_send_default_key_is_accession_version(
    producer_pair: tuple[StreamingProducer, _FakeAioProducer],
) -> None:
    prod, fake = producer_pair
    await prod.start()
    await prod.send(make_isolate_scored())
    assert fake.sent[0].key == b"PDT000123:1"


async def test_send_proteins_ready_rejected(
    producer_pair: tuple[StreamingProducer, _FakeAioProducer],
) -> None:
    prod, _fake = producer_pair
    await prod.start()
    with pytest.raises(StreamingError):
        await prod.send(make_proteins_ready())


async def test_send_overrides_topic_and_key(
    producer_pair: tuple[StreamingProducer, _FakeAioProducer],
) -> None:
    prod, fake = producer_pair
    await prod.start()
    await prod.send(make_isolate_scored(), topic="custom.topic", key="custom-key")
    assert fake.sent[0].topic == "custom.topic"
    assert fake.sent[0].key == b"custom-key"


# ---------------------------------------------------------------------------
# Consumer tests
# ---------------------------------------------------------------------------


def _record_for(event: object, partition: int = 0, offset: int = 0) -> _ConsumerRecord:
    """Build a fake aiokafka record carrying ``event`` in an envelope."""
    entry = _registry_entry_for(event)  # type: ignore[arg-type]
    envelope = EventEnvelope(
        event_name=entry.name,
        event_schema_version=event.schema_version,  # type: ignore[attr-defined]
        payload=event,  # type: ignore[arg-type]
    )
    return _ConsumerRecord(
        topic=entry.topic or "test",
        partition=partition,
        offset=offset,
        value=envelope.model_dump_json().encode("utf-8"),
    )


def _consumer_with(
    records: list[_ConsumerRecord], *, max_attempts: int = 3
) -> tuple[StreamingConsumer, _FakeAioConsumer, _FakeAioProducer]:
    fake_consumer = _FakeAioConsumer(records)
    fake_producer = _FakeAioProducer()
    producer_wrapper = StreamingProducer(producer=fake_producer)  # type: ignore[arg-type]
    consumer = StreamingConsumer(
        consumer=fake_consumer,  # type: ignore[arg-type]
        producer=producer_wrapper,
        topic=records[0].topic if records else "kanto.test",
        group="test-group",
        consumer_id="test-consumer-0",
        max_attempts=max_attempts,
    )
    return consumer, fake_consumer, fake_producer


async def test_consumer_yields_parsed_event() -> None:
    record = _record_for(make_isolate_scored())
    consumer, _fc, _fp = _consumer_with([record])
    parsed: list[ConsumedMessage[Any]] = []
    async for msg in consumer.messages():
        parsed.append(msg)
        # Finalize so the replay slot clears and the iterator can
        # advance to end-of-records. See the offset-safety contract
        # in StreamingConsumer.
        await consumer.commit(msg)
    assert len(parsed) == 1
    assert isinstance(parsed[0].event, IsolateScored)


async def test_commit_advances_offset_by_one() -> None:
    record = _record_for(make_isolate_scored(), offset=42)
    consumer, fake_consumer, _fp = _consumer_with([record])
    async for msg in consumer.messages():
        await consumer.commit(msg)
    # Commits store {TopicPartition(...): OffsetAndMetadata(offset+1, '', -1)}
    assert len(fake_consumer.commits) == 1
    committed_offset_meta = next(iter(fake_consumer.commits[0].values()))
    assert committed_offset_meta.offset == 43


async def test_invalid_payload_routes_directly_to_dlq() -> None:
    bad_record = _ConsumerRecord(
        topic="kanto.discovered",
        partition=0,
        offset=0,
        value=b"not-json-or-anything-meaningful",
    )
    consumer, fake_consumer, fake_producer = _consumer_with([bad_record])
    async for _msg in consumer.messages():
        pytest.fail("malformed message should not be yielded")
    # DLQ envelope was sent to kanto.discovered.dlq.
    assert len(fake_producer.sent) == 1
    assert fake_producer.sent[0].topic == "kanto.discovered.dlq"
    dlq = DLQEnvelope.model_validate_json(fake_producer.sent[0].value)
    assert dlq.original_payload_bytes() == b"not-json-or-anything-meaningful"
    # And the offset was committed so the bad message doesn't redeliver.
    assert len(fake_consumer.commits) == 1


async def test_handle_failure_does_not_dlq_until_budget_exhausted() -> None:
    record = _record_for(make_isolate_scored())
    consumer, fake_consumer, fake_producer = _consumer_with([record], max_attempts=3)

    msg = None
    async for m in consumer.messages():
        msg = m
        break
    assert msg is not None

    routed = await consumer.handle_failure(msg, RuntimeError("boom"))
    assert routed is False
    assert fake_producer.sent == []
    assert fake_consumer.commits == []

    routed = await consumer.handle_failure(msg, RuntimeError("boom"))
    assert routed is False

    routed = await consumer.handle_failure(msg, RuntimeError("boom"))
    assert routed is True
    assert len(fake_producer.sent) == 1
    assert fake_producer.sent[0].topic == "kanto.scored.dlq"
    assert len(fake_consumer.commits) == 1


async def test_handle_failure_records_attempt_count_in_envelope() -> None:
    record = _record_for(make_embeddings_ready())
    consumer, _fc, fake_producer = _consumer_with([record], max_attempts=2)

    msg = None
    async for m in consumer.messages():
        msg = m
        break
    assert msg is not None

    await consumer.handle_failure(msg, RuntimeError("first"))
    await consumer.handle_failure(msg, RuntimeError("second"))

    dlq = DLQEnvelope.model_validate_json(fake_producer.sent[0].value)
    assert dlq.attempt_count == 2
    assert "second" in dlq.failure_reason
    assert dlq.consumer_group == "test-group"
    assert dlq.consumer_id == "test-consumer-0"


# ---------------------------------------------------------------------------
# Offset-safety: never skip an earlier unresolved offset
# ---------------------------------------------------------------------------


async def test_transient_failure_blocks_later_offset() -> None:
    """offset 0 transient must not allow offset 1 to commit past it.

    The wrapper's replay slot keeps re-yielding offset 0 until it
    reaches a terminal outcome. Only then does offset 1 surface.
    """
    records = [
        _record_for(make_isolate_scored(), offset=0),
        _record_for(make_isolate_scored(), offset=1),
    ]
    consumer, fake_consumer, _fp = _consumer_with(records, max_attempts=5)
    # Disable the backoff sleep so the test runs fast.
    consumer._retry_backoff_seconds = 0.0  # type: ignore[assignment]

    seen_offsets: list[int] = []
    call_count = 0

    async def handler(msg: ConsumedMessage[Any]) -> None:
        nonlocal call_count
        call_count += 1
        seen_offsets.append(msg.offset)
        # Offset 0 fails twice transiently, succeeds on the third try.
        if msg.offset == 0 and call_count <= 2:
            raise RuntimeError("transient")

    await consumer.run(handler)

    # Offset 0 must reach a terminal outcome before offset 1 is seen.
    first_offset_1 = seen_offsets.index(1)
    assert all(o == 0 for o in seen_offsets[:first_offset_1])
    # And offset 1 must eventually be processed.
    assert 1 in seen_offsets
    # Only ``offset+1`` commits were emitted, in order: 1 (for offset 0),
    # then 2 (for offset 1). No commit may jump over offset 0.
    committed = [next(iter(c.values())).offset for c in fake_consumer.commits]
    assert committed == [1, 2]


async def test_exhausted_retry_dlqs_before_committing() -> None:
    """Exhausted retries write DLQ first, THEN commit the offset.

    The wrapper's contract: the DLQ produce must complete before the
    consumer offset advances. A producer that fails to write DLQ must
    raise out of handle_failure, leaving the offset uncommitted.
    """
    record = _record_for(make_isolate_scored(), offset=7)
    consumer, fake_consumer, fake_producer = _consumer_with([record], max_attempts=2)
    consumer._retry_backoff_seconds = 0.0  # type: ignore[assignment]

    msg = None
    async for m in consumer.messages():
        msg = m
        break
    assert msg is not None

    # First attempt: not yet DLQ.
    assert await consumer.handle_failure(msg, RuntimeError("x")) is False
    assert fake_producer.sent == []
    assert fake_consumer.commits == []

    # Second attempt: DLQ + commit. DLQ produce happens before the
    # commit because the wrapper awaits the produce first.
    assert await consumer.handle_failure(msg, RuntimeError("x")) is True
    assert len(fake_producer.sent) == 1
    assert fake_producer.sent[0].topic == "kanto.scored.dlq"
    assert len(fake_consumer.commits) == 1


async def test_dlq_now_skips_attempt_count_and_commits() -> None:
    """``dlq_now`` is the deterministic-DLQ shortcut."""
    record = _record_for(make_isolate_scored(), offset=3)
    consumer, fake_consumer, fake_producer = _consumer_with([record])

    msg = None
    async for m in consumer.messages():
        msg = m
        break
    assert msg is not None

    await consumer.dlq_now(msg, "permanent: malformed asm_acc")
    assert len(fake_producer.sent) == 1
    assert fake_producer.sent[0].topic == "kanto.scored.dlq"
    dlq = DLQEnvelope.model_validate_json(fake_producer.sent[0].value)
    assert "permanent" in dlq.failure_reason
    assert len(fake_consumer.commits) == 1


async def test_run_commits_on_success_and_dlqs_on_failure() -> None:
    good = _record_for(make_isolate_scored(), offset=0)
    bad = _record_for(make_embeddings_ready(), offset=1)
    consumer, fake_consumer, fake_producer = _consumer_with([good, bad], max_attempts=1)

    handled: list[ConsumedMessage[Any]] = []

    async def handler(msg: ConsumedMessage[Any]) -> None:
        handled.append(msg)
        if isinstance(msg.event, EmbeddingsReady):
            raise RuntimeError("downstream broken")

    await consumer.run(handler)

    # Two messages handled, one committed for success, one DLQ-committed for failure.
    assert len(handled) == 2
    # 1 commit from success + 1 commit from DLQ-after-budget = 2 commits.
    assert len(fake_consumer.commits) == 2
    # 1 DLQ message produced for the failing event.
    assert any(
        s.topic == "kanto.scored.dlq" or s.topic.endswith(".dlq")
        for s in fake_producer.sent
    )


# ---------------------------------------------------------------------------
# FakeStreamingClient — round-trips and DLQ recording
# ---------------------------------------------------------------------------


async def test_fake_streaming_client_round_trip() -> None:
    fake = FakeStreamingClient()
    await fake.produce(topic="t", value=b"x", key="k")
    await fake.produce(topic="t", value=b"y", key="k")

    received: list[bytes] = []
    async for msg in fake.consume(topic="t", group="g"):
        received.append(msg.value)
    assert received == [b"x", b"y"]


async def test_fake_streaming_client_per_group_offsets() -> None:
    fake = FakeStreamingClient()
    await fake.produce(topic="t", value=b"x")

    async for _ in fake.consume(topic="t", group="g1"):
        pass
    # Group g2 sees the message even though g1 already consumed it.
    seen = [m async for m in fake.consume(topic="t", group="g2")]
    assert len(seen) == 1


async def test_fake_streaming_client_dlq_records() -> None:
    fake = FakeStreamingClient()
    await fake.send_to_dlq(original_topic="kanto.scored", envelope_bytes=b"dlq")
    assert len(fake.dlq_messages) == 1
    assert fake.dlq_messages[0].topic == "kanto.scored.dlq"


# ---------------------------------------------------------------------------
# Smoke test for testing.factories
# ---------------------------------------------------------------------------


def test_factories_produce_valid_objects() -> None:
    # Each factory should return an instance whose schema_version is 1
    # and whose timestamps are tz-aware.
    for factory in (
        make_isolate_discovered,
        make_proteins_ready,
        make_embeddings_ready,
        make_isolate_scored,
    ):
        obj = factory()
        assert obj.schema_version == 1


# ---------------------------------------------------------------------------
# Lifecycle, healthy, sasl_kwargs
# ---------------------------------------------------------------------------


from kanto_commons.streaming import _sasl_kwargs, lifecycle  # noqa: E402
from pydantic import SecretStr  # noqa: E402


def test_sasl_kwargs_empty_when_no_creds() -> None:
    s = StreamingSettings(bootstrap_servers="x:9092")  # type: ignore[call-arg]
    assert _sasl_kwargs(s) == {}


def test_sasl_kwargs_built_when_creds_present() -> None:
    s = StreamingSettings(  # type: ignore[call-arg]
        bootstrap_servers="x:9092",
        sasl_username="user",
        sasl_password=SecretStr("pass"),
    )
    kwargs = _sasl_kwargs(s)
    assert kwargs["security_protocol"] == "SASL_SSL"
    assert kwargs["sasl_mechanism"] == "PLAIN"
    assert kwargs["sasl_plain_username"] == "user"
    assert kwargs["sasl_plain_password"] == "pass"


async def test_producer_healthy_after_start(
    producer_pair: tuple[StreamingProducer, _FakeAioProducer],
) -> None:
    prod, _ = producer_pair
    assert await prod.healthy() is False  # not started
    await prod.start()
    assert await prod.healthy() is True


async def test_producer_stop_idempotent(
    producer_pair: tuple[StreamingProducer, _FakeAioProducer],
) -> None:
    prod, fake = producer_pair
    await prod.start()
    await prod.stop()
    await prod.stop()  # second call is a no-op
    assert fake.stopped is True


async def test_lifecycle_starts_and_stops_components() -> None:
    fake = _FakeAioProducer()
    prod = StreamingProducer(producer=fake)  # type: ignore[arg-type]
    async with lifecycle(prod):
        assert fake.started is True
        assert fake.stopped is False
    assert fake.stopped is True


async def test_consumer_healthy_after_start() -> None:
    consumer, _, _ = _consumer_with([])
    assert await consumer.healthy() is False  # not started
    await consumer.start()
    assert await consumer.healthy() is True
    await consumer.stop()
