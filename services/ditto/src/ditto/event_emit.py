"""EmbeddingsReady emission to the kanto.embedded Event Hub.

Thin wrapper around :class:`kanto_commons.streaming.StreamingProducer`.
Kept as a wrapper for the same reasons as :mod:`ditto.azure_io`:
narrow Protocol the pipeline depends on, timing logs in one place,
and an error-mapping site.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Protocol

from kanto_commons import EmbeddingsReady
from kanto_commons.streaming import StreamingError, StreamingProducer

from ditto.errors import EventEmitError

logger = logging.getLogger(__name__)


class EventEmitter(Protocol):
    """Surface Ditto's pipeline depends on."""

    async def emit_embeddings_ready(
        self,
        *,
        accession: str,
        version: int,
        model: str,
        model_version: str,
        os_key: str,
    ) -> None: ...


class StreamingEventEmitter:
    """Production EmbeddingsReady emitter backed by the shared producer."""

    def __init__(self, *, producer: StreamingProducer) -> None:
        self._producer = producer

    async def emit_embeddings_ready(
        self,
        *,
        accession: str,
        version: int,
        model: str,
        model_version: str,
        os_key: str,
    ) -> None:
        event = EmbeddingsReady(
            accession=accession,
            version=version,
            model=model,
            model_version=model_version,
            os_key=os_key,
            embedded_at=datetime.now(UTC),
        )
        started = time.monotonic()
        try:
            await self._producer.send(event)
        except StreamingError as exc:
            raise EventEmitError(
                f"failed to emit EmbeddingsReady for {accession}@{version}: {exc}"
            ) from exc
        elapsed = time.monotonic() - started
        logger.info(
            "ditto.event_emit.embeddings_ready accession=%s version=%d " "elapsed_s=%.3f",
            accession,
            version,
            elapsed,
        )


__all__ = ["EventEmitter", "StreamingEventEmitter"]
