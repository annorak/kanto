"""Shared fixtures for Ditto tests.

The fakes here implement the same Protocols the real wrappers do
(:class:`ditto.azure_io.BlobIO`, :class:`ditto.event_emit.EventEmitter`)
so the pipeline tests run without Modal, torch, Azure, or Postgres.
"""

from __future__ import annotations

import gzip
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pytest

from ditto.batching import Batch
from ditto.config import DittoServiceSettings

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return _FIXTURE_DIR


@pytest.fixture
def settings() -> DittoServiceSettings:
    """Small batch budget so tests run fast."""
    return DittoServiceSettings(
        target_tokens_per_batch=512,
        max_batch_size=8,
        max_sequence_length=128,
        embedding_dim=8,
    )


def make_fasta_bytes(sequences: dict[str, str], *, gzipped: bool = True) -> bytes:
    """Build a small FASTA blob from ``{id: sequence}``."""
    raw = "\n".join(f">{pid}\n{seq}" for pid, seq in sequences.items()).encode("ascii")
    return gzip.compress(raw) if gzipped else raw


@dataclass
class FakeBlobIO:
    """In-memory implementation of :class:`ditto.azure_io.BlobIO`."""

    proteins: dict[tuple[str, str], bytes] = field(default_factory=dict)
    parquets: dict[tuple[str, str], bytes] = field(default_factory=dict)

    def put_protein(self, *, container: str, key: str, data: bytes) -> None:
        self.proteins[(container, key)] = data

    def fetch_protein_fasta(self, *, container: str, key: str) -> bytes:
        try:
            return self.proteins[(container, key)]
        except KeyError as exc:
            # Mirror AzureBlobIO: missing blob -> PermanentError.
            from ditto.errors import PermanentError

            raise PermanentError(f"protein FASTA not found at {container}/{key}") from exc

    def write_parquet(self, *, container: str, key: str, data: bytes) -> None:
        self.parquets[(container, key)] = data


@pytest.fixture
def fake_blob_io() -> FakeBlobIO:
    return FakeBlobIO()


@dataclass
class FakeEventEmitter:
    """In-memory implementation of :class:`ditto.event_emit.EventEmitter`."""

    emitted: list[dict[str, object]] = field(default_factory=list)
    raise_on_emit: Exception | None = None

    async def emit_embeddings_ready(
        self,
        *,
        accession: str,
        version: int,
        model: str,
        model_version: str,
        os_key: str,
    ) -> None:
        if self.raise_on_emit is not None:
            raise self.raise_on_emit
        self.emitted.append(
            {
                "accession": accession,
                "version": version,
                "model": model,
                "model_version": model_version,
                "os_key": os_key,
            }
        )


@pytest.fixture
def fake_emitter() -> FakeEventEmitter:
    return FakeEventEmitter()


@dataclass
class FakeEmbedder:
    """Drop-in for :class:`ditto.model.ESMCEmbedder`.

    Produces deterministic vectors from the sequence bytes so test
    fixtures yield stable parquet output. ``raise_on_call`` lets a
    test inject an exception on the Nth call (use a list with one
    exception per scheduled OOM).
    """

    embedding_dim: int = 8
    raise_on_call: Iterable[Exception] = ()

    def __post_init__(self) -> None:
        self._raise_iter = iter(self.raise_on_call)

    def embed_batch(self, batch: Batch) -> np.ndarray:
        try:
            raise next(self._raise_iter)
        except StopIteration:
            pass

        out = np.zeros((batch.size, self.embedding_dim), dtype=np.float16)
        for i, protein in enumerate(batch.proteins):
            seed = sum(protein.sequence.encode("ascii")) % (2**32)
            rng = np.random.default_rng(seed)
            out[i] = rng.uniform(-1, 1, size=self.embedding_dim).astype(np.float16)
        return out


@pytest.fixture
def fake_embedder(settings: DittoServiceSettings) -> FakeEmbedder:
    return FakeEmbedder(embedding_dim=settings.embedding_dim)
