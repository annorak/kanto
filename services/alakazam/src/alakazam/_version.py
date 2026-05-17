"""Single source of truth for the package version string.

Kept here (rather than importing from pyproject metadata at runtime)
so cold-start cost in the consumer is zero. Bump in lockstep with
``pyproject.toml`` on each release.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
