# Alakazam — the Scorer

Alakazam is the novelty-detection stage. It runs as a stateless
Deployment on OKE, consumes the lightweight `EmbeddingsReady` events
from `kanto.embedded`, and writes a per-genome novelty score back to
Mew. For each isolate, it looks up the per-genome aggregate vector via
accession, queries Mew for the 10 nearest neighbours over the HNSW
index, and combines three components linearly using the Strategy
pattern: nearest-neighbour cosine distance, coverage (the fraction of
the genome's ~4,000 proteins that match a small reference set of
common bacterial proteins, gated by a cheap pre-filter so OS reads stay
rare), and Mahalanobis distance from the species centroid in embedding
space. Tiered scoring keeps the hot path efficient: ~95% of isolates
get only the cheap genome-level check; the expensive coverage check
fires for the ~5% that look unusual on the first pass — this is the
only place in the v1 hot path where Alakazam touches per-protein
Parquet on Object Storage. Once the score is written, Alakazam
publishes an `IsolateScored` event on `kanto.scored` for downstream
consumers.

> **Status — placeholder.** Scoring strategy implementations, the
> reference common-protein bundle, the Helm chart under `helm/`, and
> tests have not landed yet.
