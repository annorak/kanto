# Ditto — the Embedder

Ditto is the GPU stage of the Kanto pipeline and the only component
that runs on **Modal** rather than the OKE/AKS cluster. For each
`(accession, version, os_key)` it receives, Ditto pulls the protein
FASTA from Azure Blob Storage, runs ESM C 600M forward passes in
length-grouped batches, computes per-protein 1152-dim float16
embeddings plus a per-genome length-weighted-mean aggregate, writes
the per-protein embeddings as Parquet back to Blob Storage, upserts
the per-genome aggregate into Mew (Postgres + pgvector), and emits an
`EmbeddingsReady` event to the `kanto.embedded` topic.

> The task spec was written referring to "OCI Object Storage" and
> "OCI Streaming". This codebase migrated to Azure (Blob Storage +
> Event Hubs Kafka API + AKS) before Task 7. All Azure-vs-OCI
> references in task-07-ditto-modal.md should be read as Azure.

## Why on Modal

GPU usage is bursty: ~5 GPU-hours/day at steady state, ~110 GPU-days
for the historical-data backfill. Modal's per-second billing wins
decisively over a permanently-attached GPU node pool, and the function
has a clean stateless contract that fits Modal's invocation model
naturally. The downside is that Ditto sits across a cloud boundary
from the rest of the pipeline (Mew, Blob Storage, Event Hubs are all
on Azure) — see §Cross-cloud connectivity below for the measured
impact.

## Layout

```
services/ditto/
├── pyproject.toml                 # workspace member; declares Modal + ML deps
├── README.md                      # you are here
├── src/ditto/
│   ├── modal_app.py               # `modal deploy` entry point
│   ├── pipeline.py                # embed-one-isolate orchestration
│   ├── model.py                   # ESM C 600M wrapper (bake + bf16 + no_grad)
│   ├── batching.py                # length-grouped batching (mandatory)
│   ├── aggregate.py               # length-weighted-mean per-genome aggregate
│   ├── fasta.py                   # gzipped-FASTA parser + truncation policy
│   ├── parquet_io.py              # Arrow schema + writer
│   ├── azure_io.py                # Blob Storage wrapper
│   ├── event_emit.py              # EmbeddingsReady producer wrapper
│   ├── config.py                  # DittoServiceSettings
│   ├── errors.py                  # Transient/Permanent/OOM hierarchy
│   └── scripts/
│       ├── benchmark.py           # cross-cloud timing breakdown (CLI)
│       └── cost_monitor.py        # daily Modal-spend alerter (CLI)
└── tests/                         # 64 unit tests, 97% coverage
```

## Deploy

```sh
# One-time per environment: create the Modal Secrets the function reads.
# (Operator action; see PREREQUISITES.md.)
modal secret create kanto-mew      ...
modal secret create kanto-azure    ...
modal secret create kanto-eventhubs ...
modal secret create kanto-otel     ...   # optional

# Tunables are read at deploy time from env vars on the deploying shell:
export KANTO_DITTO_GPU_TYPE=A10G          # default; see §GPU choice
export KANTO_DITTO_FUNCTION_TIMEOUT=600   # 10 minutes
export KANTO_DITTO_FUNCTION_RETRIES=2
export KANTO_DITTO_CONCURRENCY=4          # see §Backfill for the burst path

modal deploy services/ditto/src/ditto/modal_app.py
```

This builds the Modal image (which bakes the ESM C 600M weights into
the image layer — see §Image build), uploads it, and registers the
class as `kanto-ditto/DittoEmbedder.embed`. Snorlax's
`modal_client.py` looks up that reference and calls `.spawn()`.

## Local debug

```sh
# Single invocation against the deployed function in the dev workspace.
modal run services/ditto/src/ditto/modal_app.py \
    --accession PDT001234567 --version 1 --os-key PDT001234567/1.faa.gz

# Or run the pipeline in-process (no Modal). Requires `uv sync --extra gpu`
# on a Linux+CUDA box and Azure creds for the dev account.
ditto-benchmark --mode local \
    --accession PDT001234567 --version 1 --os-key PDT001234567/1.faa.gz
```

## Image build

The Modal image does five things, in order:

