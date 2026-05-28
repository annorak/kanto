"""Pipeline tests with fakes for every external system.

Coverage targets:

* Happy path: parquet written, mew upserted, event emitted, status flipped.
* Idempotency skip re-emits the event (recovery-gap-free design).
* Idempotency does not skip on a model_version mismatch.
* OOM fallback: retry the offending batch at size=1.
* OOM persistence at size=1 -> :class:`PermanentError`.
* Missing FASTA -> :class:`PermanentError`.
* Missing isolates row -> :class:`PermanentError`.
* Mew failure -> :class:`MewWriteError`.
* Event-emit failure after Mew success -> propagated to caller.
* Truncated proteins keep biological length in the aggregate + parquet.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pyarrow.parquet as pq
import pytest
from kanto_commons.mew import EmbeddingRow, IsolateStatus
from kanto_commons.mew.repositories import (
    EmbeddingNotFoundError,
    IsolateNotFoundError,
)
from kanto_commons.storage import KeyBuilder

from ditto.config import DittoServiceSettings
from ditto.errors import (
    EventEmitError,
    InferenceOOMError,
    MewWriteError,
    PermanentError,
)
from ditto.pipeline import EmbedPipeline
from tests.conftest import FakeBlobIO, FakeEmbedder, FakeEventEmitter, make_fasta_bytes

# ---------------------------------------------------------------------------
# Mew fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeMewClient:
    """Mimics :class:`kanto_commons.mew.MewClient`'s context managers.

    The repo fakes ignore the conn argument, so a sentinel object is
    enough.
    """

    @asynccontextmanager
    async def connection(self) -> Any:
        yield object()

    @asynccontextmanager
    async def transaction(self) -> Any:
        yield object()


@dataclass
class FakeEmbeddingRepo:
    existing: dict[str, EmbeddingRow] = field(default_factory=dict)
    upserts: list[EmbeddingRow] = field(default_factory=list)
    raise_on_upsert: Exception | None = None

    async def get(self, conn: Any, *, accession: str) -> EmbeddingRow:
        try:
            return self.existing[accession]
        except KeyError as exc:
            raise EmbeddingNotFoundError(accession) from exc

    async def upsert(self, conn: Any, row: EmbeddingRow) -> None:
        if self.raise_on_upsert is not None:
            raise self.raise_on_upsert
        self.upserts.append(row)


@dataclass
class FakeIsolateRepo:
    status_calls: list[tuple[str, IsolateStatus]] = field(default_factory=list)
    raise_isolate_not_found: bool = False

    async def update_status(
        self,
        conn: Any,
        *,
        accession: str,
        status: IsolateStatus,
        modal_call_id: str | None = None,
        qc_failure_reason: str | None = None,
    ) -> None:
        if self.raise_isolate_not_found:
            raise IsolateNotFoundError(accession)
        self.status_calls.append((accession, status))


# ---------------------------------------------------------------------------
# Pipeline factory
# ---------------------------------------------------------------------------


def _make_pipeline(
    *,
    settings: DittoServiceSettings,
    blob_io: FakeBlobIO,
    embedder: FakeEmbedder,
    emitter: FakeEventEmitter,
    embedding_repo: FakeEmbeddingRepo | None = None,
    isolate_repo: FakeIsolateRepo | None = None,
) -> tuple[EmbedPipeline, FakeEmbeddingRepo, FakeIsolateRepo]:
    erepo = embedding_repo or FakeEmbeddingRepo()
    irepo = isolate_repo or FakeIsolateRepo()
    pipeline = EmbedPipeline(
        embedder=embedder,
        blob_io=blob_io,
        mew=FakeMewClient(),  # type: ignore[arg-type] -- duck-types MewClient
        emitter=emitter,
        key_builder=KeyBuilder(
            proteins_container="kanto-proteins-test",
            embeddings_container="kanto-embeddings-test",
            metadata_container="kanto-metadata-test",
        ),
        settings=settings,
        isolate_repo=irepo,  # type: ignore[arg-type]
        embedding_repo=erepo,  # type: ignore[arg-type]
    )
    return pipeline, erepo, irepo


def _put_fasta(blob_io: FakeBlobIO, sequences: dict[str, str]) -> None:
    blob_io.put_protein(
        container="kanto-proteins-test",
        key="PDT001/1.faa.gz",
        data=make_fasta_bytes(sequences),
    )


async def _embed(pipeline: EmbedPipeline) -> Any:
    return await pipeline.embed_isolate(accession="PDT001", version=1, os_key="PDT001/1.faa.gz")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_happy_path_writes_everything_in_order(
    settings: DittoServiceSettings,
    fake_blob_io: FakeBlobIO,
    fake_embedder: FakeEmbedder,
    fake_emitter: FakeEventEmitter,
) -> None:
    _put_fasta(fake_blob_io, {"p1": "MAGI", "p2": "GGGT", "p3": "MMMM"})
    pipeline, erepo, irepo = _make_pipeline(
        settings=settings,
        blob_io=fake_blob_io,
        embedder=fake_embedder,
        emitter=fake_emitter,
    )

    result = await _embed(pipeline)

    assert result.proteins_embedded == 3
    assert result.skipped_idempotent is False
    assert result.parquet_key == "PDT001/1.parquet"

    parquet_blob = fake_blob_io.parquets[("kanto-embeddings-test", "PDT001/1.parquet")]
    assert parquet_blob[:4] == b"PAR1"  # parquet magic

    # Aggregate upserted with the configured model + version.
    assert len(erepo.upserts) == 1
    upsert = erepo.upserts[0]
    assert upsert.accession == "PDT001"
    assert upsert.model == "esmc_600m"
    assert upsert.model_version == settings.model_version
    assert len(upsert.embedding) == settings.embedding_dim

    assert irepo.status_calls == [("PDT001", IsolateStatus.EMBEDDED)]
    assert len(fake_emitter.emitted) == 1
    assert fake_emitter.emitted[0]["accession"] == "PDT001"


async def test_idempotency_skip_reemits_event(
    settings: DittoServiceSettings,
    fake_blob_io: FakeBlobIO,
    fake_embedder: FakeEmbedder,
    fake_emitter: FakeEventEmitter,
) -> None:
    """The skip path must still emit the event so a Modal retry after a
    crashed previous emit doesn't leave downstream stuck."""
    erepo = FakeEmbeddingRepo(
        existing={
            "PDT001": EmbeddingRow(
                accession="PDT001",
                version=1,
                model="esmc_600m",
                model_version=settings.model_version,
                embedding=[0.0] * settings.embedding_dim,
            )
        }
    )
    pipeline, _, irepo = _make_pipeline(
        settings=settings,
        blob_io=fake_blob_io,
        embedder=fake_embedder,
        emitter=fake_emitter,
        embedding_repo=erepo,
    )

    result = await _embed(pipeline)

    assert result.skipped_idempotent is True
    assert result.proteins_embedded == 0
    # No GPU work, no parquet, no Mew write.
    assert len(erepo.upserts) == 0
    assert len(irepo.status_calls) == 0
    # But the event WAS re-emitted -- consumers dedupe.
    assert len(fake_emitter.emitted) == 1
    assert fake_emitter.emitted[0]["accession"] == "PDT001"


