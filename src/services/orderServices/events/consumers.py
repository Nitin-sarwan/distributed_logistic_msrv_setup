"""What Order does when Dispatch says something.

Three handlers, and between them they are the whole reason Order and Dispatch
can be separate services. Order never calls Dispatch, never asks whether a
search is running, and does not know how a partner is chosen. It reacts to
facts.

Every handler here obeys the same two rules, and both are enforced by
`src/common/events/consumer.py` rather than by remembering:

* **Do not commit.** The consumer commits the handler's work and the
  `processed_events` row together, which is the only way a redelivery can be a
  no-op instead of a second assignment.
* **Be safe to run twice anyway.** The marker closes the window, but each
  `apply_*` also checks the order's current state, because a duplicate is not
  the only way a stale event arrives — a retry after a long outage can deliver
  `search_started` for an order that was assigned ten minutes ago.
"""

import logging
from typing import Any

from sqlalchemy.orm import Session

from src.common.events.consumer import EventConsumer
from src.common.events.types import (
    DISPATCH_SEARCH_FAILED,
    DISPATCH_SEARCH_STARTED,
    GROUP_ORDER,
    PARTNER_ASSIGNED,
    PAYMENT_CAPTURED,
    PAYMENT_REFUNDED,
)
from src.services.orderServices.database.connection import SessionLocal
from src.services.orderServices.models.processed_event_model import ProcessedEvent
from src.services.orderServices.services.order_services import DeliveryService
from src.services.orderServices.utils.exceptions import (
    InvalidTransitionError,
    OrderNotFoundError,
)

logger = logging.getLogger(__name__)


def _order_id(envelope: dict[str, Any]) -> int:
    """The aggregate id, as an int.

    Read from the envelope rather than from `data`, because the aggregate id is
    also the partition key and is therefore the field the ordering guarantee
    rests on. If those two ever disagree, the envelope is the one that decided
    where the message went.
    """
    return int(envelope["aggregate_id"])


def handle_partner_assigned(db: Session, envelope: dict[str, Any]) -> None:
    """Dispatch found a driver. Snapshot them onto the order.

    Everything needed is in the payload: Dispatch has just read it from
    partnerServices, and copying it here means "your driver is on the way" never
    needs partnerServices to be up.
    """
    data = envelope["data"]

    DeliveryService(db).apply_assignment(
        order_id=_order_id(envelope),
        partner_id=int(data["partner_id"]),
        partner_name=data["partner_name"],
        partner_phone=data["partner_phone"],
        vehicle_number=data.get("vehicle_number"),
        event_id=envelope["event_id"],
    )


def handle_search_started(db: Session, envelope: dict[str, Any]) -> None:
    """Dispatch is looking. CONFIRMED -> SEARCHING_PARTNER."""
    DeliveryService(db).apply_search_started(
        order_id=_order_id(envelope),
        event_id=envelope["event_id"],
    )


def handle_search_failed(db: Session, envelope: dict[str, Any]) -> None:
    """Dispatch gave up. Order decides what the customer sees.

    See `DeliveryService.apply_search_failed` for the policy and how to change
    it. The reason string is put in front of a person, so it comes from the
    event rather than being invented here.
    """
    data = envelope["data"]
    reason = data.get("reason") or "No delivery partner was available"

    DeliveryService(db).apply_search_failed(
        order_id=_order_id(envelope),
        reason=reason,
        event_id=envelope["event_id"],
        correlation_id=envelope.get("correlation_id"),
    )


def handle_payment_captured(db: Session, envelope: dict[str, Any]) -> None:
    """The money arrived — online, or collected by the driver. Settle if delivered."""
    DeliveryService(db).apply_payment_captured(
        order_id=_order_id(envelope),
        event_id=envelope["event_id"],
    )


def handle_payment_refunded(db: Session, envelope: dict[str, Any]) -> None:
    """The money went back after a cancellation. Shown on the order."""
    DeliveryService(db).apply_payment_refunded(order_id=_order_id(envelope))


def _guarded(handler):
    """Turn the two domain errors that are not failures into no-ops.

    Without this, an event for an order that was cancelled while the event was
    in flight is retried three times and parked in the DLQ, where it looks like
    a bug. Neither error is retryable and neither is wrong: an order that has
    moved on has moved on.

    Everything else propagates, which is what makes a real fault — a dead
    database, a schema mismatch — visible instead of swallowed.
    """

    def wrapper(db: Session, envelope: dict[str, Any]) -> None:
        try:
            handler(db, envelope)
        except OrderNotFoundError:
            logger.warning(
                "no order %s for %s (event %s) — ignoring",
                envelope.get("aggregate_id"),
                envelope.get("event_type"),
                envelope.get("event_id"),
            )
        except InvalidTransitionError as error:
            logger.warning(
                "%s not applicable to order %s: %s",
                envelope.get("event_type"),
                envelope.get("aggregate_id"),
                error.message,
            )

    wrapper.__name__ = getattr(handler, "__name__", "handler")
    return wrapper


def build_consumer() -> EventConsumer:
    """Order's consumer, wired. Started by the lifespan in main.py.

    Note what is absent: handlers for `order.*`. Order produces those, and every
    service reads the whole topic, so its own events arrive here and are
    ignored — which is correct, and is why the consumer treats an unregistered
    type as normal rather than as an error.
    """
    consumer = EventConsumer(
        group_id=GROUP_ORDER,
        session_factory=SessionLocal,
        processed_model=ProcessedEvent,
        service_name="order",
    )
    consumer.on(PARTNER_ASSIGNED, _guarded(handle_partner_assigned))
    consumer.on(DISPATCH_SEARCH_STARTED, _guarded(handle_search_started))
    consumer.on(DISPATCH_SEARCH_FAILED, _guarded(handle_search_failed))
    consumer.on(PAYMENT_CAPTURED, _guarded(handle_payment_captured))
    consumer.on(PAYMENT_REFUNDED, _guarded(handle_payment_refunded))
    return consumer
