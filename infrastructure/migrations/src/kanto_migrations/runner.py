"""Programmatic Alembic invocation used by the test harness and seed script.

The Alembic CLI is the canonical way to apply migrations. This module
provides the same operations callable from Python so that pytest can
spin up a container, run ``upgrade head``, run the test, then
``downgrade base``, all without shelling out.

Connection routing follows ``env.py``: tests set
``KANTO_MEW_DSN_OVERRIDE`` to point Alembic at the testcontainer's
ephemeral DSN.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config

# alembic.ini lives two levels above this file (src/kanto_migrations/runner.py).
_INI_PATH = Path(__file__).resolve().parents[2] / "alembic.ini"


def alembic_config(dsn: str | None = None) -> Config:
    """Build a fully-resolved Alembic Config tied to ``alembic.ini``.

    If ``dsn`` is provided, it is exported as ``KANTO_MEW_DSN_OVERRIDE``
    in the current process so ``env.py`` picks it up. Callers in tests
    typically pass the testcontainer's libpq URL.
    """
    if not _INI_PATH.exists():
        raise FileNotFoundError(
            f"alembic.ini not found at {_INI_PATH}. "
            "Run runner functions from a checkout that includes the migrations package."
        )
    if dsn is not None:
        os.environ["KANTO_MEW_DSN_OVERRIDE"] = dsn
    return Config(str(_INI_PATH))


def upgrade(dsn: str | None = None, revision: str = "head") -> None:
    """Apply migrations up to ``revision`` (default: ``head``)."""
    command.upgrade(alembic_config(dsn), revision)


def downgrade(dsn: str | None = None, revision: str = "base") -> None:
    """Reverse migrations down to ``revision`` (default: ``base`` — empty schema)."""
    command.downgrade(alembic_config(dsn), revision)


def current(dsn: str | None = None) -> None:
    """Print the currently-applied head revision (for runbook diagnostics)."""
    command.current(alembic_config(dsn))


@contextmanager
def dsn_override(dsn: str) -> Iterator[None]:
    """Temporarily set ``KANTO_MEW_DSN_OVERRIDE`` for the enclosed block.

    Useful when a test wants to point migrations at one container and
    its repository assertions at another. Restores the prior value on
    exit.
    """
    prior = os.environ.get("KANTO_MEW_DSN_OVERRIDE")
    os.environ["KANTO_MEW_DSN_OVERRIDE"] = dsn
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop("KANTO_MEW_DSN_OVERRIDE", None)
        else:
            os.environ["KANTO_MEW_DSN_OVERRIDE"] = prior


__all__ = [
    "alembic_config",
    "current",
    "downgrade",
    "dsn_override",
    "upgrade",
]