async def test_idempotency_does_not_skip_on_different_model_version(
    settings: DittoServiceSettings,
    fake_blob_io: FakeBlobIO,
    fake_embedder: FakeEmbedder,
    fake_emitter: FakeEventEmitter,
) -> None:
    _put_fasta(fake_blob_io, {"p1": "MAGI"})
    erepo = FakeEmbeddingRepo(
        existing={
            "PDT001": EmbeddingRow(
                accession="PDT001",
                version=1,
                model="esmc_600m",
                model_version="OLD",  # mismatch
                embedding=[0.0] * settings.embedding_dim,
            )
        }
    )
    pipeline, _, _ = _make_pipeline(
        settings=settings,
        blob_io=fake_blob_io,
        embedder=fake_embedder,
        emitter=fake_emitter,
        embedding_repo=erepo,
    )

    result = await _embed(pipeline)
    assert result.skipped_idempotent is False
    assert result.proteins_embedded == 1


async def test_idempotency_does_not_skip_on_higher_isolate_version(
    settings: DittoServiceSettings,
    fake_blob_io: FakeBlobIO,
    fake_embedder: FakeEmbedder,
    fake_emitter: FakeEventEmitter,
) -> None:
    """Mew has v=1 of accession PDT001; a v=2 event arrives. The pipeline
    MUST run the GPU work, not short-circuit on the stored v=1 row.
    """
    fake_blob_io.put_protein(
        container="kanto-proteins-test",
        key="PDT001/2.faa.gz",
        data=make_fasta_bytes({"p1": "MAGI", "p2": "GGGT"}),
    )
    erepo = FakeEmbeddingRepo(
        existing={
            "PDT001": EmbeddingRow(
                accession="PDT001",
                version=1,  # stored version is older than the event's
                model="esmc_600m",
                model_version=settings.model_version,
                embedding=[0.0] * settings.embedding_dim,
            )
        }
    )
    pipeline, erepo_out, irepo = _make_pipeline(
        settings=settings,
        blob_io=fake_blob_io,
        embedder=fake_embedder,
        emitter=fake_emitter,
        embedding_repo=erepo,
    )

    result = await pipeline.embed_isolate(accession="PDT001", version=2, os_key="PDT001/2.faa.gz")

    assert result.skipped_idempotent is False
    assert result.proteins_embedded == 2
    assert len(erepo_out.upserts) == 1
    assert erepo_out.upserts[0].version == 2
    assert irepo.status_calls == [("PDT001", IsolateStatus.EMBEDDED)]


