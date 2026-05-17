"""Tests for the alakazam settings layer."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from alakazam.config import AlakazamServiceSettings


def test_default_weights_load_without_env() -> None:
    settings = AlakazamServiceSettings()
    assert settings.weight_nn == 1.0
    assert settings.weight_coverage == 2.0
    assert settings.weight_mahalanobis == 0.5
    assert settings.candidate_threshold == 0.30
    assert settings.nn_k == 10


def test_all_zero_weights_rejected() -> None:
    with pytest.raises(ValidationError):
        AlakazamServiceSettings(
            weight_nn=0.0,
            weight_coverage=0.0,
            weight_mahalanobis=0.0,
        )


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KANTO_ALAKAZAM_WEIGHT_NN", "3.5")
    monkeypatch.setenv("KANTO_ALAKAZAM_CANDIDATE_THRESHOLD", "0.42")
    settings = AlakazamServiceSettings()
    assert settings.weight_nn == 3.5
    assert settings.candidate_threshold == 0.42


def test_consumer_id_picks_up_pod_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POD_NAME", "alakazam-7")
    settings = AlakazamServiceSettings()
    assert settings.consumer_id == "alakazam-7"
