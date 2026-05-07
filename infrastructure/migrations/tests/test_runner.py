"""Unit tests for kanto_migrations.runner — no docker required."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from kanto_migrations import runner


def test_alembic_config_loads_ini() -> None:
    cfg = runner.alembic_config()
    assert cfg.config_file_name is not None
    assert Path(cfg.config_file_name).name == "alembic.ini"


def test_alembic_config_sets_dsn_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KANTO_MEW_DSN_OVERRIDE", raising=False)
    runner.alembic_config(dsn="postgresql://u:p@x/db")
    assert os.environ["KANTO_MEW_DSN_OVERRIDE"] == "postgresql://u:p@x/db"


def test_alembic_config_raises_when_ini_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "_INI_PATH", Path("/nonexistent/alembic.ini"))
    with pytest.raises(FileNotFoundError, match="alembic.ini"):
        runner.alembic_config()


def test_dsn_override_restores_prior_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KANTO_MEW_DSN_OVERRIDE", "prior")
    with runner.dsn_override("inside"):
        assert os.environ["KANTO_MEW_DSN_OVERRIDE"] == "inside"
    assert os.environ["KANTO_MEW_DSN_OVERRIDE"] == "prior"


def test_dsn_override_clears_when_no_prior_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KANTO_MEW_DSN_OVERRIDE", raising=False)
    with runner.dsn_override("inside"):
        assert os.environ["KANTO_MEW_DSN_OVERRIDE"] == "inside"
    assert "KANTO_MEW_DSN_OVERRIDE" not in os.environ


def test_current_runs_against_clean_db(clean_db: str) -> None:
    """``current`` is a thin wrapper over ``alembic current``.

    Running it against a clean DB (no version table) must succeed
    without raising — ``current`` prints nothing and exits 0.
    """
    runner.current(dsn=clean_db)
