# kanto-migrations — Mew schema migrations

Alembic-managed PostgreSQL + pgvector schema for Mew. Migrations are
infrastructure: they live here, not in any service, so ownership is
unambiguous and downgrading a service image cannot accidentally roll
back schema other services depend on.

This README is the runbook. Read it before applying any migration —
especially to prod.

## Layout

```
infrastructure/migrations/
├── alembic.ini                    # Tooling config — never holds creds.
├── pyproject.toml                 # uv workspace member 'kanto-migrations'.
├── README.md                      # You are here.
├── src/kanto_migrations/
│   ├── env.py                     # Alembic env — pulls DSN from MewSettings.
│   ├── _url.py                    # URL helpers, unit-tested.
│   ├── runner.py                  # Programmatic upgrade/downgrade for tests.
│   ├── seed.py                    # Local seed data.
│   ├── safety.py                  # Risky-pattern scanner used by CI.
│   ├── script.py.mako             # Template for `alembic revision`.
│   └── versions/                  # Migration files.
└── tests/                         # testcontainers-backed integration tests.
```

The seed CLI lives at `scripts/seed-mew.py`; it is intentionally *not*
inside this package because it is a developer-laptop tool.

---

## Connection routing

Connection details come from `MewSettings`, i.e. the `KANTO_MEW_*`
environment variables. `alembic.ini` deliberately holds **no DSN** —
hard-coding one would defeat the single-source-of-truth principle.

| Variable                  | Purpose                                                   |
|---------------------------|-----------------------------------------------------------|
| `KANTO_MEW_HOST`          | Host or IP                                                |
| `KANTO_MEW_PORT`          | TCP port (default 5432)                                   |
| `KANTO_MEW_DATABASE`      | DB name                                                   |
| `KANTO_MEW_USER`          | Role with DDL privileges                                  |
| `KANTO_MEW_PASSWORD`      | Password — pulled from Vault, never written to disk       |
| `KANTO_MEW_SSLMODE`       | `disable` (local only) / `verify-full` (anywhere else)    |
| `KANTO_MEW_DSN_OVERRIDE`  | Tests only — bypasses `MewSettings`                       |

In dev and prod, `KANTO_MEW_PASSWORD` is fetched from OCI Vault by the
operator's shell wrapper (see Section *Apply to dev/prod* below) and
exported into the local environment for the duration of the migration
command.

---

## Apply: local

Prerequisites: a local Postgres with pgvector, e.g. via Docker:

```bash
docker run --rm -d --name mew \
  -e POSTGRES_USER=kanto \
  -e POSTGRES_PASSWORD=local-dev-only \
  -e POSTGRES_DB=mew \
  -p 5432:5432 \
  pgvector/pgvector:pg16
```

Set `.envrc` per `.envrc.example`. Then:

```bash
cd infrastructure/migrations
uv run alembic upgrade head
```

To seed representative data (idempotent):

```bash
KANTO_ENV=local uv run python scripts/seed-mew.py
```

The script refuses to run unless `KANTO_ENV=local` — see "Pitfalls" below.

---

## Apply: dev

Dev Mew is the OCI-managed Postgres provisioned by Task 2.

1. **Network.** Connect to the OCI bastion (or VPN). Confirm
   reachability: `nc -vz <mew-host> 5432`.
2. **Pull credentials.** Fetch the dev DDL role's password from OCI
   Vault into your shell. Do **not** write it to a file:
   ```bash
   export KANTO_MEW_PASSWORD="$(oci vault secret read --secret-id <ocid> --query 'data.\"secret-bundle-content\".content' --raw-output | base64 -d)"
   ```
3. **Set the rest of the env:**
   ```bash
   export KANTO_ENV=dev
   export KANTO_MEW_HOST=<dev-mew-host>
   export KANTO_MEW_DATABASE=mew
   export KANTO_MEW_USER=kanto_ddl
   export KANTO_MEW_SSLMODE=verify-full
   ```
