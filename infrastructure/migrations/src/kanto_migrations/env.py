"""Alembic environment.

Both online and offline modes are supported. Connection details come
exclusively from :class:`kanto_common.config.MewSettings` (i.e. from
``KANTO_MEW_*`` environment variables) via
:mod:`kanto_migrations._url`. No DSN ever lives in ``alembic.ini``;
that file holds tooling configuration only.

Tests bypass this module's settings-driven URL resolution by passing a
container DSN through the ``KANTO_MEW_DSN_OVERRIDE`` env var, which is
intentional and documented in :mod:`kanto_migrations.runner`.

We do not use SQLAlchemy autogenerate. Every migration body is hand-
written SQL because (a) we want full control over index strategy
(HNSW parameters, CONCURRENTLY, etc.) and (b) autogenerate has
historically produced non-idempotent and incomplete diffs against
pgvector schemas.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from kanto_migrations._url import resolve_url

# Alembic's Config object, populated from alembic.ini.
config = context.config

# Configure Python logging from the [loggers] section in alembic.ini.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# We hand-write SQL in every migration; no SQLAlchemy metadata to diff.
target_metadata = None


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to a live database.

    Used by ``alembic upgrade --sql`` to produce a script that a DBA
    can review and apply manually — required for the prod sign-off
    workflow described in README.md.
    """
    url = resolve_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table=config.get_main_option("version_table") or "kanto_alembic_version",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live connection."""
    url = resolve_url()
    config.set_main_option("sqlalchemy.url", url)
    section = config.get_section(config.config_ini_section, {}) or {}

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        # NullPool: migrations are short-lived and we don't want a
        # connection pool surviving past the upgrade command.
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table=config.get_main_option("version_table") or "kanto_alembic_version",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
