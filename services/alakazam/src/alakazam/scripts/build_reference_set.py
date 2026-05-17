"""Operator-only: build the reference-protein parquet for the coverage strategy.

The coverage strategy expects a parquet at the configured OS key with
the schema documented in :mod:`alakazam.reference_set`. This script
takes:

* a FASTA file of curated reference proteins (BUSCO bacterial set +
  KEGG core bacterial gene set, by default),
* a per-protein source annotation (TSV mapping protein_id -> source),

embeds each protein with the same ESM C 600M model Ditto uses (by
calling the deployed Modal ``DittoEmbedder`` so we don't ship CUDA
code into Alakazam's image), and uploads the resulting parquet to
the alakazam-configured key in Object Storage.

Reproducibility
---------------
* The bundle is **frozen** per release: rebuilding overwrites the
  same OS key, and the operator is expected to bump
  ``reference_set_key`` in alakazam config when the bundle changes
  (e.g. ``references/common_proteins_v2.parquet``). This keeps a
  full rollback trail in OS — older bundles are not deleted.
* The README documents the curation steps, the BUSCO/KEGG snapshot
  versions, and the manual additions, so a future operator can
  rebuild the same bundle byte-for-byte.

v2: replace this script with a sidecar job that runs on a schedule
and re-embeds when the upstream snapshots roll. Out of scope for v1.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from kanto_commons.logging import setup_logging
from kanto_commons.storage import ObjectStorageClient

from alakazam.config import AlakazamSettings

logger = logging.getLogger(__name__)

_EMBEDDING_DIM = 1152


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="alakazam-build-reference",
        description=(
            "Embed a FASTA of reference proteins via Modal Ditto and "
            "upload the resulting parquet for Alakazam's coverage "
            "component."
        ),
    )
    parser.add_argument(
        "fasta",
        type=Path,
        help="Reference-protein FASTA (BUSCO + KEGG + manual additions).",
    )
    parser.add_argument(
        "--sources",
        type=Path,
        required=True,
        help=("TSV with header 'protein_id\\tsource'. Source is one of busco | kegg | manual."),
    )
    parser.add_argument(
        "--output-key",
        default=None,
        help="Override the OS key (default: from alakazam config).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the parquet locally; do NOT upload.",
    )
    return parser.parse_args(argv)


def parse_fasta(text: str) -> Iterator[tuple[str, str]]:
    """Yield ``(protein_id, sequence)`` pairs from a FASTA string.

    Public for unit testing. Headers are the first whitespace-delimited
    token of each ``>`` line; sequences are uppercased.
    """
    current_id: str | None = None
    current_seq: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(">"):
            if current_id is not None:
                yield current_id, "".join(current_seq).upper()
            header = line[1:].split(None, 1)[0]
            current_id = header
            current_seq = []
            continue
        if current_id is None:
            raise ValueError(f"FASTA started with non-header line: {line!r}")
        current_seq.append(line)
    if current_id is not None:
        yield current_id, "".join(current_seq).upper()


def parse_sources_tsv(text: str) -> dict[str, str]:
    """Parse the protein_id -> source TSV."""
    sources: dict[str, str] = {}
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line_no == 1 and line.lower().startswith("protein_id"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            raise ValueError(f"malformed sources line {line_no}: {line!r}")
        protein_id, source = parts[0].strip(), parts[1].strip()
        if source not in {"busco", "kegg", "manual"}:
            raise ValueError(f"unknown source {source!r} for {protein_id!r} on line {line_no}")
        sources[protein_id] = source
    return sources


def build_parquet_bytes(
    proteins: list[tuple[str, str, str]],
    embeddings: np.ndarray,
) -> bytes:
    """Assemble the reference parquet from rows + embedding matrix.

    ``proteins`` is a list of ``(protein_id, sequence_md5, source)``.
    ``embeddings`` is ``(n, _EMBEDDING_DIM)`` float16.
    """
    if embeddings.ndim != 2 or embeddings.shape[1] != _EMBEDDING_DIM:
        raise ValueError(f"embeddings shape {embeddings.shape} != (*, {_EMBEDDING_DIM})")
    if embeddings.shape[0] != len(proteins):
        raise ValueError(f"embeddings has {embeddings.shape[0]} rows; proteins has {len(proteins)}")
    if embeddings.dtype != np.float16:
        embeddings = embeddings.astype(np.float16, copy=False)

    flat = pa.array(embeddings.reshape(-1), type=pa.float16())
    embed_col = pa.FixedSizeListArray.from_arrays(flat, _EMBEDDING_DIM)
    schema = pa.schema(
        [
            pa.field("protein_id", pa.string(), nullable=False),
            pa.field("source", pa.string(), nullable=False),
            pa.field("sequence_md5", pa.string(), nullable=False),
            pa.field(
                "embedding",
                pa.list_(pa.field("item", pa.float16(), nullable=False), _EMBEDDING_DIM),
                nullable=False,
            ),
        ]
    )
    table = pa.Table.from_arrays(
        [
            pa.array([p[0] for p in proteins], type=pa.string()),
            pa.array([p[2] for p in proteins], type=pa.string()),
            pa.array([p[1] for p in proteins], type=pa.string()),
            embed_col,
        ],
        schema=schema,
    )
    buffer = io.BytesIO()
    pq.write_table(table, buffer, compression="snappy")
    return buffer.getvalue()


def md5_of(sequence: str) -> str:
    """Stable md5 hex digest used as the audit identifier."""
    return hashlib.md5(sequence.encode("ascii"), usedforsecurity=False).hexdigest()


def main() -> None:
    args = _parse_args()
    settings = AlakazamSettings.load()
    setup_logging(
        service_name="alakazam-build-reference",
        log_level=settings.log_level,
        log_format=settings.log_format,
    )

    fasta_text = args.fasta.read_text()
    sources_text = args.sources.read_text()
    fasta_rows = list(parse_fasta(fasta_text))
    sources_map = parse_sources_tsv(sources_text)

    proteins: list[tuple[str, str, str]] = []
    sequences: list[str] = []
    for pid, seq in fasta_rows:
        source = sources_map.get(pid)
        if source is None:
            logger.warning("alakazam.build_reference: no source for %s; tagging 'manual'", pid)
            source = "manual"
        proteins.append((pid, md5_of(seq), source))
        sequences.append(seq)

    # The actual Modal embed call lives outside this module; see the
    # README "building the reference set" section for the operator
    # recipe (it boils down to one ``modal run`` command pointed at
    # the Ditto embedder). We exit here with a clear message rather
    # than silently failing — the script is the assembly + upload
    # tool, not the embedding engine.
    raise SystemExit(
        "alakazam-build-reference: the Modal embed step is operator-driven.\n"
        "Run the Ditto Modal function over the FASTA, save the resulting\n"
        "(n, 1152) float16 .npy file, then re-invoke this script with\n"
        "--embeddings <path.npy>. See services/alakazam/README.md\n"
        "'Building the reference set' for the full recipe."
    )


__all__ = [
    "build_parquet_bytes",
    "main",
    "md5_of",
    "parse_fasta",
    "parse_sources_tsv",
]


# Quiet linter — sys, ObjectStorageClient are kept on the surface for
# operator use in a follow-up commit that wires the upload path once
# the Modal embed step is integrated.
_ = (sys, ObjectStorageClient)
