# Kanto

Kanto is a continuously-running pipeline that ingests every newly-published
bacterial pathogen genome from public surveillance databases (starting with
NCBI Pathogen Detection), converts each genome's proteins into vector
embeddings using a protein language model (ESM C), and flags isolates whose
embeddings sit anomalously far from everything we have seen before. The
output is a real-time queue of "genomes that look weird and warrant a second
look," targeted at biosurveillance analysts. v1 is bacterial-only,
single-region (`us-sanjose-1`), English-language reporting.

The architecture, scale numbers, and the rationale for every non-obvious
decision live in [`docs/design.md`](docs/design.md). Read that first.

---

## Repository structure

```
.
├── .github/workflows/                 GitHub Actions: ci.yml, secret-scan.yml
├── docs/                              Architecture and operations documentation
├── infrastructure/
│   └── terraform/                     Azure Terraform modules (VNet, AKS, Blob, Event Hubs, Mew)
├── libs/
│   └── kanto-commons/                  Shared Python code across services
├── scripts/                           Operational and pre-commit helper scripts
├── services/
│   ├── alakazam/                      Scorer (k-NN + novelty score) — AKS
│   │   └── helm/                      Helm chart deployed to AKS
│   ├── chatot/                        Alerter (Slack / PagerDuty / digest) — AKS
│   │   └── helm/                      Helm chart deployed to AKS
│   ├── ditto/                         Embedder (ESM C 600M on GPU) — Modal
│   ├── growlithe/                     Discoverer (NCBI FTP polling) — AKS
│   │   └── helm/                      Helm chart deployed to AKS
│   └── snorlax/                       Ingester + gene predictor — AKS
│       └── helm/                      Helm chart deployed to AKS
└── tests/                             Repository-level checks
```

Each Helm chart lives next to the service it deploys: edits to a service's
code, image, and chart belong in the same commit and the same review.
Cross-cutting infrastructure (the AKS cluster, the Mew database, Blob
Storage buckets, Streaming topics, IAM policies) lives under
`infrastructure/terraform/` because it is not owned by any single service.

Ditto is the exception to the per-service-Helm pattern: it runs on Modal,
not on AKS, so it does not have a Helm chart. Its Modal app definition
lives inside `services/ditto/` itself.

Mew — the PostgreSQL 16 + pgvector instance — is not a service in the code
sense, so it does not get a directory under `services/`. Its schema and
migrations live under `infrastructure/`.

---

## Getting started

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full local-setup
walkthrough, including `direnv`, `uv`, Azure CLI configuration, Modal
authentication, and pre-commit hooks.

The short version:

```bash
git clone git@github.com:annorak/kanto.git
cd kanto
cp .envrc.example .envrc        # edit local values
direnv allow
uv sync --group dev
pre-commit install
```

Before any of that works against real cloud resources, complete the
manual steps in [`PREREQUISITES.md`](PREREQUISITES.md).

---

## Components

| Component  | Role                                                         | Runs on | Pointer |
|------------|--------------------------------------------------------------|---------|---------|
| Growlithe  | Polls NCBI Pathogen Detection FTP, emits new-isolate events  | AKS     | [`services/growlithe/README.md`](services/growlithe/README.md) |
| Snorlax    | Downloads FASTA, runs Prodigal, hands off to Ditto on Modal  | AKS     | [`services/snorlax/README.md`](services/snorlax/README.md) |
| Ditto      | Embeds proteins with ESM C 600M, writes Parquet + Mew rows   | Modal   | [`services/ditto/README.md`](services/ditto/README.md) |
| Alakazam   | Computes per-genome novelty scores via pgvector k-NN         | AKS     | [`services/alakazam/README.md`](services/alakazam/README.md) |
| Chatot     | Filters above-threshold isolates and dispatches alerts       | AKS     | [`services/chatot/README.md`](services/chatot/README.md) |
| Mew        | PostgreSQL 16 + pgvector — metadata, embeddings, alerts      | Azure DB  | schema and migrations under `infrastructure/` |

---

## Documentation

- [`docs/design.md`](docs/design.md) — system design (current draft, v3)
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — local development setup and PR conventions
- [`SECURITY.md`](SECURITY.md) — secret-handling policy and vulnerability reporting
- [`PREREQUISITES.md`](PREREQUISITES.md) — manual setup for Azure / Modal / GitHub

---

## License

Kanto is currently developed under a proprietary working license while v1
is being built. A formal open-source decision will be made before any
external release; until then, redistribution is not authorized.
