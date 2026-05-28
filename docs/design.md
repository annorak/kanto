# Kanto v1 — Design Document

**Status:** Draft v3
**Owner:** Varun Viswanathan
**Last updated:** May 2026

---

## 1. What Kanto is, in one paragraph

Kanto is a continuously-running pipeline that ingests every newly-published bacterial pathogen genome from public surveillance databases (starting with NCBI Pathogen Detection), converts each genome's proteins into vector embeddings using a protein language model (ESM C), and flags isolates whose embeddings sit anomalously far from everything we've seen before. The output is a real-time queue of "genomes that look weird and warrant a second look," targeted at biosurveillance analysts at agencies like CDC, FDA, USDA, and USAMRIID. v1 ships bacterial-only, single-region, English-language reporting.

**Naming.** The system is named after the Kanto region from Pokémon, because all the inhabitants are Pokémon: **Growlithe** (Discoverer), **Snorlax** (Ingester + Prodigal), **Ditto** (Embedder, runs on Modal), **Alakazam** (Scorer), **Chatot** (Alerter), and **Mew** (the Postgres + pgvector instance).

**Deployment model.** Kanto is a hybrid system. Everything except Ditto runs on Azure Kubernetes Service (AKS) with Azure Blob Storage and Azure Event Hubs (Kafka API) as the data and event layers. Mew is Azure Database for PostgreSQL with the pgvector extension. Ditto runs on Modal because the GPU workload is bursty and Modal's per-second billing eliminates the cost of always-on GPU nodes.

---

## 2. Goals and non-goals

### In scope for v1

- Ingest all new bacterial isolates published by NCBI Pathogen Detection (~1,000–3,000 per day at steady state).
- One-time backfill of all ~2 M historical isolates currently in NCBI PD.
- Embed every predicted protein in every genome with ESM C 600M on Modal.
- Index per-genome aggregate embeddings in Mew (pgvector).
- Persist per-protein embeddings to Blob as Parquet for on-demand drilldown.
- Compute a per-genome novelty score and surface flagged isolates via API + dashboard.

### Out of scope for v1 (YAGNI)

- Viral and fungal pathogens — different sequencing modalities, different reference data.
- Customer-submitted isolates (private upload path) — design hooks exist, deferred to v2.
- Multi-region, FedRAMP, IL5 — security accreditation is a v2 conversation.
- Generative tasks (designing countermeasures, predicting host adaptation) — that's v3 / Gengar.
- Per-protein global similarity search index — keep per-protein on Blob, build temporary FAISS indexes at investigation time.
- Re-embedding orchestration when ESM C v2 ships — supported by the architecture, not built in v1.

---

## 3. Background: the data

### What an isolate is

When a public health lab encounters a bacterium they care about, they grow it in pure culture, extract its DNA, and run it through an Illumina sequencer. The output is millions of short DNA reads (~150 chars each). An assembler stitches those reads back together into a draft genome, typically 1–10 megabases of A/T/G/C. That assembled genome plus its metadata is one **isolate**.

### What NCBI Pathogen Detection gives us

NCBI Pathogen Detection holds over 2 million bacterial assemblies and grows daily, covering Salmonella, E. coli, Listeria, Klebsiella pneumoniae, Staph aureus, and over 50 other taxa. We pull:

- **Genome FASTA files** (`.fna.gz`): one file per isolate, ~3 MB compressed, contains assembled DNA. **We do NOT persist this — NCBI is the source of record. We download once at processing time.**
- **Metadata TSV**: PDT accession, species, collection date, location, source, AMR profile, SNP cluster.
- **Exceptions TSV**: list of isolates that failed NCBI's QC.

Files live on the FTP site at `ftp://ftp.ncbi.nlm.nih.gov/pathogen/Results/<organism>/`, organized by organism group, with daily-updated PDG accessions (e.g., `PDG000000004.355`).

### Scale numbers

