"""Payment, until the Payment service exists.

The real flow is: Order asks Payment to create an intent, the client completes
it with the provider, and the provider's webhook makes Payment emit
`payment.authorized`, which Order consumes. Two mechanisms, minutes apart.

The stub collapses that into one synchronous answer so the order flow can be
built and demonstrated first. What it does **not** do is change the state
machine: the order still passes through PAYMENT_PENDING on its way to CONFIRMED,
so when the real service arrives the only thing that changes is who says yes.

`PAYMENT_AUTOCONFIRM=false` turns authorisation off, which leaves orders sitting
at PAYMENT_PENDING — the correct behaviour once a real provider is expected, and
a useful way to see the state the customer waits in.
"""

import logging
import uuid
from decimal import Decimal

from src.services.orderServices.config import settings

logger = logging.getLogger(__name__)


class PaymentIntent:
    __slots__ = ("intent_id", "authorized")

    def __init__(self, intent_id: str, authorized: bool):
        self.intent_id = intent_id
        self.authorized = authorized


def create_intent(order_id: int, amount: Decimal, currency: str) -> PaymentIntent:
    """Reserve the money, or pretend to.

    Returns whether it is already authorised, which the real Payment service
    never will — there, authorisation arrives later as an event.
    """
    intent = PaymentIntent(intent_id=f"pi_stub_{uuid.uuid4().hex[:16]}",
                           authorized=settings.payment_autoconfirm)

    if settings.payment_autoconfirm:
        logger.warning(
            "PAYMENT STUB: auto-authorising %s %s for order %s. "
            "Set PAYMENT_AUTOCONFIRM=false anywhere real.",
            amount, currency, order_id,
        )

    return intent
