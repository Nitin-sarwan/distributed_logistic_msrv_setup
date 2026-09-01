"""The shape every event this service emits.

Built in one place so the metadata cannot drift between producers. When
`libs/contracts/` exists this file moves there whole, and every service imports
it instead of copying it — which is the entire reason that folder is in the
blueprint.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

# Bumped only when a field is removed or retyped. Adding an optional field is
# safe and does not need a new version; removing one breaks every consumer.
EVENT_VERSION = 1

PRODUCER = "order"


def _json_safe(value: Any) -> Any:
    """Make a payload storable as JSONB and readable off Kafka.

    Decimal and datetime are the two types that reach here and cannot be
    serialised: money is Decimal throughout this service, and every timestamp is
    timezone-aware.
    """
    if isinstance(value, Decimal):
        # Money crosses the wire as a string, not a float. A consumer that
        # parses "149.00" into its own Decimal keeps the cents; one that reads
        # 149.0 as a float has already lost them.
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def build_envelope(
    event_type: str,
    aggregate_id: str,
    data: dict[str, Any],
    correlation_id: str | None = None,
) -> tuple[uuid.UUID, dict[str, Any]]:
    """Return `(event_id, envelope)`.

    The id comes back separately because it is also a column on the outbox row:
    the unique constraint there is what stops a retry writing the same event
    twice, and consumers deduplicate on the same value.

    `correlation_id` is the request id generated at the gateway. One delivery
    touches six services and two brokers; without it carried through every
    envelope, debugging is guesswork.
    """
    event_id = uuid.uuid4()

    envelope = {
        "event_id": str(event_id),
        "event_type": event_type,
        "event_version": EVENT_VERSION,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "correlation_id": correlation_id,
        "producer": PRODUCER,
        "aggregate_id": aggregate_id,
        "data": _json_safe(data),
    }

    return event_id, envelope
