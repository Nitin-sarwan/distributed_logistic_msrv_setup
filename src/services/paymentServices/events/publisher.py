"""This service's view of publishing: its outbox, its producer name, its events.

The aggregate id is the **order** id, as everywhere on the topic: it is the
partition key, and `payment.captured` must stay in order with the
`order.delivered` it settles.
"""

from typing import Any

from sqlalchemy.orm import Session

from src.common.events import publisher as common
from src.common.events.types import PAYMENT_CAPTURED, PAYMENT_REFUNDED, PRODUCER_PAYMENT
from src.services.paymentServices.models.outbox_model import Outbox
from src.services.paymentServices.models.payment_model import Payment

__all__ = ["PAYMENT_CAPTURED", "PAYMENT_REFUNDED", "publish", "payment_payload"]


def payment_payload(payment: Payment) -> dict[str, Any]:
    """What every payment event carries: enough for Order to settle and for
    Notification to say "₹92 received in cash" without asking anybody."""
    return {
        "order_id": payment.order_id,
        "public_ref": payment.public_ref,
        "customer_id": payment.customer_id,
        "partner_id": payment.partner_id,
        "method": payment.method,
        "collected_via": payment.collected_via,
        "amount": payment.amount,
        "currency": payment.currency,
        "paid_at": payment.paid_at,
    }


def publish(
    db: Session,
    event_type: str,
    payment: Payment,
    correlation_id: str | None = None,
) -> None:
    """Queue an event in the caller's transaction."""
    common.publish(
        db=db,
        outbox_model=Outbox,
        producer=PRODUCER_PAYMENT,
        event_type=event_type,
        aggregate_id=str(payment.order_id),
        data=payment_payload(payment),
        correlation_id=correlation_id,
    )
