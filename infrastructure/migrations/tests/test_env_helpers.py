"""Unit tests for kanto_migrations._url — no docker required."""

from __future__ import annotations

import pytest
from kanto_migrations._url import resolve_url, to_sqlalchemy_url


def test_to_sqlalchemy_url_rewrites_postgresql_scheme() -> None:
    out = to_sqlalchemy_url("postgresql://u:p@h:5432/d?sslmode=disable")
    assert out == "postgresql+psycopg://u:p@h:5432/d?sslmode=disable"


def test_to_sqlalchemy_url_rewrites_postgres_scheme() -> None:
    out = to_sqlalchemy_url("postgres://u:p@h:5432/d")
    assert out == "postgresql+psycopg://u:p@h:5432/d"


def test_to_sqlalchemy_url_passes_through_already_qualified_url() -> None:
    qualified = "postgresql+psycopg://u:p@h/d"
    assert to_sqlalchemy_url(qualified) == qualified


def test_to_sqlalchemy_url_rejects_unknown_scheme() -> None:
    with pytest.raises(ValueError, match="Unsupported DB URL scheme"):
        to_sqlalchemy_url("mysql://u:p@h/d")


def test_resolve_url_prefers_dsn_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "KANTO_MEW_DSN_OVERRIDE",
        "postgresql://u:p@override:5432/db",
    )
    # Settings vars are deliberately unset; if resolve_url walked past
    # the override branch it would raise pydantic.ValidationError.
    for var in (
        "KANTO_MEW_HOST",
        "KANTO_MEW_DATABASE",
        "KANTO_MEW_USER",
        "KANTO_MEW_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)
    assert resolve_url() == "postgresql+psycopg://u:p@override:5432/db"


def test_resolve_url_falls_back_to_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KANTO_MEW_DSN_OVERRIDE", raising=False)
    monkeypatch.setenv("KANTO_MEW_HOST", "h")
    monkeypatch.setenv("KANTO_MEW_DATABASE", "db")
    monkeypatch.setenv("KANTO_MEW_USER", "u")
    monkeypatch.setenv("KANTO_MEW_PASSWORD", "p")
    monkeypatch.setenv("KANTO_MEW_SSLMODE", "disable")
    out = resolve_url()
    assert out.startswith("postgresql+psycopg://u:")
    assert "h:5432/db" in out
