# Alakazam — the Scorer

Alakazam is the novelty-detection stage of the Kanto pipeline. It
runs as a stateless `Deployment` on AKS, consumes `EmbeddingsReady`
events from `kanto.embedded`, computes a three-component novelty
score against the existing embedding population in Mew, writes the
score (and component breakdown) back to the isolate row, and emits
`IsolateScored` on `kanto.scored` for Chatot to alert on.

The score itself is the combination of three independent **scoring
strategies**, each answering a slightly different question:

1. **NN distance** — _how far is this genome's embedding from its
   nearest neighbors in the existing population?_
2. **Coverage** — _what fraction of this genome's proteins look
   nothing like our curated reference set of "every-bacterium-has-
   these" proteins?_
3. **Mahalanobis** — _how unusual is this genome's embedding inside
   its claimed species cloud, accounting for per-dimension variance?_

A linear combiner produces the final score. The
[`ScoringOrchestrator`](src/alakazam/scoring/orchestrator.py) wires
the three strategies together with **tiered scoring**: the cheap
NN-distance component runs for every isolate; the expensive coverage
and Mahalanobis components only fire for the ~5% of isolates whose
NN distance crosses the configured candidate threshold.

```
kanto.embedded ──► [Alakazam pod]
                       │
                       ├── Mew: load (isolate, embedding)
                       ├── Strategy[NN distance]       ← always
                       │
                       │  if NN distance >= candidate_threshold:
                       │      ├── Strategy[Coverage]         ← OS read
                       │      └── Strategy[Mahalanobis]      ← cached centroid
                       │
                       ├── Combiner: weighted mean of computed components
                       ├── Mew: write score + components (null where skipped)
                       └── kanto.scored: emit IsolateScored
```

The scoring weights, candidate threshold, and alert threshold in
v1 are **starting values**. Task 12 calibrates them against
held-out historical isolates; this README's defaults will likely
shift before the first production release.

---

## Repository layout

```
services/alakazam/
├── pyproject.toml           # alakazam + alakazam-centroids + builder scripts
├── Dockerfile               # alakazam consumer + centroid CronJob image
├── helm/
│   ├── Chart.yaml
│   ├── values.yaml          # defaults consumed by every env
│   ├── values-dev.yaml
│   ├── values-prod.yaml
│   ├── templates/
│   │   ├── deployment.yaml
│   │   ├── service.yaml
│   │   ├── serviceaccount.yaml
│   │   ├── networkpolicy.yaml
│   │   ├── secretproviderclass.yaml
│   │   ├── poddisruptionbudget.yaml
│   │   ├── centroid-cronjob.yaml
│   │   ├── NOTES.txt
│   │   └── _helpers.tpl
│   └── tests/               # helm unittest cases
└── src/alakazam/
    ├── service.py           # long-lived consumer + health server
    ├── pipeline.py          # per-event scoring orchestration + persist + emit
    ├── config.py            # KANTO_ALAKAZAM_* settings
    ├── metrics.py           # OTel instruments
    ├── mew_gateway.py       # alakazam-specific reads/writes against Mew
    ├── reference_set.py     # in-memory reference embedding matrix
    ├── protein_embeddings.py# per-protein parquet reader (OS)
    ├── species_cache.py     # in-process centroid cache
    ├── scoring/             # Strategy pattern: strategy / orchestrator / combiner
    └── scripts/
        ├── species_centroids_job.py   # CronJob entrypoint (alakazam-centroids)
        └── build_reference_set.py     # operator-only reference parquet build
```

---

## How to run locally

### Unit tests only (no Docker)

```bash
uv run --package alakazam pytest tests/unit
```

### Integration tests (Docker required)

The kanto-commons `mew_fixture` starts a `pgvector/pgvector:pg16`
container and applies the Alembic head. Integration tests cover the
gateway round-trip, the centroid job end-to-end, and the full
scoring pipeline against a real database.

```bash
DOCKER_HOST=unix:///${HOME}/.docker/run/docker.sock \
  uv run --package alakazam pytest tests/integration
```

### Run the consumer against live infrastructure

```bash
export KANTO_ENV=local
export KANTO_MEW_HOST=...           # see libs/kanto-commons/.envrc.example
export KANTO_OS_ACCOUNT_URL=...
export KANTO_STREAMING_BOOTSTRAP_SERVERS=...
uv run --package alakazam alakazam
```

### Run the centroid recomputation job

```bash
uv run --package alakazam alakazam-centroids --dry-run            # everything
uv run --package alakazam alakazam-centroids --organism Salmonella   # one species
```

`--dry-run` computes the centroids but does not write — useful for
sanity-checking before a production refresh.

