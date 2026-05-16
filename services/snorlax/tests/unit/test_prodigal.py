"""Prodigal subprocess wrapper tests.

Most of these substitute a shell binary for ``prodigal`` so they
exercise the subprocess plumbing (timeout, exit-code handling, stderr
capture) without requiring Prodigal to be installed. A separate
``real_prodigal``-marked test runs the actual binary on a tiny FASTA
when ``$PRODIGAL_BIN`` (or ``prodigal``) is available.
"""

from __future__ import annotations

import gzip
import shutil
import textwrap
from pathlib import Path

import pytest

from snorlax.prodigal import (
    ProdigalBinaryMissingError,
    ProdigalNonZeroExitError,
    ProdigalRunner,
    ProdigalTimeoutError,
    _decompress,
)


def _write_genome_gz(path: Path, body: str) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as fp:
        fp.write(body)


def _write_fake_prodigal(path: Path, *, body: str) -> None:
    """Drop a shell script that imitates the Prodigal CLI surface."""
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.asyncio
async def test_prodigal_binary_missing(tmp_path: Path) -> None:
    runner = ProdigalRunner(
        binary=str(tmp_path / "definitely-not-here"),
        mode="single",
        timeout_seconds=5.0,
    )
    genome = tmp_path / "g.fna.gz"
    _write_genome_gz(genome, ">x\nACGT\n")
    with pytest.raises(ProdigalBinaryMissingError):
        await runner.run(genome_fasta_gz=genome, work_dir=tmp_path / "work")


@pytest.mark.asyncio
async def test_prodigal_timeout(tmp_path: Path) -> None:
    fake = tmp_path / "fake-prodigal.sh"
    _write_fake_prodigal(
        fake,
        body=textwrap.dedent("""\
            #!/bin/sh
            # Ignore args; just hang for longer than the test timeout.
            sleep 10
        """),
    )
    runner = ProdigalRunner(
        binary=str(fake),
        mode="single",
        timeout_seconds=0.5,
    )
    genome = tmp_path / "g.fna.gz"
    _write_genome_gz(genome, ">x\nACGT\n")
    with pytest.raises(ProdigalTimeoutError):
        await runner.run(genome_fasta_gz=genome, work_dir=tmp_path / "work")


@pytest.mark.asyncio
async def test_prodigal_nonzero_exit(tmp_path: Path) -> None:
    fake = tmp_path / "fake-prodigal.sh"
    _write_fake_prodigal(
        fake,
        body=textwrap.dedent("""\
            #!/bin/sh
            echo "prodigal: corrupt input" 1>&2
            exit 11
        """),
    )
    runner = ProdigalRunner(
        binary=str(fake),
        mode="single",
        timeout_seconds=5.0,
    )
    genome = tmp_path / "g.fna.gz"
    _write_genome_gz(genome, ">x\nACGT\n")
    with pytest.raises(ProdigalNonZeroExitError) as excinfo:
        await runner.run(genome_fasta_gz=genome, work_dir=tmp_path / "work")
    assert excinfo.value.exit_code == 11
    assert "corrupt input" in excinfo.value.stderr


@pytest.mark.asyncio
async def test_prodigal_zero_exit_no_output(tmp_path: Path) -> None:
    """Prodigal exited 0 but didn't produce the protein FASTA — DLQ path."""
    fake = tmp_path / "fake-prodigal.sh"
    _write_fake_prodigal(
        fake,
        body=textwrap.dedent("""\
            #!/bin/sh
            # Honour the CLI shape but don't write the -a output.
            exit 0
        """),
    )
    runner = ProdigalRunner(
        binary=str(fake),
        mode="single",
        timeout_seconds=5.0,
    )
    genome = tmp_path / "g.fna.gz"
    _write_genome_gz(genome, ">x\nACGT\n")
    with pytest.raises(ProdigalNonZeroExitError):
        await runner.run(genome_fasta_gz=genome, work_dir=tmp_path / "work")


@pytest.mark.asyncio
async def test_prodigal_success_with_fake_binary(tmp_path: Path) -> None:
    """A fake prodigal that writes a plausible protein FASTA should succeed."""
    fake = tmp_path / "fake-prodigal.sh"
    _write_fake_prodigal(
        fake,
        body=textwrap.dedent("""\
            #!/bin/sh
            # Parse -a <path> out of the args.
            while [ $# -gt 0 ]; do
                if [ "$1" = "-a" ]; then
                    shift
                    AOUT="$1"
                fi
                shift
            done
            cat > "$AOUT" <<EOF
            >prot1 # 1 # 12 # 1 # ID=1_1;
            MKVLAQ
            EOF
        """),
    )
    runner = ProdigalRunner(
        binary=str(fake),
        mode="single",
        timeout_seconds=5.0,
    )
    genome = tmp_path / "g.fna.gz"
    _write_genome_gz(genome, ">x\nACGT\n")
    result = await runner.run(genome_fasta_gz=genome, work_dir=tmp_path / "work")
    assert result.protein_fasta_path.exists()
    text = result.protein_fasta_path.read_text(encoding="utf-8")
    assert text.startswith(">prot1")


def test_decompress_helper(tmp_path: Path) -> None:
    src = tmp_path / "src.gz"
    with gzip.open(src, "wt", encoding="utf-8") as fp:
        fp.write("hello")
    dst = tmp_path / "out.txt"
    _decompress(src, dst)
    assert dst.read_text(encoding="utf-8") == "hello"


# ---------------------------------------------------------------------------
# Real-Prodigal sanity check (skipped when binary isn't on PATH)
# ---------------------------------------------------------------------------


@pytest.mark.real_prodigal
@pytest.mark.asyncio
async def test_real_prodigal_on_synthetic_genome(tmp_path: Path) -> None:
    binary = shutil.which("prodigal")
    if binary is None:
        pytest.skip("prodigal not on PATH; install bioconda/prodigal to enable")

    # Build a tiny genome with at least one real ORF. The sequence below
    # encodes a short ORF starting at the canonical ATG and ending at TAA.
    orf = (
        "ATG"  # Met
        + "AAACCCGGG"
        + "AAACCCGGG"
        + "AAACCCGGG"
        + "AAACCCGGG"
        + "AAACCCGGG"
        + "TAA"  # stop
    )
    flank = "ACGT" * 200
    body = f">test_contig\n{flank}\n{orf}\n{flank}\n"
    genome = tmp_path / "g.fna.gz"
    _write_genome_gz(genome, body)

    runner = ProdigalRunner(
        binary=binary,
        # Real Prodigal needs >=20 kb for single mode; for tiny test
        # inputs we fall back to meta which doesn't have the length
        # check. This test is a *sanity* check that we wire the binary
        # correctly — production runs use the configured ``single``.
        mode="meta",
        timeout_seconds=30.0,
    )
    result = await runner.run(genome_fasta_gz=genome, work_dir=tmp_path / "work")
    assert result.protein_fasta_path.exists()
    assert result.protein_fasta_path.stat().st_size > 0