4. **Dry-run first.** Generate SQL without applying:
   ```bash
   cd infrastructure/migrations
   uv run alembic upgrade head --sql > /tmp/migration-preview.sql
   less /tmp/migration-preview.sql
   ```
5. **Apply.**
   ```bash
   uv run alembic upgrade head
   ```
6. **Verify.**
   ```bash
   uv run alembic current
   ```

If the migration fails midway, see "Recovery" below.

---

## Apply: prod (sign-off required)

Two engineers required: an *applier* and an *approver*. Both must
have read the migration file, the `--sql` preview, and any data-shape
implications before the apply runs.

1. **PR review.** The migration must already be merged on `main` after
   review by at least one engineer who is not the author.
2. **Generate SQL preview.** As in dev step 4. Paste the preview into
   the deploy ticket so the approver can review it without running
   anything.
3. **Approver sign-off.** Approver writes `:lgtm:` (or equivalent) on
   the deploy ticket. No verbal approval; we want an audit trail.
4. **Backup.** Snapshot the prod Mew instance via OCI's automated
   backup, take note of the backup ID, and confirm restore-ability
   on a non-prod instance once per quarter (this is a separate runbook).
5. **Apply during a quiet window.** OCI Streaming consumers tolerate
   short Mew unavailability via DLQ; still, prefer applying outside
   business hours.
6. **Apply** with the same env-var setup as dev, swapping the prod
   secret. Run `alembic upgrade head` — *not* `--sql` (that ran in
   step 2 and was already reviewed).
7. **Watch.** Tail Alakazam and Chatot logs for read errors during
   the next 10 minutes. If anything goes wrong, see "Recovery."

---

## Roll back

Ground rules:

* **Never roll back if the migration deleted data.** The down migration
  cannot recover dropped rows. Restore from backup instead.
* **Never roll back if the new code already wrote new-shape data.**
  E.g. a new column with values that the down migration would discard.
  Roll forward with a fix-up migration instead.

Procedure:

```bash
cd infrastructure/migrations
uv run alembic downgrade -1   # one step back
# or
uv run alembic downgrade <revision>
```

After rollback:

