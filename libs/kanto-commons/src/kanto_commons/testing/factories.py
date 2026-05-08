"""Factories that produce valid event objects for tests.

Each factory accepts keyword overrides for any field. Defaults are
realistic enough that downstream validation passes, so a test that
only cares about one field can override that field and ignore the rest.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from kanto_commons.schemas import (
    EmbeddingsReady,
    IsolateDiscovered,
    IsolateScored,
    ProteinsReady,
)

_DEFAULT_TS = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def make_isolate_discovered(**overrides: Any) -> IsolateDiscovered:
    fields: dict[str, Any] = dict(
        accession="PDT000123.1",
        version=1,
        organism="Salmonella",
        source="ncbi-pd",
        ftp_path="ftp://ftp.ncbi.nlm.nih.gov/example.fna.gz",
        discovered_at=_DEFAULT_TS,
        metadata={},
    )
    fields.update(overrides)
    return IsolateDiscovered(**fields)


def make_proteins_ready(**overrides: Any) -> ProteinsReady:
    fields: dict[str, Any] = dict(
        accession="PDT000123.1",
        version=1,
        os_key="PDT000123.1/1.faa.gz",
        protein_count=4123,
        produced_at=_DEFAULT_TS,
    )
    fields.update(overrides)
    return ProteinsReady(**fields)


def make_embeddings_ready(**overrides: Any) -> EmbeddingsReady:
    fields: dict[str, Any] = dict(
        accession="PDT000123.1",
        version=1,
        model="esm-c-600m",
        model_version="1.0.0",
        os_key="PDT000123.1/1.parquet",
        embedded_at=_DEFAULT_TS,
    )
    fields.update(overrides)
    return EmbeddingsReady(**fields)


def make_isolate_scored(**overrides: Any) -> IsolateScored:
    fields: dict[str, Any] = dict(
        accession="PDT000123.1",
        version=1,
        novelty_score=4.2,
        nn_distance=0.31,
        coverage=0.87,
        mahalanobis=2.1,
        above_threshold=True,
        scored_at=_DEFAULT_TS,
    )
    fields.update(overrides)
    return IsolateScored(**fields)
