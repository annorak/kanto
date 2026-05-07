"""Safety scanner for new Alembic migration files.

Flags patterns that need human attention. The scanner is intentionally
conservative: it can produce false positives (e.g. a "DROP TABLE
shadow_table" inside a docstring), so it warns rather than blocks.
The CI workflow surfaces these warnings in the PR; the reviewer
decides whether the migration deserves extra scrutiny.

Patterns checked
----------------
* DROP TABLE / DROP COLUMN — destructive.
* ALTER COLUMN ... TYPE — non-trivial type changes.
* ADD COLUMN ... NOT NULL without DEFAULT on what looks like an
  existing table.
* ADD CONSTRAINT ... FOREIGN KEY / REFERENCES — requires
  reference-table population.

The scanner reads the *raw text* of each migration. We considered
walking the AST but op.execute() takes arbitrary strings, so a
text scan is just as accurate and considerably simpler.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SafetyFinding:
    """A single risky pattern detected in a migration file."""

    file: Path
    line: int
    rule: str
    message: str
    snippet: str

    def format(self) -> str:
        return (
            f"{self.file}:{self.line}: [{self.rule}] {self.message}\n" f"    {self.snippet.strip()}"
        )


# Patterns are case-insensitive and run against the raw migration source.
# The scanner is line-oriented so we surface a precise file:line for the PR.
_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "drop-table",
        "Migration drops a table — destructive. Confirm a backup exists "
        "and the table is unused in all running services.",
        re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE),
    ),
    (
        "drop-column",
        "Migration drops a column — destructive. Confirm the column is "
        "unread by all running services and back up the data first.",
        re.compile(r"\bDROP\s+COLUMN\b", re.IGNORECASE),
    ),
    (
        "alter-type",
        "Migration changes a column type. Verify implicit casts work for "
        "every existing row and that running services tolerate the new type.",
        re.compile(r"\bALTER\s+COLUMN\b[^;]*\bTYPE\b", re.IGNORECASE | re.DOTALL),
    ),
    (
        "add-not-null",
        "Migration adds a NOT NULL column without a DEFAULT. New rows are "
        "fine, but inserts from older app versions will fail. Add a DEFAULT "
        "or split into multiple migrations (add nullable → backfill → set "
        "NOT NULL).",
        re.compile(
            r"\bADD\s+COLUMN\b(?!.*\bDEFAULT\b)[^;]*\bNOT\s+NULL\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "add-foreign-key",
        "Migration adds a foreign-key constraint. The referenced table "
        "must already contain every value in the referencing column or "
        "the migration aborts.",
        re.compile(
            r"\bADD\s+CONSTRAINT\b[^;]*\bFOREIGN\s+KEY\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
)


def scan_file(path: Path) -> list[SafetyFinding]:
    """Return all findings for a single migration file."""
    if not path.is_file():
        return []
    findings: list[SafetyFinding] = []
    text = path.read_text(encoding="utf-8")
    for rule, message, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            snippet_line = text.splitlines()[line - 1]
            findings.append(
                SafetyFinding(
                    file=path, line=line, rule=rule, message=message, snippet=snippet_line
                )
            )
    return findings


def scan_directory(root: Path) -> list[SafetyFinding]:
    """Recursively scan every ``.py`` migration under ``root``.

    Scans only files inside a ``versions`` subdirectory so utility
    modules in the same package don't trigger false positives.
    """
    findings: list[SafetyFinding] = []
    for path in sorted(root.rglob("versions/*.py")):
        if path.name == "__init__.py":
            continue
        findings.extend(scan_file(path))
    return findings


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Prints findings; always exits 0 (warning-only)."""
    import argparse

    parser = argparse.ArgumentParser(description="Scan Alembic migrations for risky patterns.")
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Files or directories to scan. Defaults to ./src/kanto_migrations.",
    )
    args = parser.parse_args(argv)

    targets: list[Path] = list(args.paths) or [Path("src/kanto_migrations")]
    findings: list[SafetyFinding] = []
    for target in targets:
        if target.is_file():
            findings.extend(scan_file(target))
        else:
            findings.extend(scan_directory(target))

    if not findings:
        print("safety: no risky patterns detected.")
        return 0

    print(f"safety: {len(findings)} risky pattern(s) detected — review carefully:")
    for finding in findings:
        print()
        print(finding.format())
    # Warning, not an error. The PR check job parses this output.
    return 0


__all__ = ["SafetyFinding", "main", "scan_directory", "scan_file"]


if __name__ == "__main__":
    raise SystemExit(main())
