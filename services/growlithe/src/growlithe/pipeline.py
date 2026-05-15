"""End-to-end poll cycle orchestrator.

One :class:`Pipeline` owns one :class:`DataSource`, one streaming
producer, one Mew client, and one :class:`SnapshotCache`. A poll
cycle is:

1. Ask the source for the current snapshot per organism.
2. For each organism, compare the source's snapshot ID to the
   cursor's ``last_snapshot_id``. If unchanged, skip — nothing to
   do.
3. Otherwise, fetch the new metadata + exceptions, read the
   previously-cached snapshot for diffing, parse + filter into
   :class:`IsolateDiscovered` events, emit them via the producer,
   then commit the cursor and cache the new snapshot.

Steps 2-3 are independent per organism so we fan out via asyncio
with a small concurrency cap (``ncbi_max_concurrent``). Failures
on one organism don't abort the others.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from kanto_commons import IsolateDiscovered
from kanto_commons.mew import MewClient
from kanto_commons.tracing import span

from growlithe.cursor import DiscoveryCursorRepository
from growlithe.datasource import (
    DataSource,
    FetchedSnapshot,
    ParsedSnapshot,
    SnapshotRef,
)
from growlithe.metrics import GrowlitheMetrics
from growlithe.snapshot_cache import SnapshotCache

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Producer protocol — what the pipeline expects of an event sink.
# ---------------------------------------------------------------------------


# A minimal callable shape rather than the full StreamingProducer class so
# the pipeline can be tested against FakeStreamingClient.produce directly.
EventEmitter = Callable[[IsolateDiscovered], Awaitable[None]]


# ---------------------------------------------------------------------------
# Per-cycle and per-organism outcomes
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class OrganismResult:
    """Result of one organism's slice of a poll cycle."""

    organism: str
    snapshot_id: str | None
    emitted: int = 0
    skipped_unchanged: bool = False
    error: BaseException | None = None
    parsed: ParsedSnapshot | None = None


