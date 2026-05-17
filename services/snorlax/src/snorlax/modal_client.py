"""Modal SDK wrapper for invoking the Ditto embed function.

Snorlax calls Modal via ``.spawn`` (fire-and-forget) rather than
``.remote`` (blocking): Snorlax processes events one at a time per
partition and we want it free to handle the next event the moment
the GPU work is queued.

Two implementations live behind the :class:`ModalClient` protocol:

* :class:`RealModalClient` — wraps :mod:`modal` proper. Looked up
  lazily so unit tests don't take a Modal dependency. Configured via
  ``KANTO_SNORLAX_MODAL_FUNCTION_REF`` and the standard ``MODAL_*``
  environment variables the SDK already reads.

* :class:`NoopModalClient` — generates a deterministic fake call ID
  and logs the payload. Used in dev when Modal is disabled and in
  integration tests that don't want a real Modal account.

Function reference format
-------------------------
Ditto is deployed as a Modal *class* (``@app.cls``) with one method,
not a free-standing ``@app.function``. The function-ref is therefore
three slash-separated parts: ``app_name/class_name/method_name``. The
SDK call shape is::

    cls = modal.Cls.from_name(app, class_name, environment_name=env)
    instance = cls()
    handle = instance.embed.spawn(...)

Idempotency
-----------
Modal's public docs do not document a spawn-time idempotency_key
parameter; Ditto's own pipeline does the dedupe app-side via Mew
(see ditto/pipeline.py). We compute a stable key and stash it on the
SpawnedCall return value for traceability, but the dedupe contract
is Ditto's, not Modal's.
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
    """Stable key for traceability across retries.

    Recorded on :class:`SpawnedCall` so logs across retries correlate.
    Modal does not consume this; the dedupe contract lives in Ditto's
    pipeline (see ditto/pipeline.py).
    """
    raw = f"{payload.accession}-{payload.version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class RealModalClient:
    """Concrete Modal SDK wrapper.

    The Modal SDK import is lazy: when ``modal_enabled=False`` the
    module never needs to be installed.
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
        self._spawnable = self._lookup_spawnable()
        self._retry = Retrying(
            retry=retry_if_exception_type(_TransientSpawnError),
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=0.5, max=10.0),
            reraise=True,
        )

    def _lookup_spawnable(self) -> object:  # pragma: no cover — needs modal SDK
        try:
            import modal
        except ImportError as exc:
            raise ModalUnavailableError(
                "modal SDK is not installed — set KANTO_SNORLAX_MODAL_ENABLED=false"
            ) from exc
        parts = self._function_ref.split("/")
        if len(parts) != 3:
            raise ModalUnavailableError(
                f"function_ref must be 'app/Class/method'; got {self._function_ref!r}"
            )
        app_name, cls_name, method_name = parts
        try:
            cls_handle = modal.Cls.from_name(app_name, cls_name, environment_name=self._environment)
            return getattr(cls_handle(), method_name)
        except Exception as exc:
            raise ModalUnavailableError(f"failed to resolve {self._function_ref!r}: {exc}") from exc

    def spawn(self, *, payload: ProteinsReady) -> SpawnedCall:  # pragma: no cover — needs modal SDK
        idem = _idempotency_key_for(payload)

        def _do() -> SpawnedCall:
            try:
                handle = self._spawnable.spawn(  # type: ignore[attr-defined]
                    accession=payload.accession,
                    version=payload.version,
                    os_key=payload.os_key,
                )
            except Exception as exc:
                raise _TransientSpawnError(str(exc)) from exc
            # Modal's FunctionCall handle exposes the id as `.object_id`
            # since 1.0; falling back is paranoia.
            call_id = getattr(handle, "object_id", None) or getattr(handle, "call_id", None)
            if call_id is None:
                raise _TransientSpawnError(
                    f"modal returned a handle without an object_id: {handle!r}"
                )
            return SpawnedCall(call_id=str(call_id), idempotency_key=idem)

        try:
            return self._retry(_do)
        except _TransientSpawnError as exc:
            raise ModalSpawnError(str(exc)) from exc

    def healthy(self) -> bool:  # pragma: no cover — needs modal SDK
        return self._spawnable is not None


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
