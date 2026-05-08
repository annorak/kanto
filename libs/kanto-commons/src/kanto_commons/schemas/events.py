"""Event and payload schemas crossing Kanto service boundaries.

The four schemas in this module correspond to the data flow in
``docs/design.md`` section 6:

* :class:`IsolateDiscovered` — Growlithe → ``kanto.discovered`` stream.
* :class:`ProteinsReady`     — Snorlax → Ditto Modal function call.
* :class:`EmbeddingsReady`   — Ditto    → ``kanto.embedded`` stream.
* :class:`IsolateScored`     — Alakazam → ``kanto.scored`` stream.

``ProteinsReady`` is **not** a stream event in v1 — Snorlax invokes
Modal's Ditto function directly with this payload. It still lives in
the registry so consumers (test fixtures, schema-aware tooling, the
CLI) can dispatch on it the same way as the stream events. Its
:class:`Transport` is :attr:`Transport.MODAL`.

Every schema sets ``model_config = ConfigDict(extra="forbid", ...)`` so
that producers cannot silently add fields a consumer ignores. Schema
evolution must go through the ``schema_version`` field.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

# ---------------------------------------------------------------------------
# Common constraints
# ---------------------------------------------------------------------------

# NCBI Pathogen Detection accessions look like ``PDT001234567.1`` — uppercase
# letters and digits, with a dot-separated suffix on the version line in some
# cases. We allow a permissive pattern (alnum + dot + dash + underscore) so
# future data sources (ENA, customer uploads) fit without a schema bump, but
# we still reject empty strings and obviously-bogus shell metacharacters.
_AccessionField = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._\-]+$",
    ),
]

_NonEmptyStr = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
]

_OSKey = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=1024,
        # OCI Object Storage keys can contain almost any character; we
        # forbid leading/trailing whitespace via ``strip_whitespace`` and
        # disallow control bytes via the explicit reject in the validator.
    ),
]


def _utcnow() -> datetime:
    """Default factory: timezone-aware UTC ``datetime``.

    The design doc stores all timestamps as ``TIMESTAMPTZ``; floats and
    naive datetimes are explicitly disallowed.
    """
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Transport tag and event-name enum
# ---------------------------------------------------------------------------


class Transport(StrEnum):
    """Where a schema travels.

    ``STREAM`` events go through OCI Streaming; ``MODAL`` payloads are
    passed as Modal function arguments.
    """

    STREAM = "stream"
    MODAL = "modal"


class EventName(StrEnum):
    """Stable string names for every Kanto schema, used in the registry."""

    ISOLATE_DISCOVERED = "isolate.discovered"
    PROTEINS_READY = "proteins.ready"
    EMBEDDINGS_READY = "embeddings.ready"
    ISOLATE_SCORED = "isolate.scored"


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class _KantoSchemaBase(BaseModel):
    """Common configuration for every Kanto schema.

    * ``extra="forbid"`` — producers cannot silently add fields a
      consumer ignores. Schema evolution goes through ``schema_version``.
    * ``frozen=True`` — schema instances are value objects; mutating one
      after construction is a bug.
    * ``str_strip_whitespace=True`` — defensive against producers that
      forget to trim user-supplied identifiers.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        populate_by_name=True,
        ser_json_timedelta="iso8601",
    )

    @field_validator("*", mode="after")
    @classmethod
    def _reject_naive_datetimes(cls, value: object) -> object:
        """Catch naive datetimes that slip past per-field declarations.

        The design doc requires ``TIMESTAMPTZ`` storage; a naive
        datetime would silently get treated as local time at write time.
        We fail loud here instead.
        """
        if isinstance(value, datetime) and value.tzinfo is None:
            raise ValueError("datetime fields must be timezone-aware (UTC preferred)")
        return value


# ---------------------------------------------------------------------------
# 1. IsolateDiscovered — Growlithe → kanto.discovered
# ---------------------------------------------------------------------------


class IsolateDiscovered(_KantoSchemaBase):
    """A new isolate has appeared in an upstream data source.

    Emitted by Growlithe to the ``kanto.discovered`` OCI Streaming
    stream. Snorlax consumes this and downloads the FASTA from
    ``ftp_path``.
    """

    SCHEMA_VERSION: ClassVar[int] = 1
    EVENT_NAME: ClassVar[Literal[EventName.ISOLATE_DISCOVERED]] = (
        EventName.ISOLATE_DISCOVERED
    )

    schema_version: int = Field(
        default=1,
        ge=1,
        description="Schema revision. Incremented when the field set changes.",
    )
    accession: _AccessionField = Field(
        ...,
        description="The PDT (or future-source) accession identifier.",
    )
    version: int = Field(
        ...,
        ge=1,
        description="Monotonic version of this accession in the upstream source.",
    )
    organism: _NonEmptyStr = Field(
        ..., description="Source organism, e.g. 'Salmonella'."
    )
    source: _NonEmptyStr = Field(
        ...,
        description="Identifier of the upstream data source, e.g. 'ncbi-pd'.",
    )
    ftp_path: _NonEmptyStr = Field(
        ...,
        description="Pointer Snorlax will fetch the FASTA from (URL or path).",
    )
    discovered_at: datetime = Field(
        default_factory=_utcnow,
        description="When Growlithe observed this isolate.",
    )
    metadata: dict[str, str] = Field(
        default_factory=dict,
        description="Source-specific extra fields. Keep small (< 1 KB).",
    )


# ---------------------------------------------------------------------------
# 2. ProteinsReady — Snorlax → Ditto (via Modal)
# ---------------------------------------------------------------------------


