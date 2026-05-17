"""Species centroid recomputation job.

For each organism with at least ``centroid_min_isolates`` embedded
isolates in Mew, compute the centroid (mean) and per-dimension
variance of the embedding cloud, regularize the variance with a
small epsilon, and upsert into ``species_centroids``.

Runs as a Kubernetes CronJob on a daily schedule; safe to invoke
manually from the cluster (``kubectl exec`` into an Alakazam pod
and run ``alakazam-centroids``) for an ad-hoc refresh.

Why diagonal variance, not full covariance:
* The full 1152x1152 covariance for ~10k isolates costs ~5 MB per
  species and dominates the centroid table footprint. We don't need
  cross-dimensional terms to flag "this isolate is unusually far from
  the species mean" — the diagonal Mahalanobis is the textbook
  standardized-distance reduction.
* If we ever discover the cross terms matter we'll add a second
  column rather than break this one.

Statistical notes:
* The variance is the *population* variance (ddof=0). For ~10k
  isolates the unbiased vs population distinction is noise; for
  smaller species the regularization eps dominates anyway.
* We compute everything in float32 — the embeddings are stored as
  float32 in pgvector, and float64 accumulation buys nothing once
  the eps floor is in play.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime

import numpy as np
from kanto_commons.logging import setup_logging
from kanto_commons.mew import MewClient

from alakazam.config import AlakazamSettings
from alakazam.mew_gateway import AlakazamMewGateway, SpeciesCentroidRecord

logger = logging.getLogger(__name__)


_MODEL = "esm-c-600m"
_MODEL_VERSION = "1.0.0"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="alakazam-centroids",
        description=(
            "Recompute species centroids + diagonal covariance and "
            "upsert into Mew's species_centroids table."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute centroids but do not write to Mew.",
    )
    parser.add_argument(
        "--organism",
        action="append",
        default=None,
        help=(
            "Restrict to one organism. May be repeated. Defaults to "
            "every organism with enough isolates."
        ),
    )
    return parser.parse_args(argv)


async def run(
    settings: AlakazamSettings,
    *,
    dry_run: bool,
    organisms: list[str] | None,
) -> dict[str, int]:
    """Compute + upsert centroids. Returns ``{organism: n_isolates}``."""
    mew = await MewClient.from_settings(settings.mew)
    gateway = AlakazamMewGateway(mew=mew)

    try:
        target_organisms = _select_organisms(
            await gateway.list_organisms_with_embeddings(
                min_isolates=settings.alakazam.centroid_min_isolates,
            ),
            filter_to=organisms or _parse_filter(settings.alakazam.centroid_organism_filter),
        )
        logger.info(
            "alakazam.centroids: candidates=%d dry_run=%s",
            len(target_organisms),
            dry_run,
        )

        outcomes: dict[str, int] = {}
        for organism, count in target_organisms:
            try:
                record = await _compute_one_centroid(
                    gateway=gateway,
                    organism=organism,
                    expected_count=count,
                    regularization=settings.alakazam.mahalanobis_regularization,
                )
            except _InsufficientDataError as exc:
                logger.warning(
                    "alakazam.centroids: skip organism=%s reason=%s",
                    organism,
                    exc,
                )
                continue
            outcomes[organism] = record.n_isolates
            if dry_run:
                logger.info(
                    "alakazam.centroids: DRY-RUN organism=%s n=%d centroid_norm=%.4f mean_var=%.6f",
                    organism,
                    record.n_isolates,
                    float(np.linalg.norm(record.centroid)),
                    float(np.mean(record.covariance_diagonal)),
                )
            else:
                await gateway.upsert_species_centroid(record)
                logger.info(
                    "alakazam.centroids: upserted organism=%s n=%d",
                    organism,
                    record.n_isolates,
                )
        return outcomes
    finally:
        await mew.close()


async def _compute_one_centroid(
    *,
    gateway: AlakazamMewGateway,
    organism: str,
    expected_count: int,
    regularization: float,
) -> SpeciesCentroidRecord:
    embeddings = await gateway.stream_embeddings_by_organism(organism)
    if len(embeddings) == 0:
        raise _InsufficientDataError(f"no embeddings for organism={organism!r}")
    if len(embeddings) < 2:
        # Variance for a single sample is undefined; the regularization
        # would dominate but the centroid would be useless anyway.
        raise _InsufficientDataError(
            f"organism={organism!r} has {len(embeddings)} embedding(s); need >= 2"
        )
    if len(embeddings) != expected_count:
        # Race-ish: between the count query and the stream a new row
        # landed. Not a problem; log and proceed with what we have.
        logger.info(
            "alakazam.centroids: organism=%s expected=%d actual=%d",
            organism,
            expected_count,
            len(embeddings),
        )

    matrix = np.asarray(embeddings, dtype=np.float32)
    centroid = matrix.mean(axis=0)
    variances = matrix.var(axis=0)
    return SpeciesCentroidRecord(
        organism=organism,
        model=_MODEL,
        model_version=_MODEL_VERSION,
        n_isolates=int(matrix.shape[0]),
        centroid=centroid.tolist(),
        covariance_diagonal=variances.tolist(),
        regularization=float(regularization),
        computed_at=datetime.now(UTC),
    )


def _select_organisms(
    available: list[tuple[str, int]],
    *,
    filter_to: list[str] | None,
) -> list[tuple[str, int]]:
    if not filter_to:
        return available
    wanted = set(filter_to)
    return [(o, c) for (o, c) in available if o in wanted]


def _parse_filter(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    return [item.strip() for item in raw.split(",") if item.strip()]


class _InsufficientDataError(RuntimeError):
    """Skip-this-organism marker for the recompute loop."""


async def amain(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = AlakazamSettings.load()
    setup_logging(
        service_name="alakazam-centroids",
        log_level=settings.log_level,
        log_format=settings.log_format,
    )
    try:
        await run(settings, dry_run=args.dry_run, organisms=args.organism)
    except Exception:
        logger.exception("alakazam.centroids: job failed")
        return 1
    return 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()


__all__ = ["amain", "main", "run"]
