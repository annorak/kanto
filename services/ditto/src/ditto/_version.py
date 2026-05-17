"""Ditto version string. Kept in its own module so ``__init__`` stays
import-light — importing :mod:`ditto` from a tool that doesn't have
torch/modal installed must not fail.
"""

__version__ = "0.1.0"
