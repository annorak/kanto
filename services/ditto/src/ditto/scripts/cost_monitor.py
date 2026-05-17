"""Daily Modal-spend monitor — task-07 §11.

Pulls the previous N days of GPU-second usage from the Modal billing
endpoint, computes daily spend at the workspace's rate card, and
fires an alert if either:

* Today's spend exceeds N-times the rolling 7-day average
  (``KANTO_DITTO_COST_ALERT_DAILY_MULTIPLIER``, default 2.0).
* Month-to-date projected spend exceeds the configured budget
  (``KANTO_DITTO_COST_ALERT_MONTHLY_BUDGET_USD``).

Operator wiring
---------------
This script is run from a daily k8s CronJob (or GitHub Action; the
deployment chart is out of scope for Task 7). It writes a JSON line
to stdout — the cluster log forwarder picks it up and routes alerts
to Slack / PagerDuty / wherever.

Why not Modal's native billing alerts
-------------------------------------
Modal does ship usage alerting in its dashboard, but we want a
single pane of glass alongside Mew/Azure costs. Pulling the raw
GPU-second numbers and applying our own thresholds means the same
alert format works for every cost source.

Pricing (operator-confirmed 2026-05)
------------------------------------
Per-GPU-second list price as of the Task 7 commit. Update
:data:`MODAL_GPU_USD_PER_SECOND` if the rate sheet changes.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

# ---------------------------------------------------------------------------
# Pricing — sourced from the operator's Modal contract on 2026-05-15.
# Verify quarterly; bump the constant and the README §Cost when changed.
# ---------------------------------------------------------------------------

MODAL_GPU_USD_PER_SECOND: dict[str, float] = {
    "T4": 0.000164,
    "L4": 0.000222,
    "A10G": 0.000306,
    "L40S": 0.000542,
    "A100-40GB": 0.000583,
}


@dataclass
class DailySpend:
    date: str
    gpu_type: str
    gpu_seconds: float
    usd: float


@dataclass
class CostReport:
    workspace: str
    days: list[DailySpend]
    today_usd: float
    rolling_7d_average_usd: float
    daily_multiplier: float
    monthly_projection_usd: float
    monthly_budget_usd: float
    alerts: list[str]


# ---------------------------------------------------------------------------
# Modal billing fetch — narrow Protocol so tests substitute a fake.
# ---------------------------------------------------------------------------


def fetch_modal_usage(
    *,
    workspace: str,
    days: int,
) -> list[dict[str, Any]]:  # pragma: no cover — calls Modal API
    """Pull last ``days`` of GPU usage from Modal.

    The Modal billing API is internal; we shell out to the ``modal``
    CLI's ``modal usage`` command which returns CSV. This indirection
    is here because the Python SDK's billing interface is not stable
    across releases — the CLI command is.

    Returns a list of dicts with at minimum ``date``, ``gpu_type``,
    and ``gpu_seconds``. Function name + signature are kept narrow
    so tests inject their own.
    """
    import csv
    import subprocess

    end = datetime.now(UTC).date()
    start = end - timedelta(days=days)
    proc = subprocess.run(
        [
            "modal",
            "usage",
            "--workspace",
            workspace,
            "--start",
            start.isoformat(),
            "--end",
            end.isoformat(),
            "--format",
            "csv",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    reader = csv.DictReader(proc.stdout.splitlines())
    return list(reader)


# ---------------------------------------------------------------------------
# Pure logic — substitute fetcher for tests.
# ---------------------------------------------------------------------------


def build_report(
    *,
    workspace: str,
    raw_usage: list[dict[str, Any]],
    daily_multiplier: float,
    monthly_budget_usd: float,
    today: datetime | None = None,
) -> CostReport:
    """Aggregate raw usage rows into a CostReport.

    Parameters
    ----------
    raw_usage:
        List of ``{date, gpu_type, gpu_seconds}`` dicts. Unknown GPU
        types are billed at $0 with a warning — better to under-alert
        than to crash on a new SKU we haven't yet recorded a price
        for.
    """
    now = today or datetime.now(UTC)
    today_iso = now.date().isoformat()

    # Roll up by (date, gpu_type)
    spend_by_day: dict[tuple[str, str], DailySpend] = {}
    for row in raw_usage:
        date = str(row.get("date", ""))
        gpu = str(row.get("gpu_type", ""))
        try:
            gpu_seconds = float(row.get("gpu_seconds", 0))
        except (TypeError, ValueError):
            continue
        rate = MODAL_GPU_USD_PER_SECOND.get(gpu)
        if rate is None:
            logging.warning("unknown GPU type %r — pricing as $0", gpu)
            rate = 0.0
        usd = gpu_seconds * rate
        key = (date, gpu)
        if key in spend_by_day:
            existing = spend_by_day[key]
            spend_by_day[key] = DailySpend(
                date=date,
                gpu_type=gpu,
                gpu_seconds=existing.gpu_seconds + gpu_seconds,
                usd=existing.usd + usd,
            )
        else:
            spend_by_day[key] = DailySpend(
                date=date, gpu_type=gpu, gpu_seconds=gpu_seconds, usd=usd
            )

    days = sorted(spend_by_day.values(), key=lambda d: (d.date, d.gpu_type))

    # Roll up daily totals
    daily_totals: dict[str, float] = defaultdict(float)
    for d in days:
        daily_totals[d.date] += d.usd

    today_usd = daily_totals.get(today_iso, 0.0)

    # Rolling 7-day average (excluding today, since it might be partial).
    last_7_keys = sorted(k for k in daily_totals if k < today_iso)[-7:]
    rolling_7d = (
        sum(daily_totals[k] for k in last_7_keys) / max(len(last_7_keys), 1) if last_7_keys else 0.0
    )

    # Month-to-date and projection (linear extrapolation over the month).
    month_start = now.replace(day=1).date().isoformat()
    mtd_total = sum(v for k, v in daily_totals.items() if k >= month_start)
    days_into_month = max(now.day, 1)
    days_in_month = (
        (now.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    ).day
    monthly_projection = mtd_total * days_in_month / days_into_month

    alerts: list[str] = []
    if rolling_7d > 0 and today_usd > daily_multiplier * rolling_7d:
        alerts.append(
            f"daily spend ${today_usd:.2f} exceeds {daily_multiplier:.1f}x "
            f"rolling 7-day average ${rolling_7d:.2f}"
        )
    if monthly_projection > monthly_budget_usd:
        alerts.append(
            f"projected monthly spend ${monthly_projection:.2f} exceeds "
            f"budget ${monthly_budget_usd:.2f}"
        )

    return CostReport(
        workspace=workspace,
        days=days,
        today_usd=today_usd,
        rolling_7d_average_usd=rolling_7d,
        daily_multiplier=daily_multiplier,
        monthly_projection_usd=monthly_projection,
        monthly_budget_usd=monthly_budget_usd,
        alerts=alerts,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ditto Modal cost monitor")
    parser.add_argument(
        "--workspace",
        default=os.environ.get("KANTO_DITTO_MODAL_WORKSPACE", "kanto"),
    )
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="History window to pull. 14 covers two billing weeks comfortably.",
    )
    parser.add_argument(
        "--daily-multiplier",
        type=float,
        default=float(os.environ.get("KANTO_DITTO_COST_ALERT_DAILY_MULTIPLIER", "2.0")),
    )
    parser.add_argument(
        "--monthly-budget-usd",
        type=float,
        default=float(os.environ.get("KANTO_DITTO_COST_ALERT_MONTHLY_BUDGET_USD", "500.0")),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")

    raw = fetch_modal_usage(workspace=args.workspace, days=args.days)
    report = build_report(
        workspace=args.workspace,
        raw_usage=raw,
        daily_multiplier=args.daily_multiplier,
        monthly_budget_usd=args.monthly_budget_usd,
    )
    print(json.dumps(asdict(report), default=str))
    return 1 if report.alerts else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
