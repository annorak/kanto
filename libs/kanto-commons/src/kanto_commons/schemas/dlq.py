"""Dead-letter envelope used by the OCI Streaming consumer wrapper.

When a consumer fails to process a message after the configured retry
budget is exhausted, the wrapper publishes a :class:`DLQEnvelope` to
the per-stream DLQ topic (named ``<original-stream>.dlq``). The
envelope preserves the raw original message so an operator can replay
it after fixing the underlying bug.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DLQEnvelope(BaseModel):
    """Wraps a poison message for the per-stream DLQ topic.

    ``original_payload`` is the raw bytes of the original message. We
    keep bytes (not the parsed schema) deliberately: if the failure
    reason was a schema validation error, the parsed form is what's
    broken — the raw bytes are the only thing we can replay.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    original_topic: str = Field(..., min_length=1)
    original_partition: int | None = Field(default=None, ge=0)
    original_offset: int | None = Field(default=None, ge=0)
    # Stored as a hex-encoded string in JSON so the envelope round-trips
    # cleanly through stream layers that don't preserve bytes.
    original_payload_hex: str = Field(
        ...,
        description="Hex-encoded original message bytes.",
    )
    failure_reason: str = Field(..., min_length=1)
    failure_traceback: str | None = None
    consumer_group: str = Field(..., min_length=1)
    consumer_id: str = Field(..., min_length=1)
    attempt_count: int = Field(..., ge=1)
    failed_at: datetime = Field(default_factory=_utcnow)

    @field_validator("failed_at")
    @classmethod
    def _require_tzaware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("failed_at must be timezone-aware")
        return value

    @classmethod
    def from_raw(
        cls,
        *,
        original_topic: str,
        original_payload: bytes,
        failure_reason: str,
        consumer_group: str,
        consumer_id: str,
        attempt_count: int,
        original_partition: int | None = None,
        original_offset: int | None = None,
        failure_traceback: str | None = None,
    ) -> DLQEnvelope:
        """Convenience constructor — hex-encodes the raw payload for you."""
        return cls(
            original_topic=original_topic,
            original_partition=original_partition,
            original_offset=original_offset,
            original_payload_hex=original_payload.hex(),
            failure_reason=failure_reason,
            failure_traceback=failure_traceback,
            consumer_group=consumer_group,
            consumer_id=consumer_id,
            attempt_count=attempt_count,
        )

    def original_payload_bytes(self) -> bytes:
        """Decode ``original_payload_hex`` back to bytes."""
        return bytes.fromhex(self.original_payload_hex)
