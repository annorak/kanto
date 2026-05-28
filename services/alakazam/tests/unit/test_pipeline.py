"""Tests for the per-isolate scoring pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from kanto_commons import EmbeddingsReady, IsolateScored
from kanto_commons.mew.models import IsolateStatus
from kanto_commons.mew.repositories import (
    EmbeddingNotFoundError,
    IsolateNotFoundError,
)

from alakazam.metrics import AlakazamMetrics
from alakazam.mew_gateway import GenomeEmbeddingRecord, IsolateForScoring
from alakazam.pipeline import Outcome, Pipeline
from alakazam.scoring import (
    IsolateContext,
    LinearCombiner,
    ScoringOrchestrator,
)
from alakazam.scoring.strategy import (
    ComponentName,
    ComponentResult,
    ScoringStrategy,
    SkipReason,
)

_DIM = 1152


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class _StubGateway:
    isolates: dict[str, IsolateForScoring] = field(default_factory=dict)
    embeddings: dict[str, GenomeEmbeddingRecord] = field(default_factory=dict)
    writes: list[dict[str, Any]] = field(default_factory=list)
    raise_on_isolate: bool = False
    raise_on_embedding: bool = False

    async def get_isolate(self, accession: str) -> IsolateForScoring:
        if self.raise_on_isolate:
            raise IsolateNotFoundError(accession)
        return self.isolates[accession]

    async def get_genome_embedding(self, accession: str) -> GenomeEmbeddingRecord:
        if self.raise_on_embedding:
            raise EmbeddingNotFoundError(accession)
        return self.embeddings[accession]

    async def write_score(self, **kwargs: Any) -> None:
        self.writes.append(kwargs)


@dataclass
class _StubProducer:
    sent: list[IsolateScored] = field(default_factory=list)

    async def send(self, event: IsolateScored) -> None:
        self.sent.append(event)


@dataclass
class _ConstStrategy(ScoringStrategy):
    _name: ComponentName
    _result: ComponentResult

    @property
    def name(self) -> ComponentName:
        return self._name

    async def compute(self, ctx: IsolateContext) -> ComponentResult:
        return self._result


def _orchestrator(*, nn: float, cov: float, maha: float, candidate: float) -> ScoringOrchestrator:
    return ScoringOrchestrator(
        nn_strategy=_ConstStrategy(
            _name=ComponentName.NN_DISTANCE,
            _result=ComponentResult.computed(ComponentName.NN_DISTANCE, nn, duration_seconds=0.0),
        ),
        coverage_strategy=_ConstStrategy(
            _name=ComponentName.COVERAGE,
            _result=ComponentResult.computed(ComponentName.COVERAGE, cov, duration_seconds=0.0),
        ),
        mahalanobis_strategy=_ConstStrategy(
            _name=ComponentName.MAHALANOBIS,
            _result=ComponentResult.computed(ComponentName.MAHALANOBIS, maha, duration_seconds=0.0),
        ),
        combiner=LinearCombiner(weight_nn=1.0, weight_coverage=2.0, weight_mahalanobis=0.5),
        candidate_threshold=candidate,
        alert_threshold=0.5,
    )


def _gateway_with_one(accession: str) -> _StubGateway:
    return _StubGateway(
        isolates={
            accession: IsolateForScoring(
                accession=accession,
                version=1,
                organism="Salmonella",
                status=IsolateStatus.EMBEDDED,
            )
        },
        embeddings={
            accession: GenomeEmbeddingRecord(
                accession=accession,
                version=1,
                model="esm-c-600m",
                model_version="1.0.0",
                embedding=[0.0] * _DIM,
            )
        },
    )


def _embeddings_event(accession: str = "PDT0001") -> EmbeddingsReady:
    return EmbeddingsReady(
        accession=accession,
        version=1,
        model="esm-c-600m",
        model_version="1.0.0",
        os_key=f"{accession}/1.parquet",
        embedded_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


async def test_pipeline_success_tier_one_only() -> None:
    accession = "PDT0001"
    gateway: Any = _gateway_with_one(accession)
    producer: Any = _StubProducer()
    pipeline = Pipeline(
        gateway=gateway,
        orchestrator=_orchestrator(nn=0.10, cov=0.99, maha=99.0, candidate=0.30),
        producer=producer,
        metrics=AlakazamMetrics.build(),
        candidate_threshold=0.30,
    )
    result = await pipeline.process(_embeddings_event(accession))
    assert result.outcome is Outcome.SUCCESS
    assert not result.was_candidate
    assert len(gateway.writes) == 1
    write = gateway.writes[0]
    assert write["nn_distance"] == pytest.approx(0.10)
    assert write["coverage"] is None
    assert write["mahalanobis"] is None
    assert len(producer.sent) == 1
    sent = producer.sent[0]
    assert sent.nn_distance == pytest.approx(0.10)
    assert sent.coverage == 0.0
    assert sent.mahalanobis == 0.0


async def test_pipeline_success_tier_two() -> None:
    accession = "PDT0002"
    gateway: Any = _gateway_with_one(accession)
    producer: Any = _StubProducer()
    pipeline = Pipeline(
        gateway=gateway,
        orchestrator=_orchestrator(nn=0.5, cov=0.3, maha=2.0, candidate=0.30),
        producer=producer,
        metrics=AlakazamMetrics.build(),
        candidate_threshold=0.30,
    )
    result = await pipeline.process(_embeddings_event(accession))
    assert result.outcome is Outcome.SUCCESS
    assert result.was_candidate
    write = gateway.writes[0]
    assert write["coverage"] == pytest.approx(0.3)
    assert write["mahalanobis"] == pytest.approx(2.0)
    assert write["above_threshold"] is True


# ---------------------------------------------------------------------------
# Sad paths
# ---------------------------------------------------------------------------


async def test_pipeline_missing_isolate_dlqs() -> None:
    gateway: Any = _StubGateway(raise_on_isolate=True)
    producer: Any = _StubProducer()
    pipeline = Pipeline(
        gateway=gateway,
        orchestrator=_orchestrator(nn=0.1, cov=0.0, maha=0.0, candidate=0.30),
        producer=producer,
        metrics=AlakazamMetrics.build(),
        candidate_threshold=0.30,
    )
    result = await pipeline.process(_embeddings_event())
    assert result.outcome is Outcome.DLQ
    assert result.reason == "isolate_missing"
    assert len(producer.sent) == 0


async def test_pipeline_missing_embedding_dlqs() -> None:
    accession = "PDT0003"
    gateway = _StubGateway(
        isolates={
            accession: IsolateForScoring(
                accession=accession,
                version=1,
                organism="Salmonella",
                status=IsolateStatus.EMBEDDED,
            )
        },
    )
    gateway.raise_on_embedding = True
    producer: Any = _StubProducer()
    pipeline = Pipeline(
        gateway=gateway,  # type: ignore[arg-type]
        orchestrator=_orchestrator(nn=0.1, cov=0.0, maha=0.0, candidate=0.30),
        producer=producer,
        metrics=AlakazamMetrics.build(),
        candidate_threshold=0.30,
    )
    result = await pipeline.process(_embeddings_event(accession))
    assert result.outcome is Outcome.DLQ
    assert result.reason == "embedding_missing"


# ---------------------------------------------------------------------------
# Version reconciliation
# ---------------------------------------------------------------------------


async def test_pipeline_stale_event_skips_and_commits() -> None:
    """Mew already holds a newer version of this accession; the event is
    a redelivery of an older version. Drop without scoring (so we don't
    overwrite the newer state) and return SUCCESS so the offset advances.
    """
    accession = "PDT_STALE"
    gateway = _StubGateway(
        isolates={
            accession: IsolateForScoring(
                accession=accession,
                version=3,
                organism="Salmonella",
                status=IsolateStatus.SCORED,
            )
        },
        embeddings={
            accession: GenomeEmbeddingRecord(
                accession=accession,
                version=3,  # stored newer than event
                model="esm-c-600m",
                model_version="1.0.0",
                embedding=[0.0] * _DIM,
            )
        },
    )
    producer: Any = _StubProducer()
    pipeline = Pipeline(
        gateway=gateway,  # type: ignore[arg-type]
        orchestrator=_orchestrator(nn=0.1, cov=0.0, maha=0.0, candidate=0.30),
        producer=producer,
        metrics=AlakazamMetrics.build(),
        candidate_threshold=0.30,
    )

    event = EmbeddingsReady(
        accession=accession,
        version=1,  # stale
        model="esm-c-600m",
        model_version="1.0.0",
        os_key=f"{accession}/1.parquet",
        embedded_at=datetime.now(UTC),
    )
    result = await pipeline.process(event)
    assert result.outcome is Outcome.SUCCESS
    assert result.reason == "stale_version"
    assert gateway.writes == []
    assert producer.sent == []


async def test_pipeline_embedding_lags_event_retries() -> None:
    """The event arrived ahead of Mew catching up (race). Transient retry."""
    accession = "PDT_LAG"
    gateway = _StubGateway(
        isolates={
            accession: IsolateForScoring(
                accession=accession,
                version=1,
                organism="Salmonella",
                status=IsolateStatus.EMBEDDED,
            )
        },
        embeddings={
            accession: GenomeEmbeddingRecord(
                accession=accession,
                version=1,  # stored older than event
                model="esm-c-600m",
                model_version="1.0.0",
                embedding=[0.0] * _DIM,
            )
        },
    )
    producer: Any = _StubProducer()
    pipeline = Pipeline(
        gateway=gateway,  # type: ignore[arg-type]
        orchestrator=_orchestrator(nn=0.1, cov=0.0, maha=0.0, candidate=0.30),
        producer=producer,
        metrics=AlakazamMetrics.build(),
        candidate_threshold=0.30,
    )

    event = EmbeddingsReady(
        accession=accession,
        version=2,  # ahead of Mew
        model="esm-c-600m",
        model_version="1.0.0",
        os_key=f"{accession}/2.parquet",
        embedded_at=datetime.now(UTC),
    )
    result = await pipeline.process(event)
    assert result.outcome is Outcome.TRANSIENT_RETRY
    assert result.reason == "embedding_lags_event"
    assert gateway.writes == []


# ---------------------------------------------------------------------------
# In-flight gauge
# ---------------------------------------------------------------------------


async def test_in_flight_tracked() -> None:
    accession = "PDT0004"
    gateway: Any = _gateway_with_one(accession)
    producer: Any = _StubProducer()

    nn = _ConstStrategy(
        _name=ComponentName.NN_DISTANCE,
        _result=ComponentResult.skipped(ComponentName.NN_DISTANCE, SkipReason.COMPUTE_ERROR),
    )
    cov = _ConstStrategy(
        _name=ComponentName.COVERAGE,
        _result=ComponentResult.skipped(
            ComponentName.COVERAGE, SkipReason.BELOW_CANDIDATE_THRESHOLD
        ),
    )
    maha = _ConstStrategy(
        _name=ComponentName.MAHALANOBIS,
        _result=ComponentResult.skipped(
            ComponentName.MAHALANOBIS, SkipReason.BELOW_CANDIDATE_THRESHOLD
        ),
    )
    orch = ScoringOrchestrator(
        nn_strategy=nn,
        coverage_strategy=cov,
        mahalanobis_strategy=maha,
        combiner=LinearCombiner(weight_nn=1.0, weight_coverage=2.0, weight_mahalanobis=0.5),
        candidate_threshold=0.30,
        alert_threshold=0.5,
    )
    pipeline = Pipeline(
        gateway=gateway,
        orchestrator=orch,
        producer=producer,
        metrics=AlakazamMetrics.build(),
        candidate_threshold=0.30,
    )
    assert pipeline.in_flight == 0
    result = await pipeline.process(_embeddings_event(accession))
    assert result.outcome is Outcome.SUCCESS
    assert pipeline.in_flight == 0
