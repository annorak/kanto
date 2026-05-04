"""Shared pytest fixtures for the kanto-common test suite.

Re-exports the testcontainer-backed ``mew_pool`` fixture from
``kanto_common.testing`` so integration tests can request it by
name. Unit tests don't import this module — they are pure-Python
and don't need Docker.
"""

from __future__ import annotations

# pytest discovers fixtures imported into conftest.py.
from kanto_common.testing import mew_container, mew_pool, mew_settings

__all__ = ["mew_container", "mew_pool", "mew_settings"]
