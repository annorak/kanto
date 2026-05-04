"""In-memory test doubles for the OCI Object Storage and Streaming wrappers.

Both fakes implement the same Protocol as the real client, so a
service that depends on the protocol can swap in either at test time
with no extra wiring.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import IO

from kanto_common.storage import (
    ObjectAlreadyExistsError,
    ObjectNotFoundError,
)

# ---------------------------------------------------------------------------
# Object Storage fake
# ---------------------------------------------------------------------------


@dataclass
class _StoredObject:
    data: bytes
    content_type: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


class FakeObjectStorage:
    """Dict-backed implementation of the OS protocol.

    Storage layout: ``self._objects[bucket][key] -> _StoredObject``.
    """

    def __init__(self) -> None:
        self._objects: dict[str, dict[str, _StoredObject]] = {}

    # -------------------- writes --------------------

    def put_bytes(
        self,
        *,
        bucket: str,
        key: str,
        data: bytes,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
        if_not_exists: bool = False,
    ) -> None:
        bucket_store = self._objects.setdefault(bucket, {})
        if if_not_exists and key in bucket_store:
            raise ObjectAlreadyExistsError(f"{bucket}/{key}")
        bucket_store[key] = _StoredObject(
            data=data,
            content_type=content_type,
            metadata=dict(metadata or {}),
        )

    def put_stream(
        self,
        *,
        bucket: str,
        key: str,
        stream: IO[bytes],
        content_length: int,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
        if_not_exists: bool = False,
    ) -> None:
        data = stream.read(content_length)
        self.put_bytes(
            bucket=bucket,
            key=key,
            data=data,
            content_type=content_type,
            metadata=metadata,
            if_not_exists=if_not_exists,
        )

    # -------------------- reads --------------------

    def get_bytes(self, *, bucket: str, key: str) -> bytes:
        try:
            return self._objects[bucket][key].data
        except KeyError as exc:
            raise ObjectNotFoundError(f"{bucket}/{key}") from exc

    def get_stream(
        self, *, bucket: str, key: str, chunk_size: int = 1024 * 1024
    ) -> Iterator[bytes]:
        data = self.get_bytes(bucket=bucket, key=key)
        for offset in range(0, len(data), chunk_size):
            yield data[offset : offset + chunk_size]

    def exists(self, *, bucket: str, key: str) -> bool:
        return key in self._objects.get(bucket, {})

    def delete(self, *, bucket: str, key: str) -> None:
        try:
            del self._objects[bucket][key]
        except KeyError as exc:
            raise ObjectNotFoundError(f"{bucket}/{key}") from exc

    def ping(self) -> bool:
        return True

    # -------------------- test-only inspection --------------------

    def all_keys(self, bucket: str) -> list[str]:
        return sorted(self._objects.get(bucket, {}).keys())

    def get_metadata(self, *, bucket: str, key: str) -> dict[str, str]:
        try:
            return dict(self._objects[bucket][key].metadata)
        except KeyError as exc:
            raise ObjectNotFoundError(f"{bucket}/{key}") from exc

    def reset(self) -> None:
        self._objects.clear()


# ---------------------------------------------------------------------------
# Streaming fake
# ---------------------------------------------------------------------------


@dataclass
class StoredMessage:
    """One message stored in the in-memory streaming fake."""

    topic: str
    key: str | None
    value: bytes
    headers: dict[str, str] = field(default_factory=dict)


class FakeStreamingClient:
    """Per-topic FIFO queue with consumer-group offset tracking.

    Implements the same async produce/consume API as the real
    :class:`kanto_common.streaming.StreamingClient`. Tests can
    construct one of these, ``await produce(...)`` to enqueue messages,
    then ``async for msg in consumer.messages():`` to drain them.

    No retries, no DLQ, no real partitioning — fast and deterministic.
    """

    def __init__(self) -> None:
        # topic -> list of messages (append-only)
        self._messages: dict[str, list[StoredMessage]] = {}
        # (topic, group) -> next offset to deliver
        self._offsets: dict[tuple[str, str], int] = {}
        # Per-topic event for blocking consumers waiting on new messages.
        self._has_new: dict[str, asyncio.Event] = {}
        # DLQ messages routed via send_to_dlq.
        self.dlq_messages: list[StoredMessage] = []

    async def produce(
        self,
        *,
        topic: str,
        value: bytes,
        key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        msg = StoredMessage(
            topic=topic,
            key=key,
            value=value,
            headers=dict(headers or {}),
        )
        self._messages.setdefault(topic, []).append(msg)
        evt = self._has_new.setdefault(topic, asyncio.Event())
        evt.set()

    async def send_to_dlq(self, *, original_topic: str, envelope_bytes: bytes) -> None:
        dlq_topic = f"{original_topic}.dlq"
        self.dlq_messages.append(
            StoredMessage(topic=dlq_topic, key=None, value=envelope_bytes)
        )
        await self.produce(topic=dlq_topic, value=envelope_bytes)

    async def consume(
        self,
        *,
        topic: str,
        group: str,
        timeout: float = 0.0,
    ) -> AsyncIterator[StoredMessage]:
        """Yield each unconsumed message exactly once per ``(topic, group)``.

        ``timeout=0`` returns immediately when the queue is empty.
        Positive ``timeout`` waits for that many seconds for new
        messages before stopping.
        """
        key = (topic, group)
        offset = self._offsets.get(key, 0)
        msgs = self._messages.get(topic, [])

        # Drain anything already enqueued.
        while offset < len(msgs):
            msg = msgs[offset]
            yield msg
            offset += 1
            self._offsets[key] = offset

        # If asked to wait, block for new messages until timeout.
        if timeout > 0:
            evt = self._has_new.setdefault(topic, asyncio.Event())
            try:
                await asyncio.wait_for(evt.wait(), timeout=timeout)
            except TimeoutError:
                return
            evt.clear()
            msgs = self._messages.get(topic, [])
            while offset < len(msgs):
                msg = msgs[offset]
                yield msg
                offset += 1
                self._offsets[key] = offset

    def messages_in(self, topic: str) -> list[StoredMessage]:
        return list(self._messages.get(topic, []))

    def reset(self) -> None:
        self._messages.clear()
        self._offsets.clear()
        self._has_new.clear()
        self.dlq_messages.clear()
