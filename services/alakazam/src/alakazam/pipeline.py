"""Per-isolate scoring pipeline.

The consumer drives one :class:`Pipeline.process` call per message it
drains from ``kanto.embedded``. The pipeline owns:

1. Hydrating the :class:`IsolateContext` (organism + embedding).
2. Driving the :class:`ScoringOrchestrator`.
3. Persisting the score to Mew (NULL-aware writes for skipped
   components).
4. Producing the :class:`IsolateScored` event.

Failure semantics
-----------------
* If the seed accession has no embedding row in Mew, that's a "should
  not happen" — Ditto emits EmbeddingsReady *after* the upsert. We
  return :data:`Outcome.DLQ` so the offset advances rather than
  reprocessing forever.
* Transient Mew or stream errors propagate to the consumer, which
  uses the retry budget configured in :mod:`kanto_commons.streaming`.
* All other paths produce :data:`Outcome.SUCCESS` (with a possibly
  zero novelty score and skipped components).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from kanto_commons import EmbeddingsReady, IsolateScored
from kanto_commons.mew.repositories import (
    EmbeddingNotFoundError,
    IsolateNotFoundError,
)
from kanto_commons.streaming import StreamingProducer

from alakazam.metrics import AlakazamMetrics
from alakazam.mew_gateway import AlakazamMewGateway
from alakazam.scoring import (
    IsolateContext,
    ScoringOrchestrator,
    ScoringResult,
)

logger = logging.getLogger(__name__)


class Outcome(StrEnum):
    """One pipeline-step outcome the consumer translates into stream action."""

    SUCCESS = "success"
    DLQ = "dlq"
    TRANSIENT_RETRY = "transient_retry"


@dataclass(frozen=True, slots=True)
class ProcessingResult:
    outcome: Outcome
    reason: str | None = None
    score: ScoringResult | None = None
    was_candidate: bool = False


class Pipeline:
    """Stateless per-event scoring pipeline."""

    def __init__(
        self,
        *,
        gateway: AlakazamMewGateway,
        orchestrator: ScoringOrchestrator,
        producer: StreamingProducer,
        metrics: AlakazamMetrics,
        candidate_threshold: float,
    ) -> None:
        self._gateway = gateway
        self._orchestrator = orchestrator
        self._producer = producer
        self._metrics = metrics
        self._candidate_threshold = candidate_threshold
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        return self._in_flight

    async def process(self, event: EmbeddingsReady) -> ProcessingResult:
        self._in_flight += 1
        try:
            return await self._process(event)
        finally:
            self._in_flight -= 1

    async def _process(self, event: EmbeddingsReady) -> ProcessingResult:
        try:
            isolate = await self._gateway.get_isolate(event.accession)
        except IsolateNotFoundError:
            logger.error(
                "alakazam.pipeline: isolate row missing for %s — DLQ",
                event.accession,
            )
            self._metrics.events_failed.add(1, attributes={"category": "isolate_missing"})
            return ProcessingResult(outcome=Outcome.DLQ, reason="isolate_missing")

        try:
            embedding = await self._gateway.get_genome_embedding(event.accession)
        except EmbeddingNotFoundError:
            logger.error(
                "alakazam.pipeline: embedding row missing for %s — DLQ",
                event.accession,
            )
            self._metrics.events_failed.add(1, attributes={"category": "embedding_missing"})
            return ProcessingResult(outcome=Outcome.DLQ, reason="embedding_missing")

        ctx = IsolateContext(
            accession=event.accession,
            version=event.version,
            organism=isolate.organism,
            embedding=embedding.embedding,
            now=datetime.now(UTC),
        )

        result = await self._orchestrator.score(ctx)

        was_candidate = (
            result.nn_distance.was_computed
            and (result.nn_distance.score or 0.0) >= self._candidate_threshold
        )

        # Persist before producing — at-least-once delivery means the
        # consumer may redeliver this message; we want the durable Mew
        # write to land first so a replay is idempotent.
        nn_value = result.nn_distance.score
        coverage_value = result.coverage.score
        mahalanobis_value = result.mahalanobis.score
        scored_at = datetime.now(UTC)
        await self._gateway.write_score(
            accession=event.accession,
            novelty_score=result.novelty_score,
            nn_distance=nn_value,
            coverage=coverage_value,
            mahalanobis=mahalanobis_value,
            above_threshold=result.above_threshold,
            scored_at=scored_at,
        )

        # Emit the downstream event. The schema's coverage and
        # mahalanobis are non-nullable; for components Alakazam
        # skipped we send 0.0 (with the understanding that Mew is the
        # canonical source of truth — the event is for the alerter,
        # which looks at the combined score, not the breakdown).
        scored_event = IsolateScored(
            accession=event.accession,
            version=event.version,
            novelty_score=result.novelty_score,
            nn_distance=nn_value if nn_value is not None else 0.0,
            coverage=coverage_value if coverage_value is not None else 0.0,
            mahalanobis=mahalanobis_value if mahalanobis_value is not None else 0.0,
            above_threshold=result.above_threshold,
            scored_at=scored_at,
        )
        await self._producer.send(scored_event)
        self._metrics.record_scoring(result, was_candidate=was_candidate)
        self._metrics.events_succeeded.add(1)
        return ProcessingResult(
            outcome=Outcome.SUCCESS,
            score=result,
            was_candidate=was_candidate,
        )


__all__ = ["Outcome", "Pipeline", "ProcessingResult"]