| Quantity | Value |
|---|---|
| Total historical isolates (backfill) | ~2,000,000 |
| New isolates per day | ~1,000–3,000 |
| Compressed FASTA size per isolate | ~3 MB |
| Proteins per bacterial isolate | ~3,000–5,000 (assume 4,000) |
| Total proteins, backfill | ~8 billion |
| Embedding dim (ESM C 600M) | 1,152 |
| Bytes per embedding (float16) | 2,304 |
| Per-protein Parquet size per isolate | ~7 MB |
| **Total per-protein Parquet on Blob (backfill)** | **~14 TB** |
| Per-genome aggregate vector size | 2.3 KB |
| **Total Mew (pgvector) data at backfill** | **~5 GB (~10 GB with HNSW index)** |
| Daily embedding compute | ~10 M proteins/day |
| Daily compute (L40S, batched) | ~5 GPU-hours |
| Backfill compute (H100) | ~110 GPU-days |

---

## 4. What we persist, and what we don't

| Data | Where it lives | Why |
|---|---|---|
| Raw genome FASTA | NCBI FTP only | NCBI is the source of record; we redownload on demand. |
| Protein FASTA (Prodigal output) | Blob (`blob://kanto-proteins/{accession}/{version}.faa.gz`) | We computed it; needed for re-embedding when ESM C upgrades; also the handoff mechanism to Ditto. |
| Per-protein embeddings | Blob as Parquet (`blob://kanto-embeddings/{accession}/{version}.parquet`) | Too large to index globally; loaded on demand for drilldown. |
| Per-genome aggregate embedding | Mew (pgvector) | Indexed for fast nearest-neighbor novelty scoring. |
| Isolate metadata + scores | Mew (Postgres tables alongside pgvector) | Relational queries: "show me all flagged Salmonella from this state in the last 30 days." |
| Alerts | Mew (Postgres) | Workflow state for analyst review. |

**Note:** raw FASTAs from NCBI are *not* persisted by us. If we need to reprocess a genome (e.g., new Prodigal version), we redownload from NCBI. NCBI's accessions are guaranteed available for the lifetime of the isolate; this is fine. Reprocessing rate is extremely low.

---

## 5. Architecture: the planes

Mirroring your logging system, Kanto has four logical planes, with the addition of a hybrid compute split between AKS and Modal.

```
┌─────────────────────────────────────────────────────────────┐
│  CONTROL PLANE          (Growlithe, on AKS)                  │
│  Polls NCBI FTP, emits "new isolate" events                  │
└─────────────────────────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│  EVENT PLANE            (Event Hubs + Modal queue)           │
│  Event Hubs: small pointer messages between AKS services     │
│  Modal queue: invisible internal queue for Ditto invocations │
└─────────────────────────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│  COMPUTE PLANE                                               │
│  AKS: Snorlax (CPU) → Alakazam → Chatot                      │
│  Modal: Ditto (GPU)                                          │
└─────────────────────────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│  STORAGE & QUERY PLANE  (Blob + Mew + API, on Azure)         │
│  Blob: protein FASTAs, per-protein Parquet                   │
│  Mew: metadata, per-genome embeddings, alerts                │
└─────────────────────────────────────────────────────────────┘
```

**Why no Kafka.** At Kanto's scale (thousands of messages per day, not per second), Kafka's strengths are not load-bearing. Azure Event Hubs (Kafka API) gives us at-least-once delivery, consumer groups, replay, and partition-based fan-out — the Kafka-API-compatible properties we actually need — without the operational cost of running our own broker cluster on AKS. The team's Kafka knowledge transfers directly because the consumer protocol is the same.

**Why Modal for Ditto.** Embedding is bursty (daily NCBI publishes arrive in clumps) and the GPU is our most expensive resource by an order of magnitude. An always-on L40S on AKS would be ~14% utilized at steady state. Modal's per-second billing means we only pay for the ~5 GPU-hours/day we actually use, and elastic scaling for backfill bursts is a config change rather than a Karpenter dance.

---

## 6. Pipeline stages

Five services, three Azure Event Hubs (Kafka API) streams, one Modal app. Streams carry only small pointer messages (under 4 KB each); all bulk data flows through Azure Blob Storage.

