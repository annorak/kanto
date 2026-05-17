"""Tests for the cost monitor's pure-logic alerter."""

from __future__ import annotations

from datetime import UTC, datetime

from ditto.scripts.cost_monitor import (
    MODAL_GPU_USD_PER_SECOND,
    build_report,
)


def _row(date: str, gpu: str, seconds: float) -> dict[str, object]:
    return {"date": date, "gpu_type": gpu, "gpu_seconds": seconds}


def test_no_alerts_when_spend_is_normal() -> None:
    today = datetime(2026, 5, 15, tzinfo=UTC)
    raw = [
        _row("2026-05-08", "A10G", 1000),
        _row("2026-05-09", "A10G", 1000),
        _row("2026-05-10", "A10G", 1000),
        _row("2026-05-11", "A10G", 1000),
        _row("2026-05-12", "A10G", 1000),
        _row("2026-05-13", "A10G", 1000),
        _row("2026-05-14", "A10G", 1000),
        _row("2026-05-15", "A10G", 1000),  # today: same as average
    ]
    report = build_report(
        workspace="ws",
        raw_usage=raw,
        daily_multiplier=2.0,
        monthly_budget_usd=1000.0,
        today=today,
    )
    assert report.alerts == []


def test_daily_spike_triggers_alert() -> None:
    today = datetime(2026, 5, 15, tzinfo=UTC)
    a10_rate = MODAL_GPU_USD_PER_SECOND["A10G"]
    raw = [
        _row("2026-05-08", "A10G", 100),
        _row("2026-05-09", "A10G", 100),
        _row("2026-05-15", "A10G", 10_000),  # 100x the average
    ]
    report = build_report(
        workspace="ws",
        raw_usage=raw,
        daily_multiplier=2.0,
        monthly_budget_usd=1_000_000.0,  # huge so the monthly check doesn't fire
        today=today,
    )
    assert any("daily spend" in a for a in report.alerts)
    assert report.today_usd == 10_000 * a10_rate


def test_monthly_budget_breach_triggers_alert() -> None:
    today = datetime(2026, 5, 15, tzinfo=UTC)
    raw = [_row(f"2026-05-{day:02d}", "A100-40GB", 50_000) for day in range(1, 16)]
    report = build_report(
        workspace="ws",
        raw_usage=raw,
        daily_multiplier=10.0,  # so daily check doesn't fire
        monthly_budget_usd=100.0,
        today=today,
    )
    assert any("monthly" in a for a in report.alerts)


def test_unknown_gpu_priced_as_zero() -> None:
    today = datetime(2026, 5, 15, tzinfo=UTC)
    raw = [_row("2026-05-15", "H200", 100_000)]
    report = build_report(
        workspace="ws",
        raw_usage=raw,
        daily_multiplier=2.0,
        monthly_budget_usd=100.0,
        today=today,
    )
    assert report.today_usd == 0.0


def test_aggregates_across_gpu_types_in_one_day() -> None:
    today = datetime(2026, 5, 15, tzinfo=UTC)
    raw = [
        _row("2026-05-15", "A10G", 100),
        _row("2026-05-15", "A100-40GB", 100),
    ]
    report = build_report(
        workspace="ws",
        raw_usage=raw,
        daily_multiplier=10.0,
        monthly_budget_usd=10_000.0,
        today=today,
    )
    expected = 100 * MODAL_GPU_USD_PER_SECOND["A10G"] + 100 * MODAL_GPU_USD_PER_SECOND["A100-40GB"]
    assert abs(report.today_usd - expected) < 1e-6
