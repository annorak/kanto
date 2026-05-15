"""Unit tests for the configuration module.

We exercise: missing-required errors are clear and aggregated,
SecretStr never leaks, AliasChoices fallbacks work, and pool-size
constraints are enforced.
"""

from __future__ import annotations

import pytest
from kanto_commons.config import (
    Environment,
    KantoBaseSettings,
    LogFormat,
    MewSettings,
    ObjectStorageSettings,
    StreamingSettings,
    TracingSettings,
)
from pydantic import SecretStr, ValidationError


class _SnorlaxSettings(KantoBaseSettings):
    service_name: str = "snorlax"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wipe every KANTO_* / MEW_* / STREAMING_* var per test for isolation."""
    for key in list(os_keys()):
        monkeypatch.delenv(key, raising=False)


def os_keys() -> list[str]:
    import os

    relevant_prefixes = (
        "KANTO_",
        "MEW_",
        "STREAMING_",
        "AZURE_STORAGE_",
    )
    return [k for k in os.environ if k.startswith(relevant_prefixes)]


def _set_minimum_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KANTO_MEW_HOST", "mew.local")
    monkeypatch.setenv("KANTO_MEW_DATABASE", "mew")
    monkeypatch.setenv("KANTO_MEW_USER", "kanto")
    monkeypatch.setenv("KANTO_MEW_PASSWORD", "supersecret")
    monkeypatch.setenv(
        "KANTO_OS_ACCOUNT_URL",
        "https://kantodevdata1234.blob.core.windows.net",
    )
    monkeypatch.setenv("KANTO_STREAMING_BOOTSTRAP", "kafka:9092")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_load_with_all_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_minimum_env(monkeypatch)
    settings = _SnorlaxSettings.load()
    assert settings.service_name == "snorlax"
    assert settings.environment is Environment.DEV
    assert settings.log_level == "INFO"
    assert settings.log_format is LogFormat.JSON
    assert settings.mew.host == "mew.local"
    assert settings.streaming.bootstrap_servers == "kafka:9092"


def test_log_level_is_uppercased(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_minimum_env(monkeypatch)
    monkeypatch.setenv("KANTO_LOG_LEVEL", "debug")
    settings = _SnorlaxSettings.load()
    assert settings.log_level == "DEBUG"


def test_environment_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_minimum_env(monkeypatch)
    monkeypatch.setenv("KANTO_ENV", "prod")
    settings = _SnorlaxSettings.load()
    assert settings.environment is Environment.PROD


# ---------------------------------------------------------------------------
# KANTO_STREAMING_BOOTSTRAP alias for ergonomic shell use
# ---------------------------------------------------------------------------


def test_streaming_bootstrap_short_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """KANTO_STREAMING_BOOTSTRAP must work as a synonym for the long
    KANTO_STREAMING_BOOTSTRAP_SERVERS — that's what .envrc.example uses."""
    monkeypatch.setenv("KANTO_MEW_HOST", "mew.local")
    monkeypatch.setenv("KANTO_MEW_DATABASE", "mew")
    monkeypatch.setenv("KANTO_MEW_USER", "kanto")
    monkeypatch.setenv("KANTO_MEW_PASSWORD", "x")
    monkeypatch.setenv(
        "KANTO_OS_ACCOUNT_URL",
        "https://kantodevdata1234.blob.core.windows.net",
    )
    monkeypatch.setenv("KANTO_STREAMING_BOOTSTRAP", "kafka:9092")
    settings = _SnorlaxSettings.load()
    assert settings.streaming.bootstrap_servers == "kafka:9092"


# ---------------------------------------------------------------------------
# Missing-required behaviour: clear errors, all at once
# ---------------------------------------------------------------------------


def test_missing_all_required_raises_validation_error() -> None:
    with pytest.raises(ValidationError) as exc_info:
        MewSettings()  # type: ignore[call-arg]
    msg = str(exc_info.value)
    # Every missing required field should be reported in one go,
    # not one-at-a-time. This is what makes ops debugging tolerable.
    # We check for env var names (what ops actually has to set), not
    # field names — pydantic-settings reports the env var path when
    # AliasChoices is in play.
    for env_var in (
        "KANTO_MEW_HOST",
        "KANTO_MEW_DATABASE",
        "KANTO_MEW_USER",
        "KANTO_MEW_PASSWORD",
    ):
        assert env_var in msg


def test_object_storage_missing_account_url() -> None:
    with pytest.raises(ValidationError) as exc:
        ObjectStorageSettings()  # type: ignore[call-arg]
    assert "account_url" in str(exc.value).lower()


def test_streaming_missing_bootstrap() -> None:
    with pytest.raises(ValidationError) as exc:
        StreamingSettings()  # type: ignore[call-arg]
    assert "bootstrap" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# SecretStr safety
# ---------------------------------------------------------------------------


def test_password_repr_does_not_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_minimum_env(monkeypatch)
    settings = _SnorlaxSettings.load()
    assert "supersecret" not in repr(settings)
    assert "supersecret" not in repr(settings.mew)
    assert "supersecret" not in str(settings.mew)


def test_safe_dsn_redacts_password(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_minimum_env(monkeypatch)
    settings = _SnorlaxSettings.load()
    assert "supersecret" not in settings.mew.safe_dsn()
    assert "***" in settings.mew.safe_dsn()


def test_dsn_contains_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity check that ``dsn()`` does substitute the password — that's
    what makes ``safe_dsn`` worth caring about."""
    _set_minimum_env(monkeypatch)
    settings = _SnorlaxSettings.load()
    assert "supersecret" in settings.mew.dsn()


# ---------------------------------------------------------------------------
# Pool-size invariants
# ---------------------------------------------------------------------------


def test_pool_max_below_min_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_minimum_env(monkeypatch)
    monkeypatch.setenv("KANTO_MEW_POOL_MIN_SIZE", "10")
    monkeypatch.setenv("KANTO_MEW_POOL_MAX_SIZE", "2")
    with pytest.raises(ValidationError):
        MewSettings()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Tracing defaults
# ---------------------------------------------------------------------------


def test_tracing_defaults_to_console_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_minimum_env(monkeypatch)
    settings = _SnorlaxSettings.load()
    assert settings.tracing.endpoint is None
    assert settings.tracing.sample_rate == 1.0


def test_tracing_sample_rate_bounds() -> None:
    with pytest.raises(ValidationError):
        TracingSettings(sample_rate=1.5)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        TracingSettings(sample_rate=-0.1)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Sanity that SecretStr is the type returned (so callers must call
# .get_secret_value() — easy to spot in code review).
# ---------------------------------------------------------------------------


def test_password_is_secret_str(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_minimum_env(monkeypatch)
    settings = _SnorlaxSettings.load()
    assert isinstance(settings.mew.password, SecretStr)
