#!/usr/bin/env python3
"""Seed a *local* Mew instance with representative development data.

Refuses to run unless ``KANTO_ENV=local``. ``dev`` and any cloud
environment are blocked even with explicit override flags — wiping
shared dev data with the seed set has bitten teams before, so we
err on the side of impossible-to-misuse here. (If you really need
to reset shared dev, do it via Alembic ``downgrade base`` followed
by ``upgrade head`` from the dev runbook in
``infrastructure/migrations/README.md``.)

Idempotency
-----------
Running the script twice produces the same row count as running it
once. See :mod:`kanto_migrations.seed` for the per-table conflict
strategies.

Usage
-----
::

    uv run python scripts/seed-mew.py

Run after ``alembic upgrade head`` against the local Postgres.
"""

from __future__ import annotations

import logging
import os
import sys

from kanto_common.config import Environment, MewSettings
from kanto_migrations.seed import seed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("seed-mew")


def _assert_local_environment() -> None:
    """Refuse to run anywhere except ``KANTO_ENV=local``."""
    raw = os.environ.get("KANTO_ENV") or os.environ.get("KANTO_ENVIRONMENT")
    if raw is None:
        sys.exit(
            "KANTO_ENV is not set. Refusing to seed without an explicit "
            "environment selector. Set KANTO_ENV=local for local development."
        )
    try:
        env = Environment(raw.lower())
    except ValueError:
        sys.exit(
            f"Unknown KANTO_ENV value {raw!r}; expected one of {[e.value for e in Environment]}."
        )
    if env is not Environment.LOCAL:
        sys.exit(
            f"Refusing to seed in environment {env.value!r}. "
            "This script will only run when KANTO_ENV=local."
        )


def main() -> int:
    _assert_local_environment()
    settings = MewSettings()  # type: ignore[call-arg]
    log.info("Connecting to %s", settings.safe_dsn())
    seed(settings.dsn())
    log.info("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
