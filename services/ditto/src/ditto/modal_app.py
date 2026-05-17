"""Modal app + function definition for Ditto.

Deploy::

    modal deploy services/ditto/src/ditto/modal_app.py

Run a single invocation locally::

    modal run services/ditto/src/ditto/modal_app.py::main \\
        --accession PDT001234567 --version 1 --os-key PDT001234567/1.faa.gz

This file is intentionally thin: it owns Modal-specific wiring (image
build, secrets, GPU type, lifecycle hooks). All heavy logic lives in
:mod:`ditto.pipeline` and is unit-tested without a Modal account.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import modal

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

APP_NAME = "kanto-ditto"
app = modal.App(APP_NAME)


# ---------------------------------------------------------------------------
# Image
# ---------------------------------------------------------------------------


def _download_esmc_weights() -> None:  # pragma: no cover -- image-build only
    """Pulled at image build so weights are baked into the image layer.

    After this, ``ESMC.from_pretrained("esmc_600m")`` at @modal.enter
    time hits the local HF cache instead of re-downloading per worker.
    """
    from esm.models.esmc import ESMC

    ESMC.from_pretrained("esmc_600m")


_image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("git")
    .pip_install(
        # Pins mirror pyproject.toml. Modal's build cache keys on the
        # literal pin set, so bumps re-run downstream layers.
        "torch==2.4.1",
        "esm==3.1.5",
        "pyarrow==17.0.0",
        "azure-storage-blob>=12.20,<13",
        "azure-identity>=1.18,<2",
        "psycopg[binary]>=3.2,<4",
        "psycopg-pool>=3.2,<4",
        "pgvector>=0.3,<1",
        "aiokafka>=0.11,<0.13",
        "pydantic>=2.9,<3",
        "pydantic-settings>=2.6,<3",
        "tenacity>=9.0,<10",
        "structlog>=24.4,<25",
        "opentelemetry-api>=1.27,<2",
        "opentelemetry-sdk>=1.27,<2",
        "opentelemetry-exporter-otlp-proto-http>=1.27,<2",
        extra_options="--extra-index-url https://download.pytorch.org/whl/cu124",
    )
    .run_function(_download_esmc_weights, gpu=None, timeout=600)
    .add_local_python_source("ditto", "kanto_commons")
)


# ---------------------------------------------------------------------------
# Secrets + deploy-time tunables
# ---------------------------------------------------------------------------

_SECRETS = [
    modal.Secret.from_name("kanto-mew"),
    modal.Secret.from_name("kanto-azure"),
    modal.Secret.from_name("kanto-eventhubs"),
    modal.Secret.from_name("kanto-otel"),
]

# Decorator args are evaluated at module-import time, so per-env
# tunables come from env vars on the deploying shell. See README §Deploy.
_GPU = os.environ.get("KANTO_DITTO_GPU_TYPE", "A10G")
_TIMEOUT = int(os.environ.get("KANTO_DITTO_FUNCTION_TIMEOUT", "600"))
_RETRIES = int(os.environ.get("KANTO_DITTO_FUNCTION_RETRIES", "2"))
_CONCURRENCY = int(os.environ.get("KANTO_DITTO_CONCURRENCY", "4"))


# ---------------------------------------------------------------------------
# Function class
# ---------------------------------------------------------------------------


@app.cls(
    image=_image,
    gpu=_GPU,
    secrets=_SECRETS,
    timeout=_TIMEOUT,
    retries=modal.Retries(max_retries=_RETRIES, backoff_coefficient=2.0, initial_delay=2.0),
    max_containers=_CONCURRENCY,
)
class DittoEmbedder:
    """Per-worker init + per-call embed.

    The class shape is required so ESM C weights load once per worker
    in @modal.enter rather than per invocation.
    """

    @modal.enter()
    def load(self) -> None:
        """Per-worker init -- weights load + clients open here.

        Pays the ~30s weight load + ~5s client open once per cold
        start, amortised over every subsequent call on this worker.
        """
        from kanto_commons.mew import MewClient
        from kanto_commons.storage import KeyBuilder, ObjectStorageClient
        from kanto_commons.streaming import StreamingProducer

        from ditto.azure_io import AzureBlobIO
        from ditto.config import DittoSettings
        from ditto.event_emit import StreamingEventEmitter
        from ditto.model import load_embedder
        from ditto.pipeline import EmbedPipeline

        logging.basicConfig(level=os.environ.get("KANTO_LOG_LEVEL", "INFO"))

        self._settings = DittoSettings.load()
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        embedder = load_embedder(
            model_id=self._settings.ditto.model_id,
            embedding_dim=self._settings.ditto.embedding_dim,
            device="cuda",
        )
        self._mew = self._loop.run_until_complete(MewClient.from_settings(self._settings.mew))
        self._producer = StreamingProducer.from_settings(self._settings.streaming)
        self._loop.run_until_complete(self._producer.start())

        self._pipeline = EmbedPipeline(
            embedder=embedder,
            blob_io=AzureBlobIO(
                storage=ObjectStorageClient.from_settings(settings=self._settings.object_storage)
            ),
            mew=self._mew,
            emitter=StreamingEventEmitter(producer=self._producer),
            key_builder=KeyBuilder.from_settings(self._settings.object_storage),
            settings=self._settings.ditto,
        )

    @modal.exit()
    def shutdown(self) -> None:
        """Best-effort flush on container teardown."""
        try:
            self._loop.run_until_complete(self._producer.stop())
        except Exception:
            logging.exception("ditto.modal_app: producer stop failed")

    @modal.method()
    def embed(
        self,
        accession: str,
        version: int,
        os_key: str,
    ) -> dict[str, Any]:
        """Public Modal entry point. Signature matches Snorlax's spawn."""
        result = self._loop.run_until_complete(
            self._pipeline.embed_isolate(accession=accession, version=version, os_key=os_key)
        )
        return {
            "accession": result.accession,
            "version": result.version,
            "proteins_embedded": result.proteins_embedded,
            "parquet_key": result.parquet_key,
            "skipped_idempotent": result.skipped_idempotent,
        }


@app.local_entrypoint()
def main(accession: str, version: int, os_key: str) -> None:  # pragma: no cover
    """`modal run` entrypoint for one-off invocations."""
    result = DittoEmbedder().embed.remote(accession=accession, version=version, os_key=os_key)
    print(result)
