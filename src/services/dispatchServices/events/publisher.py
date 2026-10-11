"""This service's view of publishing: bind the two things that never vary.

The mechanism is in `src/common/events/publisher.py`. What stays here is the
binding - Dispatch's outbox table and Dispatch's producer name - plus the list
of what this service emits, re-exported so this file answers that question.

Note the aggregate id on every call is the **order** id, not the attempt id.
That is not a slip. It is the Kafka partition key, and the ordering guarantee
that matters is per delivery: `dispatch.search_started` must reach Order before
`partner.assigned` does, and they only will if both land on the same partition.
"""

from typing import Any

from sqlalchemy.orm import Session

from src.common.events import publisher as common
from src.common.events.types import (
    DISPATCH_SEARCH_FAILED,
    DISPATCH_SEARCH_STARTED,
    PARTNER_ASSIGNED,
    PARTNER_OFFERED,
    PARTNER_REJECTED,
    PARTNER_RELEASED,
    PRODUCER_DISPATCH,
)
from src.services.dispatchServices.models.outbox_model import Outbox

__all__ = [
    "DISPATCH_SEARCH_STARTED",
    "DISPATCH_SEARCH_FAILED",
    "PARTNER_OFFERED",
    "PARTNER_REJECTED",
    "PARTNER_ASSIGNED",
    "PARTNER_RELEASED",
    "publish",
]


def publish(
    db: Session,
    event_type: str,
    order_id: int,
    data: dict[str, Any],
    correlation_id: str | None = None,
) -> None:
    """Queue an event for delivery, in the caller's transaction.

    Takes `order_id` as an int rather than the generic `aggregate_id: str`,
    because there is exactly one thing Dispatch ever aggregates by and spelling
    it out stops a call site from passing the attempt id by accident.
    """
    common.publish(
        db=db,
        outbox_model=Outbox,
        producer=PRODUCER_DISPATCH,
        event_type=event_type,
        aggregate_id=str(order_id),
        data=data,
        correlation_id=correlation_id,
    )
