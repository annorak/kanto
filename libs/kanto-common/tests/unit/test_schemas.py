"""Unit tests for event schemas.

Focus: validation rejection, round-trip equality, registry coverage,
schema_version handling.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from kanto_common.schemas import (
    EVENT_REGISTRY,
    DLQEnvelope,
    EmbeddingsReady,
    EventEnvelope,
    EventName,
    IsolateDiscovered,
    IsolateScored,
    ProteinsReady,
    Transport,
)
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Factories — small helpers so tests stay readable.
# ---------------------------------------------------------------------------


def make_discovered(**overrides: object) -> IsolateDiscovered:
    defaults: dict[str, object] = dict(
        accession="PDT000123.1",
        version=1,
        organism="Salmonella",
        source="ncbi-pd",
        ftp_path="ftp://ftp.ncbi.nlm.nih.gov/path/foo.fna.gz",
    )
    defaults.update(overrides)
    return IsolateDiscovered(**defaults)  # type: ignore[arg-type]


def make_proteins_ready(**overrides: object) -> ProteinsReady:
    defaults: dict[str, object] = dict(
        accession="PDT000123.1",
        version=1,
        os_key="kanto-proteins/PDT000123.1/1.faa.gz",
        protein_count=4123,
    )
    defaults.update(overrides)
    return ProteinsReady(**defaults)  # type: ignore[arg-type]


def make_embeddings_ready(**overrides: object) -> EmbeddingsReady:
    defaults: dict[str, object] = dict(
        accession="PDT000123.1",
        version=1,
        model="esm-c-600m",
        model_version="1.0.0",
        os_key="kanto-embeddings/PDT000123.1/1.parquet",
    )
    defaults.update(overrides)
    return EmbeddingsReady(**defaults)  # type: ignore[arg-type]


def make_scored(**overrides: object) -> IsolateScored:
    defaults: dict[str, object] = dict(
        accession="PDT000123.1",
        version=1,
        novelty_score=4.2,
        nn_distance=0.31,
        coverage=0.87,
        mahalanobis=2.1,
        above_threshold=True,
    )
    defaults.update(overrides)
    return IsolateScored(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Round-trip serialization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "factory",
    [make_discovered, make_proteins_ready, make_embeddings_ready, make_scored],
    ids=["discovered", "proteins_ready", "embeddings_ready", "scored"],
)
def test_round_trip_through_json_is_identity(factory: object) -> None:
    instance = factory()  # type: ignore[operator]
    raw = instance.model_dump_json()
    restored = type(instance).model_validate_json(raw)
    assert restored == instance


def test_round_trip_via_dict_is_identity() -> None:
    instance = make_scored()
    restored = type(instance).model_validate(instance.model_dump())
    assert restored == instance


# ---------------------------------------------------------------------------
# Validation: reject bad inputs
# ---------------------------------------------------------------------------


def test_negative_score_rejected() -> None:
    with pytest.raises(ValidationError):
        make_scored(novelty_score=-0.1)


def test_coverage_above_one_rejected() -> None:
    with pytest.raises(ValidationError):
        make_scored(coverage=1.01)


def test_zero_version_rejected() -> None:
    with pytest.raises(ValidationError):
        make_discovered(version=0)


def test_empty_accession_rejected() -> None:
    with pytest.raises(ValidationError):
        make_discovered(accession="   ")


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError):
        IsolateDiscovered(  # type: ignore[call-arg]
            accession="PDT1",
            version=1,
            organism="Salmonella",
            source="ncbi-pd",
            ftp_path="ftp://x",
            future_field="hello",
        )


def test_naive_datetime_rejected() -> None:
    naive = datetime(2026, 1, 1)
    with pytest.raises(ValidationError):
        make_discovered(discovered_at=naive)


def test_aware_datetime_accepted() -> None:
    aware = datetime(2026, 1, 1, tzinfo=UTC)
    instance = make_discovered(discovered_at=aware)
    assert instance.discovered_at == aware


def test_negative_protein_count_rejected() -> None:
    with pytest.raises(ValidationError):
        make_proteins_ready(protein_count=-1)


def test_zero_protein_count_accepted() -> None:
    """Degenerate genomes can have zero proteins; Ditto must handle them."""
    assert make_proteins_ready(protein_count=0).protein_count == 0


# ---------------------------------------------------------------------------
# Frozen / value-object semantics
# ---------------------------------------------------------------------------


def test_schemas_are_frozen() -> None:
    instance = make_discovered()
    with pytest.raises(ValidationError):
        instance.accession = "PDT-OTHER"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# schema_version handling
# ---------------------------------------------------------------------------


def test_schema_version_default_is_one() -> None:
    assert make_discovered().schema_version == 1
    assert make_scored().schema_version == 1


def test_schema_version_bump_round_trips() -> None:
    """A future producer at schema_version=2 must round-trip past a v1 consumer
    in any way that doesn't add new required fields. This test asserts the
    version field itself is preserved across (de)serialization.
    """
    raw = make_scored(schema_version=2).model_dump_json()
    restored = IsolateScored.model_validate_json(raw)
    assert restored.schema_version == 2


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def test_envelope_round_trip() -> None:
    payload = make_embeddings_ready()
    envelope = EventEnvelope(
        event_name=EventName.EMBEDDINGS_READY,
        event_schema_version=payload.schema_version,
        payload=payload,
    )
    raw = envelope.model_dump_json()
    restored = EventEnvelope.model_validate_json(raw)
    assert restored.event_name == EventName.EMBEDDINGS_READY
    assert isinstance(restored.payload, EmbeddingsReady)
    assert restored.payload == payload


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_covers_every_event_name() -> None:
    assert set(EVENT_REGISTRY.keys()) == set(EventName)


def test_registry_topics_match_design_doc() -> None:
    assert EVENT_REGISTRY[EventName.ISOLATE_DISCOVERED].topic == "kanto.discovered"
    assert EVENT_REGISTRY[EventName.EMBEDDINGS_READY].topic == "kanto.embedded"
    assert EVENT_REGISTRY[EventName.ISOLATE_SCORED].topic == "kanto.scored"


def test_proteins_ready_is_modal_transport() -> None:
    entry = EVENT_REGISTRY[EventName.PROTEINS_READY]
    assert entry.transport == Transport.MODAL
    assert entry.topic is None


def test_stream_events_have_topics() -> None:
    for entry in EVENT_REGISTRY.values():
        if entry.transport == Transport.STREAM:
            assert entry.topic is not None
            assert entry.topic.startswith("kanto.")


# ---------------------------------------------------------------------------
# DLQ envelope
# ---------------------------------------------------------------------------


def test_dlq_envelope_round_trips_bytes() -> None:
    raw = b"\x00\x01\x02not-valid-json\xff"
    env = DLQEnvelope.from_raw(
        original_topic="kanto.discovered",
        original_payload=raw,
        failure_reason="ValidationError: missing required field 'organism'",
        consumer_group="snorlax",
        consumer_id="snorlax-0",
        attempt_count=3,
    )
    json_form = env.model_dump_json()
    restored = DLQEnvelope.model_validate_json(json_form)
    assert restored.original_payload_bytes() == raw
    assert restored.attempt_count == 3
    assert restored.consumer_group == "snorlax"


def test_dlq_envelope_attempt_count_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        DLQEnvelope.from_raw(
            original_topic="kanto.discovered",
            original_payload=b"x",
            failure_reason="boom",
            consumer_group="g",
            consumer_id="c",
            attempt_count=0,
        )


def test_dlq_envelope_includes_partition_offset_when_known() -> None:
    env = DLQEnvelope.from_raw(
        original_topic="kanto.embedded",
        original_payload=b"x",
        failure_reason="boom",
        consumer_group="alakazam",
        consumer_id="alakazam-0",
        attempt_count=1,
        original_partition=2,
        original_offset=42,
    )
    assert env.original_partition == 2
    assert env.original_offset == 42
