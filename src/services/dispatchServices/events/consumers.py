"""What Dispatch does when Order says something.

Three handlers, and none of them run the offer loop. `order.confirmed` writes an
attempt row and returns; the loop in `services/dispatch_worker.py` picks it up on
the next tick. That split is deliberate - a handler that ran a three-minute
search would hold its Kafka partition open the whole time and every order behind
it on that partition would wait.

Order never calls Dispatch and Dispatch never calls Order. The only HTTP either
of them makes is to partnerServices, and that is because a claim has to be
synchronous to be a claim.

Every handler obeys the two rules enforced by `src/common/events/consumer.py`:
do not commit, and be safe to run twice anyway.
"""

import logging
from typing import Any

from sqlalchemy.orm import Session

from src.common.events.consumer import EventConsumer
from src.common.events.types import (
    GROUP_DISPATCH,
    ORDER_CANCELLED,
    ORDER_CONFIRMED,
    ORDER_DELIVERED,
)
from src.services.dispatchServices.database.connection import SessionLocal
from src.services.dispatchServices.models.processed_event_model import ProcessedEvent
from src.services.dispatchServices.services.dispatch_services import DispatchService

logger = logging.getLogger(__name__)


def _order_id(envelope: dict[str, Any]) -> int:
    """The aggregate id, as an int.

    Read from the envelope rather than from `data`, because the aggregate id is
    also the partition key and is therefore what the ordering guarantee rests
    on. If the two ever disagree, the envelope is the one that decided where the
    message went.
    """
    return int(envelope["aggregate_id"])


def handle_order_confirmed(db: Session, envelope: dict[str, Any]) -> None:
    """The event the service exists for. Start a search.

    Idempotent twice over: `processed_events` stops the redelivery reaching
    here, and `dispatch_attempts.order_id` is unique, so even a marker lost to a
    schema reset cannot start two searches for one delivery.
    """
    DispatchService(db).apply_order_confirmed(
        order_id=_order_id(envelope),
        data=envelope["data"],
        correlation_id=envelope.get("correlation_id"),
    )


def handle_order_cancelled(db: Session, envelope: dict[str, Any]) -> None:
    """Stop offering, and hand back the driver if one was already claimed.

    This is Dispatch's leg of the cancellation saga. Payment reverses money,
    Notification tells people, and Dispatch releases the partner - three
    consumers of one event, none of which knows the others exist.
    """
    DispatchService(db).apply_order_cancelled(
        order_id=_order_id(envelope),
        correlation_id=envelope.get("correlation_id"),
    )


def handle_order_delivered(db: Session, envelope: dict[str, Any]) -> None:
    """The delivery finished. ON_TRIP -> ONLINE.

    Dispatch does this rather than partnerServices because Dispatch is what
    claimed them: the service that took the driver out of the pool is the one
    that owes putting them back.
    """
    DispatchService(db).apply_order_delivered(
        order_id=_order_id(envelope),
        correlation_id=envelope.get("correlation_id"),
    )


def build_consumer() -> EventConsumer:
    """Dispatch's consumer, wired. Started by the lifespan in main.py.

    Note what is absent: handlers for `partner.*` and `dispatch.*`. Dispatch
    produces those, and every service reads the whole topic, so its own events
    arrive here and are ignored - which is correct, and is why an unregistered
    type is normal rather than an error.
    """
    consumer = EventConsumer(
        group_id=GROUP_DISPATCH,
        session_factory=SessionLocal,
        processed_model=ProcessedEvent,
        service_name="dispatch",
    )
    consumer.on(ORDER_CONFIRMED, handle_order_confirmed)
    consumer.on(ORDER_CANCELLED, handle_order_cancelled)
    consumer.on(ORDER_DELIVERED, handle_order_delivered)
    return consumer
