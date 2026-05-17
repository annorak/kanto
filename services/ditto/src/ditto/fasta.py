"""FASTA parsing for Snorlax's gzipped protein output.

Snorlax writes Prodigal's predicted-protein FASTA (``.faa.gz``) to
Blob Storage under ``{accession}/{version}.faa.gz``. This module
decompresses, splits into records, and applies the truncation policy.

Truncation policy
-----------------
Proteins longer than ``max_sequence_length`` (default 2048, per the
ESM C 600M model card) are *truncated*, never skipped. Both the
biological length (pre-truncation) and the post-truncation length
are kept on the :class:`ParsedProtein` so callers can log + report
both. Skipping was rejected because it would silently drop biological
signal for the few long proteins (some kinases, secreted adhesins).

MD5 of sequence
---------------
Computed on the *pre-truncation* sequence: the dedupe semantic is
"did we already see this exact protein", which is about biology, not
the inference input.
"""

from __future__ import annotations

import gzip
import hashlib
from dataclasses import dataclass

from ditto.batching import Protein
from ditto.errors import FastaParseError


@dataclass(frozen=True)
class ParsedProtein:
    """One protein parsed from FASTA, pre-batcher."""

    id: str
    sequence_truncated: str
    truncated_length: int
    original_length: int
    sequence_md5: str

    @property
    def was_truncated(self) -> bool:
        return self.original_length > self.truncated_length

    def to_batch_protein(self) -> Protein:
        return Protein(
            id=self.id,
            sequence=self.sequence_truncated,
            sequence_length=self.truncated_length,
            original_length=self.original_length,
        )


def parse_fasta_bytes(
    data: bytes,
    *,
    max_sequence_length: int,
) -> list[ParsedProtein]:
    """Decompress + parse ``data`` into proteins.

    Accepts gzipped or plain FASTA — the gzip magic ``1f 8b`` is
    detected and either path is taken. Plain input is accepted because
    it makes tests trivial; the production path is always gzipped.
    """
    if not data:
        raise FastaParseError("empty FASTA blob")
    if data[:2] == b"\x1f\x8b":
        try:
            text = gzip.decompress(data).decode("ascii", errors="strict")
        except (OSError, EOFError, UnicodeDecodeError) as exc:
            raise FastaParseError(f"failed to decompress gzipped FASTA: {exc}") from exc
    else:
        try:
            text = data.decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise FastaParseError(f"non-ASCII FASTA content: {exc}") from exc
    return _parse_fasta_text(text, max_sequence_length=max_sequence_length)


def _md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("ascii")).hexdigest()


def _parse_fasta_text(text: str, *, max_sequence_length: int) -> list[ParsedProtein]:
    proteins: list[ParsedProtein] = []
    current_id: str | None = None
    current_chunks: list[str] = []

    def flush() -> None:
        if current_id is None:
            return
        original_seq = "".join(current_chunks).strip()
        if not original_seq:
            raise FastaParseError(f"protein {current_id!r} has empty sequence")
        truncated = original_seq[:max_sequence_length]
        proteins.append(
            ParsedProtein(
                id=current_id,
                sequence_truncated=truncated,
                truncated_length=len(truncated),
                original_length=len(original_seq),
                sequence_md5=_md5_hex(original_seq),
            )
        )

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        if line.startswith(">"):
            flush()
            # Prodigal headers: ">contig_idx # start # end # strand # ...".
            # The protein id is the first whitespace-separated token.
            header = line[1:].strip()
            if not header:
                raise FastaParseError("FASTA header without identifier")
            current_id = header.split()[0]
            current_chunks = []
        else:
            if current_id is None:
                raise FastaParseError("FASTA sequence line before any header")
            current_chunks.append(line)
    flush()

    if not proteins:
        raise FastaParseError("FASTA contained no protein records")
    return proteins


__all__ = ["ParsedProtein", "parse_fasta_bytes"]
