"""Typed row models returned by Mew repositories.

Repositories return these instead of raw tuples or dicts so the
caller's mypy passes catch column-name typos. Field names match the
column names in ``docs/design.md`` §7.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class IsolateStatus(StrEnum):
    """Per-design-doc isolate processing states."""

    DISCOVERED = "DISCOVERED"
    PROTEINS_READY = "PROTEINS_READY"
    EMBEDDED = "EMBEDDED"
    SCORED = "SCORED"
    ALERTED = "ALERTED"
    QC_FAILED = "QC_FAILED"


class AlertStatus(StrEnum):
    """Workflow state of an analyst alert."""

    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    CLOSED = "CLOSED"
    DISMISSED = "DISMISSED"


# We allow arbitrary types for these models because asyncpg / psycopg
# returns Decimal for ``NUMERIC`` columns we don't have, and JSONB lands
# as ``dict[str, Any]``.
_BASE_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    arbitrary_types_allowed=True,
)


class IsolateRow(BaseModel):
    model_config = _BASE_CONFIG

    accession: str
    version: int
    organism: str
    source: str
    collection_date: date | None = None
    location: str | None = None
    source_type: str | None = None
    status: IsolateStatus
    qc_failure_reason: str | None = None
    modal_call_id: str | None = None
    novelty_score: float | None = None
    nn_distance: float | None = None
    coverage: float | None = None
    mahalanobis: float | None = None
    above_threshold: bool | None = None
    discovered_at: datetime | None = None
    scored_at: datetime | None = None
    raw_metadata: dict[str, Any] | None = None


class EmbeddingRow(BaseModel):
    model_config = _BASE_CONFIG

    accession: str
    version: int
    model: str
    model_version: str
    embedding: list[float]


class NeighborRow(BaseModel):
    """One row returned from a k-NN query."""

    model_config = _BASE_CONFIG

    accession: str
    distance: float = Field(..., ge=0.0)


class AlertRow(BaseModel):
    model_config = _BASE_CONFIG

    id: int
    accession: str
    version: int
    score: float
    triggered_at: datetime
    status: AlertStatus
    notes: str | None = None