1. `debian_slim:python3.12` base.
2. `apt_install("git")` (esm needs it for setup_requires resolution).
3. `pip_install(...)` with the same pin set as `pyproject.toml` plus
   `torch==2.4.1` from PyTorch's CUDA 12.4 index. Pins are explicit;
   Modal's image cache keys on the literal pin set.
4. `run_function(_download_esmc_weights)` — invokes
   `ESMC.from_pretrained("esmc_600m")` at build time so the ~2 GB
   weight download lands in the image filesystem layer instead of on
   each cold start. **This is the bake-vs-lazy-load decision** — bake
   wins because the per-call savings dominate the image-size cost
   even for the bursty workload.
5. `add_local_python_source("ditto", "kanto_commons")` mounts our two
   workspace packages.

A bumped pin (esp. `esm`) re-runs all five steps on the next
`modal deploy`. Modal caches each layer; only changed layers are
re-uploaded.

## GPU choice

Modal's rate sheet (operator-confirmed 2026-05-15):

| GPU       | $/sec     | $/hr  | VRAM | bf16 | Notes                                |
|-----------|-----------|-------|------|------|--------------------------------------|
| T4        | 0.000164  | 0.59  | 16GB | no   | Turing — fp16 only; skipped.         |
| L4        | 0.000222  | 0.80  | 24GB | yes  | ~300 GB/s mem BW — bottlenecked.     |
| **A10G**  | 0.000306  | 1.10  | 24GB | yes  | **default** — 600 GB/s, sweet spot.  |
| L40S      | 0.000542  | 1.95  | 48GB | yes  | More VRAM, 1.8x cost.                |
| A100-40GB | 0.000583  | 2.10  | 40GB | yes  | **backfill** — 1.5 TB/s HBM.         |

**Default is A10G**, which is the cost/perf sweet spot for ESM C
600M at the 24 GB VRAM tier. ESM C 600M needs ~1.2 GB for weights at
bf16, so a length-grouped batch of ~64 short proteins or ~8
long-tail proteins fits comfortably. Switch to A100-40GB for backfill
bursts (see §Backfill).

The task spec recommended L40S; we deviate based on operator
guidance to minimise cost. The decision is one env var to flip.

## Batching strategy

This is the single highest-value piece of the Ditto code, per task §3
("5-10x perf difference; non-negotiable").

Naive batching wastes most of the GPU on padding: a batch of one
1500-residue protein and 60 100-residue proteins runs every position
out to length 1500, which is 96% padding. ESM C is a transformer with
quadratic attention in padded length, so the wasted work is huge.

The implemented strategy (`ditto/batching.py`):

1. **Sort proteins descending by length.** Longest is the seed of
   each batch.
2. **Greedy pack** subject to two constraints:
   - `len(batch) * longest_in_batch <= target_tokens_per_batch`
     (default 16 384) — bounds attention memory.
   - `len(batch) <= max_batch_size` (default 64) — caps kernel
     launch overhead.
3. **Singleton oversized proteins** (len > token budget) get their
   own batch; the pipeline's OOM-fallback path handles execution.
4. **Deterministic** — stable sort on (length DESC, id ASC) so
   re-runs produce byte-identical parquet output.

The pipeline's OOM handler retries a failed batch with size=1 once;
if a singleton still OOMs, that protein is too big for the configured
GPU and the genome fails permanently (logs which protein).

A `tests/unit/test_batching.py` test asserts that the worst-case
input from the task doc (one 1500 + sixty 100s) is at least 5x more
efficient under length-grouping than naive batching. A separate
`tests/integration/test_synthetic_benchmark.py` runs a 3,000-protein
exponentially-distributed proteome through the full in-process
pipeline and measures **6.43x padded-token efficiency** vs naive
single-batch packing (run with `pytest -m slow -s`).

## Per-genome aggregate

`aggregate = sum_i (length_i * embedding_i) / sum_i (length_i)`,
producing one 1152-dim float16 vector. Length is the **biological**
length (pre-truncation) — a long protein truncated to fit the model
still contributes its full residue count to the genome representation.

Precision: we accumulate in fp32 and cast to fp16 at the end.
Accumulating in fp16 saves a few KB but produces visible rounding
artifacts on a 12k-protein genome (the partial sums hit the fp16
significand limit). The fp32→fp16 cast at the boundary is recorded as
a §Decision below.

## Cross-cloud connectivity

