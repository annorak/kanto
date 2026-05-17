"""End-to-end Ditto benchmark -- task-07 section 7 (cross-cloud).

Measures wall time for one full embed cycle. Two modes:

* ``--mode local``: runs the pipeline in-process against real Azure
  + Mew + Event Hubs and a local CPU-or-GPU ESM C. Useful when
  iterating without re-deploying Modal.
* ``--mode modal``: invokes the deployed ``kanto-ditto`` class and
  reports the round-trip wall time. Per-stage timings live in the
  Modal-side structured logs (see ``ditto/azure_io.py`` and
  ``ditto/event_emit.py``); fetch them with ``modal app logs`` --
  the script intentionally does not scrape logs.

Run::

    ditto-benchmark --mode local --accession PDT001 --version 1 \\
        --os-key PDT001/1.faa.gz
    ditto-benchmark --mode modal --accession PDT001 --version 1 \\
        --os-key PDT001/1.faa.gz
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass


@dataclass
class BenchmarkResult:
    mode: str
    accession: str
    version: int
    os_key: str
    wall_seconds: float
    proteins_embedded: int
    skipped_idempotent: bool


def _build_local_pipeline() -> tuple[object, object, object]:  # pragma: no cover
    """Construct a real EmbedPipeline pointing at live services.

    Returns ``(pipeline, mew, producer)`` so the caller can flush.
    """
    from kanto_commons.mew import MewClient
    from kanto_commons.storage import KeyBuilder, ObjectStorageClient
    from kanto_commons.streaming import StreamingProducer

    from ditto.azure_io import AzureBlobIO
    from ditto.config import DittoSettings
    from ditto.event_emit import StreamingEventEmitter
    from ditto.model import load_embedder
    from ditto.pipeline import EmbedPipeline

    settings = DittoSettings.load()

    async def _open() -> tuple[object, object, object]:
        mew = await MewClient.from_settings(settings.mew)
        producer = StreamingProducer.from_settings(settings.streaming)
        await producer.start()
        embedder = load_embedder(
            model_id=settings.ditto.model_id,
            embedding_dim=settings.ditto.embedding_dim,
        )
        pipeline = EmbedPipeline(
            embedder=embedder,
            blob_io=AzureBlobIO(
                storage=ObjectStorageClient.from_settings(settings=settings.object_storage)
            ),
            mew=mew,
            emitter=StreamingEventEmitter(producer=producer),
            key_builder=KeyBuilder.from_settings(settings.object_storage),
            settings=settings.ditto,
        )
        return pipeline, mew, producer

    return asyncio.run(_open())


def _local_run(accession: str, version: int, os_key: str) -> BenchmarkResult:  # pragma: no cover
    from kanto_commons.mew import MewClient

    pipeline, mew, producer = _build_local_pipeline()
    t0 = time.monotonic()
    result = asyncio.run(
        pipeline.embed_isolate(accession=accession, version=version, os_key=os_key)  # type: ignore[attr-defined]
    )
    elapsed = time.monotonic() - t0

    async def _close() -> None:
        await producer.stop()  # type: ignore[attr-defined]
        if isinstance(mew, MewClient):
            await mew.close()

    asyncio.run(_close())
    return BenchmarkResult(
        mode="local",
        accession=accession,
        version=version,
        os_key=os_key,
        wall_seconds=elapsed,
        proteins_embedded=result.proteins_embedded,
        skipped_idempotent=result.skipped_idempotent,
    )


def _modal_run(accession: str, version: int, os_key: str) -> BenchmarkResult:  # pragma: no cover
    import modal

    cls = modal.Cls.from_name("kanto-ditto", "DittoEmbedder")
    t0 = time.monotonic()
    result = cls().embed.remote(accession=accession, version=version, os_key=os_key)
    elapsed = time.monotonic() - t0
    return BenchmarkResult(
        mode="modal",
        accession=accession,
        version=version,
        os_key=os_key,
        wall_seconds=elapsed,
        proteins_embedded=int(result["proteins_embedded"]),
        skipped_idempotent=bool(result["skipped_idempotent"]),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ditto end-to-end benchmark")
    parser.add_argument("--mode", choices=("local", "modal"), default="local")
    parser.add_argument("--accession", required=True)
    parser.add_argument("--version", type=int, required=True)
    parser.add_argument("--os-key", required=True)
    args = parser.parse_args(argv)

    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")

    runner = _local_run if args.mode == "local" else _modal_run
    result = runner(args.accession, args.version, args.os_key)
    print(json.dumps(asdict(result), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
