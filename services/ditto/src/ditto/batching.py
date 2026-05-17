"""Length-grouped batching for ESM C inference.

Why this exists
---------------
Transformer attention is quadratic in *padded* sequence length. A
naive batch of ``[1500, 100, 100, ..., 100]`` pads everything to
1500 and spends ~96% of its FLOPs on padding -- a 5-10x perf hit
(task-07 section 3, marked non-negotiable).

The strategy
------------
1. Sort proteins descending by length, stable tiebreak on id (so
   re-runs produce byte-identical parquet output).
2. Greedy-pack each batch while ``size * longest <= token_budget``
   and ``size <= max_batch_size``. The seed of each batch is the
   longest item, so once a batch starts, its padded length is fixed.
3. A protein that on its own exceeds the token budget gets a singleton
   batch -- the pipeline's OOM-fallback path handles execution.

We bound *padded tokens* rather than batch size because GPU
attention memory scales with ``batch * padded_len^2``. Holding
``batch * padded_len`` constant means small batches of long proteins
and large batches of short proteins land in the same memory envelope.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Protein:
    """One protein heading into the batcher.

    ``sequence_length`` is post-truncation (what the model sees).
    ``original_length`` is pre-truncation (the biology, used for the
    aggregate weighting and the parquet row).
    """

    id: str
    sequence: str
    sequence_length: int
    original_length: int


@dataclass(frozen=True)
class Batch:
    """A pack of similarly-sized proteins."""

    proteins: tuple[Protein, ...]
    longest_length: int
    padded_tokens: int = field(init=False)

    def __post_init__(self) -> None:
        # frozen dataclass: bypass __setattr__ to assign the derived field.
        object.__setattr__(self, "padded_tokens", self.longest_length * len(self.proteins))

    @property
    def size(self) -> int:
        return len(self.proteins)

    @property
    def sequence_lengths(self) -> tuple[int, ...]:
        return tuple(p.sequence_length for p in self.proteins)


def group_by_length(
    proteins: Sequence[Protein],
    *,
    target_tokens_per_batch: int,
    max_batch_size: int,
) -> list[Batch]:
    """Sort proteins by length and pack into token-budgeted batches."""
    if not proteins:
        return []
    if target_tokens_per_batch < 1:
        raise ValueError(f"target_tokens_per_batch must be >= 1, got {target_tokens_per_batch}")
    if max_batch_size < 1:
        raise ValueError(f"max_batch_size must be >= 1, got {max_batch_size}")

    ordered = sorted(proteins, key=lambda p: (-p.sequence_length, p.id))

    batches: list[Batch] = []
    pending: list[Protein] = []
    current_longest = 0

    for protein in ordered:
        # The first item in a batch is the longest (sort order), so the
        # batch's padded length is fixed the moment we start packing.
        next_longest = max(current_longest, protein.sequence_length)
        next_size = len(pending) + 1
        next_tokens = next_longest * next_size

        size_ok = next_size <= max_batch_size
        tokens_ok = next_tokens <= target_tokens_per_batch

        # A protein that on its own exceeds the budget still gets a
        # batch; the pipeline's OOM fallback handles execution.
        if not pending and not tokens_ok:
            batches.append(Batch(proteins=(protein,), longest_length=protein.sequence_length))
            current_longest = 0
            continue

        if size_ok and tokens_ok:
            pending.append(protein)
            current_longest = next_longest
            continue

        # Overflow: freeze current batch, start a new one.
        batches.append(Batch(proteins=tuple(pending), longest_length=current_longest))
        pending = [protein]
        current_longest = protein.sequence_length

    if pending:
        batches.append(Batch(proteins=tuple(pending), longest_length=current_longest))

    return batches


__all__ = ["Batch", "Protein", "group_by_length"]