Task §7 requires this be measured, not guessed. The
`ditto-benchmark` script produces a per-stage timing breakdown for
one invocation against the dev environment.

**Measurements pending operator-execution.** This box has no Azure
or Modal credentials and the bench cannot be run from here. Run:

```sh
ditto-benchmark --mode local --accession ... --version ... --os-key ...
ditto-benchmark --mode modal --accession ... --version ... --os-key ...
```

For both modes, expect the breakdown:

| Stage          | Local-mode (dev box) | Modal-mode (cross-cloud) |
|----------------|----------------------|--------------------------|
| blob_read      | regional latency     | + cross-cloud egress     |
| model_load     | one-shot (~30s cold) | one-shot per worker      |
| inference      | A10G/3000 proteins   | same                     |
| parquet_build  | ~50ms / 12k proteins | same                     |
| parquet_write  | regional latency     | + cross-cloud egress     |
| mew_write      | regional latency     | + cross-cloud egress     |
| event_emit     | regional latency     | + cross-cloud egress     |

The cross-cloud overhead per stage is what we can't predict in
advance. If the per-stage egress is more than ~200ms, batch size
should be tuned upward to amortise; if Mew writes individually exceed
500ms, consider buffering the upsert into the parquet write
transaction.

## Idempotency

Modal's public docs (verified 2026-05-15) do **not** document
`idempotency_key` as a parameter to `Function.spawn`, and the changelog
contains no entries for idempotency or deduplication. Snorlax's client
passes `_idempotency_key=…` (underscore-prefixed, so an internal/
undocumented kwarg). We do **not** rely on Modal-level dedupe.

App-level idempotency:

1. **Pre-flight check.** The pipeline reads the existing Mew
   `genome_embeddings` row for the accession. If present *and* the
   `model_version` matches, we short-circuit with `skipped_idempotent
   = True`. A model bump invalidates the check and re-embeds.
2. **Deterministic outputs.** Length-grouped batching is stable on
   (length DESC, id ASC). The parquet bytes are deterministic for
   the same input + model. So overwrite-on-write is safe.
3. **Idempotent Mew upsert.** `EmbeddingRepository.upsert` is `INSERT
   ... ON CONFLICT DO UPDATE`.
4. **Stream emit dedup at the consumer.** Downstream Alakazam dedupes
   on `(accession, version)` — duplicate `EmbeddingsReady` events are
   safe.

Test coverage: `test_pipeline::test_idempotency_short_circuits...`
exercises both the same-version skip and the different-version
re-embed paths.

## Failure modes & recovery

| Failure                                | Class                | Modal action      | Recovery                                                                                  |
|----------------------------------------|----------------------|-------------------|-------------------------------------------------------------------------------------------|
| Blob `get_bytes` 404                   | `PermanentError`     | no retry          | Snorlax never wrote the FASTA; investigate Snorlax DLQ.                                   |
| Blob `get_bytes` 5xx / network         | `TransientError`     | Modal retry x2    | None.                                                                                     |
| FASTA empty / malformed                | `FastaParseError`    | no retry          | Re-run Snorlax; FASTA bytes are corrupt.                                                  |
| Inference OOM at batch_size > 1        | recovered in pipeline | (none — handled) | Pipeline auto-retries the offending batch at size=1.                                      |
| Inference OOM at batch_size = 1        | `PermanentError`     | no retry          | Bump to a larger GPU (`KANTO_DITTO_GPU_TYPE=A100-40GB`) and re-run.                       |
| Inference NaN / inf                    | `InferenceError`     | no retry          | Investigate the FASTA — usually non-canonical residues; consider tightening Snorlax QC.   |
| Parquet write 5xx                      | `ParquetWriteError`  | Modal retry x2    | None.                                                                                     |
| Mew transaction error                  | `MewWriteError`      | Modal retry x2    | None; idempotent on re-run.                                                               |
| Event Hubs emit error (post Mew write) | `EventEmitError`     | Modal retry x2    | If retries exhausted: parquet + Mew already landed; run the backfill scanner (see below). |

### Recovery: parquet+Mew succeeded but event emit didn't

Pipeline order is parquet -> Mew -> event. If the function crashes
*between* the Mew commit and the event emit, the next Modal retry
hits the idempotency-skip path which **re-emits the event**. There
is no recovery gap; the operator does nothing.