async def test_idempotency_skips_on_equal_or_lower_event_version(
    settings: DittoServiceSettings,
    fake_blob_io: FakeBlobIO,
    fake_embedder: FakeEmbedder,
    fake_emitter: FakeEventEmitter,
) -> None:
    """Stored version >= event version => already embedded; skip + re-emit."""
    erepo = FakeEmbeddingRepo(
        existing={
            "PDT001": EmbeddingRow(
                accession="PDT001",
                version=3,  # stored newer than event
                model="esmc_600m",
                model_version=settings.model_version,
                embedding=[0.0] * settings.embedding_dim,
            )
        }
    )
    pipeline, erepo_out, irepo = _make_pipeline(
        settings=settings,
        blob_io=fake_blob_io,
        embedder=fake_embedder,
        emitter=fake_emitter,
        embedding_repo=erepo,
    )

    result = await pipeline.embed_isolate(accession="PDT001", version=2, os_key="PDT001/2.faa.gz")

    assert result.skipped_idempotent is True
    assert len(erepo_out.upserts) == 0
    assert len(irepo.status_calls) == 0
    # Event still re-emitted so downstream isn't stuck.
    assert len(fake_emitter.emitted) == 1


async def test_missing_fasta_raises_permanent_error(
    settings: DittoServiceSettings,
    fake_blob_io: FakeBlobIO,
    fake_embedder: FakeEmbedder,
    fake_emitter: FakeEventEmitter,
) -> None:
    pipeline, _, _ = _make_pipeline(
        settings=settings,
        blob_io=fake_blob_io,
        embedder=fake_embedder,
        emitter=fake_emitter,
    )
    with pytest.raises(PermanentError):
        await _embed(pipeline)


async def test_missing_isolates_row_raises_permanent_error(
    settings: DittoServiceSettings,
    fake_blob_io: FakeBlobIO,
    fake_embedder: FakeEmbedder,
    fake_emitter: FakeEventEmitter,
) -> None:
    _put_fasta(fake_blob_io, {"p1": "MAGI"})
    irepo = FakeIsolateRepo(raise_isolate_not_found=True)
    pipeline, _, _ = _make_pipeline(
        settings=settings,
        blob_io=fake_blob_io,
        embedder=fake_embedder,
        emitter=fake_emitter,
        isolate_repo=irepo,
    )
    with pytest.raises(PermanentError, match="no isolates row"):
        await _embed(pipeline)


