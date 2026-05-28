"""Ditto configuration.

Same KantoBaseSettings inheritance pattern as snorlax/growlithe.
Service-specific knobs live under ``KANTO_DITTO_*`` env vars; Modal
Secrets surface them inside the container.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from kanto_commons.config import (
    KantoBaseSettings,
    MewSettings,
    ObjectStorageSettings,
    StreamingSettings,
    TracingSettings,
    load_section,
)
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class GpuType(StrEnum):
    """The Modal-supported GPU SKUs we care about.

    Values match Modal's GPU specifier strings. Defaults and tradeoffs
    documented at the call site in ``modal_app.py``.
    """

    L4 = "L4"
    A10 = "A10G"
    L40S = "L40S"
    A100_40GB = "A100-40GB"


class DittoServiceSettings(BaseSettings):
    """Service-specific config for Ditto."""

    model_config = SettingsConfigDict(
        env_prefix="KANTO_DITTO_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # ---------------- Model ----------------
    model_id: str = Field(
        default="esmc_600m",
        description=(
            "ESM C pretrained ID passed to ``ESMC.from_pretrained``. "
            "Pinning here so a model bump is a deliberate config change."
        ),
    )
    model_version: str = Field(
        default="2024-12",
        description=(
            "Human-readable model version recorded in the EmbeddingsReady "
            "event and the genome_embeddings row. Matches the HF tag "
            "EvolutionaryScale/esmc-600m-2024-12."
        ),
    )
    embedding_dim: int = Field(
        default=1152,
        description="ESM C 600M hidden width. Used for parquet schema validation.",
    )
    max_sequence_length: int = Field(
        default=2048,
        ge=1,
        description=(
            "Hard cap per ESM C model card. Proteins longer than this are "
            "truncated and a metric counter bumped — see :mod:`ditto.fasta`."
        ),
    )

    # ---------------- Batching ----------------
    # Tuned for an A10 (24GB) running bf16. Re-tune on L40S if used.
    # The product max_batch_size * (longest_in_batch ^ 2) is the rough
    # attention-memory ceiling; the batcher uses tokens_per_batch to
    # bound this directly instead of a fixed batch size.
    target_tokens_per_batch: int = Field(
        default=16_384,
        ge=512,
        description=(
            "Soft cap on (padded length * batch size). 16k tokens fits "
            "comfortably in 24GB of bf16 attention memory; raise to 32k "
            "for L40S, 64k for A100."
        ),
    )
    max_batch_size: int = Field(
        default=64,
        ge=1,
        description=(
            "Hard upper bound on batch size regardless of token count. "
            "Above this, kernel launch overhead matters less and we just "
            "burn memory for no throughput gain."
        ),
    )

    # ---------------- Idempotency ----------------
    skip_if_already_embedded: bool = Field(
        default=True,
        description=(
            "App-level idempotency guard. When True, the pipeline checks "
            "Mew for an existing EMBEDDED row on (accession) and skips "
            "the work if model_version matches. Set False only for "
            "operator-driven re-embed runs."
        ),
    )

    # ---------------- Defaults for the Modal function ----------------
    gpu_type: GpuType = Field(
        default=GpuType.A10,
        description=(
            "Default GPU. A10 is the cost/perf sweet spot for ESM C 600M "
            "(see README §GPU choice). Override with KANTO_DITTO_GPU_TYPE "
            "at deploy time for backfill (A100-40GB) or to test on L40S."
        ),
    )
    function_timeout_seconds: int = Field(
        default=600,
        ge=30,
        description=(
            "Per-invocation timeout. The largest bacterial genome (~12k "
            "proteins, max-len padding) fits in 2-3 minutes on A10; 10 "
            "minutes is a generous tail-latency budget."
        ),
    )
    function_retries: int = Field(
        default=2,
        ge=0,
        le=10,
        description=(
            "Modal retries on TransientError. The function itself does "
            "not retry inside the body (transient subops are retried by "
            "kanto-commons wrappers); the outer Modal retry is for "
            "container-level failures (worker death, host eviction)."
        ),
    )
    steady_state_concurrency: int = Field(
        default=4,
        ge=1,
        description=(
            "Max concurrent containers in steady-state. Bursty Snorlax "
            "throughput is ~1-2 isolates/min — 4 containers absorbs "
            "spikes without paying for idle warm capacity."
        ),
    )
    backfill_concurrency: int = Field(
        default=32,
        ge=1,
        description=(
            "Concurrency during a backfill operator-mode deploy. The "
            "switch is documented in README §Backfill."
        ),
    )

    # ---------------- Cost monitor ----------------
    cost_alert_daily_multiplier: float = Field(
        default=2.0,
        gt=1.0,
        description="Alert when daily spend exceeds N-times the rolling 7-day average.",
    )
    cost_alert_monthly_budget_usd: float = Field(
        default=500.0,
        gt=0,
        description=(
            "Alert when month-to-date spend projects above this number. "
            "Placeholder default; operator should set per-environment."
        ),
    )


class DittoSettings(KantoBaseSettings):
    """Top-level Ditto settings."""

    service_name: str = Field(default="ditto")
    ditto: DittoServiceSettings

    @classmethod
    def load(cls) -> Self:
        return cls(
            mew=load_section(MewSettings, "mew"),
            object_storage=load_section(ObjectStorageSettings, "object_storage"),
            streaming=load_section(StreamingSettings, "streaming"),
            tracing=load_section(TracingSettings, "tracing"),
            ditto=load_section(DittoServiceSettings, "ditto"),
        )


__all__ = ["DittoServiceSettings", "DittoSettings", "GpuType"]
