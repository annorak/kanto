# Snorlax — the Ingester and Gene Predictor

Snorlax is the heavy CPU stage of the Kanto pipeline. One pod per
stream partition (two replicas per partition for hot-standby
failover) consumes `IsolateDiscovered` events from
`kanto.discovered`, downloads the genome FASTA from NCBI's HTTPS
endpoint, runs Prodigal in the same pod to predict proteins, gzips
the protein FASTA, writes it to Azure Blob Storage at
`{container}/{accession}/{version}.faa.gz`, then spawns Modal's Ditto
function with the blob key. All operations are idempotent on
`(accession, version)`.

## What it does

```
kanto.discovered ──► [Snorlax pod]
                        │
                        ├── download genome FASTA from NCBI HTTPS
                        ├── validate (FASTA shape, size, md5)
                        ├── Prodigal -p single (subprocess, in-pod)
                        ├── validate protein FASTA
                        ├── gzip + upload to Azure Blob
                        ├── Mew row: status = PROTEINS_READY
                        ├── Modal.spawn(ditto.embed, …)
                        ├── Mew row: modal_call_id = …
                        └── commit stream offset
```

Prodigal is colocated with the downloader on purpose: they share input
data and identical lifecycle requirements. Splitting them would force
us to persist intermediates between two pods for no benefit.

## How to run locally

### Unit tests only (no Docker, no NCBI)

```bash
uv run --package snorlax pytest tests/unit
```

### Integration tests against a real Postgres+pgvector container

Requires Docker. The fixture spins up `pgvector/pgvector:pg16` and
applies the kanto-migrations Alembic head before each test.

```bash
DOCKER_HOST=unix:///${HOME}/.docker/run/docker.sock \
  uv run --package snorlax pytest tests/integration
```

### Real-NCBI smoke test (download + Prodigal + upload, mock Modal)

Requires internet egress to `ftp.ncbi.nlm.nih.gov` and a Prodigal
binary on `$PATH` (or `$SNORLAX_PRODIGAL_BIN`). Modal is mocked via
`NoopModalClient` until Task 7 ships the real Ditto function.

```bash
brew install prodigal       # or: conda install -c bioconda prodigal
uv run --package snorlax pytest tests/integration/test_real_ncbi_smoke.py -m real_ncbi
```

The smoke test pulls _Pseudomonas aeruginosa_ PAO1 (`GCA_000006765.1`,
~6 MB compressed) and verifies the full download → validate →
Prodigal → upload chain end-to-end.

### Running the service against live infrastructure

The container image embeds Prodigal 2.6.3 (see "Prodigal pinning"
below). Local end-to-end runs need Mew + Event Hubs + Blob endpoints
plus a Modal account or `KANTO_SNORLAX_MODAL_ENABLED=false`.

```bash
export KANTO_ENV=local
export KANTO_MEW_HOST=...           # see libs/kanto-commons/.envrc.example
export KANTO_OS_ACCOUNT_URL=...
export KANTO_STREAMING_BOOTSTRAP_SERVERS=...
export KANTO_SNORLAX_MODAL_ENABLED=false
uv run --package snorlax snorlax
```

## Configuration reference

Every variable below is read from the environment via `pydantic-settings`.
Defaults are in `src/snorlax/config.py`; the Helm chart populates
them from values plus Azure Key Vault via the Secrets Store CSI
Driver.

### Universal (KantoBaseSettings)

| Variable | Purpose |
| --- | --- |
| `KANTO_ENV` / `KANTO_ENVIRONMENT` | `local` \| `dev` \| `staging` \| `prod`. |
| `KANTO_LOG_LEVEL`, `KANTO_LOG_FORMAT` | Structured logging knobs. |
| `KANTO_MEW_*` | Mew connection (host, port, db, user, password, sslmode). |
| `KANTO_OS_ACCOUNT_URL`, `KANTO_OS_*_CONTAINER` | Azure Blob endpoint + container names. |
| `KANTO_STREAMING_BOOTSTRAP_SERVERS`, `KANTO_STREAMING_SASL_USERNAME`, `KANTO_STREAMING_SASL_PASSWORD` | Event Hubs Kafka endpoint and SASL credentials. |
| `KANTO_STREAMING_DISCOVERED_TOPIC` | Defaults to `kanto.discovered`. |
| `KANTO_OTEL_ENDPOINT`, `KANTO_OTEL_SAMPLE_RATE` | OTel exporter target. |

### Snorlax-specific

