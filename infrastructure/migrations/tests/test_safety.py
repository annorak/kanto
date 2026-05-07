"""Tests for the migration safety scanner."""

from __future__ import annotations

from pathlib import Path

import pytest
from kanto_migrations.safety import SafetyFinding, main, scan_directory, scan_file


@pytest.fixture
def migration_dir(tmp_path: Path) -> Path:
    versions = tmp_path / "src" / "kanto_migrations" / "versions"
    versions.mkdir(parents=True)
    (versions / "__init__.py").write_text("", encoding="utf-8")
    return versions


def _write(versions: Path, name: str, body: str) -> Path:
    path = versions / name
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Per-rule detections
# ---------------------------------------------------------------------------


def test_detects_drop_table(migration_dir: Path) -> None:
    target = _write(
        migration_dir,
        "9999_drop.py",
        "def upgrade():\n    op.execute('DROP TABLE foo')\n",
    )
    findings = scan_file(target)
    assert any(f.rule == "drop-table" for f in findings)


def test_detects_drop_column(migration_dir: Path) -> None:
    target = _write(
        migration_dir,
        "9999_drop_col.py",
        "op.execute('ALTER TABLE foo DROP COLUMN bar')",
    )
    assert any(f.rule == "drop-column" for f in scan_file(target))


def test_detects_alter_column_type(migration_dir: Path) -> None:
    target = _write(
        migration_dir,
        "9999_alter_type.py",
        "op.execute('ALTER TABLE foo ALTER COLUMN bar TYPE bigint')",
    )
    assert any(f.rule == "alter-type" for f in scan_file(target))


def test_detects_add_not_null_without_default(migration_dir: Path) -> None:
    target = _write(
        migration_dir,
        "9999_add_not_null.py",
        "op.execute('ALTER TABLE foo ADD COLUMN bar TEXT NOT NULL')",
    )
    assert any(f.rule == "add-not-null" for f in scan_file(target))


def test_does_not_flag_add_not_null_with_default(migration_dir: Path) -> None:
    target = _write(
        migration_dir,
        "9999_safe.py",
        "op.execute('ALTER TABLE foo ADD COLUMN bar TEXT NOT NULL DEFAULT \\'x\\'')",
    )
    assert not any(f.rule == "add-not-null" for f in scan_file(target))


def test_detects_add_foreign_key(migration_dir: Path) -> None:
    target = _write(
        migration_dir,
        "9999_fk.py",
        ("op.execute('" "ALTER TABLE foo ADD CONSTRAINT fk_x FOREIGN KEY (a) REFERENCES b(a)'" ")"),
    )
    assert any(f.rule == "add-foreign-key" for f in scan_file(target))


# ---------------------------------------------------------------------------
# Scanner mechanics
# ---------------------------------------------------------------------------


def test_scan_file_returns_empty_for_safe_migration(migration_dir: Path) -> None:
    target = _write(
        migration_dir,
        "9999_safe.py",
        "op.execute('CREATE INDEX foo_idx ON foo (bar)')",
    )
    assert scan_file(target) == []


def test_scan_file_returns_empty_for_missing_path(tmp_path: Path) -> None:
    assert scan_file(tmp_path / "nope.py") == []


def test_scan_directory_skips_init(migration_dir: Path) -> None:
    _write(migration_dir, "__init__.py", "op.execute('DROP TABLE x')")
    _write(migration_dir, "9999_real.py", "op.execute('DROP TABLE y')")
    findings = scan_directory(migration_dir.parent.parent.parent)
    assert all(f.file.name != "__init__.py" for f in findings)
    assert any("DROP TABLE y" in f.snippet for f in findings)


def test_finding_format_includes_rule_and_snippet(migration_dir: Path) -> None:
    target = _write(
        migration_dir,
        "9999_drop.py",
        "op.execute('DROP TABLE foo')",
    )
    finding = scan_file(target)[0]
    formatted = finding.format()
    assert "drop-table" in formatted
    assert "DROP TABLE foo" in formatted
    assert str(target) in formatted


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_main_prints_no_findings_when_clean(
    migration_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(migration_dir, "9999_safe.py", "op.execute('CREATE INDEX a ON b (c)')")
    rc = main([str(migration_dir.parent.parent.parent)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "no risky patterns detected" in captured.out


def test_main_lists_findings_and_exits_zero(
    migration_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(migration_dir, "9999_drop.py", "op.execute('DROP TABLE foo')")
    rc = main([str(migration_dir.parent.parent.parent)])
    assert rc == 0  # warning-only, never blocks
    captured = capsys.readouterr()
    assert "drop-table" in captured.out
    assert "DROP TABLE foo" in captured.out


def test_main_accepts_individual_files(
    migration_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = _write(migration_dir, "9999_drop.py", "op.execute('DROP TABLE x')")
    main([str(target)])
    captured = capsys.readouterr()
    assert "drop-table" in captured.out


def test_safety_finding_dataclass_attributes() -> None:
    f = SafetyFinding(file=Path("foo.py"), line=3, rule="x", message="m", snippet="s")
    assert f.file == Path("foo.py")
    assert f.line == 3
    assert f.rule == "x"
