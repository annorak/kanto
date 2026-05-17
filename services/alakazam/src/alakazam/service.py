"""Long-lived Alakazam service: stream consumer + health + centroid refresh.

The deployment workhorse: one (or many) pod consumes ``kanto.embedded``,
drives the :class:`Pipeline`, and emits ``kanto.scored``. Three side
processes run alongside the consumer loop:

1. The aiohttp health/readiness server.
2. The species-centroid cache refresher (timer-driven).
3. SIGTERM / SIGINT graceful shutdown.

Dependency wiring lives in :meth:`AlakazamService.build` so tests can
substitute pieces.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from types import FrameType
from typing import Any, cast

from aiohttp import web
from kanto_commons import EmbeddingsReady
from kanto_commons.logging import setup_logging
from kanto_commons.mew import MewClient
from kanto_commons.storage import KeyBuilder, ObjectStorage, ObjectStorageClient
from kanto_commons.streaming import (
    ConsumedMessage,
    StreamingConsumer,
)
from kanto_commons.tracing import setup_tracing, use_extracted_context

from alakazam.config import AlakazamSettings
from alakazam.metrics import AlakazamMetrics
from alakazam.mew_gateway import AlakazamMewGateway
from alakazam.pipeline import Outcome, Pipeline, ProcessingResult
from alakazam.reference_set import ReferenceSet, load_reference_parquet
from alakazam.scoring import (
    CoverageStrategy,
    LinearCombiner,
    MahalanobisStrategy,
    NNDistanceStrategy,
    ScoringOrchestrator,
)
from alakazam.species_cache import SpeciesCentroidCache

logger = logging.getLogger(__name__)


class AlakazamService:
    """Owns the consumer, pipeline, health server, and shutdown."""

    def __init__(
        self,
        *,
        settings: AlakazamSettings,
        consumer: StreamingConsumer,
        pipeline: Pipeline,
        mew: MewClient,
        storage_health: ObjectStorage,
        species_cache: SpeciesCentroidCache,
    ) -> None:
        self._settings = settings
        self._consumer = consumer
        self._pipeline = pipeline
        self._mew = mew
        self._storage_health = storage_health
        self._species_cache = species_cache
        self._shutdown = asyncio.Event()
        self._ready = False
        self._refresh_task: asyncio.Task[None] | None = None

    @classmethod
    async def build(cls, settings: AlakazamSettings) -> AlakazamService:
        """Wire dependencies from settings."""
        mew = await MewClient.from_settings(settings.mew)
        gateway = AlakazamMewGateway(mew=mew)

        storage = ObjectStorageClient.from_settings(settings=settings.object_storage)
        key_builder = KeyBuilder.from_settings(settings.object_storage)

        reference_set = _load_reference_set(
            storage=storage,
            container=settings.alakazam.reference_set_container,
            key=settings.alakazam.reference_set_key,
        )

        species_cache = SpeciesCentroidCache(gateway=gateway)
        await species_cache.refresh()

        nn_strategy = NNDistanceStrategy(gateway=gateway, k=settings.alakazam.nn_k)
        coverage_strategy = CoverageStrategy(
            reference_set=reference_set,
            storage=storage,
            key_builder=key_builder,
            embeddings_container=settings.object_storage.embeddings_container,
            match_threshold=settings.alakazam.coverage_match_threshold,
            max_proteins=settings.alakazam.coverage_max_proteins,
        )
        mahalanobis_strategy = MahalanobisStrategy(
            centroid_cache=species_cache,
            min_samples=settings.alakazam.mahalanobis_min_samples,
            regularization=settings.alakazam.mahalanobis_regularization,
        )
        combiner = LinearCombiner(
            weight_nn=settings.alakazam.weight_nn,
            weight_coverage=settings.alakazam.weight_coverage,
            weight_mahalanobis=settings.alakazam.weight_mahalanobis,
        )
        orchestrator = ScoringOrchestrator(
            nn_strategy=nn_strategy,
            coverage_strategy=coverage_strategy,
            mahalanobis_strategy=mahalanobis_strategy,
            combiner=combiner,
            candidate_threshold=settings.alakazam.candidate_threshold,
            alert_threshold=settings.alakazam.alert_threshold,
        )

        pipeline_ref: dict[str, Pipeline] = {}
        metrics = AlakazamMetrics.build(
            in_flight_callback=lambda: pipeline_ref["p"].in_flight if "p" in pipeline_ref else 0,
            centroid_cache_size_callback=lambda: species_cache.size,
        )

        consumer = StreamingConsumer.from_settings(
            settings=settings.streaming,
            topic=settings.streaming.embedded_topic,
            group=settings.alakazam.consumer_group,
            consumer_id=settings.alakazam.consumer_id,
        )

        pipeline = Pipeline(
            gateway=gateway,
            orchestrator=orchestrator,
            # The consumer owns the producer it shares with itself; we
            # reach through to it here so we don't open a second TCP
            # session for the score emit path.
            producer=consumer._producer,
            metrics=metrics,
            candidate_threshold=settings.alakazam.candidate_threshold,
        )
        pipeline_ref["p"] = pipeline

        return cls(
            settings=settings,
            consumer=consumer,
            pipeline=pipeline,
            mew=mew,
            storage_health=storage,
            species_cache=species_cache,
        )

    # -------------------- run loop --------------------

    async def run(self) -> None:
        runner = await self._start_health_server()
        try:
            await self._consumer.start()
            self._refresh_task = asyncio.create_task(
                self._centroid_refresh_loop(),
                name="alakazam-centroid-refresh",
            )
            self._ready = True
            await self._consume_loop()
        finally:
            self._ready = False
            await self._close(runner)

    async def _consume_loop(self) -> None:
        run_once = self._settings.alakazam.run_once
        async for parsed in self._consumer.messages():
            if not isinstance(parsed.event, EmbeddingsReady):
                await self._consumer.handle_failure(
                    parsed,
                    TypeError(f"unexpected event {type(parsed.event).__name__} on {parsed.topic}"),
                )
                continue

            try:
                with use_extracted_context(parsed.headers):
                    result = await self._pipeline.process(parsed.event)
            except Exception as exc:
                logger.exception(
                    "alakazam.service: unhandled error on accession=%s",
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
        if result.outcome is Outcome.SUCCESS:
            await self._consumer.commit(msg)
            return
        if result.outcome is Outcome.DLQ:
            for _ in range(self._settings.streaming.max_processing_attempts):
                routed = await self._consumer.handle_failure(
                    msg,
                    _PipelineDLQError(result.reason or result.outcome.value),
                )
                if routed:
                    return
            logger.error(
                "alakazam.service: handle_failure did not DLQ %s; committing",
                msg.offset,
            )
            await self._consumer.commit(msg)
            return
        # TRANSIENT_RETRY: leave the offset alone; aiokafka redelivers.

    async def _centroid_refresh_loop(self) -> None:
        """Background timer that reloads the species centroid cache."""
        interval = self._settings.alakazam.centroid_refresh_seconds
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=interval)
                return  # shutdown set during the wait
            except TimeoutError:
                pass
            try:
                await self._species_cache.refresh()
            except Exception:
                # Refresh failure is non-fatal: we keep serving with
                # the older cache. Logged so oncall can see the gap.
                logger.exception("alakazam.service: centroid refresh failed")

    # -------------------- shutdown --------------------

    def request_shutdown(self) -> None:
        if not self._shutdown.is_set():
            logger.info("alakazam.service: shutdown requested")
            self._shutdown.set()

    async def _close(self, runner: web.AppRunner) -> None:
        await runner.cleanup()
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._refresh_task
        with suppress(Exception):
            await self._consumer.stop()
        with suppress(Exception):
            await self._mew.close()

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
            port=self._settings.alakazam.health_port,
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
        ok = await asyncio.to_thread(self._storage_health.ping)
        if not ok:
            return web.json_response({"ready": False, "reason": "object_storage"}, status=503)
        return web.json_response({"ready": True})


# ---------------------------------------------------------------------------
# Helpers / bootstrap
# ---------------------------------------------------------------------------


def _load_reference_set(
    *,
    storage: ObjectStorage,
    container: str,
    key: str,
) -> ReferenceSet | None:
    """Best-effort reference set load.

    A missing reference parquet is logged, not fatal — the coverage
    strategy skips with :class:`SkipReason.REFERENCE_SET_MISSING` and
    the operator gets a metric. This means a fresh environment can
    deploy Alakazam before the bundle is built, and scoring degrades
    gracefully to tier-1-only in the meantime.
    """
    try:
        return load_reference_parquet(storage, container=container, key=key)
    except Exception:
        logger.exception(
            "alakazam.service: failed to load reference set %s/%s — "
            "coverage strategy will skip every isolate until fixed",
            container,
            key,
        )
        return None


class _PipelineDLQError(RuntimeError):
    """Marker exception for pipeline-DLQ outcomes routed via the consumer."""


@asynccontextmanager
async def _logging_lifespan() -> AsyncIterator[None]:
    yield


async def amain(settings: AlakazamSettings | None = None) -> int:
    """Async entry point for the CLI and integration tests."""
    settings = settings or AlakazamSettings.load()
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
        "alakazam.service: starting env=%s group=%s consumer_id=%s",
        settings.environment.value,
        settings.alakazam.consumer_group,
        settings.alakazam.consumer_id,
    )

    service = await AlakazamService.build(settings)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _make_signal_handler(service, sig))

    try:
        async with _logging_lifespan():
            await service.run()
    except Exception:
        logger.exception("alakazam.service: unhandled error; exiting non-zero")
        return 1
    return 0


def _make_signal_handler(service: AlakazamService, sig: signal.Signals) -> Any:
    def _handler(*_args: Any, **_kwargs: Any) -> None:
        logger.info("alakazam.service: received %s", sig.name)
        service.request_shutdown()

    return cast(Any, _handler)


_ = FrameType  # keep import live for downstream typing

__all__ = ["AlakazamService", "amain"]