| Variable | Default | Purpose |
| --- | --- | --- |
| `KANTO_SNORLAX_CONSUMER_GROUP` | `snorlax` | Kafka consumer group ID. |
| `KANTO_SNORLAX_CONSUMER_ID` | `$POD_NAME` | Stable id used by the rebalancer. |
| `KANTO_SNORLAX_NCBI_ASSEMBLY_BASE_URL` | `https://ftp.ncbi.nlm.nih.gov/genomes/all/GCA/` | NCBI HTTPS base. |
| `KANTO_SNORLAX_DOWNLOAD_TIMEOUT_SECONDS` | `300` | Per-request HTTP timeout. |
| `KANTO_SNORLAX_DOWNLOAD_MAX_ATTEMPTS` | `5` | Tenacity retry budget for transient errors. |
| `KANTO_SNORLAX_DOWNLOAD_CHUNK_BYTES` | `1048576` | Streaming chunk size. |
| `KANTO_SNORLAX_MIN_GENOME_BYTES` | `204800` | Reject suspiciously tiny downloads. |
| `KANTO_SNORLAX_MAX_GENOME_BYTES` | `209715200` | Reject runaway downloads. |
| `KANTO_SNORLAX_PRODIGAL_BINARY` | `prodigal` | Resolved on `$PATH` or absolute. |
| `KANTO_SNORLAX_PRODIGAL_MODE` | `single` | `single` for bacterial single-organism genomes. |
| `KANTO_SNORLAX_PRODIGAL_TIMEOUT_SECONDS` | `300` | Hard wall-clock limit. |
| `KANTO_SNORLAX_WORK_DIR` | `/tmp/snorlax` | Pod-local scratch (tmpfs in the chart). |
| `KANTO_SNORLAX_MODAL_FUNCTION_REF` | `kanto-ditto/embed` | `app-name/function-name`. |
| `KANTO_SNORLAX_MODAL_ENVIRONMENT` | `main` | Modal environment selector. |
| `KANTO_SNORLAX_MODAL_MAX_ATTEMPTS` | `5` | Retry budget for transient spawn failures. |
| `KANTO_SNORLAX_MODAL_ENABLED` | `true` | Flip to `false` to use `NoopModalClient`. |
| `KANTO_SNORLAX_MEW_MAX_ATTEMPTS` | `5` | Retry budget for Mew writes. |
| `KANTO_SNORLAX_HEALTH_PORT` | `8081` | `/healthz` + `/readyz`. |
| `KANTO_SNORLAX_METRICS_PORT` | `9464` | Prometheus scrape. |
| `KANTO_SNORLAX_RUN_ONCE` | `false` | Process one message then exit; smoke-test use. |
| `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` | _required_ when `modal_enabled=true` | Read directly by the Modal SDK. |

## Failure handling

The pipeline maps each failure mode to a specific outcome, per
task-06 §7. The outcome drives both the Mew status transition and the
stream offset semantics.