```
        ┌───────────────────────────┐
        │  Growlithe (Deployment)   │  Polls NCBI daily, on AKS
        └─────────────┬─────────────┘
                      │ writes IsolateDiscovered (~200 bytes)
                      ▼
            ┌────────────────────────┐
            │  kanto.discovered      │  Azure Event Hubs (Kafka API)
            └────────────┬───────────┘
                         │
                         ▼
        ┌───────────────────────────┐
        │  Snorlax (StatefulSet)    │  CPU pod, on AKS
        │  ┌─────────────────────┐  │
        │  │ 1. Download FASTA   │  │  ← from NCBI FTP
        │  │ 2. Run Prodigal     │  │  ← in same pod
        │  │ 3. Write to Blob    │  │  ← protein FASTA
        │  │ 4. Spawn Modal call │  │  ← directly invokes Ditto
        │  └─────────────────────┘  │
        └─────────────┬─────────────┘
                      │ Modal SDK call: ditto.embed.spawn(os_key=...)
                      ▼
        ┌───────────────────────────┐
        │  Ditto (Modal Function)   │  GPU, on Modal
        │  1. Read FASTA from Blob  │
        │  2. ESM C 600M inference  │
        │  3. Write Parquet to Blob │  ← per-protein embeddings
        │  4. Write to Mew          │  ← per-genome aggregate
        │  5. Write event to stream │
        └─────────────┬─────────────┘
                      │ writes EmbeddingsReady (~200 bytes)
                      ▼
            ┌────────────────────────┐
            │  kanto.embedded        │  Azure Event Hubs (Kafka API)
            └────────────┬───────────┘
                         │
                         ▼
        ┌───────────────────────────┐
        │  Alakazam (Deployment)    │  on AKS
        │  1. Query Mew for k-NN    │
        │  2. Compute novelty       │
        │  3. Write score to Mew    │
        └─────────────┬─────────────┘
                      │ writes IsolateScored (~300 bytes)
                      ▼
            ┌────────────────────────┐
            │  kanto.scored          │  Azure Event Hubs (Kafka API)
            └────────────┬───────────┘
                         │
                         ▼
        ┌───────────────────────────┐
        │  Chatot (Deployment)      │  on AKS
        │  Filters above threshold, │
        │  dispatches notifications │
        └───────────────────────────┘
```

### 6.1 Growlithe (Discoverer) — on AKS

A single Deployment, not a StatefulSet — no per-partition state. Runs a CronJob every 6 hours.

**What it does.** Lists NCBI Pathogen Detection FTP, downloads the latest metadata TSV per organism, diffs against the previous run's TSV (cached in Blob — metadata only, ~tens of MB), and writes one `IsolateDiscovered` event per new PDT accession to the `kanto.discovered` stream.

**Why polling.** NCBI doesn't offer webhooks. They update at most daily; latency is not a feature.

**Idempotency.** Growlithe keeps a "last seen accession version" cursor in Mew. Downstream consumers dedupe on `(accession, version)`.

**Adapter pattern lives here.** A `DataSource` interface allows future sources (ENA, GISAID for viruses, customer submissions) to plug in without touching downstream stages.

### 6.2 Snorlax (Ingester + Gene Predictor) — on AKS, colocated

StatefulSet, 1 partition per pod, 2 replicas per partition. CPU-bound. **Prodigal runs in the same pod as the downloader** because they share input data and identical lifecycle requirements; splitting them would mean writing the FASTA to disk between them for no reason.

**Input:** consumes from `kanto.discovered`. Each message contains `{accession, version, organism, ftp_path, metadata}`.

**What it does:**
1. Downloads FASTA from NCBI FTP into pod memory (or pod-local disk if memory pressure).
2. Validates the file (correct format, hashes, size sanity).
3. Runs Prodigal in-pod (`prodigal -i genome.fna -a proteins.faa -p single`). Typical wall time: 30 seconds.
4. Compresses the protein FASTA (gzip).
5. Writes the compressed protein FASTA to Blob at `blob://kanto-proteins/{accession}/{version}.faa.gz`.
6. **Spawns a Modal function call**: `ditto.embed.spawn(accession=..., version=..., os_key=...)`. This call returns immediately with a Modal call ID; Modal queues the work internally.
7. Updates Mew: `status = PROTEINS_READY, modal_call_id = ...`.