---

## Scoring components

### NN distance (always, tier-1)

* Query: pgvector `<=>` (cosine distance) against the HNSW index on
  `genome_embeddings.embedding`, excluding the seed accession itself.
* Score: mean cosine distance to the `nn_k` (default 10) nearest
  neighbors. Higher = more unusual relative to the existing population.
* Cost: one HNSW lookup; ~milliseconds.
* Edge cases:
  * **No neighbors** (cold start): score=0.0, tagged `no_neighbors`.
    The orchestrator interprets a zero NN as "no signal" and does
    not promote to tier 2.
  * **Mew error**: skipped with `compute_error`; orchestrator
    short-circuits to tier-1-only.

### Coverage (tier-2, expensive)

* Query: download the per-protein parquet for this genome from Object
  Storage (the same blob Ditto wrote), L2-normalize the rows, then
  matmul against the L2-normalized reference matrix. For each genome
  protein, take the max similarity to any reference; count how many
  exceed `coverage_match_threshold`.
* Score: `1 - recognized_fraction`. A genome that matches the whole
  reference set scores 0; a genome with no matches scores 1.
* Cost: one OS read (~28 MB for a ~4k-protein genome) plus a small
  matmul. The OS read dominates.
* Tradeoff: this is **the only place Alakazam touches per-protein
  parquet** in the hot path. Tiered scoring exists so this read
  fires on ~5% of isolates, not 100%.
* Edge cases:
  * **Reference set missing in OS**: the strategy skips with
    `reference_set_missing` and Alakazam degrades gracefully to
    tier-1-only scoring across the whole fleet. A startup log line
    surfaces the failure; the operator should rebuild the reference
    bundle and rolling-restart Alakazam.
  * **Per-protein parquet missing**: skipped with
    `protein_parquet_missing` (usually means upstream Ditto wrote
    nothing or its retention reaped the blob).

### Mahalanobis (tier-2, expensive)

* Query: read the cached species centroid + diagonal covariance for
  this isolate's organism (the cache reloads from Mew every
  `centroid_refresh_seconds`).
* Score: `sqrt(sum_i ((x_i - mu_i)^2 / (sigma_i^2 + eps)))`.
  Standardized distance from the species mean, normalized per
  dimension.
* Cost: pure numpy, ~milliseconds. The cache lookup is in-process.
* Edge cases:
  * **Centroid missing for organism** (new species, never recomputed):
    skipped with `centroid_missing`. Operator should run the
    `alakazam-centroids` job once the species has enough samples.
  * **Centroid computed from too few isolates**: skipped with
    `centroid_insufficient_samples`. The variance estimate from
    <20 samples (default) is noisy enough that the Mahalanobis
    distance becomes unreliable; we'd rather skip than mislead.
  * **Diagonal vs full covariance**: v1 stores per-dimension
    variance only. See the migration docstring for the rationale; a
    full covariance matrix would be ~5 MB per species and is
    overkill for the standardized-distance reduction we actually
    need. The schema reserves room for a sibling column if v2
    proves it matters.

### Combiner

[`LinearCombiner`](src/alakazam/scoring/combiner.py) takes the
weighted mean of the **computed** components, normalized by the sum
of the weights that contributed. A tier-1-only result is directly
comparable to a tier-2 result on the same scale, so `alert_threshold`
is a single number rather than three.

Worked example with default weights (NN=1.0, COV=2.0, MAHA=0.5):

```
Tier 1 only:                NN=0.4
  → 0.4 * 1.0 / 1.0 = 0.40

Tier 2 fired (typical):     NN=0.5, COV=0.3, MAHA=2.0
  → (0.5*1.0 + 0.3*2.0 + 2.0*0.5) / 3.5 = 0.60
```

---

## Tiered scoring tradeoff

| Cost                | Tier 1 (always) | Tier 2 (~5%)                   |
| ------------------- | --------------- | ------------------------------ |
| Mew reads           | 1 HNSW lookup   | + 1 centroid cache read        |
| Object Storage      | 0               | + 1 parquet download (~28 MB)  |
| CPU                 | <1 ms           | +30–50 ms (parquet + matmul)   |
| Mahalanobis math    | 0               | +<1 ms                         |

The candidate threshold is the lever: lowering it brings more
isolates into tier 2 (more coverage signal, more OS load); raising
it concentrates the expensive components on the most suspicious
genomes (less signal, much cheaper).

**Isolates that don't cross the candidate threshold get a final
score = NN distance alone**, with `coverage` and `mahalanobis`
stored as SQL NULL in Mew so analysts can distinguish "didn't run"
from "ran and scored zero". The emitted `IsolateScored` event uses
`0.0` for skipped components because the schema is non-nullable;
Mew is the canonical source of truth for the component breakdown.

