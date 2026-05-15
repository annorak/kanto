# kanto-commons

Shared Python library every Kanto service depends on. Holds the
event schemas, the Event Hubs (Kafka) / Azure Blob Storage / Mew
(Postgres + pgvector) client wrappers, structured logging,
OpenTelemetry tracing, configuration, and the test helpers services
use in their own suites.

> Status: storage backend migrated from OCI to Azure Blob in Task 5
> (v0.3.0). Coverage 85%+, mypy strict, ruff clean.

---

## What lives here, at a glance

| Concern             | Module                                              |
| ------------------- | --------------------------------------------------- |
| Event schemas       | `kanto_commons.schemas`                              |
| Azure Blob Storage  | `kanto_commons.storage`                              |
| Mew (Postgres)      | `kanto_commons.mew`                                  |
| Event Hubs (Kafka)  | `kanto_commons.streaming`                            |
| Logging             | `kanto_commons.logging`                              |
| Tracing             | `kanto_commons.tracing`                              |
| Configuration       | `kanto_commons.config`                               |
| Test helpers        | `kanto_commons.testing`                              |

The most-used types are re-exported from the top level; service code
should usually `from kanto_commons import IsolateDiscovered, ...`
rather than reach into submodules.

---

## Public API the services rely on

### Schemas

Pydantic v2 models, frozen, `extra="forbid"`. Every schema carries a
`schema_version: int`, defaults to 1 today.

```python
from kanto_commons import (
    IsolateDiscovered, ProteinsReady, EmbeddingsReady, IsolateScored,
    EventEnvelope, EVENT_REGISTRY, Transport, DLQEnvelope,
)

evt = IsolateDiscovered(
    accession="PDT001234.1",
    version=1,
    organism="Salmonella",
    source="ncbi-pd",
    ftp_path="ftp://ftp.ncbi.nlm.nih.gov/...fna.gz",
)
EVENT_REGISTRY[evt.EVENT_NAME].topic   # "kanto.discovered"
```

`ProteinsReady` is the only schema not produced to a stream — it's the
structured argument Snorlax passes into the Modal Ditto function. The
registry tags it `Transport.MODAL`.

### Streaming wrapper

Async, aiokafka under the hood, but the API is Kanto-specific. The
producer wraps every event in `EventEnvelope`, attaches W3C
`traceparent` headers, and acks `all` for at-least-once delivery.

```python
from kanto_commons.streaming import StreamingProducer, StreamingConsumer, lifecycle

producer = StreamingProducer.from_settings(settings.streaming)
consumer = StreamingConsumer.from_settings(
    settings=settings.streaming,
    topic="kanto.discovered",
    group="snorlax",
    consumer_id=settings.service_name + "-0",
)

async with lifecycle(producer, consumer):
    async def handle(msg):
        # trace context is already activated for spans you create here
        ...
    await consumer.run(handle)   # auto retry → DLQ after max_attempts
```

`run()` commits offsets only after the handler returns successfully;
on failure it tracks attempts and routes to `<topic>.dlq` after the
budget is exhausted (`KANTO_STREAMING_MAX_PROCESSING_ATTEMPTS`,
default 3).

DLQ messages are `DLQEnvelope` instances containing the original raw
bytes, a failure reason, partition/offset, consumer identity, and
attempt count.

### Object Storage wrapper

Sync. Methods take keyword args; tunables (chunk thresholds) live on
`ObjectStorageSettings`. Builds canonical Kanto keys via `KeyBuilder`.

```python
from kanto_commons.storage import ObjectStorageClient, KeyBuilder

# In-cluster: AKS Workload Identity is picked up automatically via
# DefaultAzureCredential. Locally, ``az login`` works the same way.
os_client = ObjectStorageClient.from_settings(settings=settings.object_storage)
keys = KeyBuilder.from_settings(settings.object_storage)

os_client.put_bytes(
    container=keys.proteins_container,
    key=keys.protein_fasta_key("PDT001", 1),
    data=fasta_bytes,
    content_type="application/x-gzip",
    if_not_exists=False,        # overwrite is a no-op semantically
)
```

`put_stream` and `get_stream` accept file-like objects so the protein
FASTA never needs to fully sit in memory.

Errors map to Kanto-specific exceptions (`ObjectNotFoundError`,
`ObjectAlreadyExistsError`); the underlying
`azure.core.exceptions.HttpResponseError` is treated as transient on
5xx/429 and retried with exponential backoff via tenacity.

### Mew client + repositories

Async, psycopg 3, pgvector adapter pre-registered. All raw SQL lives
in `kanto_commons.mew.repositories`; service code never writes SQL.

```python
from kanto_commons.mew import (
    MewClient,
    IsolateRepository, EmbeddingRepository, AlertRepository,
    IsolateRow, IsolateStatus, EmbeddingRow,
)

mew = await MewClient.from_settings(settings.mew)
isolates, embeddings, alerts = (
    IsolateRepository(),
    EmbeddingRepository(),
    AlertRepository(),
)

async with mew.connection() as conn:
    await isolates.upsert(conn, IsolateRow(...))
    neighbors = await embeddings.k_nearest(conn, accession="PDT001", k=10)

async with mew.transaction() as conn:
    # multi-statement atomic block
    await isolates.upsert(conn, row)
    await embeddings.upsert(conn, embedding)
```