**Why directly invoke Modal instead of writing to a stream?** Modal's internal queue handles the AKS → Modal handoff cleanly. Adding an intermediate Azure Event Hubs (Kafka API) topic would be double-queuing — Modal still queues the call internally even if we have a stream in front of it. The cost is one architectural inconsistency: streams are used between AKS services, Modal SDK is used to invoke Modal. The boundary is clear and well-documented; juniors won't get confused.

**QC filtering.** Isolates appearing in NCBI's Exceptions TSV are recorded in Mew with `status = QC_FAILED` and reason; no Modal call is spawned.

**Failure handling.** If the Blob write succeeds but the Modal spawn fails, Snorlax retries the spawn (the Blob object is the durable artifact; the Modal call is just an invocation). If both fail, the Azure Event Hubs (Kafka API) consumer offset is not advanced and Snorlax will reprocess on retry. All operations are idempotent on `(accession, version)`.

### 6.3 Ditto (Embedder) — on Modal

A Modal Function with a GPU-attached container image. Auto-scales from zero based on queued invocations.

**Input:** Modal function arguments: `(accession, version, os_key)`.

**What it does:**
1. Fetches the protein FASTA from Azure Blob using the provided key. Uses the `azure-storage-blob` SDK with `DefaultAzureCredential` driven by a Modal secret.
2. Decompresses and parses the FASTA.
3. Tokenizes all proteins.
4. Runs ESM C 600M forward passes in batches of ~64 sequences, grouping by length to minimize padding waste.
5. For each protein: takes mean of per-residue final-layer hidden states → one 1,152-dim vector, stored as float16.
6. Computes per-genome aggregate: length-weighted mean of protein embeddings.
7. **Writes per-protein embeddings to Blob** as Parquet at `blob://kanto-embeddings/{accession}/{version}.parquet`.
8. **Writes per-genome aggregate vector to Mew** via the public Postgres endpoint with mTLS auth (credentials from Modal secret).
9. Updates Mew: `status = EMBEDDED`.
10. Writes `EmbeddingsReady` event to the `kanto.embedded` stream — small message, just `{accession, version, model_version}`.

**Modal Function definition (sketch):**

```python
import modal

app = modal.App("kanto-ditto")

image = (
    modal.Image.debian_slim()
    .pip_install("esm", "torch", "azure-storage-blob", "azure-identity", "psycopg[binary]", "pyarrow")
    .run_function(download_esm_c_weights)  # pre-bake weights into image
)

@app.function(
    image=image,
    gpu="L40S",
    secrets=[
        modal.Secret.from_name("azure-credentials"),
        modal.Secret.from_name("mew-credentials"),
    ],
    timeout=900,
    retries=modal.Retries(max_retries=3, backoff_coefficient=2.0),
)
def embed(accession: str, version: int, os_key: str):
    protein_fasta = fetch_from_oci(os_key)
    proteins = parse_fasta(protein_fasta)
    embeddings = run_esm_c(proteins)
    genome_embedding = aggregate(embeddings, proteins)

    write_parquet_to_oci(
        f"kanto-embeddings/{accession}/{version}.parquet",
        embeddings,
    )
    write_genome_embedding_to_mew(accession, version, genome_embedding)
    emit_event_to_streaming("kanto.embedded", {
        "accession": accession,
        "version": version,
        "model_version": "esm-c-600m-1.0.0",
    })
```

**Cold start.** First invocation after idle takes ~30 seconds (image pull is one-time per worker, model weights are baked into the image). Subsequent invocations on the same worker are sub-second to start. Modal keeps workers warm during bursts.