---

## Reference set (BUSCO + KEGG core bacterial genes)

The coverage strategy depends on a curated parquet at the
configured Object Storage key (default
`kanto-metadata/references/common_proteins_v1.parquet`). The bundle
is the union of:

* **BUSCO bacterial single-copy orthologs** (`bacteria_odb10`
  lineage set, ~124 ortholog families): every bacterium should have
  exactly one copy of each. Source URL is in the build script's
  comments — pin the snapshot version when you build.
* **KEGG core bacterial gene set** (~1.5–3k proteins): central
  metabolism, ribosomal proteins, translation machinery. The exact
  list is snapshot-locked per build; document the snapshot ID in
  the build script's TSV header.
* **Manual additions** as needed for organisms that show systematic
  gaps in BUSCO+KEGG. Tagged `manual` in the source column.

### Building the bundle

`alakazam-build-reference` is the operator-facing assembly script.
The Modal embed step (the actual ESM-C-600M forward pass) lives in
Ditto and is invoked separately so we don't ship CUDA wheels into
Alakazam's image. The recipe:

1. Assemble the FASTA: download BUSCO `bacteria_odb10`, pull the
   KEGG core gene list, splice in the manual additions. Write
   `reference_proteins.fasta`.
2. Write `reference_sources.tsv` with `protein_id\tsource` per row
   (one of `busco | kegg | manual`).
3. Run the Ditto Modal embedder over the FASTA and persist the
   resulting `(n, 1152)` float16 ndarray as `embeddings.npy`. (See
   `services/ditto/README.md` for the Modal invocation.)
4. Build the parquet:
   ```bash
   uv run --package alakazam alakazam-build-reference \
       reference_proteins.fasta \
       --sources reference_sources.tsv \
       --dry-run            # iterates through assembly without uploading
   ```
5. Upload the resulting `common_proteins_v1.parquet` to
   `${KANTO_OS_METADATA_CONTAINER}/references/common_proteins_v1.parquet`.
6. If you bumped the bundle version, set
   `KANTO_ALAKAZAM_REFERENCE_SET_KEY` (or the
   `alakazam.referenceSetKey` Helm value) to the new key and roll
   the deployment. Older bundle versions are kept in OS for rollback.

The Alakazam pod loads the bundle **once at startup** into an
in-memory `(R, D)` float32 matrix (where `R ≈ 2–3k` and `D = 1152`).
Refresh is a pod restart; v2 will add a sidecar refresher that
re-embeds on a schedule when the upstream snapshots roll.

---

## Species centroid CronJob

The Mahalanobis strategy needs a per-species centroid + diagonal
variance computed from the existing population. The
`alakazam-centroids` CronJob recomputes them daily (default schedule
`0 3 * * *`).

For each organism with at least `KANTO_ALAKAZAM_CENTROID_MIN_ISOLATES`
(default 10) embedded isolates in Mew:

1. Stream every embedding for that organism via the gateway.
2. Compute the mean and per-dimension population variance in float32.
3. Upsert into `species_centroids` with the chosen regularization
   epsilon.

Concurrency is `Forbid` so a stuck run never gets lapped by the next
schedule. Daily cadence is overnight to avoid analyst-dashboard
contention on Mew; bump it during the initial backfill, then drop
back to daily.

Manual triggers (operator-initiated):

```bash
# Recompute every species above the floor:
kubectl -n kanto-alakazam create job \
    --from=cronjob/kanto-alakazam-centroids \
    alakazam-centroids-manual-$(date +%s)

# Recompute one species, dry-run:
kubectl -n kanto-alakazam exec deploy/kanto-alakazam -- \
    alakazam-centroids --organism Salmonella --dry-run
```

---

## Configuration reference

All knobs surface as `KANTO_ALAKAZAM_*` env vars (Helm renders
each as a deployment env entry). Defaults below match
`AlakazamServiceSettings`.

