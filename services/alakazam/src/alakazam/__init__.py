"""Alakazam — the novelty scorer.

Public package surface is kept narrow; service-internal modules are
imported directly under :mod:`alakazam.<module>` by callers that
need them (notably :mod:`alakazam.service` and the test suite).
"""

from __future__ import annotations

from alakazam._version import __version__

__all__ = ["__version__"]
