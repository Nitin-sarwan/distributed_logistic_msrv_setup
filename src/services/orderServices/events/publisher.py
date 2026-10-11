"""This service's view of publishing: bind the two things that never vary.

The mechanism moved to `src/common/events/publisher.py` the day a second service
started producing. What stays here is the binding - Order's outbox table and
Order's producer name - so a call site says what happened and nothing about
where the row goes.

The event-type constants are re-exported rather than redefined. Importing them
from `common.events.types` at each call site would work; re-exporting keeps the
existing `publisher.ORDER_CONFIRMED` spelling, and means there is exactly one
list of what this service produces.
"""

from typing import Any

from sqlalchemy.orm import Session

from src.common.events import publisher as common
from src.common.events.types import (
    ORDER_CANCELLED,
    ORDER_CONFIRMED,
    ORDER_CREATED,
    ORDER_DELIVERED,
    ORDER_PICKED_UP,
    PRODUCER_ORDER,
)
from src.services.orderServices.models.outbox_model import Outbox

# Re-exported so `from ... import publisher; publisher.ORDER_CONFIRMED` keeps
# working, and so this file is the answer to "what does Order emit?".
__all__ = [
    "ORDER_CREATED",
    "ORDER_CONFIRMED",
    "ORDER_PICKED_UP",
    "ORDER_DELIVERED",
    "ORDER_CANCELLED",
    "publish",
]


def publish(
    db: Session,
    event_type: str,
    aggregate_id: str,
    data: dict[str, Any],
    correlation_id: str | None = None,
) -> None:
    """Queue an event for delivery, in the caller's transaction.

    Deliberately returns nothing. There is no "did it send?" to report - that
    question is answered by the relay, seconds later at worst, and a caller that
    could branch on it would be tempted to treat a send failure as a reason not
    to commit the order.
    """
    common.publish(
        db=db,
        outbox_model=Outbox,
        producer=PRODUCER_ORDER,
        event_type=event_type,
        aggregate_id=aggregate_id,
        data=data,
        correlation_id=correlation_id,
    )
