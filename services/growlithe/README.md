# Growlithe — the Discoverer

Growlithe is the entry point of the Kanto pipeline. It polls NCBI
Pathogen Detection on a schedule, identifies isolates it hasn't
emitted before, and writes one `IsolateDiscovered` event per new
isolate to the `kanto.discovered` Event Hubs topic. From there,
[Snorlax](../snorlax/) consumes and downloads the FASTA.

The service is small on purpose. The interesting parts are:

- A [`DataSource`](src/growlithe/datasource/__init__.py) adapter
  protocol; the v1 NCBI implementation lives in `datasource/ncbi.py`.
- A tolerant TSV parser at `datasource/parser.py` that survives NCBI
  schema additions and routes malformed rows to a counter, not a
  crash.
- A snapshot cache in Azure Blob (`snapshot_cache.py`) so we diff
  against the previous metadata snapshot rather than re-read every
  cycle.
- A per-`(source, organism)` cursor in Mew (`cursor.py`) so restarts
  pick up where the last cycle left off.
- A long-lived Deployment with an internal timer (`service.py`) —
  not a CronJob, on purpose; see "Why a Deployment, not a CronJob"
  below.

## Place in the pipeline

```
NCBI Pathogen Detection
        │   (HTTPS, every 6 h)
        ▼
   Growlithe ───────────► kanto.discovered (Event Hubs) ──► Snorlax …
        │
        ├─► Mew.discovery_cursors      (per-source-organism cursor)
        └─► kanto-metadata-{env}/      (per-snapshot TSV cache for diffs)
```

## Run locally

### Against committed fixtures (no NCBI access required)

```bash
cd services/growlithe
export KANTO_ENV=local
export KANTO_GROWLITHE_NCBI_BASE_URL=file://$(pwd)/tests/fixtures/ncbi/
export KANTO_GROWLITHE_RUN_ONCE=true
# Plus the standard KANTO_MEW_*, KANTO_OS_*, KANTO_STREAMING_* env
# vars per the kanto-common ``.envrc.example``. Mew can be a local
# Docker postgres with the migrations applied (see
# infrastructure/migrations/README.md).
uv run growlithe
```

The `file://` URL is only used by the integration test today;
production always points at `https://ftp.ncbi.nlm.nih.gov/pathogen/Results/`.

### Against real NCBI in dev

Set `KANTO_GROWLITHE_RUN_ONCE=true` and run with the standard
dev env wired to dev Mew + dev Blob + dev Event Hubs:

```bash
export KANTO_ENV=dev
export KANTO_GROWLITHE_RUN_ONCE=true
# Fewer organisms keeps the smoke test under five minutes.
export KANTO_GROWLITHE_ORGANISMS=Listeria
# Pull every other secret/host from terraform outputs (see the helm
# values-dev.yaml comment for the canonical wiring).
uv run growlithe
```

When `RUN_ONCE=true`, the process performs exactly one cycle and
exits 0.

## Test

```bash
cd services/growlithe
uv run pytest tests/unit                  # fast, no Docker
uv run pytest tests/integration           # needs Docker (testcontainers Postgres)
uv run pytest --cov=growlithe             # coverage report (gate 80 %)
```

Helm chart:

```bash
cd services/growlithe/helm
helm dependency update
helm lint . -f tests/test-values.yaml
helm unittest .
```

## Configuration

Configuration is **only** loaded from environment variables; nothing
is committed. The Helm chart populates env vars from chart values
plus Azure Key Vault via the Secrets Store CSI Driver.

| Env var                                   | Default                                 | Purpose |
|-------------------------------------------|-----------------------------------------|---------|
| `KANTO_GROWLITHE_ORGANISMS`               | the WHO foodborne + AMR core (9)        | Comma-separated NCBI organism path segments. |
| `KANTO_GROWLITHE_POLL_INTERVAL_SECONDS`   | `21600` (6 h)                           | Time between poll cycles. |
| `KANTO_GROWLITHE_POLL_JITTER_SECONDS`     | `60`                                    | Random jitter added to the interval. |
| `KANTO_GROWLITHE_NCBI_BASE_URL`           | `https://ftp.ncbi.nlm.nih.gov/pathogen/Results/` | Override for fixtures / mirrors. |
| `KANTO_GROWLITHE_NCBI_REQUEST_TIMEOUT_SECONDS` | `300`                              | Per-request HTTP timeout. |
| `KANTO_GROWLITHE_NCBI_MAX_ATTEMPTS`       | `5`                                     | Retry budget for transient HTTP/network errors. |
| `KANTO_GROWLITHE_NCBI_MAX_CONCURRENT`     | `2`                                     | How many organisms poll in parallel. |
| `KANTO_GROWLITHE_RUN_ONCE`                | `false`                                 | Run one cycle and exit. |
| `KANTO_GROWLITHE_HEALTH_PORT`             | `8081`                                  | `/healthz` + `/readyz` port. |
| `KANTO_GROWLITHE_METRICS_PORT`            | `9464`                                  | Prometheus `/metrics` port. |
| `KANTO_GROWLITHE_SOURCE_ID`               | `ncbi-pd`                               | Identifier emitted on every event. |

