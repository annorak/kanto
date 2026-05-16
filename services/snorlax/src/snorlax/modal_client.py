"""Modal SDK wrapper for invoking the Ditto embed function.

Snorlax calls Modal via ``Function.spawn`` (fire-and-forget) rather
than ``.remote`` (blocking) for the reason in the design doc: Snorlax
processes events one at a time per partition and we want it to be
free to handle the next event the moment the GPU work is queued.

Two implementations live behind the :class:`ModalClient` protocol:

* :class:`RealModalClient` — wraps :mod:`modal` proper. Looked up
  lazily so unit tests don't take a Modal dependency. Configured via
  ``KANTO_SNORLAX_MODAL_FUNCTION_REF`` and the standard ``MODAL_*``
  environment variables the SDK already reads.

* :class:`NoopModalClient` — generates a deterministic fake call ID
  and logs the payload. Used until Task 7 ships the Ditto function and
  in any integration test that doesn't want a real Modal account.

The protocol is intentionally narrow: a single ``spawn`` method that
takes the structured payload and returns a call ID. Future expansions
(status polling, cancellation) can live on the same protocol when
they're needed.

Idempotency key
---------------
Modal deduplicates calls with the same idempotency key for a short
window (~24 h at the time of writing — verify before relying on the
exact duration). We pass ``{accession}-{version}`` so a Snorlax retry
of a successful spawn doesn't kick off a duplicate GPU run.

Trace context
-------------
The spawn site is wrapped in a tracing span by the pipeline; the
client itself just inherits the active OTel context. Modal does not
yet propagate OTel context through to the function execution
automatically, so the inside of Ditto starts a fresh trace — this is
acceptable for v1 and is documented as a known limitation in the
README.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Protocol

from kanto_commons import ProteinsReady
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ModalClientError(Exception):
    """Base for Modal-side failures surfaced to the pipeline."""


class ModalSpawnError(ModalClientError):
    """Raised after the configured retry budget is exhausted.

    The pipeline routes the message to the DLQ; the protein FASTA is
    already in OS so an operator can recover by re-spawning the call
    manually.
    """


class ModalUnavailableError(ModalClientError):
    """Modal SDK couldn't be imported or no credentials are configured.

    Surfaced at startup, not per-message: if Modal isn't usable, the
    pod should fail readiness rather than DLQing every event.
    """


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpawnedCall:
    """A queued Modal function call. ``call_id`` is what we store in Mew."""

    call_id: str
    idempotency_key: str


# ---------------------------------------------------------------------------
# Protocol — pipeline depends on this, not the concrete classes.
# ---------------------------------------------------------------------------


class ModalClient(Protocol):
    """Minimal surface the pipeline depends on."""

    def spawn(self, *, payload: ProteinsReady) -> SpawnedCall: ...

    def healthy(self) -> bool: ...


# ---------------------------------------------------------------------------
# Real client
# ---------------------------------------------------------------------------


def _idempotency_key_for(payload: ProteinsReady) -> str:
    """Build a Modal idempotency key from the payload.

    Modal documents a maximum key length around 64 characters; a hex
    digest comfortably fits and never collides for distinct accessions.
    """
    raw = f"{payload.accession}-{payload.version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class RealModalClient:
    """Concrete Modal SDK wrapper.

    The Modal SDK import is lazy: when ``modal_enabled=False`` (e.g.
    Task 7 not yet shipped, or running unit tests), the module never
    needs to be installed.
    """

    def __init__(
        self,
        *,
        function_ref: str,
        environment: str,
        max_attempts: int,
    ) -> None:
        self._function_ref = function_ref
        self._environment = environment
        self._max_attempts = max_attempts
        self._function = self._lookup_function()
        self._retry = Retrying(
            retry=retry_if_exception_type(_TransientSpawnError),
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=0.5, max=10.0),
            reraise=True,
        )

    def _lookup_function(self) -> object:  # pragma: no cover — needs modal SDK
        try:
            import modal  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ModalUnavailableError(
                "modal SDK is not installed — set KANTO_SNORLAX_MODAL_ENABLED=false "
                "to use the no-op client until the Ditto function ships in Task 7"
            ) from exc
        if "/" not in self._function_ref:
            raise ModalUnavailableError(
                f"function_ref must be 'app/function'; got {self._function_ref!r}"
            )
        app_name, func_name = self._function_ref.split("/", 1)
        try:
            return modal.Function.lookup(
                app_name,
                func_name,
                environment_name=self._environment,
            )
        except Exception as exc:
            raise ModalUnavailableError(f"failed to resolve {self._function_ref!r}: {exc}") from exc

    def spawn(self, *, payload: ProteinsReady) -> SpawnedCall:  # pragma: no cover — needs modal SDK
        idem = _idempotency_key_for(payload)

        def _do() -> SpawnedCall:
            try:
                handle = self._function.spawn(  # type: ignore[attr-defined]
                    accession=payload.accession,
                    version=payload.version,
                    os_key=payload.os_key,
                    _idempotency_key=idem,
                )
            except Exception as exc:
                raise _TransientSpawnError(str(exc)) from exc
            call_id = getattr(handle, "object_id", None) or getattr(handle, "call_id", None)
            if call_id is None:
                # Modal SDKs across versions expose the id under slightly
                # different attribute names. If we ever land in a version
                # where neither is present, fail loud rather than store
                # ``None`` in Mew and lose the call.
                raise _TransientSpawnError(
                    f"modal returned a handle without an object_id: {handle!r}"
                )
            return SpawnedCall(call_id=str(call_id), idempotency_key=idem)

        try:
            return self._retry(_do)
        except _TransientSpawnError as exc:
            raise ModalSpawnError(str(exc)) from exc

    def healthy(self) -> bool:  # pragma: no cover — needs modal SDK
        # Resolving the function at init time is the proof of life;
        # there's nothing cheap to call here on the actual GPU function
        # without invoking work.
        return self._function is not None


class _TransientSpawnError(Exception):
    """Internal: marker that the retry decorator should re-run."""


# ---------------------------------------------------------------------------
# No-op client — covers Task 6 testing before Task 7 ships
# ---------------------------------------------------------------------------


class NoopModalClient:
    """Logs payloads and returns a deterministic call id.

    Useful when:

    * Modal is intentionally disabled in dev because Task 7 hasn't
      shipped yet.
    * An integration test wants to exercise the pipeline end-to-end
      without a Modal account.

    The call id is derived from the idempotency key so the same
    isolate always lands the same id, which makes Mew assertions in
    tests stable.
    """

    def __init__(self) -> None:
        self.calls: list[SpawnedCall] = []

    def spawn(self, *, payload: ProteinsReady) -> SpawnedCall:
        idem = _idempotency_key_for(payload)
        call = SpawnedCall(call_id=f"noop-{idem[:12]}", idempotency_key=idem)
        logger.info(
            "snorlax.modal_client: NOOP spawn accession=%s version=%d os_key=%s id=%s",
            payload.accession,
            payload.version,
            payload.os_key,
            call.call_id,
        )
        self.calls.append(call)
        return call

    def healthy(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_modal_client(
    *,
    enabled: bool,
    function_ref: str,
    environment: str,
    max_attempts: int,
) -> ModalClient:
    """Pick the real or the no-op client based on ``enabled``."""
    if not enabled:
        logger.info("snorlax.modal_client: using NoopModalClient (modal_enabled=false)")
        return NoopModalClient()
    return RealModalClient(
        function_ref=function_ref,
        environment=environment,
        max_attempts=max_attempts,
    )


__all__ = [
    "ModalClient",
    "ModalClientError",
    "ModalSpawnError",
    "ModalUnavailableError",
    "NoopModalClient",
    "RealModalClient",
    "SpawnedCall",
    "build_modal_client",
]