**Idempotency.** We pass `accession-version` as a Modal idempotency key. Modal deduplicates concurrent calls with the same key. Inside Ditto, all writes are idempotent (Blob overwrites are no-ops, Mew uses `INSERT ... ON CONFLICT`).

**Cost model.** L40S on Modal: roughly $2/hr equivalent at per-second billing. Daily spend at ~5 GPU-hours: ~$10/day, ~$300/month. Backfill burst: roughly $5–8K total for the full 2 M-isolate one-shot. Verify current Modal pricing before committing.

**Network path.** Modal can run in regions co-located with Azure; check current Modal region availability. If there's no Modal region in the same Azure region, egress from Azure Blob Storage to Modal applies. Verify current per-GB egress rates — at 14 TB of total backfill data, even small charges add up.

**Mew connectivity.** Mew exposes a TLS-enabled Postgres endpoint with cert-based auth. Modal connects directly. We do not put a proxy service in front — that'd be over-engineering for v1. Network ACLs on the Mew endpoint restrict access to Modal's egress IP ranges plus the AKS cluster's NAT IPs.

### 6.4 Alakazam (Scorer) — on AKS

Deployment (no per-partition state). CPU-bound, lightweight.

**Input:** consumes from `kanto.embedded` — just the accession.

**What it does:**
1. Looks up the per-genome aggregate vector in Mew using the accession.
2. Queries Mew for the 10 nearest neighbors (HNSW index, cosine distance).
3. Computes the novelty score using three components, combined linearly (Strategy pattern):
   - **NN distance**: mean cosine distance to the 10 nearest neighbors.
   - **Coverage**: of the genome's ~4,000 proteins, what fraction have a confident match against a small reference set of "common bacterial proteins" we keep on hand. This requires loading the per-protein parquet from Blob — **this is the only place in the v1 hot path where we read embeddings from Blob, and only for flagged candidates** (a cheap pre-filter on genome-level NN distance gates the expensive coverage check).
   - **Mahalanobis**: distance from the species centroid in embedding space.
4. Writes scores to Mew `isolates` table.
5. Writes `IsolateScored` event to `kanto.scored`.

**Tiered scoring.** ~95% of isolates get only the cheap genome-level check; ~5% trigger the full coverage check. Keeps Alakazam efficient and Blob reads minimal.

### 6.5 Chatot (Alerter) — on AKS — **deferred from v1**

> Chatot is not implemented in v1. The shape below is the intended v2
> design; until it ships, scored events accumulate on `kanto.scored`
> and the `alerts` table is populated only by ad-hoc scripts.

Deployment (no per-partition state).

**Input:** consumes from `kanto.scored`.

**What it does:** Filters events whose score exceeds the alert threshold. For matches, dispatches notifications via configured channels (Slack webhook, PagerDuty for critical, email digest for routine). Uses the Observer pattern — alert channels register as subscribers; new ones plug in via config.

---

## 7. Mew (the database)

Mew is a single PostgreSQL 16 instance with the pgvector extension, hosted on Azure Database for PostgreSQL. It holds:

1. **Isolate metadata** (relational): accession, organism, source, dates, AMR profile, novelty score, processing status.
2. **Per-genome embedding index** (pgvector): one HNSW index over 1,152-dim vectors. ~5 GB raw + ~5 GB index at 2 M genomes. Daily growth ~5 MB.
3. **Alerts** (relational): workflow state for analyst review.

**Sizing.** A single managed Postgres instance with 16 vCPU / 128 GB RAM and 200 GB SSD handles full backfill plus 5 years of growth. We move to a sharded setup or to Turbopuffer for the per-protein index *only* if v2 demands global per-protein search.

**Connectivity.**
- AKS services connect via the Azure VNet's private endpoint.
- Modal connects via a public TLS endpoint with mTLS cert-based auth, restricted by IP allowlist to Modal's documented egress ranges.

**Why one instance for both metadata and vectors.** KISS. Two databases means double the connection pools, double the migrations, double the backup operations. pgvector + plain Postgres in one place is the right call until proven otherwise.

### Schema