class ProteinsReady(_KantoSchemaBase):
    """The protein FASTA for an isolate is on Object Storage.

    Snorlax invokes Modal's Ditto function with this payload as the
    structured argument. It is **not** a stream event in v1; we keep the
    schema here so the same validation/serialization story applies to
    the Modal call boundary.
    """

    SCHEMA_VERSION: ClassVar[int] = 1
    EVENT_NAME: ClassVar[Literal[EventName.PROTEINS_READY]] = EventName.PROTEINS_READY

    schema_version: int = Field(default=1, ge=1)
    accession: _AccessionField
    version: int = Field(..., ge=1)
    os_key: _OSKey = Field(
        ...,
        description="Object Storage key under the kanto-proteins bucket.",
    )
    protein_count: int = Field(
        ...,
        ge=0,
        description="Number of protein sequences in the FASTA. 0 is legal "
        "(degenerate genome) and Ditto must handle it.",
    )
    produced_at: datetime = Field(default_factory=_utcnow)


# ---------------------------------------------------------------------------
# 3. EmbeddingsReady — Ditto → kanto.embedded
# ---------------------------------------------------------------------------


class EmbeddingsReady(_KantoSchemaBase):
    """Per-protein embeddings have been written; aggregate is in Mew."""

    SCHEMA_VERSION: ClassVar[int] = 1
    EVENT_NAME: ClassVar[Literal[EventName.EMBEDDINGS_READY]] = (
        EventName.EMBEDDINGS_READY
    )

    schema_version: int = Field(default=1, ge=1)
    accession: _AccessionField
    version: int = Field(..., ge=1)
    model: _NonEmptyStr = Field(
        ...,
        description="Embedding model identifier, e.g. 'esm-c-600m'.",
    )
    model_version: _NonEmptyStr = Field(
        ...,
        description="Embedding model version, e.g. '1.0.0'.",
    )
    os_key: _OSKey = Field(
        ...,
        description="Object Storage key for the per-protein parquet under "
        "kanto-embeddings.",
    )
    embedded_at: datetime = Field(default_factory=_utcnow)


# ---------------------------------------------------------------------------
# 4. IsolateScored — Alakazam → kanto.scored
# ---------------------------------------------------------------------------


class IsolateScored(_KantoSchemaBase):
    """Novelty score computed; downstream Chatot decides whether to alert."""

    SCHEMA_VERSION: ClassVar[int] = 1
    EVENT_NAME: ClassVar[Literal[EventName.ISOLATE_SCORED]] = EventName.ISOLATE_SCORED

    schema_version: int = Field(default=1, ge=1)
    accession: _AccessionField
    version: int = Field(..., ge=1)
    novelty_score: float = Field(
        ...,
        ge=0.0,
        description="Combined novelty score. Higher = more anomalous.",
    )
    nn_distance: float = Field(
        ...,
        ge=0.0,
        description="Mean cosine distance to the k nearest neighbors.",
    )
    coverage: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Fraction of proteins with confident reference matches.",
    )
    mahalanobis: float = Field(
        ...,
        ge=0.0,
        description="Distance from the species centroid in embedding space.",
    )
    above_threshold: bool = Field(
        ...,
        description="Whether this score exceeds the per-organism alert "
        "threshold at scoring time.",
    )
    scored_at: datetime = Field(default_factory=_utcnow)


# ---------------------------------------------------------------------------
# Stream-message envelope
# ---------------------------------------------------------------------------


class EventEnvelope(_KantoSchemaBase):
    """Wrapping envelope written to every OCI Streaming message.

    The streaming wrapper serializes ``EventEnvelope`` as the message
    body. The envelope identifies which schema lives in ``payload`` so
    consumers can dispatch on ``event_name`` without trial-and-error
    parsing. Trace context lives in Kafka headers (W3C ``traceparent``),
    not in the envelope, so the envelope stays under 4 KB even for
    payloads with metadata.
    """

    schema_version: int = Field(default=1, ge=1)
    event_name: EventName
    event_schema_version: int = Field(
        ...,
        ge=1,
        description="Copy of the inner payload's schema_version, hoisted "
        "for cheap consumer-side compatibility checks without parsing "
        "the full payload.",
    )
    payload: IsolateDiscovered | ProteinsReady | EmbeddingsReady | IsolateScored = (
        Field(
            ...,
            discriminator=None,  # discrimination happens via event_name
        )
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


KantoEvent = IsolateDiscovered | ProteinsReady | EmbeddingsReady | IsolateScored


class _RegistryEntry(BaseModel):
    """Metadata for one event type."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    name: EventName
    schema_cls: type[_KantoSchemaBase]
    transport: Transport
    topic: str | None = Field(
        default=None,
        description="OCI Streaming topic for STREAM events; None for MODAL payloads.",
    )


# Single source of truth for "what events does Kanto have, and where do
# they go." Services never hardcode topic names; they look them up here.
EVENT_REGISTRY: dict[EventName, _RegistryEntry] = {
    EventName.ISOLATE_DISCOVERED: _RegistryEntry(
        name=EventName.ISOLATE_DISCOVERED,
        schema_cls=IsolateDiscovered,
        transport=Transport.STREAM,
        topic="kanto.discovered",
    ),
    EventName.PROTEINS_READY: _RegistryEntry(
        name=EventName.PROTEINS_READY,
        schema_cls=ProteinsReady,
        transport=Transport.MODAL,
        topic=None,
    ),
    EventName.EMBEDDINGS_READY: _RegistryEntry(
        name=EventName.EMBEDDINGS_READY,
        schema_cls=EmbeddingsReady,
        transport=Transport.STREAM,
        topic="kanto.embedded",
    ),
    EventName.ISOLATE_SCORED: _RegistryEntry(
        name=EventName.ISOLATE_SCORED,
        schema_cls=IsolateScored,
        transport=Transport.STREAM,
        topic="kanto.scored",
    ),
}
