"""Shared fixtures for Growlithe tests.

The ``ncbi_fixture_dir`` fixture exposes the committed NCBI metadata
snapshots so unit tests can read them without re-fetching from the
public FTP.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def ncbi_fixture_dir() -> Path:
    """Directory of committed NCBI metadata + exceptions + listing fixtures."""
    return _FIXTURE_DIR / "ncbi"
