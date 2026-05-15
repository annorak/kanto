"""Live smoke test against the real NCBI Pathogen Detection FTP.

Opt-in via ``-m slow``. CI does NOT run this by default — NCBI's
public FTP is shared infrastructure and we shouldn't hammer it on
every PR. Operators run this once after deployment to confirm the
adapter still parses today's NCBI output.

The full end-to-end smoke (Growlithe → Mew → Event Hubs) needs dev
cluster credentials and is described in the service README under
"Run locally → Against real NCBI in dev".

What this test verifies
-----------------------
* TLS, DNS, and the User-Agent header all work against the real
  hostname.
* The Apache directory listing parser still recognises the
  metadata-TSV link in NCBI's HTML.
* A first-100-KB range read of the actual metadata TSV parses into
  at least one valid IsolateDiscovered event.

What this test does NOT verify
------------------------------
* Cursor advance (no Mew here).
* Event emit (no Event Hubs here).
* Snapshot-cache write (no Blob here).
"""

from __future__ import annotations

import httpx
import pytest

from growlithe.datasource.ncbi import NCBIPathogenDetection
from growlithe.datasource.parser import iter_rows

pytestmark = [pytest.mark.slow, pytest.mark.integration]


def test_ncbi_listing_and_metadata_round_trip() -> None:
    """Hit real NCBI, list the latest Listeria snapshot, and parse a row."""
    client = httpx.Client(
        timeout=httpx.Timeout(60.0),
        headers={"User-Agent": "kanto-growlithe-smoke/0.1"},
        follow_redirects=True,
    )
    adapter = NCBIPathogenDetection(
        organisms=["Listeria"],
        base_url="https://ftp.ncbi.nlm.nih.gov/pathogen/Results/",
        client=client,
        max_attempts=3,
    )
    try:
        snapshots = adapter.list_current_snapshots()
        assert snapshots, "NCBI returned no Listeria snapshot — check the URL"
        ref = snapshots[0]
        assert ref.organism == "Listeria"
        assert ref.snapshot_id.startswith("PDG")
        assert ref.metadata_url.endswith(".metadata.tsv")

        # Fetch only the first 100 KB of the metadata TSV (NCBI Listeria
        # metadata is ~63 MB; that's wasteful for a smoke). We bypass the
        # adapter's fetch and use the underlying client directly with a
        # Range header so we still exercise the real NCBI server.
        partial = client.get(ref.metadata_url, headers={"Range": "bytes=0-102399"})
        partial.raise_for_status()
        rows = list(iter_rows(partial.content))
        assert rows, "NCBI metadata TSV parsed into zero rows"
        assert "target_acc" in rows[0], (
            "first row missing target_acc; " "NCBI may have changed the column set"
        )
    finally:
        client.close()
