"""Emitting an event means writing a row.

Nothing outside `relay.py` ever calls a broker. A publisher that talked to Kafka
directly would have to do it either inside the transaction — where a slow broker
holds a database lock and a rollback cannot unsend the message — or after the
commit, where a crash loses the event permanently.

So `publish()` adds to the session and returns. The caller commits it alongside
the state change, and the relay does the rest.
"""

import logging
from typing import Any

from sqlalchemy.orm import Session

from src.common.events.envelope import build_envelope
from src.common.events.types import ALL_EVENT_TYPES

logger = logging.getLogger(__name__)


def publish(
    db: Session,
    outbox_model: type,
    producer: str,
    event_type: str,
    aggregate_id: str,
    data: dict[str, Any],
    correlation_id: str | None = None,
) -> None:
    """Queue an event for delivery, in the caller's transaction.

    `outbox_model` is the service's own Outbox class — each service owns its own
    database, so each has its own table, and the relay reads whichever one it
    was handed.

    Deliberately returns nothing. There is no "did it send?" to report — that
    question is answered by the relay, seconds later at worst, and a caller that
    could branch on it would be tempted to treat a send failure as a reason not
    to commit the state change.
    """
    if event_type not in ALL_EVENT_TYPES:
        # Loud, and at the call site rather than in a consumer that silently
        # never matches. A new event type is a contract change; it belongs in
        # types.py before it belongs on the wire.
        raise ValueError(
            f"{event_type!r} is not a registered event type. Add it to "
            f"src/common/events/types.py before publishing it."
        )

    event_id, envelope = build_envelope(
        event_type, aggregate_id, data, producer, correlation_id
    )

    db.add(
        outbox_model(
            event_id=event_id,
            event_type=event_type,
            aggregate_id=aggregate_id,
            payload=envelope,
        )
    )

    logger.info("queued %s for order %s (event %s)", event_type, aggregate_id, event_id)
