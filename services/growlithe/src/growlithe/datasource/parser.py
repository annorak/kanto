"""Tolerant parser for NCBI Pathogen Detection metadata TSVs.

NCBI publishes a wide TSV with many columns; the exact set changes
over time as they add fields. Growlithe depends on a small core
subset, validates each row, and routes unparseable rows to a counter
rather than crashing — partial data is more useful than no data
when one row is malformed.

Columns we depend on
--------------------

* ``target_acc`` — the PDT accession with version suffix (e.g.
  ``PDT000000123.4``). Required.
* ``asm_acc`` — GenBank assembly accession (e.g. ``GCA_004104335.1``).
  Required so Snorlax has something to fetch.
* ``target_creation_date`` — date NCBI created the row (ISO). Used
  for ``discovered_at`` when present; falls back to event-time.

Columns we forward into ``IsolateDiscovered.metadata`` when present:

* ``scientific_name``
* ``serovar``
* ``collection_date``
* ``geo_loc_name``
* ``host``
* ``isolation_source``
* ``bioproject_acc``
* ``biosample_acc``
* ``wgs_master_acc``

Any other columns NCBI adds in the future are tolerated and
ignored. Rows missing required fields are counted via
:class:`ParseStats` and dropped with a logged warning.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Column constants
# ---------------------------------------------------------------------------


REQUIRED_COLUMNS: tuple[str, ...] = (
    "target_acc",
    "asm_acc",
)

# Columns we lift verbatim into IsolateDiscovered.metadata when present
# and non-NULL. Keep this list small; the metadata dict has a 1 KB
# practical ceiling.
METADATA_PASSTHROUGH_COLUMNS: tuple[str, ...] = (
    "scientific_name",
    "serovar",
    "collection_date",
    "geo_loc_name",
    "host",
    "isolation_source",
    "bioproject_acc",
    "biosample_acc",
    "wgs_master_acc",
)

# NCBI represents nulls as the literal "NULL" string in the TSV; treat
# them as missing.
_NULL_TOKEN = "NULL"

# target_acc looks like ``PDT000000123.4`` — capture the integer
# version suffix for the IsolateDiscovered.version field.
_VERSION_RE = re.compile(r"\.(\d+)$")


# ---------------------------------------------------------------------------
# Parsed row + stats
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedRow:
    """One isolate's worth of parsed metadata.

    Intentionally a simple value object; the pipeline turns this into
    an :class:`IsolateDiscovered` plus the per-row ftp_path.

    ``target_acc`` is the raw NCBI identifier including the version
    suffix (e.g. ``PDT000000123.4``) and is what the snapshot diff and
    exceptions list key by. ``accession`` is the normalized base
    identifier without the suffix (e.g. ``PDT000000123``); ``version``
    is the parsed integer. Downstream services consume
    ``(accession, version)`` so that a version bump never collides
    with the prior version in Mew.
    """

    target_acc: str
    accession: str
    version: int
    asm_acc: str
    target_creation_date: datetime | None
    extras: dict[str, str] = field(default_factory=dict)


@dataclass
class ParseStats:
    """Counters surfaced to the pipeline / Prometheus.

    The pipeline reports these as gauges per poll cycle so oncall
    notices a spike of malformed rows (NCBI schema change) or a spike
    of QC-filtered rows (a particular organism's run failed).
    """

    total_rows: int = 0
    parsed_rows: int = 0
    missing_required: int = 0
    bad_version: int = 0
    qc_filtered: int = 0


# ---------------------------------------------------------------------------
# Parsing entry points
# ---------------------------------------------------------------------------


def read_exceptions(exceptions_tsv: bytes | None) -> frozenset[str]:
    """Return the set of ``target_acc`` values flagged in the exceptions TSV.

    NCBI's exceptions TSV lists isolates that failed their internal
    QC; we drop these before emitting events because their FASTA
    files are typically truncated or missing and processing them
    downstream wastes compute.

    Returns an empty set when no exceptions file is provided.
    """
    if not exceptions_tsv:
        return frozenset()
    reader = _open_tsv(exceptions_tsv)
    return frozenset(
        row["target_acc"]
        for row in reader
        if "target_acc" in row and row["target_acc"] != _NULL_TOKEN
    )


def iter_rows(metadata_tsv: bytes) -> Iterator[dict[str, str]]:
    """Yield raw row dicts from a metadata TSV. No filtering."""
    yield from _open_tsv(metadata_tsv)


def parse_metadata(
    metadata_tsv: bytes,
    *,
    exceptions: frozenset[str] = frozenset(),
    skip_target_accs: frozenset[str] = frozenset(),
) -> tuple[list[ParsedRow], ParseStats]:
    """Parse a metadata TSV into ``ParsedRow`` objects.

    Args:
        metadata_tsv: Raw TSV bytes (already-decompressed). The first
            line is the header (may begin with a leading ``#``).
        exceptions: ``target_acc`` values to drop because they failed
            NCBI's QC.
        skip_target_accs: ``target_acc`` values already seen in a
            previous snapshot. Used by the pipeline to suppress
            duplicate events when diffing.

    Returns the kept rows and the per-cycle stats.

    Rows missing :data:`REQUIRED_COLUMNS` are counted in
    ``stats.missing_required`` and dropped. Rows whose ``target_acc``
    suffix cannot be parsed as an integer are counted in
    ``stats.bad_version`` and dropped.
    """
    stats = ParseStats()
    parsed: list[ParsedRow] = []

    for row in _open_tsv(metadata_tsv):
        stats.total_rows += 1

        target_acc = row.get("target_acc", _NULL_TOKEN)
        if target_acc == _NULL_TOKEN or not target_acc:
            stats.missing_required += 1
            continue

        if target_acc in exceptions:
            stats.qc_filtered += 1
            continue

        if target_acc in skip_target_accs:
            # Not new since last cycle; silently skip.
            continue

        asm_acc = row.get("asm_acc", _NULL_TOKEN)
        if asm_acc == _NULL_TOKEN or not asm_acc:
            stats.missing_required += 1
            logger.debug(
                "growlithe.parser: dropping row missing asm_acc target_acc=%s",
                target_acc,
            )
            continue

        match = _VERSION_RE.search(target_acc)
        if match is None:
            stats.bad_version += 1
            logger.warning(
                "growlithe.parser: dropping row with malformed target_acc=%s",
                target_acc,
            )
            continue
        version = int(match.group(1))
        accession = _VERSION_RE.sub("", target_acc)

        extras = _extract_extras(row, METADATA_PASSTHROUGH_COLUMNS)

        parsed.append(
            ParsedRow(
                target_acc=target_acc,
                accession=accession,
                version=version,
                asm_acc=asm_acc,
                target_creation_date=_parse_date(row.get("target_creation_date")),
                extras=extras,
            )
        )
        stats.parsed_rows += 1

    return parsed, stats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_tsv(data: bytes) -> Iterator[dict[str, str]]:
    """Open ``data`` as a TSV with NCBI's ``#header\\ttok\\t...`` quirk."""
    text = data.decode("utf-8", errors="replace")
    buffer = io.StringIO(text)
    # NCBI prefixes the header line with ``#``. csv.DictReader doesn't
    # know to strip it; pre-strip the leading ``#`` of the very first
    # column so downstream code looks the column up by its plain name.
    # We don't use Python's ``csv.Sniffer`` because NCBI's quoting is
    # inconsistent across files (some fields are quoted, most aren't).
    first_line = buffer.readline()
    rest = buffer.read()
    header = first_line.lstrip("#").rstrip("\r\n")
    fieldnames = _split_header(header)
    yield from _row_dict_reader(io.StringIO(rest), fieldnames)


def _split_header(header: str) -> list[str]:
    return [col.strip() for col in header.split("\t")]


def _row_dict_reader(stream: io.StringIO, fieldnames: list[str]) -> Iterator[dict[str, str]]:
    """Yield ``{column: value}`` dicts.

    ``csv.DictReader`` with ``restval``/``restkey`` would handle
    missing/extra fields, but we want explicit visibility: rows with
    fewer columns than the header are padded with ``NULL``; extras are
    dropped. NCBI rarely produces ragged rows in practice.
    """
    reader = csv.reader(stream, delimiter="\t", quoting=csv.QUOTE_NONE)
    for row in reader:
        if not row:
            continue
        # csv.reader gives us a list; zip-truncate to header length and
        # pad missing fields with NULL.
        padded = list(row) + [_NULL_TOKEN] * (len(fieldnames) - len(row))
        yield dict(zip(fieldnames, padded, strict=False))


def _extract_extras(row: dict[str, str], columns: Iterable[str]) -> dict[str, str]:
    """Lift a small set of columns into the event's metadata dict.

    Values equal to the NCBI ``NULL`` token are omitted so consumers
    can rely on key-presence as "this field has data".
    """
    out: dict[str, str] = {}
    for col in columns:
        value = row.get(col)
        if value is None or value == _NULL_TOKEN or value == "":
            continue
        out[col] = value
    return out


def _parse_date(value: str | None) -> datetime | None:
    """Parse an ``YYYY-MM-DD`` (or longer) ISO date into a UTC datetime."""
    if not value or value == _NULL_TOKEN:
        return None
    try:
        # Truncate to date if only YYYY-MM-DD was provided.
        if len(value) == 10:
            parsed = datetime.strptime(value, "%Y-%m-%d")
        else:
            parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


__all__ = [
    "METADATA_PASSTHROUGH_COLUMNS",
    "REQUIRED_COLUMNS",
    "ParseStats",
    "ParsedRow",
    "iter_rows",
    "parse_metadata",
    "read_exceptions",
]
