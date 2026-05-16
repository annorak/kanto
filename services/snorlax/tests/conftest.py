"""Shared fixtures for Snorlax tests."""

from __future__ import annotations

from pathlib import Path

import pytest

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """Shared fixture directory (small NCBI bytes, sample FASTAs)."""
    return _FIXTURE_DIR


@pytest.fixture
def tmp_work_dir(tmp_path: Path) -> Path:
    """Pod-style scratch dir for pipeline runs in unit tests."""
    work = tmp_path / "work"
    work.mkdir()
    return work
