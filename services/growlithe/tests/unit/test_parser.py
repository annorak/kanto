"""Parser tests against real NCBI metadata fixtures.

The two snapshot fixtures were taken from the actual Listeria
``PDG000000001.4703`` metadata TSV at NCBI on 2026-05-15 (header +
ten rows), trimmed to five rows for the "older" snapshot. The
exceptions TSV is hand-rolled in NCBI's documented shape.

Working against committed fixtures rather than mock-generated
strings keeps the tests honest: a schema change at NCBI will fail a
fixture-driven test loudly the next time someone re-generates the
fixture and a real row no longer parses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from growlithe.datasource.parser import (
    METADATA_PASSTHROUGH_COLUMNS,
    REQUIRED_COLUMNS,
    iter_rows,
    parse_metadata,
    read_exceptions,
)


def _load(ncbi_fixture_dir: Path, name: str) -> bytes:
    return (ncbi_fixture_dir / name).read_bytes()


# ---------------------------------------------------------------------------
# Header + happy path
# ---------------------------------------------------------------------------


def test_parser_reads_real_header(ncbi_fixture_dir: Path) -> None:
    """Every NCBI column we declare a dependency on appears in the fixture."""
    rows = list(iter_rows(_load(ncbi_fixture_dir, "Listeria_PDG000000001.4703.metadata.tsv")))
    assert rows, "fixture must produce at least one row"
    sample = rows[0]
    for column in (*REQUIRED_COLUMNS, *METADATA_PASSTHROUGH_COLUMNS):
        assert column in sample, (
            f"NCBI Pathogen Detection no longer exposes column {column!r}; "
            "Growlithe's column-dependency list needs updating."
        )


def test_parses_all_five_old_rows(ncbi_fixture_dir: Path) -> None:
    """The older snapshot has 5 well-formed rows, no exceptions."""
    rows, stats = parse_metadata(_load(ncbi_fixture_dir, "Listeria_PDG000000001.4702.metadata.tsv"))
    assert stats.total_rows == 5
    assert stats.parsed_rows == 5
    assert stats.missing_required == 0
    assert stats.bad_version == 0
    assert stats.qc_filtered == 0
    assert [r.target_acc for r in rows] == [
        "PDT000000011.3",
        "PDT000000012.3",
        "PDT000000013.5",
        "PDT000000014.5",
        "PDT000000015.3",
    ]


def test_extracts_version_from_target_acc(ncbi_fixture_dir: Path) -> None:
    rows, _ = parse_metadata(_load(ncbi_fixture_dir, "Listeria_PDG000000001.4702.metadata.tsv"))
    versions = {r.target_acc: r.version for r in rows}
    assert versions["PDT000000011.3"] == 3
    assert versions["PDT000000013.5"] == 5
    assert versions["PDT000000014.5"] == 5


def test_passthrough_columns_lifted_into_extras(ncbi_fixture_dir: Path) -> None:
    rows, _ = parse_metadata(_load(ncbi_fixture_dir, "Listeria_PDG000000001.4702.metadata.tsv"))
    first = rows[0]
    # The 10 sampled real rows all have scientific_name set; if NCBI ever
    # publishes a row without one, this test will turn into a regression
    # alert.
    assert first.extras["scientific_name"] == "Listeria monocytogenes"
    assert first.extras["bioproject_acc"].startswith("PRJ")


# ---------------------------------------------------------------------------
# Exceptions filtering
# ---------------------------------------------------------------------------


def test_exceptions_are_filtered_out(ncbi_fixture_dir: Path) -> None:
    """Rows listed in the exceptions TSV are dropped pre-emit."""
    exceptions = read_exceptions(
        _load(ncbi_fixture_dir, "Listeria_PDG000000001.4703.exceptions.tsv")
    )
    assert exceptions == frozenset({"PDT000000020.3", "PDT000000018.5"})

    rows, stats = parse_metadata(
        _load(ncbi_fixture_dir, "Listeria_PDG000000001.4703.metadata.tsv"),
        exceptions=exceptions,
    )
    kept_accs = {r.target_acc for r in rows}
    assert "PDT000000020.3" not in kept_accs
    assert "PDT000000018.5" not in kept_accs
    assert stats.qc_filtered == 2
    assert stats.parsed_rows == 8  # 10 in fixture - 2 filtered


def test_read_exceptions_handles_missing_file() -> None:
    assert read_exceptions(None) == frozenset()
    assert read_exceptions(b"") == frozenset()


# ---------------------------------------------------------------------------
# Diff suppression via skip_target_accs
# ---------------------------------------------------------------------------


def test_skip_target_accs_suppresses_previously_seen(ncbi_fixture_dir: Path) -> None:
    """When passed the old set, only the 3 truly-new rows come through."""
    old_rows, _ = parse_metadata(_load(ncbi_fixture_dir, "Listeria_PDG000000001.4702.metadata.tsv"))
    seen = frozenset(r.target_acc for r in old_rows)

    new_rows, stats = parse_metadata(
        _load(ncbi_fixture_dir, "Listeria_PDG000000001.4703.metadata.tsv"),
        skip_target_accs=seen,
    )

    # 5 returning rows skipped (no stats counter; intentional — they're
    # not "filtered out", they're "already emitted").
    new_accs = sorted(r.target_acc for r in new_rows)
    assert new_accs == [
        "PDT000000016.4",
        # 18 and 20 are NOT in this list — they'd be picked up if not
        # in the exceptions set (this test doesn't pass exceptions).
        "PDT000000018.5",
        "PDT000000019.11",
        "PDT000000020.3",
        "PDT000000021.3",
    ]
    assert stats.parsed_rows == 5


# ---------------------------------------------------------------------------
# Tolerant parsing
# ---------------------------------------------------------------------------


def test_unknown_columns_are_ignored() -> None:
    """An NCBI schema change that adds a column must not break us."""
    tsv = (
        b"#target_acc\tasm_acc\tnewfield_added_by_ncbi_2027\n"
        b"PDT000999000.1\tGCA_999000999.1\tweird\n"
    )
    rows, stats = parse_metadata(tsv)
    assert stats.parsed_rows == 1
    assert rows[0].target_acc == "PDT000999000.1"
    assert rows[0].version == 1


def test_missing_required_increments_counter_and_drops_row() -> None:
    tsv = (
        b"#target_acc\tasm_acc\n"
        b"PDT000999000.1\tNULL\n"  # missing asm_acc
        b"NULL\tGCA_999000999.1\n"  # missing target_acc
        b"PDT000999001.1\tGCA_999000999.1\n"  # ok
    )
    rows, stats = parse_metadata(tsv)
    assert stats.total_rows == 3
    assert stats.parsed_rows == 1
    assert stats.missing_required == 2
    assert rows[0].target_acc == "PDT000999001.1"


def test_bad_version_is_counted() -> None:
    """target_acc with no version suffix is dropped, not crashed on."""
    tsv = b"#target_acc\tasm_acc\n" b"PDT_NO_VERSION_SUFFIX\tGCA_001.1\n"
    rows, stats = parse_metadata(tsv)
    assert stats.parsed_rows == 0
    assert stats.bad_version == 1
    assert rows == []


@pytest.mark.parametrize("date_in,expect_year", [("2013-11-19", 2013), ("NULL", None), ("", None)])
def test_target_creation_date_parsing(date_in: str, expect_year: int | None) -> None:
    tsv = (
        "#target_acc\tasm_acc\ttarget_creation_date\n" f"PDT000999000.1\tGCA_001.1\t{date_in}\n"
    ).encode()
    rows, _ = parse_metadata(tsv)
    if expect_year is None:
        assert rows[0].target_creation_date is None
    else:
        assert rows[0].target_creation_date is not None
        assert rows[0].target_creation_date.year == expect_year