@dataclass(slots=True)
class CycleResult:
    """Aggregate result of one full poll cycle across all organisms."""

    started_at: datetime
    finished_at: datetime
    per_organism: list[OrganismResult] = field(default_factory=list)

    @property
    def total_emitted(self) -> int:
        return sum(r.emitted for r in self.per_organism)

    @property
    def errors(self) -> list[OrganismResult]:
        return [r for r in self.per_organism if r.error is not None]


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class Pipeline:
    """One poll cycle across every organism the data source tracks."""

    def __init__(
        self,
        *,
        source: DataSource,
        cache: SnapshotCache,
        mew: MewClient,
        cursors: DiscoveryCursorRepository,
        emit: EventEmitter,
        metrics: GrowlitheMetrics,
        max_concurrent: int = 2,
    ) -> None:
        self._source = source
        self._cache = cache
        self._mew = mew
        self._cursors = cursors
        self._emit = emit
        self._metrics = metrics
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def run_once(self) -> CycleResult:
        """Run a single end-to-end poll cycle."""
        started = datetime.now(UTC)
        self._metrics.poll_cycles_started.add(1)
        with span("growlithe.poll_cycle"):
            try:
                refs = await asyncio.to_thread(self._source.list_current_snapshots)
            except Exception:
                self._metrics.poll_cycles_failed.add(1)
                logger.exception("growlithe.pipeline: snapshot listing failed")
                raise

            tasks = [self._process_organism(ref) for ref in refs]
            per_organism = await asyncio.gather(*tasks, return_exceptions=False)

        finished = datetime.now(UTC)
        result = CycleResult(
            started_at=started,
            finished_at=finished,
            per_organism=list(per_organism),
        )
        if result.errors:
            self._metrics.poll_cycles_failed.add(1)
        else:
            self._metrics.poll_cycles_completed.add(1)
        logger.info(
            "growlithe.pipeline: cycle done emitted=%d errors=%d duration=%.1fs",
            result.total_emitted,
            len(result.errors),
            (finished - started).total_seconds(),
        )
        return result

    async def _process_organism(self, ref: SnapshotRef) -> OrganismResult:
        """Process a single organism. Per-organism errors are captured.

        Holds the concurrency semaphore so we don't open N+1 connections
        to NCBI at once. Any exception is wrapped into the
        :class:`OrganismResult`; we do not raise out of here so a flaky
        organism doesn't abort the rest of the cycle.
        """
        async with self._semaphore:
            with span("growlithe.poll_organism", attributes={"organism": ref.organism}):
                started = datetime.now(UTC)
                try:
                    return await self._process_organism_inner(ref, started)
                except Exception as exc:
                    logger.exception("growlithe.pipeline: organism=%s failed", ref.organism)
                    return OrganismResult(
                        organism=ref.organism,
                        snapshot_id=ref.snapshot_id,
                        error=exc,
                    )
                finally:
                    elapsed = (datetime.now(UTC) - started).total_seconds()
                    self._metrics.poll_cycle_duration_seconds.record(
                        elapsed, {"organism": ref.organism}
                    )

    async def _process_organism_inner(self, ref: SnapshotRef, started: datetime) -> OrganismResult:
        source_name = self._source.name
        async with self._mew.connection() as conn:
            await self._cursors.record_attempt(
                conn,
                source=source_name,
                organism=ref.organism,
                polled_at=started,
            )
            cursor = await self._cursors.get(conn, source=source_name, organism=ref.organism)

        if cursor is not None and cursor.last_snapshot_id == ref.snapshot_id:
            logger.info(
                "growlithe.pipeline: organism=%s snapshot=%s unchanged; skipping",
                ref.organism,
                ref.snapshot_id,
            )
            # Still bump cursor's last_succeeded_at so dashboards show
            # "we successfully decided not to poll" rather than stale.
            async with self._mew.connection() as conn:
                await self._cursors.commit_success(
                    conn,
                    source=source_name,
                    organism=ref.organism,
                    snapshot_id=ref.snapshot_id,
                    succeeded_at=datetime.now(UTC),
                    emitted_count=0,
                )
            return OrganismResult(
                organism=ref.organism,
                snapshot_id=ref.snapshot_id,
                skipped_unchanged=True,
            )

        # Cold/new snapshot: fetch upstream metadata + exceptions.
        snapshot = await asyncio.to_thread(self._source.fetch_snapshot, ref)

        # Load whatever we cached previously (might be None on first poll).
        previous_metadata = await asyncio.to_thread(
            self._cache.read_snapshot,
            source=source_name,
            organism=ref.organism,
            snapshot_id=(cursor.last_snapshot_id if cursor else None) or "",
        )

        parsed = await asyncio.to_thread(
            self._source.parse_rows,
            snapshot,
            previous_metadata_tsv=previous_metadata,
        )

        self._record_filter_metrics(ref.organism, parsed)

        emitted = await self._emit_all(parsed.events, organism=ref.organism)

        # Cache the new snapshot only after every event has been
        # acknowledged — otherwise a crash mid-emit leaves the cache
        # ahead of the cursor, and we'd miss isolates on restart.
        await asyncio.to_thread(
            self._cache.write_snapshot,
            source=source_name,
            organism=ref.organism,
            snapshot_id=ref.snapshot_id,
            metadata_tsv=snapshot.metadata_tsv,
        )

        async with self._mew.connection() as conn:
            await self._cursors.commit_success(
                conn,
                source=source_name,
                organism=ref.organism,
                snapshot_id=ref.snapshot_id,
                succeeded_at=datetime.now(UTC),
                emitted_count=emitted,
            )

        return OrganismResult(
            organism=ref.organism,
            snapshot_id=ref.snapshot_id,
            emitted=emitted,
            parsed=parsed,
        )

    async def _emit_all(
        self,
        events: list[IsolateDiscovered],
        *,
        organism: str,
    ) -> int:
        for event in events:
            await self._emit(event)
        self._metrics.isolates_discovered.add(len(events), {"organism": organism})
        return len(events)

    def _record_filter_metrics(self, organism: str, parsed: ParsedSnapshot) -> None:
        if parsed.qc_filtered:
            self._metrics.isolates_filtered.add(
                parsed.qc_filtered,
                {"organism": organism, "reason": "qc_failed"},
            )
        if parsed.missing_required:
            self._metrics.isolates_filtered.add(
                parsed.missing_required,
                {"organism": organism, "reason": "missing_required"},
            )
        if parsed.bad_version:
            self._metrics.isolates_filtered.add(
                parsed.bad_version,
                {"organism": organism, "reason": "bad_version"},
            )


# Suppress an unused-import warning on FetchedSnapshot when the type is
# only referenced in docstrings/imports above.
_ = FetchedSnapshot

__all__ = [
    "CycleResult",
    "EventEmitter",
    "OrganismResult",
    "Pipeline",
]
