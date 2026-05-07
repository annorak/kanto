"""Tests for :mod:`kanto_migrations.seed` and the local-env guard.

Coverage of the CLI script itself is via subprocess: we invoke
``scripts/seed-mew.py`` with a varying ``KANTO_ENV`` and assert the
guard refuses non-local environments.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import psycopg
import pytest
from kanto_migrations import seed as seed_mod

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SEED_SCRIPT = _REPO_ROOT / "scripts" / "seed-mew.py"


def _row_count(dsn: str, table: str) -> int:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


# ---------------------------------------------------------------------------
# Row construction
# ---------------------------------------------------------------------------


def test_isolate_rows_are_deterministic() -> None:
    a = seed_mod.isolate_rows()
    b = seed_mod.isolate_rows()
    assert a == b
    assert len({r["accession"] for r in a}) == len(a)


def test_embedding_rows_skip_qc_failed() -> None:
    isolates = seed_mod.isolate_rows()
    qc_failed = {r["accession"] for r in isolates if r["status"] == "QC_FAILED"}
    embeddings = seed_mod.embedding_rows(isolates)
    embedding_accessions = {r["accession"] for r in embeddings}
    assert qc_failed.isdisjoint(embedding_accessions)
    assert all(len(r["embedding"]) == seed_mod.EMBEDDING_DIM for r in embeddings)


def test_alert_rows_only_for_alerted_status() -> None:
    isolates = seed_mod.isolate_rows()
    alerted = {r["accession"] for r in isolates if r["status"] == "ALERTED"}
    alerts = seed_mod.alert_rows(isolates)
    assert {r["accession"] for r in alerts} <= alerted
    assert len(alerts) <= seed_mod.N_ALERTS


# ---------------------------------------------------------------------------
# Insert + idempotency
# ---------------------------------------------------------------------------


def test_seed_inserts_expected_row_counts(applied_db: str) -> None:
    counts = seed_mod.seed(applied_db)
    assert _row_count(applied_db, "isolates") == counts["isolates"]
    assert _row_count(applied_db, "genome_embeddings") == counts["genome_embeddings"]
    assert _row_count(applied_db, "alerts") == counts["alerts"]


def test_seed_is_idempotent(applied_db: str) -> None:
    """Running ``seed`` twice produces the same row counts as once."""
    seed_mod.seed(applied_db)
    after_first = (
        _row_count(applied_db, "isolates"),
        _row_count(applied_db, "genome_embeddings"),
        _row_count(applied_db, "alerts"),
    )
    seed_mod.seed(applied_db)
    after_second = (
        _row_count(applied_db, "isolates"),
        _row_count(applied_db, "genome_embeddings"),
        _row_count(applied_db, "alerts"),
    )
    assert after_first == after_second


# ---------------------------------------------------------------------------
# CLI guard
# ---------------------------------------------------------------------------


def _run_script(env_value: str | None) -> subprocess.CompletedProcess[str]:
    """Invoke the script with a given KANTO_ENV setting (or unset).

    No ``KANTO_MEW_*`` vars are set, so even a non-rejecting run
    fails fast at MewSettings — but the guard runs first, so the
    failure mode is what we're testing.
    """
    import os as _os

    env = _os.environ.copy()
    env.pop("KANTO_ENV", None)
    env.pop("KANTO_ENVIRONMENT", None)
    if env_value is not None:
        env["KANTO_ENV"] = env_value
    return subprocess.run(
        [sys.executable, str(_SEED_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


@pytest.mark.parametrize("env_value", ["dev", "staging", "prod"])
def test_seed_script_refuses_non_local(env_value: str) -> None:
    result = _run_script(env_value)
    assert result.returncode != 0
    assert "Refusing to seed" in result.stderr


def test_seed_script_refuses_when_env_unset() -> None:
    result = _run_script(None)
    assert result.returncode != 0
    assert "KANTO_ENV is not set" in result.stderr


def test_seed_script_refuses_unknown_env_value() -> None:
    result = _run_script("space-station")
    assert result.returncode != 0
    assert "Unknown KANTO_ENV" in result.stderr
