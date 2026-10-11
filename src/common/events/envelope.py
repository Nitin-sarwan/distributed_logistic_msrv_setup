"""The shape every event on the platform has.

Moved here from `orderServices/events/envelope.py`, which said it would move the
day a second producer existed. Dispatch is that producer.

Built in one place so the metadata cannot drift: a consumer reads
`envelope["data"]` from every service, and one producer that spells it
`payload` breaks every handler at once.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

# Bumped only when a field is removed or retyped. Adding an optional field is
# safe and does not need a new version; removing one breaks every consumer.
EVENT_VERSION = 1


def json_safe(value: Any) -> Any:
    """Make a payload storable as JSONB and readable off Kafka.

    Decimal and datetime are the two types that reach here and cannot be
    serialised: money and distances are Decimal throughout, and every timestamp
    is timezone-aware.
    """
    if isinstance(value, Decimal):
        # Money crosses the wire as a string, not a float. A consumer that
        # parses "149.00" into its own Decimal keeps the cents; one that reads
        # 149.0 as a float has already lost them.
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def build_envelope(
    event_type: str,
    aggregate_id: str,
    data: dict[str, Any],
    producer: str,
    correlation_id: str | None = None,
) -> tuple[uuid.UUID, dict[str, Any]]:
    """Return `(event_id, envelope)`.

    The id comes back separately because it is also a column on the outbox row:
    the unique constraint there is what stops a retry queueing the same event
    twice, and consumers deduplicate on the same value.

    `aggregate_id` is the order id, on every event from every producer. It
    becomes the Kafka partition key, and that is the whole ordering guarantee —
    see `types.TOPIC`.

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
        "producer": producer,
        "aggregate_id": aggregate_id,
        "data": json_safe(data),
    }

    return event_id, envelope
