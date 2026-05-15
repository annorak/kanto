"""Growlithe configuration.

Extends :class:`kanto_commons.config.KantoBaseSettings` with the
service-specific knobs documented in ``services/growlithe/README.md``.

All configuration is environment-driven; no config files are
committed. The Helm chart populates the env vars from the chart
values or from Azure Key Vault via the Secrets Store CSI Driver
(``KANTO_MEW_PASSWORD`` in particular).
"""

from __future__ import annotations

from typing import Annotated, Self

from kanto_commons.config import (
    KantoBaseSettings,
    MewSettings,
    ObjectStorageSettings,
    StreamingSettings,
    TracingSettings,
)
from pydantic import (
    AliasChoices,
    Field,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from growlithe.organisms import DEFAULT_ORGANISMS


class GrowlitheServiceSettings(BaseSettings):
    """Service-specific config.

    Sibling settings class (rather than fields on
    :class:`GrowlitheSettings`) so the env vars all live under one
    flat ``KANTO_GROWLITHE_*`` namespace without the
    pydantic-settings nested-delimiter weirdness.
    """

    model_config = SettingsConfigDict(
        env_prefix="KANTO_GROWLITHE_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    organisms: Annotated[tuple[str, ...], NoDecode] = Field(
        default=DEFAULT_ORGANISMS,
        validation_alias=AliasChoices("KANTO_GROWLITHE_ORGANISMS"),
        description=(
            "Comma-separated NCBI Pathogen Detection organism path "
            "segments (matched literally against /pathogen/Results/<X>/). "
            "See growlithe.organisms.DEFAULT_ORGANISMS for the v1 default."
        ),
    )
    poll_interval_seconds: int = Field(
        default=6 * 60 * 60,  # 6h
        ge=60,
        le=24 * 60 * 60,
        description=(
            "Seconds between poll cycles. NCBI updates roughly daily; 6h " "is conservative."
        ),
    )
    poll_jitter_seconds: int = Field(
        default=60,
        ge=0,
        le=600,
        description=(
            "Random jitter added to the poll interval so multiple replicas "
            "(if ever scaled out) don't hit NCBI in lockstep."
        ),
    )
    ncbi_base_url: str = Field(
        default="https://ftp.ncbi.nlm.nih.gov/pathogen/Results/",
        description=(
            "Base URL for NCBI Pathogen Detection. Override to point at "
            "fixtures or a mirror in tests / disaster recovery."
        ),
    )
    ncbi_request_timeout_seconds: float = Field(
        default=300.0,
        gt=0,
        description=(
            "Per-request HTTP timeout. NCBI can be slow during its nightly "
            "update window; a generous timeout avoids spurious failures."
        ),
    )
    ncbi_max_attempts: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Retry budget for transient HTTP/network errors per request.",
    )
    ncbi_max_concurrent: int = Field(
        default=2,
        ge=1,
        le=8,
        description=(
            "How many organisms can poll NCBI concurrently. Low by default "
            "so we stay a polite citizen of public infrastructure."
        ),
    )
    health_port: int = Field(
        default=8081,
        ge=1,
        le=65535,
        description="Port for /healthz and /readyz.",
    )
    metrics_port: int = Field(
        default=9464,
        ge=1,
        le=65535,
        description="Port for Prometheus /metrics.",
    )
    run_once: bool = Field(
        default=False,
        description=(
            "If True, run a single poll cycle and exit. Used by the smoke "
            "test and by the operator for ad-hoc invocations."
        ),
    )
    # Source identifier emitted on every IsolateDiscovered event for this
    # adapter. Kept on Settings rather than hard-coded so a second NCBI
    # mirror or a forked source can be distinguished downstream.
    source_id: str = Field(
        default="ncbi-pd",
        description=("DataSource identifier set on emitted IsolateDiscovered events."),
    )

    @field_validator("organisms", mode="before")
    @classmethod
    def _parse_comma_list(cls, value: object) -> object:
        """Accept ``A,B,C`` strings from env vars; pass tuples/lists through."""
        if isinstance(value, str):
            return tuple(s.strip() for s in value.split(",") if s.strip())
        return value

    @model_validator(mode="after")
    def _check_organisms_non_empty(self) -> Self:
        if not self.organisms:
            raise ValueError("KANTO_GROWLITHE_ORGANISMS must list at least one organism")
        return self


class GrowlitheSettings(KantoBaseSettings):
    """Top-level Growlithe settings — KantoBaseSettings + service knobs."""

    service_name: str = Field(default="growlithe")
    growlithe: GrowlitheServiceSettings

    @classmethod
    def load(cls) -> Self:
        return cls(
            mew=MewSettings(),  # type: ignore[call-arg]
            object_storage=ObjectStorageSettings(),  # type: ignore[call-arg]
            streaming=StreamingSettings(),  # type: ignore[call-arg]
            tracing=TracingSettings(),
            growlithe=GrowlitheServiceSettings(),
        )


__all__ = ["GrowlitheServiceSettings", "GrowlitheSettings"]
