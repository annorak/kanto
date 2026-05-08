"""Configuration loaded from environment variables.

The module exposes :class:`KantoBaseSettings`, the parent class every
service derives from to add its own service-specific config. The base
class itself contains the universal config that every service needs:
Mew connection details, OS bucket names, OCI Streaming endpoints, and
observability endpoints.

Configuration is **only** loaded from environment variables. Files
committed to the repo (``.envrc``, ``.envrc.example``) supply env vars
in dev; OCI Vault → Helm → pod env supplies them in production. We
never read a TOML/YAML/JSON config file.

Naming convention
-----------------
Every Kanto runtime variable is prefixed ``KANTO_``. Sections within
the unified ``KantoBaseSettings`` use a double underscore as
separator (``KANTO_MEW__HOST``); the per-section settings classes
(:class:`MewSettings` etc.) accept their own native prefix
(``KANTO_MEW_HOST``) for ergonomic shell use, which is what
``.envrc.example`` and Helm chart values rely on. The single exception
is ``KANTO_ENV``, which is the dev-friendly short name for
``KANTO_ENVIRONMENT``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    AliasChoices,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    """The deployment tier this service is running in.

    ``LOCAL`` distinguishes a developer laptop (with seeded test data
    and relaxed safety guards) from a real cloud environment. The
    seed-mew script refuses to run unless ``KANTO_ENV=local``.
    """

    LOCAL = "local"
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


class LogFormat(StrEnum):
    """How structured logs are rendered."""

    JSON = "json"
    CONSOLE = "console"


class MewSettings(BaseSettings):
    """Postgres + pgvector (Mew) connection settings.

    Each required field is given an explicit single-element
    ``validation_alias`` so that pydantic's "missing required field"
    errors name the actual ``KANTO_MEW_*`` env var the operator
    needs to set, not the bare field name.
    """

    model_config = SettingsConfigDict(
        env_prefix="KANTO_MEW_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    host: str = Field(
        ...,
        validation_alias=AliasChoices("KANTO_MEW_HOST"),
    )
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = Field(
        ...,
        validation_alias=AliasChoices("KANTO_MEW_DATABASE"),
    )
    user: str = Field(
        ...,
        validation_alias=AliasChoices("KANTO_MEW_USER"),
    )
    # Stored as SecretStr so it never appears in repr/str/log output.
    password: SecretStr = Field(
        ...,
        validation_alias=AliasChoices("KANTO_MEW_PASSWORD"),
    )
    sslmode: str = Field(default="verify-full")
    pool_min_size: int = Field(default=2, ge=0, le=100)
    pool_max_size: int = Field(default=10, ge=1, le=100)

    def dsn(self) -> str:
        """libpq-format DSN string with the password substituted in.

        Use this for psycopg/aiokafka connection strings. Logging this
        return value would reveal the password — don't.
        """
        return (
            f"postgresql://{self.user}:{self.password.get_secret_value()}"
            f"@{self.host}:{self.port}/{self.database}?sslmode={self.sslmode}"
        )

    def safe_dsn(self) -> str:
        """DSN with the password redacted, safe to log or include in errors.

        The ``***`` between ``:`` and ``@`` below is gitleaks-allowed inline
        because it is the literal redaction marker, not a real password.
        """
        return (
            f"postgresql://{self.user}:***@{self.host}:{self.port}"  # gitleaks:allow
            f"/{self.database}?sslmode={self.sslmode}"
        )

    @model_validator(mode="after")
    def _check_pool_sizes(self) -> Self:
        if self.pool_max_size < self.pool_min_size:
            raise ValueError(
                f"pool_max_size ({self.pool_max_size}) must be >= "
                f"pool_min_size ({self.pool_min_size})"
            )
        return self


class ObjectStorageSettings(BaseSettings):
    """OCI Object Storage bucket and namespace settings."""

    model_config = SettingsConfigDict(
        env_prefix="KANTO_OS_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    namespace: str = Field(
        ...,
        validation_alias=AliasChoices("KANTO_OS_NAMESPACE"),
        description="OCI Object Storage namespace (per-tenancy unique).",
    )
    region: str = Field(
        ...,
        validation_alias=AliasChoices("KANTO_OS_REGION"),
    )
    proteins_bucket: str = Field(default="kanto-proteins")
    embeddings_bucket: str = Field(default="kanto-embeddings")
    cache_bucket: str = Field(default="kanto-cache")
    # Connection pool size for OCI SDK requests session.
    max_connections: int = Field(default=20, ge=1, le=200)


class StreamingSettings(BaseSettings):
    """OCI Streaming (Kafka API) settings."""

    model_config = SettingsConfigDict(
        env_prefix="KANTO_STREAMING_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    bootstrap_servers: str = Field(
        ...,
        validation_alias=AliasChoices(
            "KANTO_STREAMING_BOOTSTRAP_SERVERS",
            "KANTO_STREAMING_BOOTSTRAP",
        ),
        description="Comma-separated list of Kafka bootstrap host:port pairs.",
    )
    discovered_topic: str = Field(default="kanto.discovered")
    embedded_topic: str = Field(default="kanto.embedded")
    scored_topic: str = Field(default="kanto.scored")
    # SASL credentials for Kafka API. None == use IAM auth (instance principal).
    sasl_username: str | None = None
    sasl_password: SecretStr | None = None
    # Default per-message retry budget before DLQ.
    max_processing_attempts: int = Field(default=3, ge=1, le=20)


class TracingSettings(BaseSettings):
    """OpenTelemetry tracing settings."""

    model_config = SettingsConfigDict(
        env_prefix="KANTO_OTEL_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    endpoint: str | None = Field(
        default=None,
        description="OTLP HTTP endpoint. If unset, tracing falls back to the "
        "console exporter (suitable for dev).",
    )
    sample_rate: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Parent-based sampling rate. 1.0 = sample everything.",
    )
    headers: str | None = Field(
        default=None,
        description="Comma-separated 'key=value' pairs sent on every OTLP "
        "export request, e.g. for backend authentication.",
    )


class KantoBaseSettings(BaseSettings):
    """Universal config shared by every Kanto service.

    Service-specific settings live in each service's own subclass. The
    inherited fields here cover Mew, Object Storage, Streaming, logging,
    and tracing.

    Loading
    -------
    The recommended pattern is one ``settings = ServiceSettings.load()``
    construction at service startup, passed (or imported) by every
    module that needs config. Don't construct settings ad hoc inside
    request handlers; that re-reads env vars on every call.
    """

    model_config = SettingsConfigDict(
        env_prefix="KANTO_",
        env_nested_delimiter="__",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    service_name: str = Field(
        ...,
        description="Logical service name, used in logs and trace resource "
        "attributes. Each service overrides the default in its own subclass.",
    )
    environment: Environment = Field(
        default=Environment.DEV,
        # KANTO_ENV is the short form everyone types; KANTO_ENVIRONMENT
        # is what the field name would expand to under the standard
        # KANTO_ prefix. Both are KANTO_-prefixed, so this preserves
        # the single-convention rule.
        validation_alias=AliasChoices("KANTO_ENV", "KANTO_ENVIRONMENT"),
    )
    log_level: Annotated[
        str, Field(pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    ] = Field(default="INFO")
    log_format: LogFormat = Field(default=LogFormat.JSON)

    mew: MewSettings
    object_storage: ObjectStorageSettings
    streaming: StreamingSettings
    tracing: TracingSettings

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        if isinstance(value, str):
            return value.upper()
        return value

    @classmethod
    def load(cls) -> Self:
        """Eagerly construct from environment.

        Reads ``KANTO_*`` variables, builds each section's settings
        from its own native prefix, and assembles them into the
        composite. Raises :class:`pydantic.ValidationError` listing
        every missing required variable in one message.
        """
        return cls(
            mew=MewSettings(),  # type: ignore[call-arg]
            object_storage=ObjectStorageSettings(),  # type: ignore[call-arg]
            streaming=StreamingSettings(),  # type: ignore[call-arg]
            tracing=TracingSettings(),
        )
