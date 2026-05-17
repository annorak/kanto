"""Tests for length-grouped batching.

Coverage targets the §3 acceptance criteria:

* Sorts by length descending so longest is first.
* Token budget is respected: ``size * longest_length <= budget``.
* Max-batch-size cap is respected even when token budget would allow more.
* Singleton oversized protein is allowed through (caller handles OOM).
* Determinism: same input → same batches.
* Empty input → empty list.
"""

from __future__ import annotations

import pytest

from ditto.batching import Protein, group_by_length


def _p(pid: str, length: int) -> Protein:
    return Protein(
        id=pid,
        sequence="A" * length,
        sequence_length=length,
        original_length=length,
    )


def test_empty_input_produces_no_batches() -> None:
    assert group_by_length([], target_tokens_per_batch=1024, max_batch_size=8) == []


def test_groups_short_proteins_into_one_batch() -> None:
    proteins = [_p(f"p{i}", 50) for i in range(8)]
    batches = group_by_length(proteins, target_tokens_per_batch=1024, max_batch_size=8)
    assert len(batches) == 1
    assert batches[0].size == 8
    assert batches[0].longest_length == 50


def test_token_budget_caps_batch_size() -> None:
    # 4 proteins of length 100 = 400 tokens; budget is 200 → 2 batches.
    proteins = [_p(f"p{i}", 100) for i in range(4)]
    batches = group_by_length(proteins, target_tokens_per_batch=200, max_batch_size=64)
    assert len(batches) == 2
    for batch in batches:
        assert batch.size * batch.longest_length <= 200


def test_max_batch_size_caps_count_even_under_budget() -> None:
    proteins = [_p(f"p{i}", 10) for i in range(20)]
    batches = group_by_length(proteins, target_tokens_per_batch=10_000, max_batch_size=4)
    assert all(b.size <= 4 for b in batches)
    # 20 items / max 4 = 5 batches
    assert sum(b.size for b in batches) == 20
    assert len(batches) == 5


def test_oversized_singleton_gets_its_own_batch() -> None:
    # Protein at 5000 residues with token budget 1024 — must still produce
    # a batch (size 1) rather than refusing.
    proteins = [_p("huge", 5000), _p("small", 50)]
    batches = group_by_length(proteins, target_tokens_per_batch=1024, max_batch_size=8)
    # First batch: huge alone. Second: small alone (since the huge one was singleton)
    assert batches[0].size == 1
    assert batches[0].proteins[0].id == "huge"
    assert batches[1].size == 1
    assert batches[1].proteins[0].id == "small"


def test_sorts_descending_by_length() -> None:
    proteins = [_p("short", 10), _p("long", 1000), _p("medium", 100)]
    batches = group_by_length(proteins, target_tokens_per_batch=10_000, max_batch_size=2)
    # First batch should contain the longest protein at index 0.
    assert batches[0].proteins[0].id == "long"


def test_deterministic_across_runs() -> None:
    proteins = [_p(f"p{i}", 100 - i) for i in range(10)]
    a = group_by_length(proteins, target_tokens_per_batch=400, max_batch_size=4)
    b = group_by_length(proteins, target_tokens_per_batch=400, max_batch_size=4)
    assert [tuple(p.id for p in batch.proteins) for batch in a] == [
        tuple(p.id for p in batch.proteins) for batch in b
    ]


def test_padded_tokens_property() -> None:
    proteins = [_p("a", 100), _p("b", 80), _p("c", 60)]
    batches = group_by_length(proteins, target_tokens_per_batch=500, max_batch_size=8)
    # Single batch of 3, longest 100 → padded_tokens = 300
    assert len(batches) == 1
    assert batches[0].padded_tokens == 300
    assert batches[0].sequence_lengths == (100, 80, 60)


def test_invalid_arguments_raise() -> None:
    proteins = [_p("a", 50)]
    with pytest.raises(ValueError):
        group_by_length(proteins, target_tokens_per_batch=0, max_batch_size=8)
    with pytest.raises(ValueError):
        group_by_length(proteins, target_tokens_per_batch=128, max_batch_size=0)


def test_packing_is_efficient_versus_naive() -> None:
    """Sanity check: length-grouped packing wastes far fewer padded tokens
    than the naive 'just put them in arrival order' alternative.

    This is the §3 motivation captured as an executable assertion. The
    pathological case in the task doc: one 1500 + sixty 100s.
    """
    proteins = [_p("big", 1500)] + [_p(f"small{i}", 100) for i in range(60)]
    batches = group_by_length(proteins, target_tokens_per_batch=8_000, max_batch_size=64)
    grouped_padded_tokens = sum(b.padded_tokens for b in batches)

    # Naive: stuff them all into one batch of 61, padded to 1500.
    naive_padded_tokens = 61 * 1500

    # Length-grouped should be at least 5x more efficient on this input.
    assert grouped_padded_tokens * 5 <= naive_padded_tokens
