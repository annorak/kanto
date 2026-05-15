"""Pipeline orchestrator tests.

We stub the DataSource, the MewClient, the cursor repo, and the
emitter so the pipeline runs entirely in-memory. The test exercises:

* First poll (cold start) emits every isolate.
* Repeat poll with same snapshot ID skips outright.
* New snapshot with diff against cached previous emits only the
  diff.
* Cursor advances only after a successful emit.
* Per-organism failure doesn't abort the rest of the cycle.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest
from kanto_commons import IsolateDiscovered

from growlithe.cursor import DiscoveryCursor, DiscoveryCursorRepository
from growlithe.datasource import (
    DataSource,
    FetchedSnapshot,
    ParsedSnapshot,
    SnapshotRef,
)
from growlithe.metrics import GrowlitheMetrics
from growlithe.pipeline import Pipeline

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _make_event(accession: str, organism: str = "Listeria") -> IsolateDiscovered:
    return IsolateDiscovered(
        accession=accession,
        version=int(accession.rsplit(".", 1)[1]),
        organism=organism,
        source="ncbi-pd",
        ftp_path="https://example.test/genomes/all/GCA/000/000/000/",
        metadata={"snapshot_id": "PDG-x"},
    )


class _StubSource(DataSource):
    def __init__(
        self,
        *,
        snapshots: list[SnapshotRef],
        events_by_snapshot: dict[str, list[IsolateDiscovered]],
        metadata_by_snapshot: dict[str, bytes] | None = None,
        listing_error: BaseException | None = None,
        fetch_error_for: str | None = None,
    ) -> None:
        self._snapshots = snapshots
        self._events_by_snapshot = events_by_snapshot
        self._metadata_by_snapshot = metadata_by_snapshot or {
            ref.snapshot_id: b"#col\n" for ref in snapshots
        }
        self._listing_error = listing_error
        self._fetch_error_for = fetch_error_for
        self.parse_calls: list[tuple[str, bytes | None]] = []

    @property
    def name(self) -> str:
        return "ncbi-pd"

    def list_current_snapshots(self) -> list[SnapshotRef]:
        if self._listing_error is not None:
            raise self._listing_error
        return list(self._snapshots)

    def fetch_snapshot(self, ref: SnapshotRef) -> FetchedSnapshot:
        if self._fetch_error_for == ref.organism:
            raise RuntimeError("fetch boom")
        return FetchedSnapshot(
            ref=ref,
            metadata_tsv=self._metadata_by_snapshot[ref.snapshot_id],
            exceptions_tsv=None,
        )

    def parse_rows(
        self,
        snapshot: FetchedSnapshot,
        *,
        previous_metadata_tsv: bytes | None,
    ) -> ParsedSnapshot:
        self.parse_calls.append((snapshot.ref.snapshot_id, previous_metadata_tsv))
        events = self._events_by_snapshot.get(snapshot.ref.snapshot_id, [])
        return ParsedSnapshot(events=events)


@dataclass
class _StubCursorRow:
    last_snapshot_id: str | None = None
    last_emitted_count: int = 0
    last_polled_at: datetime | None = None
    last_succeeded_at: datetime | None = None


class _StubCursorRepo(DiscoveryCursorRepository):
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], _StubCursorRow] = {}

    async def get(self, conn: Any, *, source: str, organism: str) -> DiscoveryCursor | None:
        row = self.rows.get((source, organism))
        if row is None:
            return None
        return DiscoveryCursor(
            source=source,
            organism=organism,
            last_snapshot_id=row.last_snapshot_id,
            last_polled_at=row.last_polled_at,
            last_succeeded_at=row.last_succeeded_at,
            last_emitted_count=row.last_emitted_count,
        )

    async def record_attempt(
        self, conn: Any, *, source: str, organism: str, polled_at: datetime
    ) -> None:
        self.rows.setdefault((source, organism), _StubCursorRow()).last_polled_at = polled_at

    async def commit_success(
        self,
        conn: Any,
        *,
        source: str,
        organism: str,
        snapshot_id: str,
        succeeded_at: datetime,
        emitted_count: int,
    ) -> None:
        row = self.rows.setdefault((source, organism), _StubCursorRow())
        row.last_snapshot_id = snapshot_id
        row.last_succeeded_at = succeeded_at
        row.last_emitted_count = emitted_count


class _StubMew:
    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Any]:
        yield object()


class _StubCache:
    """Records what was written and serves what's been written so far."""

    def __init__(self) -> None:
        self._snapshots: dict[tuple[str, str, str], bytes] = {}
        self._latest: dict[tuple[str, str], str] = {}
        self.write_calls: list[tuple[str, str, str]] = []

    def latest_snapshot_id(self, *, source: str, organism: str) -> str | None:
        return self._latest.get((source, organism))

    def read_snapshot(self, *, source: str, organism: str, snapshot_id: str) -> bytes | None:
        return self._snapshots.get((source, organism, snapshot_id))

    def write_snapshot(
        self,
        *,
        source: str,
        organism: str,
        snapshot_id: str,
        metadata_tsv: bytes,
    ) -> None:
        self._snapshots[(source, organism, snapshot_id)] = metadata_tsv
        self._latest[(source, organism)] = snapshot_id
        self.write_calls.append((source, organism, snapshot_id))


