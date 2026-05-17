"""Smoke tests for DittoServiceSettings construction and bounds."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ditto.config import DittoServiceSettings, GpuType


def test_default_settings_construct() -> None:
    s = DittoServiceSettings()
    assert s.model_id == "esmc_600m"
    assert s.embedding_dim == 1152
    assert s.max_sequence_length == 2048
    assert s.gpu_type is GpuType.A10


def test_target_tokens_per_batch_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        DittoServiceSettings(target_tokens_per_batch=0)


def test_max_batch_size_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        DittoServiceSettings(max_batch_size=0)


def test_alert_multiplier_must_be_greater_than_one() -> None:
    with pytest.raises(ValidationError):
        DittoServiceSettings(cost_alert_daily_multiplier=1.0)


def test_monthly_budget_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        DittoServiceSettings(cost_alert_monthly_budget_usd=0)
