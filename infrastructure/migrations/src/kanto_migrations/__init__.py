"""Mew schema migrations.

The public surface is intentionally small. Operators interact with
Alembic via the CLI (``alembic upgrade head``); CI uses the helpers in
:mod:`kanto_migrations.runner` to apply migrations against a
testcontainer.
"""

from __future__ import annotations

__all__: list[str] = []
