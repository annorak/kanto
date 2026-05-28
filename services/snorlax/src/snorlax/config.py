"""Snorlax configuration.

Service-specific knobs documented in ``services/snorlax/README.md``.
Mirrors the layout of :mod:`growlithe.config`: a sibling
``SnorlaxServiceSettings`` keeps the env vars under a flat
``KANTO_SNORLAX_*`` namespace.
"""

from __future__ import annotations

from typing import Self

from kanto_commons.config import (
    KantoBaseSettings,
    MewSettings,
    ObjectStorageSettings,
    StreamingSettings,
    TracingSettings,
    load_section,
)
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SnorlaxServiceSettings(BaseSettings):
    """Service-specific config for Snorlax."""

    model_config = SettingsConfigDict(
        env_prefix="KANTO_SNORLAX_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # ---------------- Consumer wiring ----------------
    consumer_group: str = Field(
        default="snorlax",
        description="Kafka consumer group ID. One group per logical consumer.",
    )
    # Stable identifier per pod. Helm sets it to ``$(POD_NAME)`` so the
    # consumer group rebalancer can tell replicas apart cleanly.
    consumer_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "KANTO_SNORLAX_CONSUMER_ID",
            "POD_NAME",
        ),
        description=(
            "Stable id for this consumer instance. Defaults to POD_NAME "
            "when running under Kubernetes."
        ),
    )

    # ---------------- NCBI download ----------------
    ncbi_assembly_base_url: str = Field(
        default="https://ftp.ncbi.nlm.nih.gov/genomes/all/GCA/",
        description="Base URL for GenBank assembly downloads (HTTPS, not FTP).",
    )
    download_timeout_seconds: float = Field(
        default=300.0,
        gt=0,
        description=(
            "Per-download HTTP timeout. Bacterial genomes are ~3 MB but "
            "NCBI gets slow during their nightly window."
        ),
    )
    download_max_attempts: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Retry budget for transient download failures.",
    )
    download_chunk_bytes: int = Field(
        default=1024 * 1024,
        ge=4096,
        description="Streaming download chunk size — keeps memory bounded.",
    )
    max_genome_bytes: int = Field(
        default=200 * 1024 * 1024,
        ge=1024 * 1024,
        description=(
            "Reject downloads larger than this. Bacterial genomes are at "
            "most ~15 MB compressed; anything 10x bigger is a wrong file "
            "or a misconfig."
        ),
    )
    min_genome_bytes: int = Field(
        default=200 * 1024,
        ge=1,
        description=(
            "Reject downloads smaller than this. Real bacterial genomes "
            "are >500 KB compressed; tiny files mean a malformed redirect."
        ),
    )

    # ---------------- Prodigal ----------------
    prodigal_binary: str = Field(
        default="prodigal",
        description="Path to the Prodigal binary. Resolved on $PATH if relative.",
    )
    prodigal_timeout_seconds: float = Field(
        default=300.0,
        gt=0,
        description="Hard timeout for the Prodigal subprocess.",
    )
    prodigal_mode: str = Field(
        default="single",
        pattern=r"^(single|meta)$",
        description="Prodigal procedure. v1 is single-organism only.",
    )
    work_dir: str = Field(
        default="/tmp/snorlax",
        description=(
            "Pod-local scratch directory for in-flight FASTAs. Cleared "
            "after each isolate (success or failure) so the pod's "
            "ephemeral storage doesn't fill up."
        ),
    )

    # ---------------- Modal ----------------
    modal_function_ref: str = Field(
        default="kanto-ditto/DittoEmbedder/embed",
        description=(
            "Modal reference in ``app/Class/method`` form. Ditto is a "
            "@app.cls deployment, so Snorlax resolves the class via "
            "modal.Cls.from_name(...)() and then dispatches the method."
        ),
    )
    modal_environment: str = Field(
        default="main",
        description="Modal environment name; selects between dev/prod stacks.",
    )
    modal_max_attempts: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Retry budget for transient Modal spawn failures.",
    )
    modal_enabled: bool = Field(
        default=True,
        description=(
            "When False the in-memory NoopModalClient is used instead of "
            "the real Modal SDK. Set to False in dev/staging before "
            "Task 7 has shipped the Ditto function."
        ),
    )

    # ---------------- Mew ----------------
    mew_max_attempts: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Retry budget for Mew status updates.",
    )

    # ---------------- Health ----------------
    health_port: int = Field(default=8081, ge=1, le=65535)
    metrics_port: int = Field(default=9464, ge=1, le=65535)

    # ---------------- Test/dev knobs ----------------
    run_once: bool = Field(
        default=False,
        description=(
            "If True, process one message then exit. Smoke-test and operator-debug use only."
        ),
    )


class SnorlaxSettings(KantoBaseSettings):
    """Top-level Snorlax settings — KantoBaseSettings + service knobs."""

    service_name: str = Field(default="snorlax")
    snorlax: SnorlaxServiceSettings

    @classmethod
    def load(cls) -> Self:
        return cls(
            mew=load_section(MewSettings, "mew"),
            object_storage=load_section(ObjectStorageSettings, "object_storage"),
            streaming=load_section(StreamingSettings, "streaming"),
            tracing=load_section(TracingSettings, "tracing"),
            snorlax=load_section(SnorlaxServiceSettings, "snorlax"),
        )


__all__ = ["SnorlaxServiceSettings", "SnorlaxSettings"]
