"""Test helpers re-exported for service authors.

Service ``conftest.py`` files import from this module:

.. code-block:: python

    from kanto_common.testing import (
        FakeObjectStorage,
        FakeStreamingClient,
        mew_pool,            # session-scoped pytest fixture
        bootstrap_schema,    # used by mew_pool to set up tables
        make_isolate_discovered,
    )

The fakes implement the same protocols as the real clients so a
service can swap them in for unit tests with no extra wiring.
"""

from kanto_common.testing.factories import (
    make_embeddings_ready,
    make_isolate_discovered,
    make_isolate_scored,
    make_proteins_ready,
)
from kanto_common.testing.fakes import (
    FakeObjectStorage,
    FakeStreamingClient,
    StoredMessage,
)
from kanto_common.testing.mew_fixture import (
    bootstrap_schema,
    mew_container,
    mew_pool,
    mew_settings,
)

__all__ = [
    "FakeObjectStorage",
    "FakeStreamingClient",
    "StoredMessage",
    "bootstrap_schema",
    "make_embeddings_ready",
    "make_isolate_discovered",
    "make_isolate_scored",
    "make_proteins_ready",
    "mew_container",
    "mew_pool",
    "mew_settings",
]