async def test_oom_fallback_succeeds_at_batch_size_one(
    settings: DittoServiceSettings,
    fake_blob_io: FakeBlobIO,
    fake_emitter: FakeEventEmitter,
) -> None:
    _put_fasta(fake_blob_io, {"p1": "MAGI", "p2": "GGGT", "p3": "MMMM"})
    # First call OOMs (the multi-protein batch); singletons succeed.
    embedder = FakeEmbedder(
        embedding_dim=settings.embedding_dim,
        raise_on_call=[InferenceOOMError("batch=3 OOM")],
    )
    pipeline, erepo, _ = _make_pipeline(
        settings=settings,
        blob_io=fake_blob_io,
        embedder=embedder,
        emitter=fake_emitter,
    )

    result = await _embed(pipeline)
    assert result.proteins_embedded == 3
    assert len(erepo.upserts) == 1


async def test_oom_persists_at_size_one_raises_permanent_error(
    settings: DittoServiceSettings,
    fake_blob_io: FakeBlobIO,
    fake_emitter: FakeEventEmitter,
) -> None:
    _put_fasta(fake_blob_io, {"p1": "MAGI"})
    embedder = FakeEmbedder(
        embedding_dim=settings.embedding_dim,
        raise_on_call=[
            InferenceOOMError("batch=1 OOM"),
            InferenceOOMError("singleton OOM"),
        ],
    )
    pipeline, _, _ = _make_pipeline(
        settings=settings,
        blob_io=fake_blob_io,
        embedder=embedder,
        emitter=fake_emitter,
    )
    with pytest.raises(PermanentError, match="OOMs at batch_size=1"):
        await _embed(pipeline)


async def test_mew_upsert_failure_raises_mew_write_error(
    settings: DittoServiceSettings,
    fake_blob_io: FakeBlobIO,
    fake_embedder: FakeEmbedder,
    fake_emitter: FakeEventEmitter,
) -> None:
    _put_fasta(fake_blob_io, {"p1": "MAGI"})
    erepo = FakeEmbeddingRepo(raise_on_upsert=psycopg.errors.OperationalError("pool"))
    pipeline, _, _ = _make_pipeline(
        settings=settings,
        blob_io=fake_blob_io,
        embedder=fake_embedder,
        emitter=fake_emitter,
        embedding_repo=erepo,
    )
    with pytest.raises(MewWriteError):
        await _embed(pipeline)
    # Parquet was written BEFORE the Mew step; idempotent on re-run.
    assert ("kanto-embeddings-test", "PDT001/1.parquet") in fake_blob_io.parquets


async def test_event_emit_failure_after_mew_success_propagates(
    settings: DittoServiceSettings,
    fake_blob_io: FakeBlobIO,
    fake_embedder: FakeEmbedder,
) -> None:
    _put_fasta(fake_blob_io, {"p1": "MAGI"})
    emitter = FakeEventEmitter(raise_on_emit=EventEmitError("event hubs down"))
    pipeline, erepo, irepo = _make_pipeline(
        settings=settings,
        blob_io=fake_blob_io,
        embedder=fake_embedder,
        emitter=emitter,
    )

    with pytest.raises(EventEmitError):
        await _embed(pipeline)

    # Mew write + status flip already happened. A subsequent run hits
    # the idempotent skip path, which re-emits the event -- no recovery
    # gap. This is the behavior `test_idempotency_skip_reemits_event`
    # asserts on the next pipeline call.
    assert len(erepo.upserts) == 1
    assert irepo.status_calls == [("PDT001", IsolateStatus.EMBEDDED)]


async def test_truncated_protein_uses_biological_length_in_parquet(
    settings: DittoServiceSettings,
    fake_blob_io: FakeBlobIO,
    fake_embedder: FakeEmbedder,
    fake_emitter: FakeEventEmitter,
) -> None:
    # max_sequence_length=128 in the fixture; ship a 200-residue protein.
    _put_fasta(fake_blob_io, {"long": "M" * 200})
    pipeline, _, _ = _make_pipeline(
        settings=settings,
        blob_io=fake_blob_io,
        embedder=fake_embedder,
        emitter=fake_emitter,
    )
    await _embed(pipeline)

    parquet_blob = fake_blob_io.parquets[("kanto-embeddings-test", "PDT001/1.parquet")]
    import io

    table = pq.read_table(io.BytesIO(parquet_blob))
    # The parquet row reports the PRE-truncation (biological) length.
    assert table.column("sequence_length").to_pylist() == [200]
