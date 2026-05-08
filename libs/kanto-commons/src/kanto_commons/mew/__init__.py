"""Mew (Postgres + pgvector) client and Repository implementations.

Async by default — services benefit from concurrency at the
connection level. The pool is owned by :class:`MewClient`; the
:class:`IsolateRepository`, :class:`EmbeddingRepository`, and
:class:`AlertRepository` borrow connections from it for the duration
of each operation.

Raw SQL lives only in this subpackage; service code never writes SQL.
"""

from kanto_commons.mew.client import MewClient
from kanto_commons.mew.models import (
    AlertRow,
    AlertStatus,
    EmbeddingRow,
    IsolateRow,
    IsolateStatus,
    NeighborRow,
)
from kanto_commons.mew.repositories import (
    AlertRepository,
    EmbeddingRepository,
    IsolateRepository,
)

__all__ = [
    "AlertRepository",
    "AlertRow",
    "AlertStatus",
    "EmbeddingRepository",
    "EmbeddingRow",
    "IsolateRepository",
    "IsolateRow",
    "IsolateStatus",
    "MewClient",
    "NeighborRow",
]
