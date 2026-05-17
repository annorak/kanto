"""Alakazam configuration.

Service-specific knobs documented in ``services/alakazam/README.md``.
Mirrors the layout of :mod:`growlithe.config` / :mod:`snorlax.config`:
a sibling ``AlakazamServiceSettings`` keeps the env vars under a flat
``KANTO_ALAKAZAM_*`` namespace.

The scoring weights and thresholds defined here are *starting values*.
Task 12 calibrates them against held-out historical isolates; values
should be expected to shift between v1.0 and the first production
release. Document any operator-side override in the runbook section
of the README so future-you can reproduce the choice.
"""

from __future__ import annotations

from typing import Self

from kanto_commons.config import (
    KantoBaseSettings,
    MewSettings,
    ObjectStorageSettings,
    StreamingSettings,
    TracingSettings,
)
from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AlakazamServiceSettings(BaseSettings):
    """Service-specific config for Alakazam."""

    model_config = SettingsConfigDict(
        env_prefix="KANTO_ALAKAZAM_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # ---------------- Consumer wiring ----------------
    consumer_group: str = Field(
        default="alakazam",
        description="Kafka consumer group ID. One group per logical consumer.",
    )
    consumer_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "KANTO_ALAKAZAM_CONSUMER_ID",
            "POD_NAME",
        ),
        description=(
            "Stable id for this consumer instance. Defaults to POD_NAME "
            "when running under Kubernetes."
        ),
    )

    # ---------------- Scoring weights ----------------
    # These are the linear weights of the three strategies. The combiner
    # takes their weighted sum, normalized by the sum of the weights that
    # actually contributed (so a tier-1-only result remains directly
    # comparable to a tier-2 result). v1 defaults err toward the
    # coverage component because it's the most semantically informative
    # signal for the "known species, weird proteins" case.
    weight_nn: float = Field(
        default=1.0,
        ge=0.0,
        description="Linear weight on the NN-distance component.",
    )
    weight_coverage: float = Field(
        default=2.0,
        ge=0.0,
        description="Linear weight on the coverage component.",
    )
    weight_mahalanobis: float = Field(
        default=0.5,
        ge=0.0,
        description="Linear weight on the Mahalanobis component.",
    )

    # ---------------- Tiered scoring ----------------
    candidate_threshold: float = Field(
        default=0.30,
        ge=0.0,
        description=(
            "Mean cosine distance to k nearest neighbors above which "
            "the expensive coverage + Mahalanobis components fire. "
            "Default chosen so that ~5%% of isolates cross it; tune in "
            "Task 12 against historical data."
        ),
    )
    nn_k: int = Field(
        default=10,
        ge=1,
        le=200,
        description="k for the NN-distance component.",
    )

    # ---------------- Coverage component ----------------
    reference_set_container: str = Field(
        default="kanto-metadata",
        description=(
            "OS container holding the reference-protein parquet. We "
            "use the metadata container by convention; the parquet is "
            "small and metadata-shaped, not embedding-shaped."
        ),
    )
    reference_set_key: str = Field(
        default="references/common_proteins_v1.parquet",
        description="OS blob key for the reference-protein parquet.",
    )
    coverage_match_threshold: float = Field(
        default=0.70,
        ge=-1.0,
        le=1.0,
        description=(
            "Cosine-similarity threshold above which a genome protein "
            "is considered to 'match' a reference protein. v1 default "
            "is conservative; Task 12 tunes against held-out data."
        ),
    )
    coverage_max_proteins: int = Field(
        default=20_000,
        ge=1,
        description=(
            "Hard cap on the number of genome proteins compared "
            "against the reference set. A normal bacterial genome has "
            "~4k proteins; this guards against an upstream bug or a "
            "huge metagenomic input that would blow the OS read budget."
        ),
    )

    # ---------------- Mahalanobis component ----------------
    mahalanobis_min_samples: int = Field(
        default=20,
        ge=1,
        description=(
            "Skip the Mahalanobis component when the species centroid "
            "was computed from fewer than this many isolates. The "
            "covariance estimate is too noisy below this floor."
        ),
    )
    mahalanobis_regularization: float = Field(
        default=1e-4,
        gt=0.0,
        description=(
            "Floor added to every diagonal variance term before the "
            "Mahalanobis denominator. Prevents zero-variance "
            "dimensions from blowing up the score. Matches the value "
            "the centroid job writes to species_centroids; document "
            "the relationship if you change one without the other."
        ),
    )

    # ---------------- Alert threshold ----------------
    # The "alert" flag on IsolateScored is a coarse, system-wide
    # threshold here; Chatot owns per-organism tuning in Task 12.
    alert_threshold: float = Field(
        default=0.60,
        ge=0.0,
        description=(
            "Combined novelty score above which ``above_threshold`` is "
            "true on the emitted IsolateScored event."
        ),
    )

    # ---------------- Centroid refresh ----------------
    centroid_refresh_seconds: int = Field(
        default=3600,
        ge=60,
        description=(
            "How often the running service refreshes its in-memory "
            "species-centroid cache from Mew. The centroid job writes "
            "daily; an hourly refresh keeps us close to the latest "
            "without hammering Mew."
        ),
    )

    # ---------------- Centroid CronJob (operator-facing) ----------------
    centroid_min_isolates: int = Field(
        default=10,
        ge=2,
        description=(
            "Skip organisms with fewer than this many embedded "
            "isolates in the centroid recomputation job. Falls back "
            "to the default-skip behavior at scoring time."
        ),
    )
    centroid_organism_filter: str | None = Field(
        default=None,
        description=(
            "Optional comma-separated organism allowlist for the "
            "centroid job. Unset (default) recomputes every species "
            "with enough samples."
        ),
    )

    # ---------------- Mew ----------------
    mew_max_attempts: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Retry budget for Mew writes.",
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

    @model_validator(mode="after")
    def _check_weights(self) -> Self:
        total = self.weight_nn + self.weight_coverage + self.weight_mahalanobis
        if total <= 0:
            raise ValueError(
                "At least one of weight_nn / weight_coverage / "
                "weight_mahalanobis must be > 0; otherwise the combiner "
                "produces a zero score for every isolate."
            )
        return self


class AlakazamSettings(KantoBaseSettings):
    """Top-level Alakazam settings — KantoBaseSettings + service knobs."""

    service_name: str = Field(default="alakazam")
    alakazam: AlakazamServiceSettings

    @classmethod
    def load(cls) -> Self:
        return cls(
            mew=MewSettings(),  # type: ignore[call-arg]
            object_storage=ObjectStorageSettings(),  # type: ignore[call-arg]
            streaming=StreamingSettings(),  # type: ignore[call-arg]
            tracing=TracingSettings(),
            alakazam=AlakazamServiceSettings(),
        )


__all__ = ["AlakazamServiceSettings", "AlakazamSettings"]
