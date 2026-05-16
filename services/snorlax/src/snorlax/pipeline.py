"""Per-event pipeline orchestrator with explicit failure handling.

This module owns Task 6 §7: for every failure mode in the pipeline,
return the correct outcome so the streaming consumer commits (or
doesn't), DLQs (or doesn't), and Mew records the right status.

The outcomes are encoded as the :class:`Outcome` enum below. The
caller (``run_event`` from :mod:`snorlax.service`) maps outcomes onto
the streaming consumer's commit / handle_failure semantics:

* ``SUCCESS`` and ``PERMANENT_FAILURE`` — commit the offset. The
  message has been fully handled; redelivery would either be a
  duplicate (success) or an infinite loop (the file doesn't exist,
  Prodigal will fail the same way next time, etc.). For permanent
  per-isolate failures we record QC_FAILED in Mew with a
  category-specific reason.
* ``TRANSIENT_RETRY`` — do NOT commit. The next consumer poll
  redelivers the message and we try again. After
  ``max_processing_attempts`` exhausted attempts the streaming
  wrapper auto-DLQs.
* ``DLQ`` — emit to DLQ explicitly and commit. Used for terminal
  *application*-level failures (Prodigal exited non-zero, Modal
  exhausted retries) where a redelivery would just hit the same
  problem; the operator needs to look.

Each handler stage logs the reason and bumps the per-stage metric
counter so dashboards show the distribution of failure modes.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from kanto_commons import IsolateDiscovered, ProteinsReady
from kanto_commons.mew import (
    IsolateRepository,
    IsolateRow,
    IsolateStatus,
    MewClient,
)
from kanto_commons.storage import KeyBuilder
from kanto_commons.tracing import span
from opentelemetry import trace

from snorlax.downloader import (
    DownloadError,
    GenomeChecksumError,
    GenomeDownloader,
    GenomeNotFoundError,
    GenomeTooLargeError,
    GenomeTooSmallError,
)
from snorlax.fasta import (
    FastaValidationError,
    count_proteins,
    validate_genome_fasta,
    validate_protein_fasta,
)
from snorlax.metrics import SnorlaxMetrics
from snorlax.modal_client import (
    ModalClient,
    ModalSpawnError,
    SpawnedCall,
)
from snorlax.prodigal import (
    ProdigalBinaryMissingError,
    ProdigalNonZeroExitError,
    ProdigalRunner,
    ProdigalTimeoutError,
)
from snorlax.uploader import ProteinUploader, UploadError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


class Outcome(StrEnum):
    """What the consumer should do with the message."""

    SUCCESS = "success"
    PERMANENT_FAILURE = "permanent_failure"
    TRANSIENT_RETRY = "transient_retry"
    DLQ = "dlq"


class FailureCategory(StrEnum):
    """Stable strings used for metric labels and Mew ``qc_failure_reason``.

    The names map onto the failure modes enumerated in task-06 §7. A
    stable string is more useful than a free-form message because
    operators can group by it.
    """

    NOT_FOUND = "not_found"
    VALIDATION_FAILED = "validation_failed"
    DOWNLOAD_TOO_LARGE = "download_too_large"
    DOWNLOAD_TRANSIENT_EXHAUSTED = "download_transient_exhausted"
    PRODIGAL_FAILED = "prodigal_failed"
    PRODIGAL_TIMEOUT = "prodigal_timeout"
    UPLOAD_FAILED = "upload_failed"
    MODAL_SPAWN_FAILED = "modal_spawn_failed"
    MEW_UPDATE_FAILED = "mew_update_failed"


@dataclass(frozen=True)
class ProcessingResult:
    """Outcome plus diagnostic fields. Logged + tested against."""

    outcome: Outcome
    category: FailureCategory | None = None
    reason: str | None = None
    modal_call_id: str | None = None
    protein_count: int | None = None
    os_key: str | None = None


# ---------------------------------------------------------------------------
# Stage shorthand
# ---------------------------------------------------------------------------


class _Stage(StrEnum):
    DOWNLOAD = "download"
    VALIDATE = "validate"
    PRODIGAL = "prodigal"
    UPLOAD = "upload"
    MEW_PROTEINS_READY = "mew_proteins_ready"
    MODAL_SPAWN = "modal_spawn"
    MEW_MODAL_RECORDED = "mew_modal_recorded"


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class Pipeline:
    """Stateless per-event processor. One instance shared by the pod.

    ``process`` is awaitable and **never** raises for an application
    error — it always returns a :class:`ProcessingResult`. This is the
    contract the caller relies on to map outcomes onto stream commit
    semantics deterministically.

    Infrastructure-fatal errors (Modal SDK missing at startup, Prodigal
    binary missing) are surfaced as raised exceptions because they
    should fail the pod, not the message.
    """

    def __init__(
        self,
        *,
        downloader: GenomeDownloader,
        prodigal: ProdigalRunner,
        uploader: ProteinUploader,
        modal_client: ModalClient,
        mew: MewClient,
        key_builder: KeyBuilder,
        metrics: SnorlaxMetrics,
        work_dir: Path,
        mew_max_attempts: int = 5,
    ) -> None:
        self._downloader = downloader
        self._prodigal = prodigal
        self._uploader = uploader
        self._modal = modal_client
        self._mew = mew
        self._key_builder = key_builder
        self._metrics = metrics
        self._work_dir = work_dir
        self._isolates = IsolateRepository()
        self._in_flight = 0
        self._mew_max_attempts = mew_max_attempts

    @property
    def in_flight(self) -> int:
        return self._in_flight

    async def process(self, event: IsolateDiscovered) -> ProcessingResult:
        """Run one IsolateDiscovered event through the full pipeline."""
        self._metrics.events_processed.add(1)
        self._in_flight += 1
        scratch = self._scratch_dir_for(event)
        try:
            with span(
                "snorlax.process_event",
                attributes={
                    "accession": event.accession,
                    "version": event.version,
                    "organism": event.organism,
                },
            ):
                result = await self._process_inner(event, scratch)
            self._record_outcome(result)
            return result
        finally:
            self._in_flight -= 1
            _purge_dir(scratch)

    # -------------------- the actual pipeline --------------------

    async def _process_inner(self, event: IsolateDiscovered, scratch: Path) -> ProcessingResult:
        # Ensure the isolates row exists before any status transition.
        # Growlithe emits events without writing Mew; Snorlax owns the
        # row's lifecycle from DISCOVERED onwards. If Mew is down, this
        # surfaces as TRANSIENT_RETRY so the offset isn't committed.
        try:
            await self._ensure_isolate_row(event)
        except _MewWriteError as exc:
            logger.warning(
                "snorlax.pipeline: cannot reach mew accession=%s; will retry: %s",
                event.accession,
                exc,
            )
            self._metrics.stage_errors.add(1, {"stage": _Stage.MEW_PROTEINS_READY.value})
            return ProcessingResult(
                outcome=Outcome.TRANSIENT_RETRY,
                category=FailureCategory.MEW_UPDATE_FAILED,
                reason=str(exc),
            )

        asm_acc = event.metadata.get("asm_acc")
        if not asm_acc:
            # Growlithe is supposed to populate ``asm_acc`` on every
            # event. A missing value is a contract violation we can't
            # recover from on retry, so route it to QC_FAILED rather
            # than spin.
            return await self._handle_permanent(
                event,
                category=FailureCategory.VALIDATION_FAILED,
                reason="event.metadata.asm_acc is missing",
            )

        # ---------------- DOWNLOAD ----------------
        download_started = time.perf_counter()
        try:
            with span(f"snorlax.{_Stage.DOWNLOAD.value}"):
                downloaded = await asyncio.to_thread(
                    self._downloader.download,
                    parent_dir_url=event.ftp_path,
                    asm_acc=asm_acc,
                    dest=scratch / "genome.fna.gz",
                )
            self._record_stage(
                _Stage.DOWNLOAD,
                success=True,
                duration=time.perf_counter() - download_started,
            )
        except GenomeNotFoundError as exc:
            self._record_stage(
                _Stage.DOWNLOAD,
                success=False,
                duration=time.perf_counter() - download_started,
            )
            return await self._handle_permanent(
                event, category=FailureCategory.NOT_FOUND, reason=str(exc)
            )
        except GenomeTooLargeError as exc:
            self._record_stage(
                _Stage.DOWNLOAD,
                success=False,
                duration=time.perf_counter() - download_started,
            )
            return await self._handle_permanent(
                event,
                category=FailureCategory.DOWNLOAD_TOO_LARGE,
                reason=str(exc),
            )
        except (GenomeTooSmallError, GenomeChecksumError, DownloadError) as exc:
            # The downloader already exhausted its tenacity retry budget;
            # leave the offset alone and let the stream redeliver. NCBI's
            # nightly publish window resolves itself in minutes.
            self._record_stage(
                _Stage.DOWNLOAD,
                success=False,
                duration=time.perf_counter() - download_started,
            )
            return ProcessingResult(
                outcome=Outcome.TRANSIENT_RETRY,
                category=FailureCategory.DOWNLOAD_TRANSIENT_EXHAUSTED,
                reason=str(exc),
            )
        self._metrics.download_bytes.record(downloaded.size_bytes)

        # ---------------- VALIDATE (input FASTA) ----------------
        try:
            with span("snorlax.validate_genome"):
                await asyncio.to_thread(validate_genome_fasta, downloaded.local_path)
        except FastaValidationError as exc:
            return await self._handle_permanent(
                event,
                category=FailureCategory.VALIDATION_FAILED,
                reason=str(exc),
            )

        # ---------------- PRODIGAL ----------------
        try:
            with span("snorlax.prodigal"):
                started = time.perf_counter()
                prodigal_result = await self._prodigal.run(
                    genome_fasta_gz=downloaded.local_path,
                    work_dir=scratch / "prodigal",
                )
                self._record_stage(
                    _Stage.PRODIGAL,
                    success=True,
                    duration=time.perf_counter() - started,
                )
        except ProdigalTimeoutError as exc:
            return await self._handle_dlq(
                event,
                category=FailureCategory.PRODIGAL_TIMEOUT,
                reason=str(exc),
            )
        except ProdigalNonZeroExitError as exc:
            return await self._handle_dlq(
                event,
                category=FailureCategory.PRODIGAL_FAILED,
                reason=f"exit={exc.exit_code} stderr={exc.stderr[:500]}",
            )

        # ---------------- VALIDATE (output FASTA) ----------------
        try:
            with span("snorlax.validate_proteins"):
                stats = await asyncio.to_thread(
                    validate_protein_fasta, prodigal_result.protein_fasta_path
                )
        except FastaValidationError as exc:
            # Prodigal exited 0 but produced gibberish. Almost always
            # follows a sneaky corrupt input that passed our DNA-side
            # check. DLQ for inspection.
            return await self._handle_dlq(
                event,
                category=FailureCategory.PRODIGAL_FAILED,
                reason=f"protein-fasta validation: {exc}",
            )

        protein_count = stats.record_count
        # Cross-check Prodigal's stderr-derived count if we ever expose
        # it; for now we trust the header count from the validator.
        _ = count_proteins  # keep import alive for unit tests

        # ---------------- UPLOAD ----------------
        try:
            with span("snorlax.upload"):
                started = time.perf_counter()
                uploaded = await asyncio.to_thread(
                    self._uploader.upload,
                    accession=event.accession,
                    version=event.version,
                    protein_fasta_path=prodigal_result.protein_fasta_path,
                    protein_count=protein_count,
                    source_md5=downloaded.md5_hex,
                )
                self._record_stage(
                    _Stage.UPLOAD,
                    success=True,
                    duration=time.perf_counter() - started,
                )
        except UploadError as exc:
            # The kanto-commons storage client already did transient
            # retry; what reaches here is final. DLQ the message: the
            # protein FASTA never landed in OS, so Modal can't run.
            return await self._handle_dlq(
                event,
                category=FailureCategory.UPLOAD_FAILED,
                reason=str(exc),
            )

        # ---------------- MEW: PROTEINS_READY ----------------
        try:
            with span("snorlax.mew_proteins_ready"):
                await self._update_mew_proteins_ready(event, protein_count)
        except _MewWriteError as exc:
            return await self._handle_dlq(
                event,
                category=FailureCategory.MEW_UPDATE_FAILED,
                reason=f"PROTEINS_READY update: {exc}",
            )

        # ---------------- MODAL SPAWN ----------------
        proteins_ready = ProteinsReady(
            accession=event.accession,
            version=event.version,
            os_key=uploaded.key,
            protein_count=protein_count,
            produced_at=datetime.now(UTC),
        )
        try:
            with span("snorlax.modal_spawn"):
                started = time.perf_counter()
                spawned = await asyncio.to_thread(self._modal.spawn, payload=proteins_ready)
                self._record_stage(
                    _Stage.MODAL_SPAWN,
                    success=True,
                    duration=time.perf_counter() - started,
                )
        except ModalSpawnError as exc:
            return await self._handle_dlq(
                event,
                category=FailureCategory.MODAL_SPAWN_FAILED,
                reason=str(exc),
            )

        # ---------------- MEW: record call id ----------------
        try:
            with span("snorlax.mew_record_modal_call"):
                await self._record_modal_call_id(event, spawned)
        except _MewWriteError as exc:
            # Mew is out of sync with reality (we did spawn). DLQ +
            # alert: a human needs to backfill the call id manually.
            return await self._handle_dlq(
                event,
                category=FailureCategory.MEW_UPDATE_FAILED,
                reason=f"modal_call_id update: {exc}",
                modal_call_id=spawned.call_id,
            )

        self._metrics.protein_count.record(protein_count)
        self._metrics.events_succeeded.add(1)
        logger.info(
            "snorlax.pipeline: success accession=%s version=%d proteins=%d modal_call_id=%s",
            event.accession,
            event.version,
            protein_count,
            spawned.call_id,
        )
        return ProcessingResult(
            outcome=Outcome.SUCCESS,
            modal_call_id=spawned.call_id,
            protein_count=protein_count,
            os_key=uploaded.key,
        )

    # -------------------- terminal handlers --------------------

    async def _handle_permanent(
        self,
        event: IsolateDiscovered,
        *,
        category: FailureCategory,
        reason: str,
    ) -> ProcessingResult:
        """Record QC_FAILED in Mew and commit the offset. No DLQ."""
        logger.warning(
            "snorlax.pipeline: permanent failure accession=%s category=%s reason=%s",
            event.accession,
            category.value,
            reason,
        )
        self._metrics.events_failed.add(1, {"category": category.value})
        try:
            await self._mark_qc_failed(event, category=category, reason=reason)
        except _MewWriteError:
            # Couldn't even record QC_FAILED. DLQ instead so we don't
            # silently drop the message.
            logger.exception(
                "snorlax.pipeline: failed to write QC_FAILED for %s; routing to DLQ",
                event.accession,
            )
            return ProcessingResult(
                outcome=Outcome.DLQ,
                category=FailureCategory.MEW_UPDATE_FAILED,
                reason=f"could not record {category.value}: {reason}",
            )
        return ProcessingResult(
            outcome=Outcome.PERMANENT_FAILURE,
            category=category,
            reason=reason,
        )

    async def _handle_dlq(
        self,
        event: IsolateDiscovered,
        *,
        category: FailureCategory,
        reason: str,
        modal_call_id: str | None = None,
    ) -> ProcessingResult:
        """Mark Mew QC_FAILED then route to DLQ."""
        logger.error(
            "snorlax.pipeline: DLQ accession=%s category=%s reason=%s",
            event.accession,
            category.value,
            reason,
        )
        self._metrics.events_dlqed.add(1, {"category": category.value})
        try:
            await self._mark_qc_failed(event, category=category, reason=reason)
        except _MewWriteError:
            logger.exception(
                "snorlax.pipeline: failed to write QC_FAILED for %s on DLQ path",
                event.accession,
            )
        return ProcessingResult(
            outcome=Outcome.DLQ,
            category=category,
            reason=reason,
            modal_call_id=modal_call_id,
        )

    # -------------------- Mew writes --------------------

    async def _ensure_isolate_row(self, event: IsolateDiscovered) -> None:
        """Insert (or update on retry) the isolate row with status=DISCOVERED.

        Status transitions later in the pipeline use ``update_status``
        which is a no-op if the row doesn't exist; the upsert here is
        the gate that makes those updates meaningful. The row carries
        the discovery timestamp plus the source-side metadata so even
        on a Snorlax crash the operator can see *something* about each
        accession.
        """
        row = IsolateRow(
            accession=event.accession,
            version=event.version,
            organism=event.organism,
            source=event.source,
            status=IsolateStatus.DISCOVERED,
            discovered_at=event.discovered_at,
            raw_metadata=dict(event.metadata) if event.metadata else None,
        )

        async def _do() -> None:
            async with self._mew.connection() as conn:
                await self._isolates.upsert(conn, row)

        await self._mew_write(description=f"ensure_row:{event.accession}", do=_do)

    async def _mark_qc_failed(
        self,
        event: IsolateDiscovered,
        *,
        category: FailureCategory,
        reason: str,
    ) -> None:
        """Best-effort QC_FAILED record with the category in qc_failure_reason."""
        payload = f"{category.value}: {reason}"[:500]

        async def _do() -> None:
            async with self._mew.connection() as conn:
                await self._isolates.update_status(
                    conn,
                    accession=event.accession,
                    status=IsolateStatus.QC_FAILED,
                    qc_failure_reason=payload,
                )

        await self._mew_write(description=f"qc_failed:{event.accession}", do=_do)

    async def _update_mew_proteins_ready(
        self, event: IsolateDiscovered, protein_count: int
    ) -> None:
        _ = protein_count  # carried separately to Modal; not stored in Mew yet

        async def _do() -> None:
            async with self._mew.connection() as conn:
                await self._isolates.update_status(
                    conn,
                    accession=event.accession,
                    status=IsolateStatus.PROTEINS_READY,
                )

        await self._mew_write(description=f"proteins_ready:{event.accession}", do=_do)

    async def _record_modal_call_id(
        self,
        event: IsolateDiscovered,
        spawned: SpawnedCall,
    ) -> None:
        async def _do() -> None:
            async with self._mew.connection() as conn:
                await self._isolates.update_status(
                    conn,
                    accession=event.accession,
                    status=IsolateStatus.PROTEINS_READY,
                    modal_call_id=spawned.call_id,
                )

        await self._mew_write(description=f"modal_call_id:{event.accession}", do=_do)

    async def _mew_write(
        self,
        *,
        description: str,
        do: Callable[[], Awaitable[None]],
    ) -> None:
        """Apply ``do()`` against Mew with bounded retries.

        Hand-rolled rather than tenacity so the test surface stays
        simple: we deterministically attempt ``mew_max_attempts`` times
        with a short exponential delay between attempts and raise
        :class:`_MewWriteError` if every attempt fails.
        """
        last_exc: BaseException | None = None
        delay = 0.5
        for attempt in range(1, self._mew_max_attempts + 1):
            try:
                await do()
                return
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "snorlax.pipeline: mew write %s attempt %d/%d failed: %s",
                    description,
                    attempt,
                    self._mew_max_attempts,
                    exc,
                )
                if attempt == self._mew_max_attempts:
                    break
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, 10.0)
        raise _MewWriteError(f"{description}: {last_exc}")

    # -------------------- metrics helpers --------------------

    def _record_stage(self, stage: _Stage, *, success: bool, duration: float) -> None:
        self._metrics.stage_duration.record(duration, {"stage": stage.value})
        if not success:
            self._metrics.stage_errors.add(1, {"stage": stage.value})

    def _record_outcome(self, result: ProcessingResult) -> None:
        # success counter is bumped inline at end of happy path; failures
        # were bumped in the handlers. Trace span gets the outcome.
        current = trace.get_current_span()
        current.set_attribute("snorlax.outcome", result.outcome.value)
        if result.category is not None:
            current.set_attribute("snorlax.failure_category", result.category.value)
        if result.modal_call_id is not None:
            current.set_attribute("snorlax.modal_call_id", result.modal_call_id)

    # -------------------- scratch --------------------

    def _scratch_dir_for(self, event: IsolateDiscovered) -> Path:
        d = self._work_dir / f"{event.accession}-{event.version}"
        d.mkdir(parents=True, exist_ok=True)
        return d


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _MewWriteError(Exception):
    """Internal marker: the Mew retry budget was exhausted."""


def _purge_dir(path: Path) -> None:
    """Best-effort recursive removal. Errors logged, never raised."""
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # pragma: no cover — defensive
        logger.exception("snorlax.pipeline: failed to purge %s", path)


# Public surface.
__all__ = [
    "FailureCategory",
    "Outcome",
    "Pipeline",
    "ProcessingResult",
]


# Keep diagnostic imports referenced for mypy.
_ = ProdigalBinaryMissingError