Plus the kanto-common universal env vars (`KANTO_ENV`,
`KANTO_LOG_*`, `KANTO_MEW_*`, `KANTO_OS_*`, `KANTO_STREAMING_*`,
`KANTO_OTEL_*`).

## Add a new organism

1. Confirm NCBI publishes the organism under
   `https://ftp.ncbi.nlm.nih.gov/pathogen/Results/<Name>/latest_kmer/Metadata/`.
   The path is case-sensitive and uses `_` for spaces.
2. Add the path segment to `KANTO_GROWLITHE_ORGANISMS` in
   `helm/values-{env}.yaml`.
3. Redeploy.

The first cycle after adding an organism emits **every** isolate in
the current PDG (cold start). Downstream services will scale up
briefly; this is expected.

## Add a new DataSource

1. Implement the protocol in `src/growlithe/datasource/<name>.py`.
   The protocol surface is:
   - `name: str`
   - `list_current_snapshots() -> list[SnapshotRef]`
   - `fetch_snapshot(ref) -> FetchedSnapshot`
   - `parse_rows(snapshot, *, previous_metadata_tsv) -> ParsedSnapshot`
2. Add a factory function alongside it (e.g.
   `ENADataSource.from_config(...)`).
3. Extend `growlithe.service.GrowlitheService.build` to wire the new
   source into the pipeline. The pipeline itself doesn't change.
4. Add a config block on `GrowlitheServiceSettings` for any
   source-specific env vars; prefix them
   `KANTO_GROWLITHE_<SOURCE>__*` so they don't collide with the
   NCBI knobs.
5. Commit a few real fixture rows under `tests/fixtures/<source>/`
   the same way the NCBI fixtures are committed today, and mirror
   the test pattern in `tests/unit/test_ncbi_datasource.py`.

The adapter is **pure**: it parses bytes into events but does not
emit them, doesn't touch Mew, doesn't write to Blob. Those are the
pipeline's responsibility, by design.

## Why a Deployment, not a CronJob

A Kubernetes CronJob is the textbook shape for "every 6 hours". We
chose a long-lived Deployment because:

- Probes (`/healthz`, `/readyz`) work out of the box.
- In-process metric counters survive across cycles, so the
  Prometheus dashboards show real trends, not last-run snapshots.
- A single tracer can chain spans across cycles.
- SIGTERM-driven graceful shutdown is the standard pod lifecycle;
  CronJob teardown semantics are clunkier.

The pod is idle for almost six hours between cycles. At the `small`
resource preset that's well under one node-tenth.

## Runbook

### "Growlithe is unhealthy" (readiness probe failing)

The readyz endpoint returns `{"ready": false, "reason": ...}` with
one of:

| reason    | What it means                                       | Likely cause                       |
|-----------|------------------------------------------------------|------------------------------------|
| `draining`| The pod received SIGTERM and is shutting down.        | Rollout in progress; non-event.    |
| `mew`     | Postgres ping failed.                                 | Mew unreachable; check the Mew dashboard. |
| `producer`| Kafka producer can't see any brokers.                 | Event Hubs namespace down or credentials rotated; check Key Vault sync. |

Liveness is intentionally **shallow** — it only checks the in-process
HTTP server. A transient Mew or Event Hubs blip won't restart the
pod.

### Alerts

| Alert                                        | Threshold | Severity |
|----------------------------------------------|-----------|----------|
| `kanto_growlithe_poll_cycles_failed_total > 0` over 30 m | Any non-zero rate     | Page     |
| `kanto_growlithe_isolates_discovered_total == 0` over 24 h | Suspicious quiet      | Ticket   |
| `kanto_growlithe_poll_cycle_duration_seconds{quantile=0.99} > 600` | One organism stuck    | Ticket   |

