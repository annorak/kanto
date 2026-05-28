"""Configuration loaded from a TOML file + environment variables.

The module exposes :class:`KantoBaseSettings`, the parent class every
service derives from to add its own service-specific config. The base
class itself contains the universal config that every service needs:
Mew connection details, Azure Blob container names, Azure Event Hubs
(Kafka API) endpoints, and observability endpoints.

Configuration precedence
------------------------
1. Pydantic field defaults (lowest).
2. ``KANTO_CONFIG_FILE`` TOML file values, when the variable points at
   a readable file. Sections of the file map to per-section settings
   classes (``[mew]`` → :class:`MewSettings`, etc.).
3. Environment variables (highest). Secrets — DB password, SAS tokens,
   Modal tokens, OTLP auth headers — must stay env- / Key-Vault-only
   and are never committed to the file.

The file is read once at :meth:`KantoBaseSettings.load`. A restart is
required to pick up changes; there is no hot reload.

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

import os
import tomllib
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Self, TypeVar

from pydantic import (
    AliasChoices,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


# Env var pointing at the TOML config file; absent or unset means
# file-config is disabled and only env vars are consulted.
CONFIG_FILE_ENV_VAR = "KANTO_CONFIG_FILE"


@lru_cache(maxsize=1)
def _read_config_file() -> dict[str, Any]:
    """Return parsed TOML contents, or ``{}`` if no file is configured.

    Cached for the process lifetime so each service start parses the
    file at most once. A unit test that needs to swap files between
    cases must call ``_read_config_file.cache_clear()``.
    """
    path = os.environ.get(CONFIG_FILE_ENV_VAR)
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    with p.open("rb") as fp:
        return tomllib.load(fp)


_T = TypeVar("_T", bound=BaseSettings)


def load_section(cls: type[_T], section: str | None = None) -> _T:
    """Construct ``cls`` with TOML overrides applied for unset env aliases.

    ``section`` names the TOML table to draw from (e.g. ``"mew"``);
    when None, the top-level table is used (for :class:`KantoBaseSettings`'s
    own fields). For each field on ``cls``, the file value is passed as
    an init kwarg **only when none of the field's env aliases are set
    in the environment**, which keeps ``env > file > defaults`` order.
    """
    file_data = _read_config_file()
    table = file_data if section is None else file_data.get(section, {})
    if not isinstance(table, dict):
        # Misconfigured file (e.g. ``mew = "foo"`` instead of ``[mew]``).
        # Fall back to env/defaults rather than silently drop.
        return cls()

    env_prefix = cls.model_config.get("env_prefix", "") or ""
    init_kwargs: dict[str, Any] = {}
    for field_name, field in cls.model_fields.items():
        if field_name not in table:
            continue
        if _any_env_name_set(field_name, field, env_prefix):
            continue
        init_kwargs[field_name] = table[field_name]
    return cls(**init_kwargs)


def _any_env_name_set(field_name: str, field: Any, env_prefix: str) -> bool:
    """True if any env var that would populate ``field`` is currently set.

    Checks both pydantic ``validation_alias`` choices and the
    pydantic-settings convention of ``<env_prefix><FIELD_NAME>``.
    """
    names: list[str] = []
    alias = field.validation_alias
    if alias is not None:
        if isinstance(alias, str):
            names.append(alias)
        else:
            # AliasChoices exposes its choices on .choices in pydantic v2.
            for choice in getattr(alias, "choices", []) or []:
                if isinstance(choice, str):
                    names.append(choice)
    # pydantic-settings env-prefix convention: case-insensitive match
    # against ``<env_prefix><field_name>``. Use the uppercased form
    # because that's what env vars look like in practice.
    names.append(f"{env_prefix}{field_name}".upper())
    # Case-insensitive scan because settings_config has
    # ``case_sensitive=False`` across all our settings classes.
    env_upper = {k.upper() for k in os.environ}
    return any(n.upper() in env_upper for n in names)


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
    """Azure Blob Storage account and container settings.

    The ``account_url`` is the storage account's blob endpoint (e.g.
    ``https://kantodevdata1234.blob.core.windows.net``). Authentication
    in the cluster is via :class:`azure.identity.DefaultAzureCredential`
    — AKS Workload Identity surfaces the AKS-workloads UAMI, which
    Terraform grants ``Storage Blob Data Contributor`` on the proteins
    and metadata containers and ``Storage Blob Data Reader`` on the
    embeddings container.
    """

    model_config = SettingsConfigDict(
        env_prefix="KANTO_OS_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    account_url: str = Field(
        ...,
        validation_alias=AliasChoices(
            "KANTO_OS_ACCOUNT_URL",
            "AZURE_STORAGE_BLOB_ENDPOINT",
        ),
        description=(
            "Blob endpoint of the storage account, e.g. "
            "https://kantodevdata1234.blob.core.windows.net"
        ),
    )
    proteins_container: str = Field(
        default="kanto-proteins",
        validation_alias=AliasChoices("KANTO_OS_PROTEINS_CONTAINER"),
    )
    embeddings_container: str = Field(
        default="kanto-embeddings",
        validation_alias=AliasChoices("KANTO_OS_EMBEDDINGS_CONTAINER"),
    )
    metadata_container: str = Field(
        default="kanto-metadata",
        validation_alias=AliasChoices(
            "KANTO_OS_METADATA_CONTAINER",
            # Old name kept as a fallback so dev .envrc files don't break
            # mid-migration. Drop once everyone has updated.
            "KANTO_OS_CACHE_BUCKET",
        ),
    )
    # Tunables forwarded to the BlobServiceClient. Defaults match the SDK.
    max_single_get_size: int = Field(
        default=32 * 1024 * 1024,
        ge=1,
        description="Max bytes pulled in a single GET before chunking.",
    )
    max_single_put_size: int = Field(
        default=64 * 1024 * 1024,
        ge=1,
        description="Max bytes uploaded in a single PUT before chunked upload.",
    )


class StreamingSettings(BaseSettings):
    """Azure Event Hubs (Kafka API) settings.

    Event Hubs exposes a Kafka-protocol endpoint per namespace. The
    aiokafka client in :mod:`kanto_commons.streaming` connects to it
    with SASL/SSL. For dev, ``sasl_username`` is ``$ConnectionString``
    and ``sasl_password`` is the full SAS connection string sourced
    from Azure Key Vault; for prod we will switch to OAUTHBEARER with
    federated workload identity (separate follow-up).
    """

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
        """Eagerly construct from file + environment.

        Reads ``KANTO_CONFIG_FILE`` (TOML) once, then builds each
        section's settings, with env vars overriding file values and
        file values overriding pydantic defaults. Raises
        :class:`pydantic.ValidationError` listing every missing
        required variable in one message.
        """
        return cls(  # type: ignore[call-arg]
            mew=load_section(MewSettings, "mew"),
            object_storage=load_section(ObjectStorageSettings, "object_storage"),
            streaming=load_section(StreamingSettings, "streaming"),
            tracing=load_section(TracingSettings, "tracing"),
        )