This works because the event payload is fully determined by
``(accession, version, model_version)`` plus the canonical parquet
key — all of which are known from Mew's existing row — and consumers
dedupe on ``(accession, version)``.

## Backfill

For the ~110 GPU-day historical backfill, switch the deploy to a
beefier GPU and higher concurrency:

```sh
KANTO_DITTO_GPU_TYPE=A100-40GB \
KANTO_DITTO_CONCURRENCY=32 \
KANTO_DITTO_TARGET_TOKENS_PER_BATCH=65536 \
modal deploy services/ditto/src/ditto/modal_app.py
```

Then drain the backfill queue. After the burst, re-deploy with the
defaults to fall back to A10G concurrency=4 for steady state.

## Rollback

```sh
# List recent deploys.
modal app list
modal app history kanto-ditto

# Roll back to a specific revision.
modal app rollback kanto-ditto <revision>
```

The image is content-addressed, so rolling back is fast — Modal
just flips the function's image pointer.

## Cost monitoring

`ditto-cost-monitor` pulls the past two weeks of Modal usage,
aggregates per-day spend, and alerts if either:

* Today's spend exceeds N times the rolling 7-day average
  (`KANTO_DITTO_COST_ALERT_DAILY_MULTIPLIER`, default 2.0).
* Month-to-date projected spend exceeds the configured budget
  (`KANTO_DITTO_COST_ALERT_MONTHLY_BUDGET_USD`, default $500).

Run as a daily k8s CronJob; the script writes one JSON line to
stdout for log forwarders to ingest. Exit code 1 if any alert fires
(useful as a `CronJob.spec.concurrencyPolicy: Forbid` signal).

Pricing constants live at the top of `ditto/scripts/cost_monitor.py`;
update when Modal's rate sheet changes.

## Observability

* **Logs.** All log lines are structured (key=value pairs). Modal's
  console captures them; for long-term storage, configure a Modal
  log-forwarder (TODO; pending separate infra task).
* **Metrics.** Modal exposes per-function GPU utilisation, cold-start
  count, and concurrency natively. Custom metrics
  (`ditto.pipeline.batching`, `ditto.pipeline.inference`,
  `ditto.azure_io.*`, `ditto.event_emit.*`) are emitted as structured
  log fields and aggregated downstream.
* **Tracing.** Snorlax can pass a W3C `traceparent` kwarg through to
  `embed`; when present, the function rehydrates the OTel context.
  Modal does not propagate OTel automatically, so without an explicit
  `traceparent` arg the Ditto span starts a fresh trace.

## Decisions

* **A10G default, A100-40GB backfill.** §GPU choice. Deviates from
  the task §6 recommendation (L40S) on cost grounds.
* **Bake ESM C weights into the image.** §Image build. Bigger image,
  faster cold start.
* **bf16 inference, no autocast.** §Common Pitfalls. bf16 has fp32's
  exponent range — no overflow risk on attention scores, no
  GradScaler needed.
* **fp32 aggregate accumulation, fp16 final cast.** §Per-genome
  aggregate. Avoids fp16 precision loss on 12k-protein sums.
* **Truncate proteins > 2048 residues.** §fasta.py. Truncate (don't
  skip) so every protein appears in parquet; record both biological
  and post-truncation lengths.
* **App-level idempotency, not Modal-level.** §Idempotency. Modal's
  spawn-time idempotency is undocumented public API.
* **Length-weighted aggregate is the v1 formula.** §Per-genome
  aggregate. Per task §4. v2 may revisit.

## Tests

```sh
cd services/ditto
uv run pytest tests/unit/                    # 64 tests, 97% coverage
uv run pytest tests/integration/ -m gpu      # requires CUDA + ESM C weights
uv run pytest tests/integration/ -m integration  # requires deployed Modal + dev Azure
```

Coverage gate: 75% (lower than other services per task §12; GPU code
is integration-tested, not unit-tested). The actual coverage runs
~97%.

## Open follow-ups (out of Task 7 scope)

1. Modal log-forwarder to Azure Log Analytics (separate infra task).
2. Cross-cloud per-stage measurements — `ditto-benchmark --mode modal`
   needs to be run against the deployed function once the operator's
   Modal Secrets are populated. The script and result schema are in
   place; only execution + capture is pending.