```sql
CREATE TABLE isolates (
    accession        TEXT PRIMARY KEY,
    version          INT NOT NULL,
    organism         TEXT NOT NULL,
    source           TEXT NOT NULL,
    collection_date  DATE,
    location         TEXT,
    source_type      TEXT,        -- 'clinical' | 'food' | 'environment'
    status           TEXT NOT NULL,
    qc_failure_reason TEXT,
    modal_call_id    TEXT,         -- for tracing Ditto invocations
    novelty_score    DOUBLE PRECISION,
    nn_distance      DOUBLE PRECISION,
    coverage         DOUBLE PRECISION,
    mahalanobis      DOUBLE PRECISION,
    above_threshold  BOOLEAN,
    discovered_at    TIMESTAMPTZ,
    scored_at        TIMESTAMPTZ,
    raw_metadata     JSONB
);

CREATE TABLE genome_embeddings (
    accession TEXT PRIMARY KEY REFERENCES isolates(accession),
    version   INT NOT NULL,
    model     TEXT NOT NULL,
    model_version TEXT NOT NULL,
    embedding vector(1152) NOT NULL
);

CREATE INDEX ON genome_embeddings USING hnsw (embedding vector_cosine_ops);

CREATE TABLE alerts (
    id              BIGSERIAL PRIMARY KEY,
    accession       TEXT REFERENCES isolates(accession),
    version         INT NOT NULL,
    score           DOUBLE PRECISION NOT NULL,
    triggered_at    TIMESTAMPTZ DEFAULT NOW(),
    status          TEXT DEFAULT 'OPEN',
    notes           TEXT
);
```

---

## 8. Data flow size summary

For one bacterial isolate (~4,000 proteins) at steady state:

| Hop | Bytes moving | Mechanism | Persisted? |
|---|---|---|---|
| NCBI FTP → Snorlax | ~3 MB compressed FASTA | HTTP(S) | No, ephemeral in pod |
| Snorlax in-memory: FASTA → Prodigal | ~10 MB uncompressed | local memory | N/A |
| Snorlax → Blob | ~500 KB–2 MB compressed protein FASTA | Blob PUT | Yes (`kanto-proteins` container) |
| Snorlax → Modal | ~200 bytes (function args with Blob key) | Modal SDK | Modal queue (transient) |
| Blob → Ditto (on Modal) | ~500 KB–2 MB | Blob GET (cross-cloud or same-region) | N/A |
| Ditto → Blob | ~7 MB Parquet (per-protein embeddings) | Blob PUT | Yes (`kanto-embeddings` container) |
| Ditto → Mew | 2.3 KB (per-genome aggregate) | Postgres INSERT over TLS | Yes (pgvector index) |
| Ditto → Azure Event Hubs (Kafka API) | ~200 bytes | stream produce | retained 7 days |
| Streaming → Alakazam | ~200 bytes | stream consume | N/A |
| Alakazam → Mew | k-NN query + UPDATE | SQL | Yes |
| Alakazam → Blob (only ~5%) | Read ~7 MB Parquet | Blob GET, gated | N/A |
| Alakazam → Azure Event Hubs (Kafka API) | ~300 bytes | stream produce | retained 7 days |
| Streaming → Chatot | ~300 bytes | stream consume | N/A |
| Chatot → outbound | tens of bytes per channel | webhook/email | external |

Total bytes leaving Snorlax per isolate: **roughly 1–2 MB** (one Blob write, one tiny Modal call).
Total Blob writes per isolate: **2** (Snorlax writes protein FASTA, Ditto writes embedding Parquet).
Total Blob reads per isolate on hot path: **1** (Ditto reads protein FASTA on Modal). Plus the optional Alakazam coverage check on ~5% of isolates.

---

## 9. One-time backfill vs daily steady-state

Two jobs sharing the same code, different orchestration.

### Steady-state (daily)
The streaming pipeline above. Growlithe wakes up, finds new isolates, the rest drains over hours. Modal scales Ditto from zero up to whatever the burst requires, then back to zero. ~5 GPU-hours/day, ~$10/day on Modal.

