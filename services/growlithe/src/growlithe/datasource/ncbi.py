"""NCBI Pathogen Detection :class:`DataSource` implementation.

Layout (verified against ``ftp.ncbi.nlm.nih.gov`` 2026-05):

``{base}/{organism}/latest_kmer/Metadata/``
  Apache directory listing. Contains:
  * ``PDG<n>.<m>.metadata.tsv`` — the full metadata for the current PDG.
  * ``PDG<n>.<m>.exceptions.tsv`` — QC-failed isolates (when present).
  * ``PDG<n>.<m>.metadata.xml`` — same data in XML (not used).

The PDG version is **not** published in a separate file; we recover
it from the metadata file name in the directory listing. The
listing's HTML is small and cheap to fetch, so we do it once per
poll cycle per organism.

Why HTTPS, not FTP:
  NCBI serves the same path tree over both FTP and HTTPS. HTTPS is
  encrypted in transit, plays nicely with corporate egress filters,
  and is the default for new code in 2025+.

Why a long timeout:
  The metadata TSV for big organisms (e.g. Salmonella) is hundreds
  of MB. During NCBI's nightly update window the GET can stall for
  minutes; we'd rather wait than fail.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from urllib.parse import urljoin

import httpx
from kanto_commons import IsolateDiscovered
from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from growlithe.datasource import (
    FetchedSnapshot,
    ParsedSnapshot,
    SnapshotRef,
)
from growlithe.datasource.parser import (
    ParsedRow,
    iter_rows,
    parse_metadata,
    read_exceptions,
)

logger = logging.getLogger(__name__)


# Apache's directory listing renders each file as a hyperlink. We
# regex the listing rather than parse HTML because the listing is
# stable across decades and a real HTML parser is overkill.
_LISTING_RE = re.compile(
    r'href="(?P<name>PDG\d+\.\d+\.metadata\.tsv)"',
    re.IGNORECASE,
)

# Source identifier emitted on every event from this adapter.
SOURCE_ID = "ncbi-pd"

# Canonical GenBank assembly base path. ``ftp_path`` on each event
# points at the parent of the per-assembly directory; Snorlax lists
# it to find the actual ``*_protein.faa.gz``.
GENBANK_ASSEMBLY_BASE = "https://ftp.ncbi.nlm.nih.gov/genomes/all/GCA/"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class NCBIDataSourceError(Exception):
    """Wraps every NCBI-side failure surfaced to the pipeline."""


class NoSnapshotAvailableError(NCBIDataSourceError):
    """Raised when no PDG metadata TSV is visible in the listing.

    Either NCBI is mid-publish (the file briefly disappears) or the
    operator typo'd an organism name. Both are recoverable on the
    next cycle.
    """


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class NCBIPathogenDetection:
    """Concrete :class:`DataSource` for NCBI Pathogen Detection."""

    def __init__(
        self,
        *,
        organisms: list[str],
        base_url: str,
        client: httpx.Client,
        max_attempts: int,
    ) -> None:
        self._organisms = list(organisms)
        # Ensure trailing slash; the ``urljoin`` calls below rely on it.
        self._base_url = base_url if base_url.endswith("/") else base_url + "/"
        self._client = client
        self._retry = Retrying(
            retry=retry_if_exception(_is_transient),
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=0.5, max=10.0),
            reraise=True,
        )

    @classmethod
    def from_config(
        cls,
        *,
        organisms: list[str],
        base_url: str,
        timeout_seconds: float,
        max_attempts: int,
    ) -> NCBIPathogenDetection:
        client = httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            headers={"User-Agent": "kanto-growlithe/0.1 (+https://kanto.io)"},
            follow_redirects=True,
        )
        return cls(
            organisms=organisms,
            base_url=base_url,
            client=client,
            max_attempts=max_attempts,
        )

    def close(self) -> None:
        self._client.close()

    # ---------------- Protocol surface ----------------

    @property
    def name(self) -> str:
        return SOURCE_ID

    def list_current_snapshots(self) -> list[SnapshotRef]:
        refs: list[SnapshotRef] = []
        for organism in self._organisms:
            try:
                ref = self._list_snapshot_for(organism)
            except NoSnapshotAvailableError:
                logger.warning(
                    "growlithe.ncbi: no snapshot for organism=%s; will retry next cycle",
                    organism,
                )
                continue
            refs.append(ref)
        return refs

    def fetch_snapshot(self, ref: SnapshotRef) -> FetchedSnapshot:
        metadata_bytes = self._http_get(ref.metadata_url)
        exceptions_bytes: bytes | None = None
        if ref.exceptions_url is not None:
            try:
                exceptions_bytes = self._http_get(ref.exceptions_url)
            except _NotFoundError:
                # NCBI publishes exceptions inconsistently; absence
                # means "no QC failures this cycle", not an error.
                exceptions_bytes = None
        return FetchedSnapshot(
            ref=ref,
            metadata_tsv=metadata_bytes,
            exceptions_tsv=exceptions_bytes,
        )

    def parse_rows(
        self,
        snapshot: FetchedSnapshot,
        *,
        previous_metadata_tsv: bytes | None,
    ) -> ParsedSnapshot:
        exceptions = read_exceptions(snapshot.exceptions_tsv)
        previous_seen = _previous_target_accs(previous_metadata_tsv)
        parsed, stats = parse_metadata(
            snapshot.metadata_tsv,
            exceptions=exceptions,
            skip_target_accs=previous_seen,
        )
        events = [self._to_event(snapshot.ref, row) for row in parsed]
        return ParsedSnapshot(
            events=events,
            qc_filtered=stats.qc_filtered,
            missing_required=stats.missing_required,
            bad_version=stats.bad_version,
        )

    # ---------------- Internals ----------------

    def _organism_metadata_dir(self, organism: str) -> str:
        return urljoin(self._base_url, f"{organism}/latest_kmer/Metadata/")

    def _list_snapshot_for(self, organism: str) -> SnapshotRef:
        listing_url = self._organism_metadata_dir(organism)
        listing = self._http_get(listing_url).decode("utf-8", errors="replace")
        match = _LISTING_RE.search(listing)
        if match is None:
            raise NoSnapshotAvailableError(f"no .metadata.tsv link found at {listing_url}")
        metadata_name = match.group("name")
        pdg_version = metadata_name.removesuffix(".metadata.tsv")
        exceptions_name = f"{pdg_version}.exceptions.tsv"
        return SnapshotRef(
            organism=organism,
            snapshot_id=pdg_version,
            metadata_url=urljoin(listing_url, metadata_name),
            exceptions_url=urljoin(listing_url, exceptions_name),
        )

    def _http_get(self, url: str) -> bytes:
        def _do() -> bytes:
            response = self._client.get(url)
            if response.status_code == 404:
                raise _NotFoundError(url)
            response.raise_for_status()
            return response.content

        return self._retry(_do)

    def _to_event(self, ref: SnapshotRef, row: ParsedRow) -> IsolateDiscovered:
        metadata = {
            "snapshot_id": ref.snapshot_id,
            "asm_acc": row.asm_acc,
            **row.extras,
        }
        return IsolateDiscovered(
            accession=row.target_acc,
            version=row.version,
            organism=ref.organism,
            source=self.name,
            ftp_path=_genbank_assembly_dir_url(row.asm_acc),
            discovered_at=row.target_creation_date or _utcnow_floor_to_minute(),
            metadata=metadata,
        )


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


class _NotFoundError(Exception):
    """Raised inside the retry loop for HTTP 404 — not transient."""


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, _NotFoundError | NoSnapshotAvailableError):
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    return isinstance(exc, httpx.TimeoutException | httpx.NetworkError)


def _previous_target_accs(prev_tsv: bytes | None) -> frozenset[str]:
    """Build the ``target_acc`` set from a previously-cached snapshot.

    Used to suppress already-emitted accessions when diffing. A
    missing or zero-byte previous snapshot returns an empty set so
    the first poll of an organism emits every isolate.
    """
    if not prev_tsv:
        return frozenset()
    return frozenset(
        row["target_acc"]
        for row in iter_rows(prev_tsv)
        if row.get("target_acc") and row["target_acc"] != "NULL"
    )


def _genbank_assembly_dir_url(asm_acc: str) -> str:
    """Return the GenBank parent-of-assembly directory URL for ``asm_acc``.

    Format: ``.../genomes/all/GCA/AAA/BBB/CCC/`` where AAA/BBB/CCC is
    the 3-3-3 split of the numeric assembly id. Snorlax lists this
    directory to find the actual ``GCA_xxx.V_*`` subdir containing
    the protein FASTA.
    """
    if not asm_acc.startswith("GCA_"):
        # Fall back to whatever the operator passed — Snorlax will log
        # an unhelpful error rather than us crashing here.
        return f"{GENBANK_ASSEMBLY_BASE}{asm_acc}/"
    numeric = asm_acc.removeprefix("GCA_").split(".")[0]
    a, b, c = numeric[0:3], numeric[3:6], numeric[6:9]
    return f"{GENBANK_ASSEMBLY_BASE}{a}/{b}/{c}/"


def _utcnow_floor_to_minute() -> datetime:
    """``datetime.now`` rounded down to the minute — keeps fixtures stable."""
    now = datetime.now(UTC)
    return now.replace(second=0, microsecond=0)


__all__ = [
    "GENBANK_ASSEMBLY_BASE",
    "SOURCE_ID",
    "NCBIDataSourceError",
    "NCBIPathogenDetection",
    "NoSnapshotAvailableError",
]
