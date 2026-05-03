# Ditto — the Embedder

Ditto is the GPU stage of the pipeline and the only Kanto component
that runs on Modal rather than OKE. It is defined as a Modal Function
with a GPU-attached image (L40S in steady state, H100 for backfill)
and auto-scales from zero based on queued invocations. For each
`(accession, version, os_key)` it receives, Ditto pulls the protein
FASTA from OCI Object Storage, tokenises and runs ESM C 600M forward
passes in length-grouped batches of ~64 sequences, takes the mean of
per-residue final-layer hidden states for each protein (1,152-dim
float16), computes a length-weighted mean as the per-genome aggregate,
writes per-protein embeddings as Parquet to
`os://kanto-embeddings/{accession}/{version}.parquet`, writes the
per-genome aggregate vector to Mew over public TLS with cert-based
auth, and emits an `EmbeddingsReady` event to `kanto.embedded`. The
hybrid OKE-plus-Modal split is justified by GPU economics: bursty
~5 GPU-hours/day at steady state, ~110 GPU-days for backfill — Modal's
per-second billing wins by a wide margin over an always-on GPU node
pool. `(accession, version)` is used as the Modal idempotency key so
concurrent re-invocations dedupe at the platform level.

> **Status — placeholder.** The Modal app definition, image build,
> ESM C weight pre-baking, Modal Secrets wiring, and tests have not
> landed yet. Ditto deploys to Modal, not OKE, so this directory will
> not contain a Helm chart.