### One-time backfill
Different orchestration because pushing 2 M isolates through the daily streaming topology takes years and annoys NCBI.

1. **Bulk download** via NCBI Datasets CLI in batches of ~10,000, parallelized across 16 worker pods on AKS, rate-limited to ~10 concurrent FTP connections (be a polite citizen).
2. **Bulk Prodigal** runs on the same Snorlax image but invoked in batch mode against local files instead of streaming.
3. **Bulk embedding on Modal.** Spawn the same Ditto function but with up to 32 concurrent GPUs (configurable via `@app.function(concurrency_limit=32)`). Modal handles the GPU fleet entirely. Target completion: 1–2 weeks at ~110 GPU-days total. Estimated cost: $5–8K depending on H100 vs L40S split.
4. **Bulk indexing** writes to Mew via `COPY`, not `INSERT` — order of magnitude faster.
5. **Backfill scoring** runs once the index is fully populated, producing the baseline distribution of novelty scores used to set alert thresholds.

DRY: backfill workers reuse the exact same Snorlax download/Prodigal code and the exact same Ditto Modal function. They differ only in input source (filesystem batches vs streaming) and orchestrator (Argo Workflow vs streaming consumer group).

---

## 10. Failure modes and idempotency

Every stage is idempotent on `(accession, version)`. The contract: if a worker crashes mid-processing and the message gets redelivered, processing again produces the same final state.

- Azure Event Hubs (Kafka API) consumer offsets commit only after Blob write + Mew update + downstream stream produce all succeed.
- Blob writes are idempotent: same accession+version overwrites are no-ops semantically.
- Mew updates use `INSERT ... ON CONFLICT (accession) DO UPDATE`.
- Modal invocations use `(accession, version)` as the idempotency key; Modal dedupes concurrent calls with the same key.

**Poison messages** go to per-stage DLQ streams (`kanto.discovered.dlq`, `kanto.embedded.dlq`, `kanto.scored.dlq`) after 3 failed attempts. A daily DLQ-review job alerts on-call. For Modal, function-level retries are configured at 3 with exponential backoff; permanent failures are written to a `kanto.modal-failures` stream that Snorlax also subscribes to so it can mark the isolate as failed in Mew.

**Snorlax → Modal failure modes.**
- Blob write succeeds, Modal spawn fails: Snorlax retries the spawn. The Blob object is the durable artifact. Eventually succeeds or goes to DLQ.
- Blob write fails: Snorlax does not spawn Modal call. Stream offset not advanced; consumer retries the whole download+Prodigal. This is the safest failure mode because Prodigal is fast.
- Both succeed but the Mew status update fails: the isolate is in Blob and in Modal's queue but Mew doesn't know. On Snorlax retry, the dedup check on `(accession, version)` in Mew shows no row, so it reprocesses — but Modal dedups the call, so we don't waste GPU. End state is correct.

**NCBI accession version bumps.** `(accession, v=N+1)` is treated as fresh; the pipeline runs end-to-end and replaces the prior version's Mew entry.

**Model upgrades.** When ESM C v2 ships, we kick off a backfill job that reads protein FASTAs from Blob (the durability copy) and re-embeds via the same Modal function but with a new model version. New embeddings populate a new pgvector table; reads cut over atomically.

---

## 11. Design patterns earning their keep

| Pattern | Where | Why |
|---|---|---|
| **Adapter** | `DataSource` interface in Growlithe | Future sources (ENA, customer uploads) plug in identically. |
| **Producer-Consumer** | Azure Event Hubs (Kafka API) between AKS stages | Standard staged pipeline; mirrors logging system patterns. |
| **Strategy** | Alakazam scoring components | Add new scoring algorithms (engineered-sequence detector in v2) without touching the scorer skeleton. |
| **Observer** | Chatot subscriber list | New alert channels plug in without changing core logic. |
| **Repository** | Data access layer over Mew + Blob | One abstraction for "fetch isolate X with all its data"; trivially testable. |