| Env var | Default | Purpose |
| ------- | ------- | ------- |
| `KANTO_ALAKAZAM_CONSUMER_GROUP` | `alakazam` | Kafka consumer group ID. |
| `KANTO_ALAKAZAM_CONSUMER_ID` | `$POD_NAME` | Per-pod consumer identifier. |
| `KANTO_ALAKAZAM_WEIGHT_NN` | `1.0` | NN-distance weight in the combiner. |
| `KANTO_ALAKAZAM_WEIGHT_COVERAGE` | `2.0` | Coverage weight in the combiner. |
| `KANTO_ALAKAZAM_WEIGHT_MAHALANOBIS` | `0.5` | Mahalanobis weight in the combiner. |
| `KANTO_ALAKAZAM_CANDIDATE_THRESHOLD` | `0.30` | NN-distance value above which tier-2 fires. |
| `KANTO_ALAKAZAM_NN_K` | `10` | k for the NN-distance lookup. |
| `KANTO_ALAKAZAM_REFERENCE_SET_CONTAINER` | `kanto-metadata` | OS container for the reference parquet. |
| `KANTO_ALAKAZAM_REFERENCE_SET_KEY` | `references/common_proteins_v1.parquet` | OS key for the reference parquet. |
| `KANTO_ALAKAZAM_COVERAGE_MATCH_THRESHOLD` | `0.70` | Cosine-sim threshold above which a protein is "recognized". |
| `KANTO_ALAKAZAM_COVERAGE_MAX_PROTEINS` | `20000` | Hard cap on per-genome proteins. |
| `KANTO_ALAKAZAM_MAHALANOBIS_MIN_SAMPLES` | `20` | Skip Mahalanobis below this centroid sample count. |
| `KANTO_ALAKAZAM_MAHALANOBIS_REGULARIZATION` | `1e-4` | Floor on diagonal variance. |
| `KANTO_ALAKAZAM_ALERT_THRESHOLD` | `0.60` | Combined-score threshold for `above_threshold=true`. |
| `KANTO_ALAKAZAM_CENTROID_REFRESH_SECONDS` | `3600` | In-process centroid cache TTL. |
| `KANTO_ALAKAZAM_CENTROID_MIN_ISOLATES` | `10` | Minimum population for the centroid job to run on a species. |
| `KANTO_ALAKAZAM_CENTROID_ORGANISM_FILTER` | _(unset)_ | Comma-separated organism allowlist for the job. |
| `KANTO_ALAKAZAM_MEW_MAX_ATTEMPTS` | `5` | Mew write retry budget. |
| `KANTO_ALAKAZAM_HEALTH_PORT` | `8081` | aiohttp `/healthz`+`/readyz` port. |
| `KANTO_ALAKAZAM_METRICS_PORT` | `9464` | Prometheus scrape port. |
| `KANTO_ALAKAZAM_RUN_ONCE` | `false` | Process one message then exit. Smoke-test only. |

Universal kanto-commons env vars (Mew, Object Storage, Streaming,
OTel) are documented in `libs/kanto-commons/README.md`.

---

## Observability

| Metric | Type | Description |
| ------ | ---- | ----------- |
| `kanto_alakazam_events_processed_total` | counter | Drained from `kanto.embedded`. |
| `kanto_alakazam_events_succeeded_total` | counter | Fully processed end-to-end. |
| `kanto_alakazam_events_failed_total{category=...}` | counter | Terminal per-isolate failure (isolate/embedding missing, etc). |
| `kanto_alakazam_events_dlqed_total` | counter | Sent to DLQ after retry exhaustion. |
| `kanto_alakazam_novelty_score` | histogram | Final combined novelty score. |
| `kanto_alakazam_component_score{component=...}` | histogram | Per-component score. |
| `kanto_alakazam_component_duration_seconds{component=...}` | histogram | Per-component latency. |
| `kanto_alakazam_candidates_promoted_total` | counter | Isolates promoted into tier 2. |
| `kanto_alakazam_component_skipped_total{component,reason}` | counter | Strategy skips (categorized by reason). |
| `kanto_alakazam_in_flight_isolates` | gauge | Live in-flight count per pod. |
| `kanto_alakazam_species_cache_size` | gauge | Centroids currently held in the cache. |

The `candidates_promoted_total / events_processed_total` ratio is
the **tier-2 promotion rate** — keep it near the design target of
5%. Drift indicates either the candidate threshold needs retuning
or the embedding population has shifted.

---

## Operational runbook

### "All isolates are scoring above the alert threshold"

1. Check `kanto_alakazam_component_score{component=nn_distance}` —
   if every isolate's NN distance is huge, the embedding population
   in Mew is probably tiny (cold start) or your model rolled and the
   new embeddings don't cluster with the old ones.
2. Check the centroid cache size gauge. A zero means the centroid
   job has never run successfully on this Mew — Mahalanobis is
   contributing nothing. Run `alakazam-centroids` manually.
3. If the coverage histogram is concentrated near 1.0, the reference
   set is probably missing or stale. Look for
   `alakazam.service: failed to load reference set` in the startup
   logs and rebuild the bundle.
4. Last resort: lower `KANTO_ALAKAZAM_ALERT_THRESHOLD` via a
   rolling deploy, *then* hand the problem to Task 12 for
   recalibration.

