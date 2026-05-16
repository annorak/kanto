"""Settings loading + defaults."""

from __future__ import annotations

import pytest

from snorlax.config import SnorlaxServiceSettings, SnorlaxSettings


def test_service_settings_defaults() -> None:
    s = SnorlaxServiceSettings()
    assert s.consumer_group == "snorlax"
    assert s.prodigal_mode == "single"
    assert s.modal_function_ref.startswith("kanto-ditto/")
    assert s.modal_enabled is True
    assert s.health_port == 8081
    assert s.metrics_port == 9464


def test_modal_function_ref_validation() -> None:
    # The /-separated form is enforced at use time inside the modal
    # client; the settings type itself only checks emptiness via the
    # underlying pydantic Field constraints. We still want the round
    # trip to keep the slash.
    s = SnorlaxServiceSettings(modal_function_ref="kanto-ditto/embed")
    assert "/" in s.modal_function_ref


def test_top_level_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KANTO_MEW_HOST", "localhost")
    monkeypatch.setenv("KANTO_MEW_DATABASE", "kanto")
    monkeypatch.setenv("KANTO_MEW_USER", "kanto")
    monkeypatch.setenv("KANTO_MEW_PASSWORD", "pw")
    monkeypatch.setenv(
        "KANTO_OS_ACCOUNT_URL",
        "https://example.blob.core.windows.net",
    )
    monkeypatch.setenv(
        "KANTO_STREAMING_BOOTSTRAP_SERVERS",
        "example.servicebus.windows.net:9093",
    )
    settings = SnorlaxSettings.load()
    assert settings.service_name == "snorlax"
    assert settings.snorlax.consumer_group == "snorlax"
