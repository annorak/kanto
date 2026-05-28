"""Long-lived Snorlax service: stream consumer + health server.

This is the StatefulSet workhorse: one pod per partition consumes
``kanto.discovered``, runs the pipeline, and commits the offset after
the full work is durable. Active-passive replicas are handled by the
Kafka consumer group itself — the rebalancer hands the partition to
the surviving replica when the active pod dies.

Three responsibilities live here:

1. Dependency wiring (:meth:`SnorlaxService.build`) — assembles the
   pipeline from settings. Tests can build one with stubs.
2. The consume-process-commit loop (:meth:`SnorlaxService.run`) with
   the failure-mode → offset semantics described in
   :mod:`snorlax.pipeline`.
3. Health/readiness server and graceful shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from types import FrameType
from typing import Any, cast

from aiohttp import web
from kanto_commons import IsolateDiscovered
from kanto_commons.logging import setup_logging
from kanto_commons.mew import MewClient
from kanto_commons.storage import KeyBuilder, ObjectStorage, ObjectStorageClient
from kanto_commons.streaming import (
    ConsumedMessage,
    StreamingConsumer,
)
from kanto_commons.tracing import setup_tracing, use_extracted_context

from snorlax.config import SnorlaxSettings
from snorlax.downloader import GenomeDownloader
from snorlax.metrics import SnorlaxMetrics
from snorlax.modal_client import (
    ModalClient,
    ModalUnavailableError,
    build_modal_client,
)
from snorlax.pipeline import Outcome, Pipeline, ProcessingResult
from snorlax.prodigal import ProdigalBinaryMissingError, ProdigalRunner
from snorlax.uploader import ProteinUploader

logger = logging.getLogger(__name__)


class SnorlaxService:
    """Owns the consumer, pipeline, health server, and shutdown."""

    def __init__(
        self,
        *,
        settings: SnorlaxSettings,
        consumer: StreamingConsumer,
        pipeline: Pipeline,
        mew: MewClient,
        storage_health: ObjectStorage,
        modal: ModalClient,
        downloader: GenomeDownloader,
    ) -> None:
        self._settings = settings
        self._consumer = consumer
        self._pipeline = pipeline
        self._mew = mew
        self._storage_health = storage_health
        self._modal = modal
        self._downloader = downloader
        self._shutdown = asyncio.Event()
        self._ready = False

    @classmethod
    async def build(cls, settings: SnorlaxSettings) -> SnorlaxService:
        """Wire all dependencies from settings.

        Kept here so ``__main__`` is tiny and tests have one place to
        substitute pieces. Raises on infrastructure-fatal misconfigs
        (e.g. Modal SDK missing when ``modal_enabled=True``) so the
        pod fails readiness instead of DLQing every event.
        """
        mew = await MewClient.from_settings(settings.mew)

        storage = ObjectStorageClient.from_settings(settings=settings.object_storage)
        key_builder = KeyBuilder.from_settings(settings.object_storage)
        uploader = ProteinUploader(storage=storage, key_builder=key_builder)

        downloader = GenomeDownloader.from_settings(
            timeout_seconds=settings.snorlax.download_timeout_seconds,
            max_attempts=settings.snorlax.download_max_attempts,
            chunk_bytes=settings.snorlax.download_chunk_bytes,
            max_bytes=settings.snorlax.max_genome_bytes,
            min_bytes=settings.snorlax.min_genome_bytes,
        )

        prodigal = ProdigalRunner(
            binary=settings.snorlax.prodigal_binary,
            mode=settings.snorlax.prodigal_mode,
            timeout_seconds=settings.snorlax.prodigal_timeout_seconds,
        )

        try:
            modal = build_modal_client(
                enabled=settings.snorlax.modal_enabled,
                function_ref=settings.snorlax.modal_function_ref,
                environment=settings.snorlax.modal_environment,
                max_attempts=settings.snorlax.modal_max_attempts,
            )
        except ModalUnavailableError:
            logger.exception("snorlax.service: modal client unavailable")
            raise

        work_dir = Path(settings.snorlax.work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

        # Forward-reference the pipeline so the gauge callback can read
        # its live in-flight count once it's constructed below.
        pipeline_ref: dict[str, Pipeline] = {}
        metrics = SnorlaxMetrics.build(
            in_flight_callback=lambda: pipeline_ref["p"].in_flight if "p" in pipeline_ref else 0,
        )
        pipeline = Pipeline(
            downloader=downloader,
            prodigal=prodigal,
            uploader=uploader,
            modal_client=modal,
            mew=mew,
            key_builder=key_builder,
            metrics=metrics,
            work_dir=work_dir,
            mew_max_attempts=settings.snorlax.mew_max_attempts,
        )
        pipeline_ref["p"] = pipeline

        consumer = StreamingConsumer.from_settings(
            settings=settings.streaming,
            topic=settings.streaming.discovered_topic,
            group=settings.snorlax.consumer_group,
            consumer_id=settings.snorlax.consumer_id,
        )

        return cls(
            settings=settings,
            consumer=consumer,
            pipeline=pipeline,
            mew=mew,
            storage_health=storage,
            modal=modal,
            downloader=downloader,
        )

    # -------------------- run loop --------------------

    async def run(self) -> None:
        """Consume → process → commit, until shutdown."""
        runner = await self._start_health_server()
        try:
            await self._consumer.start()
            self._ready = True
            await self._consume_loop()
        finally:
            self._ready = False
            await self._close(runner)

    async def _consume_loop(self) -> None:
        run_once = self._settings.snorlax.run_once
        async for parsed in self._consumer.messages():
            if not isinstance(parsed.event, IsolateDiscovered):
                # Wrong event on this topic is deterministic — straight to DLQ.
                await self._consumer.dlq_now(
                    parsed,
                    f"unexpected event {type(parsed.event).__name__} on {parsed.topic}",
                )
                continue

            try:
                with use_extracted_context(parsed.headers):
                    result = await self._pipeline.process(parsed.event)
            except Exception as exc:
                # Anything raised here is an infrastructure-fatal — the
                # pipeline never raises for application errors. Use
                # the consumer's transient-retry path so the offset
                # isn't lost on a transient pod issue.
                logger.exception(
                    "snorlax.service: unhandled error on accession=%s",
                    parsed.event.accession,
                )
                await self._consumer.handle_failure(parsed, exc)
                continue

            await self._apply_outcome(parsed, result)
            if run_once:
                break
            if self._shutdown.is_set():
                break

    async def _apply_outcome(
        self,
        msg: ConsumedMessage[Any],
        result: ProcessingResult,
    ) -> None:
        """Translate a :class:`ProcessingResult` into stream-side action."""
        if result.outcome in (Outcome.SUCCESS, Outcome.PERMANENT_FAILURE):
            await self._consumer.commit(msg)
            return
        if result.outcome is Outcome.DLQ:
            await self._consumer.dlq_now(msg, result.reason or result.outcome.value)
            return
        # TRANSIENT_RETRY: register the failure with the consumer so it
        # bumps the retry counter. The wrapper keeps the message's
        # offset uncommitted and re-yields it on the next iteration;
        # when ``max_processing_attempts`` is exhausted the wrapper
        # auto-DLQs it.
        await self._consumer.handle_failure(
            msg, _PipelineDLQError(result.reason or result.outcome.value)
        )

    # -------------------- shutdown --------------------

    def request_shutdown(self) -> None:
        if not self._shutdown.is_set():
            logger.info("snorlax.service: shutdown requested")
            self._shutdown.set()

    async def _close(self, runner: web.AppRunner) -> None:
        await runner.cleanup()
        with suppress(Exception):
            await self._consumer.stop()
        with suppress(Exception):
            await self._mew.close()
        with suppress(Exception):
            self._downloader.close()

    # -------------------- health server --------------------

    async def _start_health_server(self) -> web.AppRunner:
        app = web.Application()
        app.add_routes(
            [
                web.get("/healthz", self._healthz),
                web.get("/readyz", self._readyz),
            ]
        )
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(
            runner,
            host="0.0.0.0",
            port=self._settings.snorlax.health_port,
        )
        await site.start()
        return runner

    async def _healthz(self, _request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _readyz(self, _request: web.Request) -> web.Response:
        if not self._ready or self._shutdown.is_set():
            return web.json_response({"ready": False, "reason": "draining"}, status=503)
        if not await self._consumer.healthy():
            return web.json_response({"ready": False, "reason": "consumer"}, status=503)
        if not await self._mew.healthy():
            return web.json_response({"ready": False, "reason": "mew"}, status=503)
        # OS health is sync; defer to thread to keep the event loop fluid.
        ok = await asyncio.to_thread(self._storage_health.ping)
        if not ok:
            return web.json_response({"ready": False, "reason": "object_storage"}, status=503)
        if not self._modal.healthy():
            return web.json_response({"ready": False, "reason": "modal"}, status=503)
        return web.json_response({"ready": True})


# ---------------------------------------------------------------------------
# Helpers / bootstrap
# ---------------------------------------------------------------------------


class _PipelineDLQError(RuntimeError):
    """Marker exception used to push pipeline-DLQ outcomes through the consumer."""


@asynccontextmanager
async def _logging_lifespan() -> AsyncIterator[None]:
    """Cleanup-context manager kept for symmetry with growlithe.

    Currently a no-op; provides a single place to attach future
    process-wide aclose hooks without rippling through callers.
    """
    yield


async def amain(settings: SnorlaxSettings | None = None) -> int:
    """Async entry point for the CLI and integration tests."""
    settings = settings or SnorlaxSettings.load()
    setup_logging(
        service_name=settings.service_name,
        log_level=settings.log_level,
        log_format=settings.log_format,
    )
    setup_tracing(
        service_name=settings.service_name,
        environment=settings.environment.value,
        settings=settings.tracing,
    )
    logger.info(
        "snorlax.service: starting env=%s group=%s consumer_id=%s",
        settings.environment.value,
        settings.snorlax.consumer_group,
        settings.snorlax.consumer_id,
    )

    try:
        service = await SnorlaxService.build(settings)
    except ProdigalBinaryMissingError:
        logger.exception("snorlax.service: prodigal binary missing")
        return 2
    except ModalUnavailableError:
        return 2

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _make_signal_handler(service, sig))

    try:
        async with _logging_lifespan():
            await service.run()
    except Exception:
        logger.exception("snorlax.service: unhandled error; exiting non-zero")
        return 1
    return 0


def _make_signal_handler(service: SnorlaxService, sig: signal.Signals) -> Any:
    def _handler(*_args: Any, **_kwargs: Any) -> None:
        logger.info("snorlax.service: received %s", sig.name)
        service.request_shutdown()

    return cast(Any, _handler)


_ = FrameType  # keep import live for downstream typing

__all__ = ["SnorlaxService", "amain"]
