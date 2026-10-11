"""What Payment does when other services say something.

Two handlers. Payment learns who the driver is from `partner.assigned` — the
driver is who may collect — and voids or refunds on `order.cancelled`. It does
not consume `order.delivered`: delivered is not paid, and the two are separate
facts with separate owners.

Both obey `src/common/events/consumer.py`'s rules: do not commit, and be safe
to run twice.
"""

import logging
from typing import Any

from sqlalchemy.orm import Session

from src.common.events.consumer import EventConsumer
from src.common.events.types import GROUP_PAYMENT, ORDER_CANCELLED, PARTNER_ASSIGNED
from src.services.paymentServices.database.connection import SessionLocal
from src.services.paymentServices.models.processed_event_model import ProcessedEvent
from src.services.paymentServices.services.payment_services import PaymentService

logger = logging.getLogger(__name__)


def _order_id(envelope: dict[str, Any]) -> int:
    return int(envelope["aggregate_id"])


def handle_partner_assigned(db: Session, envelope: dict[str, Any]) -> None:
    PaymentService(db).apply_partner_assigned(
        order_id=_order_id(envelope),
        partner_id=int(envelope["data"]["partner_id"]),
    )


def handle_order_cancelled(db: Session, envelope: dict[str, Any]) -> None:
    PaymentService(db).apply_order_cancelled(
        order_id=_order_id(envelope),
        correlation_id=envelope.get("correlation_id"),
    )


def build_consumer() -> EventConsumer:
    consumer = EventConsumer(
        group_id=GROUP_PAYMENT,
        session_factory=SessionLocal,
        processed_model=ProcessedEvent,
        service_name="payment",
    )
    consumer.on(PARTNER_ASSIGNED, handle_partner_assigned)
    consumer.on(ORDER_CANCELLED, handle_order_cancelled)
    return consumer
