"""Prodigal subprocess management.

Prodigal is a small C binary; we invoke it as a subprocess from the
same pod that did the download. Three concerns this module owns:

1. **Input handling.** Prodigal reads plain FASTA on a path; the
   downloaded file is gzipped. We decompress to a sibling path before
   invoking and clean up after.
2. **Timeout.** Prodigal usually finishes in tens of seconds, but a
   pathological / corrupt input can hang. We use a hard wall-clock
   limit (``prodigal_timeout_seconds``) and kill the process if it
   exceeds it. Killed processes are reported as a distinct error so
   the pipeline can DLQ them.
3. **Diagnostics.** Prodigal writes useful debug info to stderr —
   warnings about short contigs, alternative start codons, etc. We
   capture stderr in full (it's small; usually <10 KB) and surface it
   on the error path.
"""

from __future__ import annotations

import asyncio
import gzip
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProdigalError(Exception):
    """Base class for Prodigal failures."""


class ProdigalBinaryMissingError(ProdigalError):
    """``prodigal`` is not on ``$PATH`` or the configured path is wrong.

    This is a deployment misconfig, not a per-isolate failure. The
    pipeline surfaces it as a fatal startup error rather than DLQing
    the message — every subsequent isolate would fail the same way.
    """


class ProdigalTimeoutError(ProdigalError):
    """Prodigal exceeded the configured wall-clock limit.

    Treated as a permanent per-isolate failure (DLQ): a sane
    bacterial genome finishes in seconds, so a timeout always
    indicates corrupt or pathological input.
    """


class ProdigalNonZeroExitError(ProdigalError):
    """Prodigal exited non-zero.

    The ``stderr`` attribute carries the captured diagnostics so the
    pipeline can log them and the operator can copy-paste them into a
    bug report.
    """

    def __init__(self, *, exit_code: int, stderr: str) -> None:
        super().__init__(f"prodigal exited {exit_code}: {stderr.strip()[:500]}")
        self.exit_code = exit_code
        self.stderr = stderr


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProdigalResult:
    """Outcome of a successful Prodigal run."""

    protein_fasta_path: Path
    stderr: str
    duration_seconds: float


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class ProdigalRunner:
    """Invokes the Prodigal binary with the bacterial single-genome preset.

    Stateless and cheap to construct. We make it a class so tests can
    swap it for a fake without monkeypatching module-level functions.
    """

    def __init__(
        self,
        *,
        binary: str,
        mode: str,
        timeout_seconds: float,
    ) -> None:
        self._binary = binary
        self._mode = mode
        self._timeout_seconds = timeout_seconds

    async def run(
        self,
        *,
        genome_fasta_gz: Path,
        work_dir: Path,
    ) -> ProdigalResult:
        """Decompress and run Prodigal; return the protein FASTA path.

        Both intermediate (decompressed genome) and output files land
        in ``work_dir``. Callers are expected to remove the directory
        after they've uploaded the result — we don't take ownership of
        cleanup here so the failure-handling code path retains access
        to the output for debugging.
        """
        if shutil.which(self._binary) is None and not Path(self._binary).exists():
            raise ProdigalBinaryMissingError(self._binary)

        work_dir.mkdir(parents=True, exist_ok=True)
        decompressed = work_dir / "genome.fna"
        protein_out = work_dir / "proteins.faa"

        _decompress(genome_fasta_gz, decompressed)

        cmd = [
            self._binary,
            "-i",
            str(decompressed),
            "-a",
            str(protein_out),
            "-p",
            self._mode,
            # ``-o`` is required even when the gene-call output isn't
            # useful to us; route it to /dev/null to keep the FS clean.
            "-o",
            "/dev/null",
            # Quieter logging; we still capture stderr for diagnostics.
            "-q",
        ]

        logger.info(
            "snorlax.prodigal: invoking %s on %s",
            self._binary,
            decompressed.name,
        )

        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ProdigalBinaryMissingError(self._binary) from exc

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                process.communicate(),
                timeout=self._timeout_seconds,
            )
        except TimeoutError as exc:
            process.kill()
            # ``wait`` so we don't leave a zombie if the kill is slow.
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:  # pragma: no cover — defensive
                logger.warning("snorlax.prodigal: kill did not reap subprocess")
            raise ProdigalTimeoutError(
                f"prodigal exceeded {self._timeout_seconds}s on {genome_fasta_gz.name}"
            ) from exc

        duration = loop.time() - started
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")
        _ = stdout_b  # Prodigal writes nothing meaningful to stdout in -q

        if process.returncode != 0:
            raise ProdigalNonZeroExitError(
                exit_code=process.returncode or -1,
                stderr=stderr,
            )

        if not protein_out.exists() or protein_out.stat().st_size == 0:
            raise ProdigalNonZeroExitError(
                exit_code=0,
                stderr=f"prodigal exited 0 but produced no proteins; stderr={stderr}",
            )

        return ProdigalResult(
            protein_fasta_path=protein_out,
            stderr=stderr,
            duration_seconds=duration,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decompress(src_gz: Path, dest: Path) -> None:
    """Gunzip ``src_gz`` to ``dest``. Pure helper for testability.

    Uses ``shutil.copyfileobj`` over a gzip stream so memory stays
    bounded for genomes up to a few hundred MB (well above the
    bacterial range, but cheap insurance).
    """
    with gzip.open(src_gz, "rb") as src_fp, dest.open("wb") as dst_fp:
        shutil.copyfileobj(src_fp, dst_fp, length=1024 * 1024)


__all__ = [
    "ProdigalBinaryMissingError",
    "ProdigalError",
    "ProdigalNonZeroExitError",
    "ProdigalResult",
    "ProdigalRunner",
    "ProdigalTimeoutError",
]
