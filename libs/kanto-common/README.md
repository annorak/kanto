# kanto-common

Shared Python code consumed by every Kanto service. The expected scope
includes the `IsolateDiscovered` / `EmbeddingsReady` / `IsolateScored`
event schemas, the OCI Streaming and Object Storage client wrappers,
the Repository abstraction over Mew (Postgres + pgvector + per-protein
Parquet on Object Storage), and the structured logging configuration.
Anything that needs to behave identically across Growlithe, Snorlax,
Ditto, Alakazam, and Chatot belongs here; anything that is genuinely
service-specific stays in its service directory. Keeping shared code in
one workspace member rather than five copies is the only way the "every
stage is idempotent on `(accession, version)`" contract holds across
the pipeline without subtle drift.

> **Status — placeholder.** No concrete shared modules have landed
> yet.