* Confirm `alembic current` matches the expected revision.
* Restart any service that read schema metadata at boot (most don't).

---

## Recovery from a partially-applied migration

Alembic wraps each migration in a transaction by default
(`transaction_per_migration = false`, single transaction per upgrade
invocation). If the upgrade aborts mid-statement, Postgres rolls back
the open transaction and `alembic current` continues to report the
*old* revision — no manual intervention needed.

Exceptions:

* If you set `transaction_per_migration = true` for a specific change
  (e.g., for `CREATE INDEX CONCURRENTLY`, which cannot run inside a
  transaction), partial state is possible. The migration's docstring
  must call this out and supply a manual recovery procedure.
* If the connection died after the migration committed but before
  Alembic recorded it in `kanto_alembic_version`, Alembic will re-run
  the migration on the next upgrade. Most migrations are idempotent
  (`IF NOT EXISTS`, `ON CONFLICT`); when a migration is *not*
  idempotent, the docstring must explain how to recover.

---

## Writing a new migration

1. **Generate the file:**
   ```bash
   cd infrastructure/migrations
   uv run alembic revision -m "short imperative description"
   ```
   The filename becomes
   `<YYYYMMDD>_<HHMM>_<short_imperative>.py`.

2. **Fill in the docstring** with the *why*, not just the *what*. The
   reviewer will read it before approving.

3. **Write the body in pure SQL** via `op.execute(...)`. SQLAlchemy
   autogenerate is intentionally not used — we want full control over
   index strategy, pgvector specifics, and concurrent operations.

4. **Always implement `downgrade()`.** Reversibility is a contract.
   The only acceptable exception is a one-way data migration; if
   that's the right call, explain in the docstring why and provide a
   restore-from-backup procedure.

5. **Run tests:**
   ```bash
   cd infrastructure/migrations
   uv run pytest tests/
   ```
   New tests are required for any non-trivial schema change.

6. **Commit.** Commit message: `feat(mew): <description>` or
   `fix(mew): <description>`. The migration file's docstring is the
   long-form rationale; the commit message is the headline.

---

## Long-running migrations (production)

Some operations need extra care on a populated table. The runbook
below mirrors the pattern Postgres recommends:

| Operation                | Safe pattern                                                                                |
|--------------------------|---------------------------------------------------------------------------------------------|
| Add an index             | `CREATE INDEX CONCURRENTLY` in a separate, transactionless migration                        |
| Drop an index            | `DROP INDEX CONCURRENTLY`                                                                   |
| Add a NOT NULL column    | Add nullable + DEFAULT → backfill in batches → set NOT NULL                                 |
| Change a column type     | Add new column → dual-write → backfill → switch readers → drop old                           |
| Re-shape a foreign key   | Drop old FK → backfill → add new FK with `NOT VALID` → `VALIDATE CONSTRAINT` async         |

For an HNSW re-index on a populated `genome_embeddings`, **do not**
copy the v1 migration verbatim. Instead:

1. Build the new index `CONCURRENTLY` with the new parameters.
2. Drop the old index `CONCURRENTLY`.
3. Confirm queries still hit the index via `EXPLAIN`.

Each step is its own migration (so rollback is sane) with
`transaction_per_migration = true` set in `alembic.ini` for the affected
revision (alternatively, override per-revision in the migration body
with `op.execute("COMMIT")` then issue the index DDL — uglier but
self-contained).

`maintenance_work_mem` should be temporarily increased on the session
running the index build (e.g. `SET LOCAL maintenance_work_mem = '4GB'`)
so the build doesn't spill to disk. This is set inline by the
migration; it does **not** persist beyond the transaction.

---

## CI

`.github/workflows/ci.yml` runs the migration test job on every PR
that touches `infrastructure/migrations/`. The job:

1. Spins up a Docker daemon on the GitHub runner.
2. Runs `pytest infrastructure/migrations/tests/` with coverage.
3. Fails the job if coverage drops below 90 percent.
4. Runs the safety scanner; findings are surfaced as a PR comment
   (warning, not blocking — the reviewer decides).

The test job uses the same `pgvector/pgvector:pg16` image as local
runs, so behavior matches across machines.

---

## Pitfalls

* **`pgvector` extension name.** The extension is named `vector`, not
  `pgvector`. `CREATE EXTENSION pgvector` silently does nothing.
* **HNSW + `CONCURRENTLY`.** Only relevant on populated tables. The
  initial migration creates the index without `CONCURRENTLY` because
  the table is empty at apply time.
* **`SET LOCAL` under autocommit.** Settings reset between statements.
  Always set non-`LOCAL` or wrap in an explicit transaction.
* **Seed script in non-local environments.** The script refuses any
  `KANTO_ENV` other than `local`. Even an explicit `--force` flag is
  not provided. To reset shared dev, use `alembic downgrade base &&
  alembic upgrade head`, then a careful manual data load.
* **Credentials in `alembic.ini`.** Don't. The pre-commit gitleaks
  hook will catch the obvious cases; the harder cases are review-only.
* **Autogenerate.** `alembic revision --autogenerate` is intentionally
  not configured. Pure SQL only.

---

## Tests required for migration changes

Any PR that adds or modifies a migration must pass:

* `pytest infrastructure/migrations/tests/test_initial_schema.py`
  (or a sibling for the new schema state) — table/index/constraint
  shape assertions.
* `pytest infrastructure/migrations/tests/test_down_migration.py`
  — upgrade → downgrade → upgrade is idempotent.
* `pytest infrastructure/migrations/tests/test_kanto_common_against_schema.py`
  — every kanto-common repository method exercises the new schema.

Coverage gate is enforced at 90 percent.
