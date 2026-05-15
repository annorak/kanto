"""Growlithe configuration tests."""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from growlithe.config import GrowlitheServiceSettings, GrowlitheSettings
from growlithe.organisms import DEFAULT_ORGANISMS

_BASE_ENV: dict[str, str] = {
    "KANTO_MEW_HOST": "mew.local",
    "KANTO_MEW_DATABASE": "mew",
    "KANTO_MEW_USER": "kanto",
    "KANTO_MEW_PASSWORD": "supersecret",
    "KANTO_OS_ACCOUNT_URL": "https://kantodevdata.blob.core.windows.net",
    "KANTO_STREAMING_BOOTSTRAP": "kafka:9092",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith(("KANTO_", "AZURE_STORAGE_")):
            monkeypatch.delenv(key, raising=False)


def _apply_base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _BASE_ENV.items():
        monkeypatch.setenv(k, v)


def test_defaults_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_base_env(monkeypatch)
    settings = GrowlitheSettings.load()
    assert settings.service_name == "growlithe"
    assert settings.growlithe.organisms == DEFAULT_ORGANISMS
    assert settings.growlithe.poll_interval_seconds == 21600  # 6h
    assert settings.growlithe.source_id == "ncbi-pd"
    assert settings.growlithe.run_once is False


def test_organisms_override_via_comma_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_base_env(monkeypatch)
    monkeypatch.setenv("KANTO_GROWLITHE_ORGANISMS", "Salmonella,Listeria")
    settings = GrowlitheSettings.load()
    assert settings.growlithe.organisms == ("Salmonella", "Listeria")


def test_organisms_must_be_non_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_base_env(monkeypatch)
    monkeypatch.setenv("KANTO_GROWLITHE_ORGANISMS", "")
    with pytest.raises(ValidationError):
        GrowlitheSettings.load()


def test_poll_interval_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_base_env(monkeypatch)
    monkeypatch.setenv("KANTO_GROWLITHE_POLL_INTERVAL_SECONDS", "30")  # below floor
    with pytest.raises(ValidationError):
        GrowlitheSettings.load()


def test_run_once_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    _apply_base_env(monkeypatch)
    monkeypatch.setenv("KANTO_GROWLITHE_RUN_ONCE", "true")
    assert GrowlitheSettings.load().growlithe.run_once is True


def test_service_settings_accepts_explicit_tuple() -> None:
    """Construct directly (no env vars) for unit-test wiring convenience."""
    settings = GrowlitheServiceSettings(  # type: ignore[call-arg]
        organisms=("Listeria",),
    )
    assert settings.organisms == ("Listeria",)
