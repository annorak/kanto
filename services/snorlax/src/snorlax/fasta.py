"""FASTA validation and tiny helpers.

Two distinct checks live here:

* :func:`validate_genome_fasta` — runs *before* Prodigal. Ensures the
  bytes downloaded from NCBI look like a DNA FASTA (header lines start
  with ``>``, sequence is restricted to IUPAC nucleotide letters, the
  file isn't suspiciously short). Catches "NCBI served us an HTML
  error page" and similar.

* :func:`count_proteins` — counts the number of ``>`` headers in the
  protein FASTA produced by Prodigal. Used to fill the
  ``protein_count`` field on the ``ProteinsReady`` payload.

Both functions stream their input rather than slurping it, so a
maliciously large file can't OOM the pod.
"""

from __future__ import annotations

import gzip
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# IUPAC nucleotide codes plus '-' and '.' for gaps and stop characters
# we tolerate. Lower-cased on lookup so the set itself stays compact.
# We accept the standard ambiguous codes (M, R, W, S, Y, K, V, H, D, B,
# N) — Prodigal handles them, and NCBI sometimes emits ambiguous bases
# for repetitive or low-coverage regions.
_DNA_ALPHABET = frozenset("ACGTUMRWSYKVHDBNX-.acgtumrwsykvhdbnx")

# Protein FASTA alphabet — the 20 amino acids, plus ``*`` for stop
# codons (Prodigal emits these), ``X`` for unknown, and the small
# ambiguity codes (B, Z, J).
_PROTEIN_ALPHABET = frozenset("ACDEFGHIKLMNPQRSTVWYBZJX*acdefghiklmnpqrstvwybzjx")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FastaValidationError(Exception):
    """Raised when a FASTA fails one of the structural checks.

    The pipeline catches this and routes the isolate to QC_FAILED in
    Mew with the validation reason; it does not retry, because a bad
    file from NCBI is unlikely to fix itself on the next pull.
    """


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FastaStats:
    """Summary stats produced as a side effect of validation."""

    record_count: int
    total_residues: int


def validate_genome_fasta(path: Path) -> FastaStats:
    """Validate ``path`` as a gzipped or plain DNA FASTA.

    Returns :class:`FastaStats` on success; raises
    :class:`FastaValidationError` with a human-readable reason otherwise.

    Streamed line-by-line so memory stays flat regardless of input
    size. The first non-empty line MUST be a header (``>...``) — that
    one rule alone rules out almost every "served us an HTML page"
    accident.
    """
    record_count = 0
    total_residues = 0
    saw_first_line = False

    for line in _iter_text_lines(path):
        if not saw_first_line:
            if not line.startswith(">"):
                raise FastaValidationError(f"{path}: first non-empty line is not a FASTA header")
            saw_first_line = True

        if line.startswith(">"):
            record_count += 1
            continue

        bad_idx = _first_invalid_index(line, _DNA_ALPHABET)
        if bad_idx is not None:
            raise FastaValidationError(
                f"{path}: invalid nucleotide character {line[bad_idx]!r} in record {record_count}"
            )
        total_residues += len(line)

    if record_count == 0:
        raise FastaValidationError(f"{path}: no FASTA records found")
    if total_residues == 0:
        raise FastaValidationError(f"{path}: all records were empty")

    return FastaStats(record_count=record_count, total_residues=total_residues)


def count_proteins(path: Path) -> int:
    """Count ``>``-prefixed header lines in a (possibly gzipped) protein FASTA.

    Used to populate the ``protein_count`` field on the
    ``ProteinsReady`` payload. Tolerates trailing whitespace and CR/LF.
    """
    count = 0
    for line in _iter_text_lines(path):
        if line.startswith(">"):
            count += 1
    return count


def validate_protein_fasta(path: Path) -> FastaStats:
    """Validate a Prodigal protein FASTA.

    Same shape as :func:`validate_genome_fasta` but with the protein
    alphabet. Mostly catches "Prodigal exited 0 but wrote a corrupt
    file" — rare, but it has happened on truncated genomes.
    """
    record_count = 0
    total_residues = 0
    saw_first_line = False

    for line in _iter_text_lines(path):
        if not saw_first_line:
            if not line.startswith(">"):
                raise FastaValidationError(f"{path}: first non-empty line is not a FASTA header")
            saw_first_line = True

        if line.startswith(">"):
            record_count += 1
            continue

        bad_idx = _first_invalid_index(line, _PROTEIN_ALPHABET)
        if bad_idx is not None:
            raise FastaValidationError(
                f"{path}: invalid amino-acid character {line[bad_idx]!r} in record {record_count}"
            )
        total_residues += len(line)

    if record_count == 0:
        raise FastaValidationError(f"{path}: no FASTA records found")
    return FastaStats(record_count=record_count, total_residues=total_residues)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _iter_text_lines(path: Path) -> Iterator[str]:
    """Yield non-empty stripped lines from ``path``.

    Transparently opens gzipped files (``.gz``). We don't sniff the
    magic bytes — NCBI uses ``.fna.gz`` and ``.faa.gz`` consistently
    and Prodigal output is plain text. Keeping the dispatch
    extension-based avoids a bug class where a corrupt file's first
    bytes happen to look like gzip.
    """
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as fp:
        for raw in fp:
            stripped = raw.rstrip("\r\n").strip()
            if not stripped:
                continue
            yield stripped


def _first_invalid_index(seq: str, alphabet: frozenset[str]) -> int | None:
    """Index of the first character not in ``alphabet``; ``None`` if all valid."""
    for idx, ch in enumerate(seq):
        if ch not in alphabet:
            return idx
    return None


__all__ = [
    "FastaStats",
    "FastaValidationError",
    "count_proteins",
    "validate_genome_fasta",
    "validate_protein_fasta",
]