Repository methods take an `AsyncConnection` so transaction
composition is always visible at the call site.

Schema migrations are owned by Task 3 (Alembic). For the moment the
tables are created via the hard-coded DDL in
`kanto_commons.testing.bootstrap_schema` — see "Testing helpers" below.

### Configuration

Pydantic-settings, `KANTO_*` env-var prefix, nested sections via `__`.
Service authors derive a small subclass:

```python
from kanto_commons.config import KantoBaseSettings

class SnorlaxSettings(KantoBaseSettings):
    service_name: str = "snorlax"
    ncbi_ftp_root: str = "ftp://ftp.ncbi.nlm.nih.gov/pathogen"
    prodigal_threads: int = 4

settings = SnorlaxSettings.load()       # raises pydantic.ValidationError
                                        # listing every missing env var
```

Passwords are `SecretStr`. `MewSettings.dsn()` returns the libpq URI;
`MewSettings.safe_dsn()` is the same with the password redacted (use
this in any log message).

Every Kanto runtime variable is `KANTO_*`-prefixed; `.envrc.example`
matches. The two short-form aliases that exist are
`KANTO_ENV` (= `KANTO_ENVIRONMENT`) and
`KANTO_STREAMING_BOOTSTRAP` (= `KANTO_STREAMING_BOOTSTRAP_SERVERS`).

### Logging

structlog. JSON in production, human-readable colored output for
`KANTO_LOG_FORMAT=console`. Every record includes `service`,
timezone-aware `timestamp`, OTel `trace_id`/`span_id` when a span is
active, and `isolate_accession` when bound via the helper.

```python
from kanto_commons.logging import setup_logging, get_logger, bind_accession

setup_logging(
    service_name=settings.service_name,
    log_level=settings.log_level,
    log_format=settings.log_format,
)
log = get_logger(__name__)

with bind_accession("PDT001234.1"):
    log.info("downloaded fasta", bytes=len(blob))
```

Records with `password`, `secret`, `token`, `api_key`, `private_key`,
`dsn`, `connection_string` keys are redacted. DSN-shaped values under
non-sensitive keys have only the password component redacted.

### Tracing

OTel SDK setup, OTLP HTTP exporter when an endpoint is configured,
console exporter otherwise.

```python
from kanto_commons.tracing import setup_tracing, span, use_extracted_context

provider = setup_tracing(
    service_name=settings.service_name,
    environment=settings.environment.value,
    settings=settings.tracing,
)

with span("ingest.download_fasta", attributes={"isolate.accession": acc}):
    ...

# On the consumer side, propagate trace context across Kafka messages:
with use_extracted_context(message.headers):
    with span("ingest.process"):
        ...
```

The streaming wrapper's `consumer.run(handler)` activates the
extracted context for you — only call `use_extracted_context`
explicitly when using the lower-level `consumer.messages()` iterator.

---

## Testing helpers

`from kanto_commons.testing import ...` gives services:

* **`FakeObjectStorage`** — dict-backed implementation of the OS
  protocol. Same API as the real client.
* **`FakeStreamingClient`** — in-memory queue per topic, per-group
  offsets, captures DLQ sends.
* **Event factories** — `make_isolate_discovered()`,
  `make_proteins_ready()`, `make_embeddings_ready()`,
  `make_isolate_scored()`. All accept keyword overrides.
* **Mew testcontainer fixtures** — `mew_container`, `mew_settings`,
  `mew_pool`. Session-scoped Postgres + pgvector container, per-test
  TRUNCATE for isolation. `bootstrap_schema(dsn)` applies the design-doc
  DDL once.

Service `conftest.py`:

```python
from kanto_commons.testing import (
    mew_container, mew_pool, mew_settings,   # pytest fixtures
    FakeObjectStorage, FakeStreamingClient,
    make_isolate_discovered,
)
```

Integration tests that need real Postgres are marked
`@pytest.mark.integration` and skipped by default. Run them with
`uv run pytest -m integration` — Docker is required.

---

## Stability & evolution

* Schemas are contracts: bumping a `schema_version` requires
  coordinated producer/consumer updates. Adding optional fields with
  defaults is a minor change; renaming or removing fields is a major
  change.
* The wrapper APIs deliberately do not leak `azure-*`, `aiokafka`, or
  `psycopg` types. Callers can rely on the protocol surface and the
  in-memory fakes will keep working.
* `bootstrap_schema` and the testcontainer fixture get replaced when
  Task 3 lands Alembic. The DDL itself will move into a migration; the
  fixture will run `alembic upgrade head` instead.

---

## Local dev

```bash
uv sync --all-packages
uv run pytest libs/kanto-commons/tests/        # unit tests
uv run pytest libs/kanto-commons/tests/ -m integration   # needs Docker
uv run mypy libs/kanto-commons/src/kanto_commons
```

The package follows the repo-wide ruff/mypy strict configuration; the
package's own `pyproject.toml` adds an 85% coverage gate (above the
repo-wide 70% floor) because every service depends on this code.