### "Coverage scores look implausibly high (every isolate is novel)"

* Validate the reference bundle: download the parquet from OS,
  load it locally, and spot-check that the protein IDs are what
  you expect (`busco:OG…` / `kegg:K…`).
* Confirm the embedding dimension matches: the parquet's
  `embedding` column must be `fixed_size_list[float16, 1152]` for
  ESM-C-600M. Rebuilding under a different model produces silently
  garbage cosine similarities.
* If you recently rolled the embedding model, the bundle needs
  re-embedding under the new model and an OS key bump.

### "Mahalanobis is skipping every isolate"

* Read the skip-reason metric:
  `kanto_alakazam_component_skipped_total{component=mahalanobis}`.
* `centroid_missing` for most organisms: run the centroid job.
* `centroid_insufficient_samples`: most organisms have fewer than
  the floor. Either lower `MAHALANOBIS_MIN_SAMPLES` (risk: noisy
  variances) or wait for more isolates.
* Centroid job is failing silently: check the CronJob's recent runs
  in `kubectl -n kanto-alakazam get jobs`. The job's pod logs show
  the per-organism progress and any DB error.

### "Consumer is lagging on `kanto.embedded`"

* `kubectl -n kanto-alakazam scale deploy/kanto-alakazam --replicas=N`
  up to the partition count of the topic. Above the partition count,
  extra replicas idle.
* If tier-2 promotion rate has spiked, the coverage component is
  burning OS bandwidth. Inspect
  `kanto_alakazam_component_duration_seconds{component=coverage}`
  — long latencies plus high promotion rate together mean the
  candidate threshold drifted; raise it until you're back near the
  design target.

### "I rolled the embedding model and Alakazam started misbehaving"

The centroid rows + the reference parquet are both **model-tagged**.
A model swap requires:

1. Rebuild the reference bundle under the new model (see "Building
   the bundle"). Bump the OS key.
2. Recompute every centroid by truncating `species_centroids` and
   running the centroid job; the job tags each row with
   `model` + `model_version` so the next scoring run can verify it.
3. Roll the Alakazam deployment with the new reference key.

Pre-rollout consistency check: every row in `species_centroids`
should match every row in `genome_embeddings` on
`(model, model_version)`. A mismatch means the centroid job ran
against partial data; recompute.

### Refreshing the reference set

For a bundle content change (added KEGG genes, removed deprecated
BUSCO orthologs):

1. Follow "Building the bundle" above.
2. Upload to a *new* OS key (e.g. `references/common_proteins_v2.parquet`)
   — do **not** overwrite the v1 key, so a rollback is one helm
   `--set` away.
3. Roll out the Helm chart with
   `--set alakazam.referenceSetKey=references/common_proteins_v2.parquet`.

Coverage scores will drift across the transition; expect a few
isolates to flip from "above threshold" to "below" or vice versa.

---

## What this service does NOT do (v1)

* **Threshold tuning** — Task 12 calibrates `candidate_threshold`,
  `alert_threshold`, and the weights against historical isolates.
  v1 ships starting values.
* **Engineered-sequence detection** — explicit detection of
  laboratory designs lives in the v2 roadmap as a fourth Strategy.
* **Real-time score updates** — each isolate is scored once at
  processing time; the existing population drifting doesn't
  retroactively re-score old isolates. v2 adds a sweeping
  re-scorer.
* **Ad-hoc per-isolate scoring API** — v1 is stream-driven only.

---

## Extending the scorer

To add a fourth Strategy (engineered-sequence detection, v2):

1. Implement the `ScoringStrategy` protocol in
   `src/alakazam/scoring/<your_strategy>.py`. The protocol is two
   members: `name: ComponentName` and
   `async def compute(self, ctx: IsolateContext) -> ComponentResult`.
2. Add a new value to `ComponentName` and (if needed) `SkipReason`.
3. Update `LinearCombiner` to weight the new component (or
   introduce a `MultiplicativeCombiner` if the new component needs
   different math — both implement the `ScoreCombiner` protocol).
4. Add per-component score / duration metrics to
   `AlakazamMetrics.record_scoring`.
5. Add the new strategy to `ScoringOrchestrator` (either tier-1 if
   cheap, or behind the candidate threshold if expensive).
6. Add a unit test that exercises the strategy in isolation, plus
   an integration test that verifies the combined score across all
   strategies.

The Strategy pattern's whole point is that this is a small,
contained change. Resist the temptation to broaden the
`ScoringStrategy` interface to expose component-specific knobs —
push those into the strategy's constructor and keep the protocol
narrow.
