"""DSN resolution helpers used by ``env.py``.

Lives in its own module so unit tests can exercise it without
importing ``env.py`` (which evaluates Alembic's runtime context at
module load and is only safe to import inside an ``alembic`` invocation).
"""

from __future__ import annotations

import os

from kanto_commons.config import MewSettings


def to_sqlalchemy_url(libpq_url: str) -> str:
    """Convert a libpq URL to SQLAlchemy's psycopg3 dialect URL.

    SQLAlchemy 2.x defaults to psycopg2 for the bare ``postgresql://``
    scheme; we must say ``postgresql+psycopg://`` explicitly to pick
    psycopg3 — the same driver kanto-commons uses at runtime.
    """
    if libpq_url.startswith("postgresql+psycopg://"):
        return libpq_url
    if libpq_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + libpq_url.removeprefix("postgresql://")
    if libpq_url.startswith("postgres://"):
        return "postgresql+psycopg://" + libpq_url.removeprefix("postgres://")
    raise ValueError(f"Unsupported DB URL scheme: {libpq_url[:32]!r}")


def resolve_url() -> str:
    """Return the libpq URL Alembic should connect with.

    Resolution order:

    1. ``KANTO_MEW_DSN_OVERRIDE`` — used by the testcontainers harness
       to point Alembic at a per-test ephemeral container without
       polluting the operator's real ``KANTO_MEW_*`` environment.
    2. ``MewSettings()`` from the standard ``KANTO_MEW_*`` variables.
    """
    override = os.environ.get("KANTO_MEW_DSN_OVERRIDE")
    if override:
        return to_sqlalchemy_url(override)
    # MewSettings reads host/database/user/password from KANTO_MEW_*
    # env vars at construction time. mypy can't see that contract
    # because pydantic-settings does the lookup at runtime.
    settings = MewSettings()  # type: ignore[call-arg]
    return to_sqlalchemy_url(settings.dsn())


__all__ = ["resolve_url", "to_sqlalchemy_url"]
