"""OCI Streaming wrapper (Kafka-compatible producer/consumer).

Async API throughout. We default to ``aiokafka`` because the OCI
Streaming Kafka endpoint speaks the standard Kafka wire protocol; on
the consumer side aiokafka's per-partition offsets fit our
StatefulSet-pod-per-partition pattern naturally.

Three responsibilities:

1. **Typed producer** — accepts a Kanto schema instance, wraps it in
   the standard envelope, attaches W3C trace headers, and produces
   to a topic with at-least-once acks.

2. **Typed consumer** — yields parsed schema instances. The processing
   callback gets each message in an ``async with`` block; the
   wrapper commits the offset only after the block exits cleanly.

3. **DLQ routing** — when a callback raises after the configured
   number of attempts, the wrapper publishes a
   :class:`~kanto_commons.schemas.DLQEnvelope` to ``<topic>.dlq`` and
   advances the consumer offset.

The wrapper deliberately does not expose ``aiokafka`` types in its
public API.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.errors import KafkaError
from pydantic import ValidationError

from kanto_commons.config import StreamingSettings
from kanto_commons.schemas import (
    EVENT_REGISTRY,
    DLQEnvelope,
    EventEnvelope,
    KantoEvent,
    Transport,
)
from kanto_commons.tracing import (
    inject_context_into_carrier,
    use_extracted_context,
)

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=KantoEvent)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StreamingError(Exception):
    """Base class for streaming wrapper errors."""


class UnknownEventError(StreamingError):
    """A consumed message had an event_name not in the registry."""


# ---------------------------------------------------------------------------
# Producer
# ---------------------------------------------------------------------------


class StreamingProducer:
    """Typed producer for Kanto events.

    Owns a single ``aiokafka.AIOKafkaProducer``. Idempotent: calling
    ``send`` twice with the same ``(topic, key)`` produces two
    messages — Kanto consumers dedupe on ``(accession, version)`` at
    the application layer.
    """

    def __init__(
        self,
        *,
        producer: AIOKafkaProducer,
    ) -> None:
        self._producer = producer
        self._started = False

    @classmethod
    def from_settings(  # pragma: no cover — real Kafka required
        cls, settings: StreamingSettings
    ) -> StreamingProducer:
        producer = AIOKafkaProducer(
            bootstrap_servers=settings.bootstrap_servers,
            acks="all",  # at-least-once
            enable_idempotence=True,
            **_sasl_kwargs(settings),
        )
        return cls(producer=producer)

    async def start(self) -> None:
        await self._producer.start()
        self._started = True

    async def stop(self) -> None:
        if self._started:
            await self._producer.stop()
            self._started = False

    async def send(
        self,
        event: KantoEvent,
        *,
        topic: str | None = None,
        key: str | None = None,
    ) -> None:
        """Produce one event with a wrapping envelope and trace headers.

        ``topic`` defaults to the registry-resolved topic for the
        event's class. ``key`` defaults to the accession; this gives
        same-isolate ordering on a single partition for free.
        ``ProteinsReady`` cannot be produced (it's a Modal payload).
        """
        registry_entry = _registry_entry_for(event)
        if registry_entry.transport is Transport.MODAL:
            raise StreamingError(
                f"{type(event).__name__} is a Modal payload, not a stream event"
            )
        target_topic = topic or registry_entry.topic
        if target_topic is None:  # pragma: no cover — registry invariant
            raise StreamingError(
                f"No topic for {type(event).__name__}; check the registry"
            )

        envelope = EventEnvelope(
            event_name=registry_entry.name,
            event_schema_version=event.schema_version,
            payload=event,
        )

        carrier: dict[str, str] = {}
        inject_context_into_carrier(carrier)
        headers = [(k, v.encode("utf-8")) for k, v in carrier.items()]

        message_key = (key or _default_key(event)).encode("utf-8")
        await self._producer.send_and_wait(
            target_topic,
            value=envelope.model_dump_json().encode("utf-8"),
            key=message_key,
            headers=headers,
        )

    async def healthy(self) -> bool:
        """Cheap readiness check.

        ``True`` if the producer's metadata client has bootstrapped to
        at least one broker; ``False`` otherwise. Suitable for k8s
        readiness probes.
        """
        if not self._started:
            return False
        try:
            cluster = self._producer.client.cluster
            return bool(cluster.brokers())
        except Exception:
            return False


def _default_key(event: KantoEvent) -> str:
    return f"{event.accession}:{event.version}"


def _registry_entry_for(event: KantoEvent) -> Any:
    for entry in EVENT_REGISTRY.values():
        if entry.schema_cls is type(event):
            return entry
    raise StreamingError(f"{type(event).__name__} not in EVENT_REGISTRY")


# ---------------------------------------------------------------------------
# Consumer
# ---------------------------------------------------------------------------


@dataclass
class ConsumedMessage(Generic[T]):  # noqa: UP046 — PEP 695 syntax not supported with @dataclass on 3.12
    """A successfully-parsed message yielded from the consumer."""

    event: T
    raw_value: bytes
    topic: str
    partition: int
    offset: int
    headers: dict[str, str]


# Type for the user's processing callback.
HandlerT = Callable[[ConsumedMessage[KantoEvent]], Awaitable[None]]


class StreamingConsumer:
    """Typed consumer with explicit commit-after-processing semantics.

    Usage::

        consumer = StreamingConsumer.from_settings(
            settings=settings,
            topic="kanto.discovered",
            group="snorlax",
            consumer_id="snorlax-0",
        )
        await consumer.start()
        async for parsed in consumer.messages():
            try:
                await handle(parsed)
                await consumer.commit(parsed)
            except Exception:
                await consumer.handle_failure(parsed, ...)

    A higher-level :meth:`run` helper wraps the boilerplate with
    automatic retry + DLQ routing for callers who don't need the
    finer control.
    """

    def __init__(
        self,
        *,
        consumer: AIOKafkaConsumer,
        producer: StreamingProducer,
        topic: str,
        group: str,
        consumer_id: str,
        max_attempts: int,
    ) -> None:
        self._consumer = consumer
        self._producer = producer
        self._topic = topic
        self._group = group
        self._consumer_id = consumer_id
        self._max_attempts = max_attempts
        self._started = False
        # In-memory tracking of attempt counts for messages we've seen.
        # ``(topic, partition, offset) -> attempt_count``.
        self._attempts: dict[tuple[str, int, int], int] = {}

    @classmethod
    def from_settings(
        cls,
        *,
        settings: StreamingSettings,
        topic: str,
        group: str,
        consumer_id: str | None = None,
    ) -> StreamingConsumer:  # pragma: no cover — real Kafka required
        consumer = AIOKafkaConsumer(
            topic,
            bootstrap_servers=settings.bootstrap_servers,
            group_id=group,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            **_sasl_kwargs(settings),
        )
        producer = StreamingProducer.from_settings(settings)
        return cls(
            consumer=consumer,
            producer=producer,
            topic=topic,
            group=group,
            consumer_id=consumer_id or socket.gethostname(),
            max_attempts=settings.max_processing_attempts,
        )

    async def start(self) -> None:
        await self._consumer.start()
        await self._producer.start()
        self._started = True

    async def stop(self) -> None:
        if self._started:
            await self._consumer.stop()
            await self._producer.stop()
            self._started = False

    async def messages(self) -> AsyncIterator[ConsumedMessage[KantoEvent]]:
        """Async iterator that yields parsed messages.

        Malformed messages (JSON errors, schema validation errors,
        unknown event_name) go straight to the DLQ and the offset is
        advanced — they're not yielded to the caller. Application
        errors raised by the caller go through :meth:`handle_failure`.

        Trace context is **not** activated here; activate it inside
        the processing scope via
        :func:`kanto_commons.tracing.use_extracted_context` and the
        message's ``headers``. The high-level :meth:`run` does this
        for you.
        """
        async for raw in self._consumer:
            headers = {k: v.decode("utf-8") for k, v in (raw.headers or [])}
            try:
                envelope = EventEnvelope.model_validate_json(raw.value)
                event = envelope.payload
            except (ValidationError, ValueError) as exc:
                await self._dispatch_to_dlq(
                    raw_value=raw.value,
                    partition=raw.partition,
                    offset=raw.offset,
                    failure_reason=f"schema validation: {exc}",
                    failure_traceback=traceback.format_exc(),
                    attempt_count=1,
                )
                await self._commit_offset(raw.partition, raw.offset)
                continue

            yield ConsumedMessage(
                event=event,
                raw_value=raw.value,
                topic=raw.topic,
                partition=raw.partition,
                offset=raw.offset,
                headers=headers,
            )

    async def commit(self, message: ConsumedMessage[KantoEvent]) -> None:
        """Commit the offset for ``message``. Call after successful processing.

        We commit ``offset + 1`` — Kafka semantics: "next message to read".
        """
        await self._commit_offset(message.partition, message.offset)
        self._attempts.pop((message.topic, message.partition, message.offset), None)

    async def _commit_offset(self, partition: int, offset: int) -> None:
        from aiokafka import TopicPartition  # local import — keep top-level slim
        from aiokafka.structs import OffsetAndMetadata

        await self._consumer.commit(
            {
                TopicPartition(self._topic, partition): OffsetAndMetadata(
                    offset=offset + 1, metadata=""
                )
            }
        )

    async def handle_failure(
        self,
        message: ConsumedMessage[KantoEvent],
        exc: BaseException,
    ) -> bool:
        """Record one failure for ``message``. Returns True if DLQ-routed.

        If the configured retry budget is exhausted, the message is
        sent to the DLQ and the offset is committed (so the next
        consumer instance doesn't reprocess). Otherwise the offset is
        NOT committed; aiokafka will redeliver after the next poll.
        """
        coords = (message.topic, message.partition, message.offset)
        self._attempts[coords] = self._attempts.get(coords, 0) + 1
        attempt = self._attempts[coords]
        if attempt < self._max_attempts:
            logger.warning(
                "stream-consumer: attempt %d/%d failed on %s",
                attempt,
                self._max_attempts,
                coords,
            )
            return False
        await self._dispatch_to_dlq(
            raw_value=message.raw_value,
            partition=message.partition,
            offset=message.offset,
            failure_reason=str(exc),
            failure_traceback=traceback.format_exc(),
            attempt_count=attempt,
        )
        await self._commit_offset(message.partition, message.offset)
        self._attempts.pop(coords, None)
        return True

    async def _dispatch_to_dlq(
        self,
        *,
        raw_value: bytes,
        partition: int,
        offset: int,
        failure_reason: str,
        failure_traceback: str | None,
        attempt_count: int,
    ) -> None:
        envelope = DLQEnvelope.from_raw(
            original_topic=self._topic,
            original_partition=partition,
            original_offset=offset,
            original_payload=raw_value,
            failure_reason=failure_reason,
            failure_traceback=failure_traceback,
            consumer_group=self._group,
            consumer_id=self._consumer_id,
            attempt_count=attempt_count,
        )
        dlq_topic = f"{self._topic}.dlq"
        # Use the underlying aiokafka producer directly here — we don't
        # want to wrap a DLQ envelope in another EventEnvelope.
        try:
            await self._producer._producer.send_and_wait(
                dlq_topic,
                value=envelope.model_dump_json().encode("utf-8"),
                key=str(offset).encode("utf-8"),
            )
        except KafkaError:
            logger.exception(
                "stream-consumer: DLQ publish FAILED for %s offset=%d",
                self._topic,
                offset,
            )
            raise

    async def run(self, handler: HandlerT) -> None:
        """High-level loop: yield → handle → commit-or-DLQ. Runs forever.

        Activates the message's W3C trace context around each handler
        call so the handler's spans chain to the producer's. Cancel
        the task to shut down. ``stop()`` is the caller's responsibility
        after cancellation.
        """
        async for parsed in self.messages():
            try:
                with use_extracted_context(parsed.headers):
                    await handler(parsed)
            except Exception as exc:
                routed = await self.handle_failure(parsed, exc)
                if not routed:
                    # Yield control so retry timing isn't a busy loop.
                    await asyncio.sleep(0)
                continue
            await self.commit(parsed)

    async def healthy(self) -> bool:
        if not self._started:
            return False
        try:
            return bool(self._consumer._client.cluster.brokers())
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _sasl_kwargs(settings: StreamingSettings) -> dict[str, Any]:
    """Build the SASL/SSL kwargs for aiokafka, if SASL is configured."""
    if settings.sasl_username is None or settings.sasl_password is None:
        return {}
    return {
        "security_protocol": "SASL_SSL",
        "sasl_mechanism": "PLAIN",
        "sasl_plain_username": settings.sasl_username,
        "sasl_plain_password": settings.sasl_password.get_secret_value(),
    }


# ---------------------------------------------------------------------------
# Health check protocol that services compose into liveness probes.
# ---------------------------------------------------------------------------


class HealthCheck(Protocol):
    async def healthy(self) -> bool: ...


@asynccontextmanager
async def lifecycle(
    *to_start: Any,
) -> AsyncIterator[None]:
    """Async context manager: ``start`` everything on entry, ``stop`` on exit.

    Convenience for service main functions::

        async with lifecycle(producer, consumer):
            await consumer.run(handler)
    """
    started: list[Any] = []
    try:
        for component in to_start:
            await component.start()
            started.append(component)
        yield
    finally:
        for component in reversed(started):
            try:
                await component.stop()
            except Exception:
                logger.exception("lifecycle: error stopping %r", component)


__all__ = [
    "ConsumedMessage",
    "HandlerT",
    "HealthCheck",
    "StreamingConsumer",
    "StreamingError",
    "StreamingProducer",
    "UnknownEventError",
    "lifecycle",
]
