"""Event and payload schemas shared across all Kanto services.

Every event flowing through OCI Streaming and every structured payload
crossing a service boundary (e.g., the Snorlax → Modal handoff) is
defined here as a pydantic v2 model. Service code never invents
ad-hoc dicts to talk to other services; it always serializes one of
these schemas.

Each schema carries its own ``schema_version`` field. Bumping that
field is a contract change — coordinate the change across producer
and consumer services.

The :data:`EVENT_REGISTRY` exposes a typed mapping of event name to
its model class plus transport metadata (which OCI Streaming topic the
event flows through, or ``modal`` for the Snorlax → Ditto payload).
"""

from kanto_common.schemas.dlq import DLQEnvelope
from kanto_common.schemas.events import (
    EVENT_REGISTRY,
    EmbeddingsReady,
    EventEnvelope,
    EventName,
    IsolateDiscovered,
    IsolateScored,
    KantoEvent,
    ProteinsReady,
    Transport,
)

__all__ = [
    "EVENT_REGISTRY",
    "DLQEnvelope",
    "EmbeddingsReady",
    "EventEnvelope",
    "EventName",
    "IsolateDiscovered",
    "IsolateScored",
    "KantoEvent",
    "ProteinsReady",
    "Transport",
]