Patterns explicitly *not* used:
- **Mediator** — Azure Event Hubs (Kafka API) already mediates.
- **Saga / orchestration framework** — strict DAG with idempotent stages, no compensation needed.
- **CQRS** — read and write models are the same shape.
- **Event sourcing** — streams support replay but Mew is source of truth for current state.

---

## 12. Service philosophy for v1

We have five services, deliberately not split further:

- **Snorlax + Prodigal in one pod** because they share input, lifecycle, and scaling profile. Splitting would mean an extra stream and an extra StatefulSet for zero benefit.
- **Alakazam separate from Chatot** because they have different change cadences (Alakazam evolves with scoring strategies; Chatot evolves with alert channels) and different responsibilities (computation vs external integration).
- **Ditto isolated on Modal** because its resource profile (bursty GPU) is fundamentally different from everything else. Hybrid AKS + Modal is two deployment systems but one logical service per component.
- **Mew = pgvector + metadata Postgres on one instance** because separating them would double our database operational surface for no current benefit.

Service boundaries can split later when seams emerge; they're hard to merge once they exist. Start coarse.

### The hybrid-deployment cost we accept

Running Ditto on Modal while everything else runs on AKS introduces real complexity:

- **Two CI pipelines.** AKS manifests deploy via your existing pipeline; Modal apps deploy via `modal deploy`.
- **Split observability.** Modal logs live in Modal's UI; we ship them to Azure Monitor logs via Modal's log forwarding to keep one place to look.
- **Cross-cloud secrets.** Modal needs Azure credentials (for Blob access) and Mew credentials. Stored as Modal Secrets.
- **Cross-cloud network.** Blob reads and Mew writes from Modal go over the public internet (TLS-secured). Egress from Azure to Modal is a recurring cost we monitor.

We accept all of this because GPU economics are favorable enough to make it worthwhile. A v2 review will revisit whether to bring Ditto back onto AKS GPU nodes if scale changes the calculus.

---

## 13. Threshold tuning

The novelty score is a continuous number; the alert threshold is the policy choice. We tune on a held-out set:

- **Negative set** (~10,000 isolates): randomly-sampled common, well-characterized strains. Should score *low*.
- **Positive set** built three ways: NCBI's "anomalous"-flagged isolates; synthetic perturbations (insert virulence factors from other species, confirm the modified genome scores higher); historical "missed by BLAST" cases from biosurveillance literature.

Threshold is set to achieve target precision (~50% true positives in alerts) at the highest achievable recall. Analyst feedback in production becomes new training signal for v2.

This is the part where ML rigor matters most. Budget two weeks of iteration before going live, not two days.

---

## 14. Open questions for v2 and beyond

- **Per-protein global similarity index** — defer until customer queries demand it; would require Turbopuffer at full scale.
- **Engineered-sequence detector** as a fourth scoring component.
- **Multi-tenancy** for customer-submitted isolates (separate index per tenant).
- **Real-time streaming sequencer ingestion** (live read streams instead of assembled genomes).
- **Federation across databases** (ENA, GISAID) via additional Adapters in Growlithe.
- **Re-embedding orchestration** when protein language models upgrade.
- **Bring Ditto back onto AKS GPU nodes** if scale changes the GPU economics or cross-cloud egress becomes painful.
- **Move Mew to private endpoint only** by introducing an AKS-side proxy service that Modal calls over a more locked-down protocol. Defer until security review demands it.

---

## 15. Out of scope, explicitly (the YAGNI list)

To prevent scope creep, v1 will NOT:

- Train custom ML (use ESM C off the shelf).
- Multi-region deploy.
- Active-active HA across regions.
- Customer auth beyond a shared API key.
- FedRAMP / IL5 accreditation.
- Polished customer UI; v1 is internal dashboard only.
- Generative protein design.
- Re-embedding orchestration.
- Metagenomic samples (mixed-organism); single-isolate only.
- Per-protein global similarity index (drilldown is on-demand FAISS).
- Custom Modal deployment automation; use `modal deploy` and standard CLI.
- Self-hosted GPU pool on AKS.

---

**Document ends.**
