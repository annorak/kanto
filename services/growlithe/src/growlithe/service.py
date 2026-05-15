"""Long-lived Growlithe service: timer + health server + graceful shutdown.

Choosing long-lived Deployment over Kubernetes CronJob
------------------------------------------------------

Both shapes work for "poll every 6h". A long-lived Deployment is the
operationally simpler default because:

* Probes (``/healthz``, ``/readyz``) work out of the box; a CronJob
  needs a sidecar for that.
* In-process metrics counters survive across cycles, so dashboards
  show real trends, not just last-run snapshots.
* Tracing-context carryover between cycles is trivial; with a
  CronJob, each invocation is a fresh trace.
* SIGTERM-driven graceful shutdown is the standard pod lifecycle.

The cost is one always-on pod doing nothing for 5h 55m. That cost is
well under one node-tenth at the smallest preset.

Graceful shutdown
-----------------

On SIGTERM (or SIGINT), the service marks itself unready, stops
accepting new cycle triggers, allows any in-progress cycle to
complete (bounded by the pod's terminationGracePeriodSeconds), then
exits 0. This keeps half-emitted cycles from leaving the cursor out
of sync.
"""

from __future__ import annotations

import asyncio
import logging
import random
import signal
from contextlib import suppress
from types import FrameType
from typing import Any, cast

from aiohttp import web
from kanto_commons.logging import setup_logging
from kanto_commons.mew import MewClient
from kanto_commons.storage import ObjectStorageClient
from kanto_commons.streaming import StreamingProducer
from kanto_commons.tracing import setup_tracing

from growlithe.config import GrowlitheSettings
from growlithe.cursor import DiscoveryCursorRepository
from growlithe.datasource.ncbi import NCBIPathogenDetection
from growlithe.metrics import GrowlitheMetrics
from growlithe.pipeline import CycleResult, Pipeline
from growlithe.snapshot_cache import SnapshotCache

logger = logging.getLogger(__name__)


class GrowlitheService:
    """Owns the pipeline, the timer, the health server, and shutdown."""

    def __init__(
        self,
        *,
        settings: GrowlitheSettings,
        pipeline: Pipeline,
        mew: MewClient,
        producer: StreamingProducer,
        ncbi: NCBIPathogenDetection,
    ) -> None:
        self._settings = settings
        self._pipeline = pipeline
        self._mew = mew
        self._producer = producer
        self._ncbi = ncbi
        self._shutdown = asyncio.Event()
        self._ready = False
        self._last_cycle: CycleResult | None = None

    @classmethod
    async def build(cls, settings: GrowlitheSettings) -> GrowlitheService:
        """Wire up every dependency from settings.

        Kept here so ``__main__`` is a 3-line file; gives tests one
        place to mock pieces of when they want a near-real instance.
        """
        mew = await MewClient.from_settings(settings.mew)
        producer = StreamingProducer.from_settings(settings.streaming)
        await producer.start()
        storage = ObjectStorageClient.from_settings(settings=settings.object_storage)
        cache = SnapshotCache(
            storage=storage,
            container=settings.object_storage.metadata_container,
        )
        ncbi = NCBIPathogenDetection.from_config(
            organisms=list(settings.growlithe.organisms),
            base_url=settings.growlithe.ncbi_base_url,
            timeout_seconds=settings.growlithe.ncbi_request_timeout_seconds,
            max_attempts=settings.growlithe.ncbi_max_attempts,
        )
        metrics = GrowlitheMetrics.build()
        pipeline = Pipeline(
            source=ncbi,
            cache=cache,
            mew=mew,
            cursors=DiscoveryCursorRepository(),
            emit=producer.send,
            metrics=metrics,
            max_concurrent=settings.growlithe.ncbi_max_concurrent,
        )
        return cls(
            settings=settings,
            pipeline=pipeline,
            mew=mew,
            producer=producer,
            ncbi=ncbi,
        )

    # -------------------- run loop --------------------

    async def run(self) -> None:
        """Run cycles forever, or once if ``run_once`` is True."""
        health_runner = await self._start_health_server()
        try:
            self._ready = True
            if self._settings.growlithe.run_once:
                self._last_cycle = await self._pipeline.run_once()
                return

            await self._cycle_loop()
        finally:
            self._ready = False
            await self._close(health_runner)

    async def _cycle_loop(self) -> None:
        interval = self._settings.growlithe.poll_interval_seconds
        jitter = self._settings.growlithe.poll_jitter_seconds
        while not self._shutdown.is_set():
            try:
                self._last_cycle = await self._pipeline.run_once()
            except Exception:
                logger.exception("growlithe.service: cycle aborted with unhandled error")
            wait = max(1, interval + random.randint(-jitter, jitter))
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._shutdown.wait(), timeout=wait)

    # -------------------- shutdown --------------------

    def request_shutdown(self) -> None:
        if not self._shutdown.is_set():
            logger.info("growlithe.service: shutdown requested")
            self._shutdown.set()

    async def _close(self, health_runner: web.AppRunner) -> None:
        await health_runner.cleanup()
        with suppress(Exception):
            await self._producer.stop()
        with suppress(Exception):
            await self._mew.close()
        with suppress(Exception):
            self._ncbi.close()

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
            port=self._settings.growlithe.health_port,
        )
        await site.start()
        return runner

    async def _healthz(self, _request: web.Request) -> web.Response:
        # Liveness must not depend on downstreams — a transient Mew or
        # Event Hubs blip should not restart the pod.
        return web.json_response({"status": "ok"})

    async def _readyz(self, _request: web.Request) -> web.Response:
        if not self._ready or self._shutdown.is_set():
            return web.json_response({"ready": False, "reason": "draining"}, status=503)
        # Cheap downstream probes; both must succeed.
        ok = await self._mew.healthy()
        if not ok:
            return web.json_response({"ready": False, "reason": "mew"}, status=503)
        ok = await self._producer.healthy()
        if not ok:
            return web.json_response({"ready": False, "reason": "producer"}, status=503)
        return web.json_response({"ready": True})


# ---------------------------------------------------------------------------
# Process bootstrap
# ---------------------------------------------------------------------------


async def amain(settings: GrowlitheSettings | None = None) -> int:
    """Async entry point used by ``__main__`` and the smoke test.

    Returns the process exit code (0 on clean shutdown / run_once,
    nonzero on an unhandled error).
    """
    settings = settings or GrowlitheSettings.load()
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
        "growlithe.service: starting env=%s organisms=%s",
        settings.environment.value,
        ",".join(settings.growlithe.organisms),
    )

    service = await GrowlitheService.build(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _make_signal_handler(service, sig))

    try:
        await service.run()
    except Exception:
        logger.exception("growlithe.service: unhandled error; exiting non-zero")
        return 1
    return 0


def _make_signal_handler(service: GrowlitheService, sig: signal.Signals) -> Any:
    """Return a closure suitable for ``loop.add_signal_handler``."""

    def _handler(*_args: Any, **_kwargs: Any) -> None:
        logger.info("growlithe.service: received %s", sig.name)
        service.request_shutdown()

    # add_signal_handler accepts a Callable[..., None]; cast to satisfy mypy
    return cast(Any, _handler)


# Keep `FrameType` referenced so the import survives strict ruff/mypy.
_ = FrameType

__all__ = ["GrowlitheService", "amain"]
