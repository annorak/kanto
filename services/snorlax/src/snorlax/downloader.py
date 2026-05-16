"""NCBI GenBank genome downloader.

Snorlax receives an ``IsolateDiscovered`` event whose ``ftp_path`` is
the GenBank *parent* directory (the 3-3-3 numeric split, e.g.
``.../genomes/all/GCA/000/123/456/``). Inside that directory NCBI
publishes one subdirectory per assembly version
(``GCA_000123456.1_AssemblyName/``) which contains the genomic FASTA
under a predictable name suffix (``*_genomic.fna.gz``).

This module:

1. Lists the parent directory's Apache HTML index.
2. Picks the subdirectory matching ``GCA_<numeric>.<version>``.
3. Streams the ``*_genomic.fna.gz`` to disk with per-chunk progress
   bookkeeping, size limits, and an optional MD5 check against
   NCBI's ``md5checksums.txt`` (when present).

We deliberately use HTTPS rather than FTP for the same reasons
documented in :mod:`growlithe.datasource.ncbi`: encryption in transit,
egress filter friendliness, and identical content tree.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import httpx
from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

# Apache directory listings render each entry as ``<a href="name/">name/</a>``.
# We match links of the form ``GCA_000123456.1_AssemblyLabel/``.
_ASSEMBLY_DIR_RE = re.compile(
    r'href="(?P<dirname>GCA_(?P<numeric>\d{9,})\.(?P<version>\d+)_[^"/]+)/"',
    re.IGNORECASE,
)

# ``*_genomic.fna.gz`` is the canonical compressed genome FASTA for an
# assembly. NCBI also publishes ``*_protein.faa.gz`` for assemblies
# they pre-annotated, but we want the unannotated genome because
# Prodigal does the ORF calling.
_GENOMIC_FILE_RE = re.compile(
    r'href="(?P<name>[^"]+_genomic\.fna\.gz)"',
    re.IGNORECASE,
)

# ``md5checksums.txt`` lives in the assembly directory. Each line is
# ``<md5_hex>  ./<filename>``; we hash-check the genomic file when the
# checksum is published.
_MD5_LINE_RE = re.compile(r"^(?P<md5>[0-9a-f]{32})\s+\./(?P<name>\S+)\s*$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DownloadError(Exception):
    """Base for every downloader error."""


class GenomeNotFoundError(DownloadError):
    """NCBI returned 404 or the genomic file isn't where we expected.

    This is the *permanent* failure mode for Snorlax: NCBI cleaned up
    the assembly between Growlithe discovering it and Snorlax fetching
    it, or the metadata pointed at a path that doesn't exist. Either
    way, no amount of retry will fix it — the pipeline should mark the
    isolate failed and move on.
    """


class TransientDownloadError(DownloadError):
    """Retryable network/HTTP error.

    The pipeline's failure handler treats this as "leave the offset
    uncommitted and let the message be redelivered".
    """


class GenomeTooLargeError(DownloadError):
    """The download exceeded ``max_genome_bytes``.

    Probably a misconfig or a wrong file. Permanent failure for this
    isolate.
    """


class GenomeTooSmallError(DownloadError):
    """The download was smaller than ``min_genome_bytes``.

    NCBI sometimes serves a truncated body during their nightly update
    window; treat as transient and retry. A persistently tiny file
    eventually trips ``download_max_attempts`` and falls into the
    transient-exhausted bucket (which the pipeline treats as
    "redeliver from the stream").
    """


class GenomeChecksumError(DownloadError):
    """The downloaded file's MD5 didn't match NCBI's published checksum.

    Treated as transient: NCBI sometimes serves a partial body during
    their update window, and a retry usually gets the full file.
    """


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DownloadedGenome:
    """Result of a successful genome download."""

    local_path: Path
    source_url: str
    size_bytes: int
    md5_hex: str
    checksum_verified: bool


# ---------------------------------------------------------------------------
# Downloader
# ---------------------------------------------------------------------------


class GenomeDownloader:
    """Resolves and streams a genome FASTA from NCBI."""

    def __init__(
        self,
        *,
        client: httpx.Client,
        max_attempts: int,
        chunk_bytes: int,
        max_bytes: int,
        min_bytes: int,
    ) -> None:
        self._client = client
        self._chunk_bytes = chunk_bytes
        self._max_bytes = max_bytes
        self._min_bytes = min_bytes
        self._retry = Retrying(
            retry=retry_if_exception(_is_transient),
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=0.5, max=10.0),
            reraise=True,
        )

    @classmethod
    def from_settings(
        cls,
        *,
        timeout_seconds: float,
        max_attempts: int,
        chunk_bytes: int,
        max_bytes: int,
        min_bytes: int,
    ) -> GenomeDownloader:
        client = httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            headers={"User-Agent": "kanto-snorlax/0.1 (+https://kanto.io)"},
            follow_redirects=True,
        )
        return cls(
            client=client,
            max_attempts=max_attempts,
            chunk_bytes=chunk_bytes,
            max_bytes=max_bytes,
            min_bytes=min_bytes,
        )

    def close(self) -> None:
        self._client.close()

    # -------------------- public API --------------------

    def download(
        self,
        *,
        parent_dir_url: str,
        asm_acc: str,
        dest: Path,
    ) -> DownloadedGenome:
        """Resolve and download the genome FASTA for ``asm_acc``.

        ``parent_dir_url`` is the 3-3-3-split parent URL from the
        ``ftp_path`` field on the ``IsolateDiscovered`` event.
        ``asm_acc`` is the assembly accession (e.g. ``GCA_000123456.1``).
        ``dest`` is the destination file path; parent dirs must exist.

        Returns a :class:`DownloadedGenome` describing the bytes on disk.
        Raises one of the ``*Error`` subclasses on failure.
        """
        assembly_dir = self._resolve_assembly_dir(parent_dir_url, asm_acc)
        listing = self._http_get_text(assembly_dir)
        genome_name = _find_genomic_filename(listing)
        if genome_name is None:
            raise GenomeNotFoundError(f"no *_genomic.fna.gz in assembly dir {assembly_dir}")
        genome_url = urljoin(assembly_dir, genome_name)

        # md5checksums.txt is best-effort. NCBI publishes it on almost
        # every assembly but a missing file is not an error.
        expected_md5 = self._lookup_md5(assembly_dir, genome_name)

        downloaded_md5 = self._stream_to_disk(genome_url, dest)
        size_bytes = dest.stat().st_size

        if size_bytes > self._max_bytes:
            dest.unlink(missing_ok=True)
            raise GenomeTooLargeError(
                f"{genome_url} produced {size_bytes} bytes (max {self._max_bytes})"
            )
        if size_bytes < self._min_bytes:
            dest.unlink(missing_ok=True)
            raise GenomeTooSmallError(
                f"{genome_url} produced {size_bytes} bytes (min {self._min_bytes})"
            )

        checksum_verified = False
        if expected_md5 is not None:
            if expected_md5.lower() != downloaded_md5.lower():
                dest.unlink(missing_ok=True)
                raise GenomeChecksumError(
                    f"md5 mismatch for {genome_url}: expected={expected_md5} got={downloaded_md5}"
                )
            checksum_verified = True

        return DownloadedGenome(
            local_path=dest,
            source_url=genome_url,
            size_bytes=size_bytes,
            md5_hex=downloaded_md5,
            checksum_verified=checksum_verified,
        )

    # -------------------- internals --------------------

    def _resolve_assembly_dir(self, parent_dir_url: str, asm_acc: str) -> str:
        """Find the per-assembly subdirectory inside the parent.

        Listed once per call: the parent dir usually has a handful of
        siblings, the Apache HTML is small, and caching adds little
        value for the request volume Snorlax sees.
        """
        if not parent_dir_url.endswith("/"):
            parent_dir_url = parent_dir_url + "/"
        listing = self._http_get_text(parent_dir_url)
        numeric, version = _parse_assembly_accession(asm_acc)
        match = _find_assembly_subdir(listing, numeric=numeric, version=version)
        if match is None:
            raise GenomeNotFoundError(f"no subdir matching {asm_acc} in {parent_dir_url}")
        return urljoin(parent_dir_url, f"{match}/")

    def _lookup_md5(self, assembly_dir: str, genome_name: str) -> str | None:
        """Best-effort fetch of ``md5checksums.txt``."""
        checksum_url = urljoin(assembly_dir, "md5checksums.txt")
        try:
            text = self._http_get_text(checksum_url)
        except GenomeNotFoundError:
            logger.debug(
                "snorlax.downloader: no md5checksums.txt at %s (ok)",
                checksum_url,
            )
            return None
        return _parse_md5_for(text, genome_name)

    def _http_get_text(self, url: str) -> str:
        """GET ``url`` and return the body decoded as UTF-8."""

        def _do() -> str:
            response = self._client.get(url)
            if response.status_code == 404:
                raise GenomeNotFoundError(url)
            response.raise_for_status()
            return response.text

        return self._retry(_do)

    def _stream_to_disk(self, url: str, dest: Path) -> str:
        """Stream ``url`` to ``dest`` chunk by chunk; return MD5 hex."""

        def _do() -> str:
            md5 = hashlib.md5()
            written = 0
            with self._client.stream("GET", url) as response:
                if response.status_code == 404:
                    raise GenomeNotFoundError(url)
                response.raise_for_status()
                with dest.open("wb") as fp:
                    for chunk in response.iter_bytes(chunk_size=self._chunk_bytes):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > self._max_bytes:
                            # Bail early so we don't burn disk on a runaway.
                            raise GenomeTooLargeError(
                                f"{url} exceeded {self._max_bytes} mid-stream"
                            )
                        md5.update(chunk)
                        fp.write(chunk)
            return md5.hexdigest()

        return self._retry(_do)


# ---------------------------------------------------------------------------
# Pure helpers (broken out so unit tests don't need an httpx mock)
# ---------------------------------------------------------------------------


def _parse_assembly_accession(asm_acc: str) -> tuple[str, str]:
    """Split ``GCA_000123456.7`` into ``("000123456", "7")``.

    Raises :class:`GenomeNotFoundError` for malformed accessions —
    a wrong accession from upstream is a permanent failure for this
    isolate, not transient.
    """
    if not asm_acc.startswith("GCA_"):
        raise GenomeNotFoundError(f"unsupported assembly accession: {asm_acc!r}")
    rest = asm_acc.removeprefix("GCA_")
    if "." not in rest:
        raise GenomeNotFoundError(f"missing version in assembly accession: {asm_acc!r}")
    numeric, version = rest.split(".", 1)
    if not numeric.isdigit() or not version.isdigit():
        raise GenomeNotFoundError(f"non-numeric parts in {asm_acc!r}")
    return numeric, version


def _find_assembly_subdir(listing_html: str, *, numeric: str, version: str) -> str | None:
    """Pick the ``GCA_<numeric>.<version>_*`` link from an Apache index.

    Returns the directory name without the trailing slash, or ``None``
    if not present.
    """
    for m in _ASSEMBLY_DIR_RE.finditer(listing_html):
        if m.group("numeric") == numeric and m.group("version") == version:
            return m.group("dirname")
    return None


def _find_genomic_filename(listing_html: str) -> str | None:
    """Pick the first ``*_genomic.fna.gz`` link in an Apache index."""
    match = _GENOMIC_FILE_RE.search(listing_html)
    return match.group("name") if match else None


def _parse_md5_for(checksum_body: str, target_name: str) -> str | None:
    """Return the md5 hex for ``target_name`` from a ``md5checksums.txt``."""
    for line in checksum_body.splitlines():
        m = _MD5_LINE_RE.match(line)
        if m is None:
            continue
        if m.group("name") == target_name:
            return m.group("md5")
    return None


def _is_transient(exc: BaseException) -> bool:
    """Decide whether the retry decorator should re-run."""
    if isinstance(
        exc,
        GenomeNotFoundError | GenomeTooLargeError,
    ):
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code >= 500 or code == 429
    if isinstance(exc, GenomeTooSmallError | GenomeChecksumError):
        return True
    return isinstance(exc, httpx.TimeoutException | httpx.NetworkError)


# Public surface — keep small and explicit.
__all__ = [
    "DownloadError",
    "DownloadedGenome",
    "GenomeChecksumError",
    "GenomeDownloader",
    "GenomeNotFoundError",
    "GenomeTooLargeError",
    "GenomeTooSmallError",
    "TransientDownloadError",
]


# Keep the iterator import used in the inline streaming closure visible
# to mypy even though the closure consumes it lazily.
_ = Iterator