### Manually trigger a poll for debugging

```bash
NS=kanto-growlithe
kubectl -n $NS set env deploy/kanto-growlithe KANTO_GROWLITHE_RUN_ONCE=true
kubectl -n $NS rollout restart deploy/kanto-growlithe
kubectl -n $NS logs -f -l app.kubernetes.io/name=kanto-growlithe
# When done:
kubectl -n $NS set env deploy/kanto-growlithe KANTO_GROWLITHE_RUN_ONCE=false
kubectl -n $NS rollout restart deploy/kanto-growlithe
```

### Reset a cursor

> ⚠️  **Resetting a cursor causes the next poll to emit every isolate
> NCBI currently lists for that organism.** Downstream services dedupe
> on `(accession, version)` but cost (Modal/Ditto) will spike for one
> cycle. Plan the reset for off-hours and warn downstream owners.

```bash
NS=kanto-growlithe
kubectl -n $NS exec deploy/kanto-growlithe -- \
  python -c "
import asyncio, os
from kanto_commons.mew import MewClient
from kanto_commons.config import MewSettings
from growlithe.cursor import DiscoveryCursorRepository

async def main():
    mew = await MewClient.from_settings(MewSettings())
    repo = DiscoveryCursorRepository()
    async with mew.connection() as conn:
        await repo.reset(conn, source='ncbi-pd', organism='Listeria')
    await mew.close()

asyncio.run(main())
"
```

The next cycle will fully re-emit every isolate the source lists for
the organism. Watch `kanto_growlithe_isolates_discovered_total` and
expect a one-cycle spike.

### Common errors

| Log line                                                 | Cause                                          | Fix |
|----------------------------------------------------------|------------------------------------------------|-----|
| `growlithe.ncbi: no snapshot for organism=X`             | Either typo or NCBI mid-publish.                | Verify `https://ftp.ncbi.nlm.nih.gov/pathogen/Results/X/latest_kmer/Metadata/` returns a `.metadata.tsv`. |
| `growlithe.parser: dropping row with malformed target_acc` | NCBI published a non-PDT row.                  | Investigate; usually a schema-change canary. |
| `growlithe.cache: corrupt cache entry`                   | Blob was partially written; pipeline re-fetches | Self-healing — no action. Confirms a previous crash. |
| `error parsing value for field "organisms"`              | Bad `KANTO_GROWLITHE_ORGANISMS`                 | Must be a comma-separated string, no JSON syntax. |
| Connection error to Mew at startup                       | `KANTO_MEW_PASSWORD` not synced from Key Vault. | Check the SecretProviderClass and that the K8s Secret named `kanto-growlithe-akv` exists. |

## Files

```
services/growlithe/
├── Dockerfile                 # Multi-stage, distroless-style runtime, non-root.
├── README.md                  # You are here.
├── pyproject.toml             # uv workspace member ``growlithe``.
├── helm/                      # Service chart; depends on ../infrastructure/helm/kanto-common.
├── src/growlithe/
│   ├── config.py              # GrowlitheSettings (KantoBaseSettings + service knobs).
│   ├── organisms.py           # DEFAULT_ORGANISMS — the WHO foodborne + AMR core.
│   ├── datasource/
│   │   ├── __init__.py        # DataSource protocol + payload types.
│   │   ├── ncbi.py            # NCBI Pathogen Detection adapter.
│   │   └── parser.py          # Tolerant NCBI metadata TSV parser.
│   ├── cursor.py              # discovery_cursors repository.
│   ├── snapshot_cache.py      # Per-snapshot Blob cache.
│   ├── pipeline.py            # Poll-cycle orchestrator.
│   ├── service.py             # Long-lived service: timer, health, shutdown.
│   ├── metrics.py             # OTel meter + instruments.
│   ├── __main__.py            # ``python -m growlithe`` entry point.
│   └── __init__.py
└── tests/
    ├── conftest.py
    ├── fixtures/ncbi/         # Real NCBI metadata samples + directory listings.
    ├── unit/                  # Parser, adapter, cursor, cache, pipeline, config.
    └── integration/           # Full pipeline against testcontainers Postgres.
```
