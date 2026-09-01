"""Emitting an event means writing a row.

Nothing in this service ever calls a broker. A publisher that talked to Kafka
directly would have to do it either inside the transaction — where a slow broker
holds a database lock and a rollback cannot unsend the message — or after the
commit, where a crash loses the event permanently.

So `publish()` adds to the session and returns. The caller commits it alongside
the state change, and `events/relay.py` does the rest.
"""

import logging
from typing import Any

from sqlalchemy.orm import Session

from src.services.orderServices.events.envelope import build_envelope
from src.services.orderServices.models.outbox_model import Outbox

logger = logging.getLogger(__name__)

# Every event this service produces. Listed so a typo is an ImportError rather
# than a topic nobody subscribes to.
ORDER_CREATED = "order.created"
ORDER_CONFIRMED = "order.confirmed"
ORDER_PICKED_UP = "order.picked_up"
ORDER_DELIVERED = "order.delivered"
ORDER_CANCELLED = "order.cancelled"


def publish(
    db: Session,
    event_type: str,
    aggregate_id: str,
    data: dict[str, Any],
    correlation_id: str | None = None,
) -> None:
    """Queue an event for delivery, in the caller's transaction.

    Deliberately returns nothing. There is no "did it send?" to report — that
    question is answered by the relay, minutes later at worst, and a caller that
    could branch on it would be tempted to treat a send failure as a reason not
    to commit the order.
    """
    event_id, envelope = build_envelope(event_type, aggregate_id, data, correlation_id)

    db.add(
        Outbox(
            event_id=event_id,
            event_type=event_type,
            aggregate_id=aggregate_id,
            payload=envelope,
        )
    )

    logger.info("queued %s for order %s (event %s)", event_type, aggregate_id, event_id)
