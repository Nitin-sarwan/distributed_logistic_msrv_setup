"""Payment: opened synchronously when the customer confirms.

Synchronous because Order's answer depends on it. An on-delivery order is
confirmed straight away; an online one only when its payment is already paid —
otherwise it waits at PAYMENT_PENDING for a provider. Money that moves later
(the driver collecting at the door) arrives as `payment.captured`, which Order
consumes — see events/consumers.py.
"""

import logging
from decimal import Decimal

import httpx

from src.services.orderServices.config import settings
from src.services.orderServices.utils.exceptions import PaymentServiceUnavailableError

logger = logging.getLogger(__name__)

ONLINE = "online"
ON_DELIVERY = "on_delivery"
PAID = "paid"


class OpenedPayment:
    __slots__ = ("method", "status")

    def __init__(self, method: str, status: str):
        self.method = method
        self.status = status

    @property
    def confirms_order(self) -> bool:
        """Pay-on-delivery is confirmed now and paid later; online needs the money."""
        return self.method == ON_DELIVERY or self.status == PAID


def open_payment(
    order_id: int,
    public_ref: str,
    customer_id: int,
    amount: Decimal,
    currency: str,
    method: str,
) -> OpenedPayment:
    url = f"{settings.payment_service_url.rstrip('/')}/internal/payments"
    body = {
        "order_id": order_id,
        "public_ref": public_ref,
        "customer_id": customer_id,
        "amount": str(amount),
        "currency": currency,
        "method": method,
    }
    try:
        response = httpx.post(
            url,
            json=body,
            headers={"X-Internal-Key": settings.internal_api_key},
            timeout=settings.payment_service_timeout_seconds,
        )
    except httpx.HTTPError as error:
        logger.warning("Payment service unreachable: %s", error)
        raise PaymentServiceUnavailableError() from error

    if response.status_code >= 400:
        logger.warning(
            "Payment service answered %s for order %s: %s",
            response.status_code, order_id, response.text[:300],
        )
        raise PaymentServiceUnavailableError()

    data = response.json()
    return OpenedPayment(method=data["method"], status=data["status"])
