"""Tests for the per-genome length-weighted-mean aggregate."""

from __future__ import annotations

import numpy as np
import pytest

from ditto.aggregate import length_weighted_mean


def test_single_protein_returns_that_proteins_embedding() -> None:
    emb = np.array([[1.0, 2.0, 3.0, 4.0]], dtype=np.float16)
    result = length_weighted_mean(emb, [100])
    np.testing.assert_array_almost_equal(result, emb[0], decimal=3)
    assert result.dtype == np.float16


def test_uniform_lengths_equal_arithmetic_mean() -> None:
    emb = np.array(
        [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]],
        dtype=np.float32,
    )
    result = length_weighted_mean(emb, [100, 100, 100])
    np.testing.assert_array_almost_equal(result, np.mean(emb, axis=0), decimal=3)


def test_length_weighting_is_correct() -> None:
    """A 200-residue protein with ones must drag the mean toward ones."""
    emb = np.array(
        [[1.0, 1.0, 1.0], [0.0, 0.0, 0.0]],
        dtype=np.float32,
    )
    # Weights 200 vs 100 → expected = (200*1 + 100*0)/300 = 0.667
    result = length_weighted_mean(emb, [200, 100])
    expected = np.array([2.0 / 3.0] * 3, dtype=np.float16)
    np.testing.assert_array_almost_equal(result, expected, decimal=3)


def test_weights_known_example_three_proteins() -> None:
    """Worked-through example: hand-compute the weighted mean."""
    emb = np.array(
        [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float32,
    )
    lengths = [10, 20, 70]
    # weighted sum = (10, 20, 70); /100 = (0.1, 0.2, 0.7)
    result = length_weighted_mean(emb, lengths)
    np.testing.assert_array_almost_equal(
        result, np.array([0.1, 0.2, 0.7], dtype=np.float16), decimal=3
    )


def test_returns_float16() -> None:
    emb = np.ones((4, 6), dtype=np.float32)
    result = length_weighted_mean(emb, [1, 2, 3, 4])
    assert result.dtype == np.float16
    assert result.shape == (6,)


def test_zero_proteins_raises() -> None:
    with pytest.raises(ValueError, match="zero proteins"):
        length_weighted_mean(np.zeros((0, 4), dtype=np.float32), [])


def test_mismatched_lengths_raises() -> None:
    emb = np.zeros((3, 4), dtype=np.float32)
    with pytest.raises(ValueError, match="lengths"):
        length_weighted_mean(emb, [10, 20])


def test_zero_weight_raises() -> None:
    emb = np.zeros((2, 4), dtype=np.float32)
    with pytest.raises(ValueError, match="total weight"):
        length_weighted_mean(emb, [0, 0])


def test_one_dim_input_rejected() -> None:
    with pytest.raises(ValueError, match="2D"):
        length_weighted_mean(np.zeros(4, dtype=np.float32), [1])