| Failure | Mew status | Stream action | Why |
| --- | --- | --- | --- |
| NCBI 404 (`GenomeNotFoundError`) | `QC_FAILED` (reason `not_found`) | commit | Permanent; the file no longer exists on NCBI. Retrying loops forever. |
| Download > `max_genome_bytes` | `QC_FAILED` (reason `download_too_large`) | commit | Permanent; almost always wrong-file. |
| Transient download exhausted (md5 mismatch, partial body, timeout) | _untouched_ | **no commit** | NCBI's nightly window resolves in minutes — let the stream redeliver. |
| Genome FASTA fails validation | `QC_FAILED` (reason `validation_failed`) | commit | Bad bytes from NCBI; retry won't fix. |
| Prodigal non-zero exit | `QC_FAILED` (reason `prodigal_failed`) | DLQ | Almost always corrupt input — operator review. |
| Prodigal timeout | `QC_FAILED` (reason `prodigal_timeout`) | DLQ | Pathological input. |
| Protein FASTA fails validation | `QC_FAILED` (reason `prodigal_failed`) | DLQ | Prodigal exited 0 but produced garbage. |
| Azure Blob upload exhausted | `QC_FAILED` (reason `upload_failed`) | DLQ | The kanto-commons client already retried transient errors. |
| Modal spawn exhausted | `QC_FAILED` (reason `modal_spawn_failed`) | DLQ | Protein FASTA is already in OS; operator can re-spawn manually. |
| Mew write exhausted | (best-effort QC_FAILED) | DLQ | Mew/Snorlax inconsistency — alert + manual remediation. |
| Mew unreachable on entry | _none_ (we couldn't write) | **no commit** | Treat as transient; redeliver later. |
| Wrong event type on the topic | n/a | DLQ | Should not happen; we DLQ to surface schema regressions. |

The "commit" action means we acknowledge the offset and move on; the
"DLQ" action publishes a `DLQEnvelope` to `kanto.discovered.dlq` and
then commits.

## Operational runbook

### Scaling

`replicas = partitionCount × replicasPerPartition`. To scale the
pipeline, add partitions on the Event Hub via Terraform, then bump
`snorlax.partitionCount` in the values overlay and `helm upgrade`.
The chart guarantees `replicas` follows.

### Handling DLQ messages

The DLQ topic is `kanto.discovered.dlq` (see `kanto_commons.streaming`).
Each envelope carries the original payload, the failure reason, and a
truncated traceback. Triage flow:

1. `kafkacat -C -b $BROKER -t kanto.discovered.dlq -o earliest`
   (or your preferred Kafka client) to inspect.
2. The corresponding Mew row will be in `QC_FAILED` with
   `qc_failure_reason` carrying the failure category.
3. For Prodigal failures, the easiest reproduction is to re-download
   the FASTA from NCBI and run `prodigal -i genome.fna -a out.faa -p
   single -q` locally with the pinned version (2.6.3).
4. If the issue is fixed (operator-side), re-publish the original
   payload onto `kanto.discovered` to reprocess.

### Investigating stuck isolates

The `kanto_snorlax_in_flight_isolates` gauge shows live work per
pod. If it stays elevated, check:

* Per-stage latency histograms (`kanto_snorlax_stage_duration_seconds`)
  — which stage is slow.
* Prodigal stderr in logs — corrupt input often surfaces here.
* Consumer lag on the partition this pod owns.

### Tuning Prodigal parameters

v1 uses `prodigal -p single` for every bacterial genome with no
per-organism customization. Set `KANTO_SNORLAX_PRODIGAL_MODE=meta`
(or change `snorlax.prodigalMode` in the chart) to switch the
default — but this is a global flag; per-organism tuning is out of
scope until v2 (see `docs/design.md`).

## Design decisions

### Why colocate Prodigal in the Snorlax pod?

Prodigal needs the genome bytes that Snorlax just downloaded, and
they have identical lifecycle requirements (start when a message
arrives, stop when processing completes). Splitting them across
pods would mean either persisting the FASTA to PVCs or piping it
through another stream — both of which add complexity for zero
benefit. The design doc spells this out in §6.2 and the task-06
brief reinforces it.

### Why StatefulSet with two replicas per partition (not Deployment)?

We want active-passive failover at the partition level. The Kafka
consumer group rebalancer handles failover automatically as long as
both replicas of a partition share a stable group ID and `consumer_id`
distinguishes them. A Deployment would deliver the same set of pods
but with no guarantees about pod identity, and we lose the natural
1-pod-per-partition mental model. The active-passive choice (rather
than active-active across replicas) means each partition has exactly
one in-flight isolate at a time, which is the right level of
concurrency given the per-stage costs.

### Why direct Modal.spawn instead of another stream?

See `docs/design.md` §6.2. Modal queues internally; adding a stream
between Snorlax and Modal would double-queue.

### Why mockable Modal client?

Task 7 (Ditto) ships after Task 6 (Snorlax). The `NoopModalClient`
lets dev environments exercise the rest of the pipeline before the
real Modal function exists, and gives unit/integration tests a stable
substitute that doesn't require a Modal account.

## Prodigal pinning

The Dockerfile pins Prodigal to **2.6.3** (the canonical maintained
release; `make INSTALLDIR=… install` from the upstream tarball at
`github.com/hyattpd/Prodigal`). The version is set via the
`PRODIGAL_VERSION` build arg in `services/snorlax/Dockerfile`.

To bump, change `ARG PRODIGAL_VERSION=…` and rebuild. The
gene-call output format is stable across patch versions; a major bump
warrants regression testing against a known-good genome fixture.

## Tests

```bash
# Unit + integration (Docker required)
DOCKER_HOST=unix:///${HOME}/.docker/run/docker.sock \
  uv run --package snorlax pytest

# Real-NCBI smoke
uv run --package snorlax pytest -m real_ncbi tests/integration/test_real_ncbi_smoke.py

# Helm chart unit tests
helm unittest services/snorlax/helm
```

Coverage target is 80% (see `pyproject.toml`); the current run lands
near 92%.