@dataclass
class _RecorderEmit:
    received: list[IsolateDiscovered] = field(default_factory=list)
    fail_after: int | None = None

    async def __call__(self, event: IsolateDiscovered) -> None:
        if self.fail_after is not None and len(self.received) >= self.fail_after:
            raise RuntimeError("emit boom")
        self.received.append(event)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ref(organism: str, snapshot_id: str) -> SnapshotRef:
    return SnapshotRef(
        organism=organism,
        snapshot_id=snapshot_id,
        metadata_url=f"https://example.test/{organism}/{snapshot_id}.tsv",
        exceptions_url=None,
    )


def _build_pipeline(
    *,
    source: DataSource,
    cursors: DiscoveryCursorRepository,
    cache: _StubCache,
    emitter: _RecorderEmit,
) -> Pipeline:
    return Pipeline(
        source=source,
        cache=cache,  # type: ignore[arg-type]  # SnapshotCache duck-typed by Pipeline
        mew=_StubMew(),  # type: ignore[arg-type]
        cursors=cursors,
        emit=emitter,
        metrics=GrowlitheMetrics.build(),
        max_concurrent=4,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_cold_start_emits_everything_and_caches() -> None:
    ref = _ref("Listeria", "PDG-1.1")
    events = [_make_event("PDT0001.1"), _make_event("PDT0002.1")]
    source = _StubSource(snapshots=[ref], events_by_snapshot={"PDG-1.1": events})
    cursors = _StubCursorRepo()
    cache = _StubCache()
    emitter = _RecorderEmit()
    pipeline = _build_pipeline(source=source, cursors=cursors, cache=cache, emitter=emitter)
    result = await pipeline.run_once()

    assert result.total_emitted == 2
    assert {e.accession for e in emitter.received} == {"PDT0001.1", "PDT0002.1"}
    assert cache.write_calls == [("ncbi-pd", "Listeria", "PDG-1.1")]
    row = cursors.rows[("ncbi-pd", "Listeria")]
    assert row.last_snapshot_id == "PDG-1.1"
    assert row.last_emitted_count == 2


async def test_idempotent_same_snapshot_no_double_emit() -> None:
    """Cycle B with the same snapshot ID as cycle A skips upstream entirely."""
    ref = _ref("Listeria", "PDG-1.1")
    events = [_make_event("PDT0001.1")]
    source = _StubSource(snapshots=[ref], events_by_snapshot={"PDG-1.1": events})
    cursors = _StubCursorRepo()
    cache = _StubCache()
    emitter = _RecorderEmit()
    pipeline = _build_pipeline(source=source, cursors=cursors, cache=cache, emitter=emitter)

    await pipeline.run_once()
    # Re-run with no upstream change; the adapter must not even be asked
    # to parse anything.
    parse_calls_before = len(source.parse_calls)
    result = await pipeline.run_once()
    assert result.per_organism[0].skipped_unchanged is True
    assert len(source.parse_calls) == parse_calls_before
    assert len(emitter.received) == 1  # still just cycle A's event


async def test_new_snapshot_diffs_against_previous_cache() -> None:
    """Snapshot bumps PDG version; pipeline emits only the diff."""
    old_ref = _ref("Listeria", "PDG-1.1")
    new_ref = _ref("Listeria", "PDG-1.2")
    source = _StubSource(
        snapshots=[old_ref],
        events_by_snapshot={"PDG-1.1": [_make_event("PDT0001.1")]},
        metadata_by_snapshot={
            "PDG-1.1": b"#target_acc\nPDT0001.1\n",
            "PDG-1.2": b"#target_acc\nPDT0001.1\nPDT0002.1\n",
        },
    )
    cursors = _StubCursorRepo()
    cache = _StubCache()
    emitter = _RecorderEmit()
    pipeline = _build_pipeline(source=source, cursors=cursors, cache=cache, emitter=emitter)

    # Cold cycle emits 1 isolate, caches old snapshot.
    await pipeline.run_once()

    # Now switch the upstream to the newer PDG and rerun.
    source._snapshots = [new_ref]
    source._events_by_snapshot["PDG-1.2"] = [_make_event("PDT0002.1")]
    await pipeline.run_once()

    # The pipeline should have asked the adapter to diff PDG-1.2's
    # metadata against the cached PDG-1.1 bytes.
    diff_calls = [c for c in source.parse_calls if c[0] == "PDG-1.2"]
    assert diff_calls and diff_calls[0][1] is not None
    # And only the new isolate should have been emitted.
    accs = [e.accession for e in emitter.received]
    assert accs == ["PDT0001.1", "PDT0002.1"]


async def test_cursor_not_advanced_when_emit_fails() -> None:
    """An emit error rolls the cycle; cursor stays at the previous snapshot."""
    ref = _ref("Listeria", "PDG-1.1")
    events = [_make_event("PDT0001.1"), _make_event("PDT0002.1")]
    source = _StubSource(snapshots=[ref], events_by_snapshot={"PDG-1.1": events})
    cursors = _StubCursorRepo()
    cache = _StubCache()
    emitter = _RecorderEmit(fail_after=1)  # second event raises
    pipeline = _build_pipeline(source=source, cursors=cursors, cache=cache, emitter=emitter)

    result = await pipeline.run_once()
    assert result.errors  # the organism errored
    row = cursors.rows[("ncbi-pd", "Listeria")]
    assert row.last_snapshot_id is None  # cursor never advanced
    assert cache.write_calls == []  # cache never updated either


async def test_one_organism_failure_does_not_abort_cycle() -> None:
    listeria = _ref("Listeria", "PDG-1.1")
    salmonella = _ref("Salmonella", "PDG-2.1")
    source = _StubSource(
        snapshots=[listeria, salmonella],
        events_by_snapshot={
            "PDG-1.1": [_make_event("PDT0001.1", organism="Listeria")],
            "PDG-2.1": [_make_event("PDT0002.1", organism="Salmonella")],
        },
        fetch_error_for="Listeria",
    )
    cursors = _StubCursorRepo()
    cache = _StubCache()
    emitter = _RecorderEmit()
    pipeline = _build_pipeline(source=source, cursors=cursors, cache=cache, emitter=emitter)
    result = await pipeline.run_once()

    by_organism = {r.organism: r for r in result.per_organism}
    assert by_organism["Listeria"].error is not None
    assert by_organism["Salmonella"].error is None
    assert by_organism["Salmonella"].emitted == 1
    assert cursors.rows[("ncbi-pd", "Salmonella")].last_snapshot_id == "PDG-2.1"
    assert ("ncbi-pd", "Listeria") in cursors.rows  # attempt recorded
    assert cursors.rows[("ncbi-pd", "Listeria")].last_snapshot_id is None


async def test_listing_error_propagates() -> None:
    """If the source can't list snapshots at all, the cycle fails fast."""
    source = _StubSource(
        snapshots=[],
        events_by_snapshot={},
        listing_error=RuntimeError("ncbi down"),
    )
    pipeline = _build_pipeline(
        source=source,
        cursors=_StubCursorRepo(),
        cache=_StubCache(),
        emitter=_RecorderEmit(),
    )
    with pytest.raises(RuntimeError, match="ncbi down"):
        await pipeline.run_once()
