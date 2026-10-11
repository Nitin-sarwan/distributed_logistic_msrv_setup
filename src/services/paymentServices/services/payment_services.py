"""Business rules for money.

Payment decides one thing: whether an order's money has arrived. It does not
know what was delivered, and Order does not know how the money moved — they
meet only in events: Payment announces `payment.captured`, Order settles.

Every state change and the event announcing it are one commit, through the
outbox. The `apply_*` methods are called from Kafka handlers and stage without
committing — the consumer commits them together with its dedup marker.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.services.paymentServices.config import settings
from src.services.paymentServices.domain.states import (
    DRIVER_COLLECTABLE,
    CollectedVia,
    PaymentMethod,
    PaymentStatus,
)
from src.services.paymentServices.domain.upi import upi_link
from src.services.paymentServices.events import publisher
from src.services.paymentServices.models.payment_model import Payment
from src.services.paymentServices.repositories.payment_repositories import (
    PaymentRepository,
)
from src.services.paymentServices.utils.exceptions import (
    NotCollectableError,
    PaymentNotFoundError,
)

logger = logging.getLogger(__name__)


class PaymentService:
    def __init__(self, db: Session):
        self.db = db
        self.repository = PaymentRepository(db)

    # ── Order confirms: one payment per order ─────────────────────────────

    def open_for_order(
        self,
        order_id: int,
        public_ref: str,
        customer_id: int,
        amount: Decimal,
        currency: str,
        method: PaymentMethod,
        correlation_id: str | None = None,
    ) -> Payment:
        """Start this order's payment. Called synchronously by Order on confirm.

        Synchronous because Order's answer to the customer depends on it: an
        on-delivery order is confirmed straight away, an online one only once
        the money is in. Idempotent on `order_id`, so a retried confirm finds
        the payment it already opened.
        """
        existing = self.repository.find_for_order(order_id)
        if existing is not None:
            return existing

        payment = Payment(
            order_id=order_id,
            public_ref=public_ref,
            customer_id=customer_id,
            method=method.value,
            status=PaymentStatus.AWAITING.value,
            amount=amount,
            currency=currency,
        )
        self.repository.add(payment)

        if method is PaymentMethod.ONLINE and settings.payment_online_autocapture:
            # No provider yet: "paid online" is simulated. Logged loudly so it
            # cannot quietly survive into a real deployment.
            logger.warning(
                "PAYMENT_ONLINE_AUTOCAPTURE: treating order %s as paid online "
                "(%s %s) with no provider. Development only.",
                order_id, amount, currency,
            )
            self._mark_paid(payment, CollectedVia.ONLINE, correlation_id)

        try:
            self.db.commit()
        except IntegrityError:
            # Two confirms raced past the read above; the unique order_id let
            # one through. Return that one.
            self.db.rollback()
            winner = self.repository.find_for_order(order_id)
            if winner is None:
                raise
            return winner

        self.db.refresh(payment)
        return payment

    # ── The driver at the door ────────────────────────────────────────────

    def for_partner(self, partner_id: int, order_id: int) -> Payment:
        payment = self.repository.find_for_partner(order_id, partner_id)
        if payment is None:
            raise PaymentNotFoundError()
        return payment

    def upi_request(self, partner_id: int, order_id: int) -> tuple[Payment, str]:
        """The UPI link for the QR. Only for an unpaid, pay-on-delivery order."""
        payment = self._collectable(partner_id, order_id)
        link = upi_link(
            settings.payment_upi_vpa,
            settings.payment_upi_payee_name,
            payment.amount,
            payment.public_ref,
        )
        return payment, link

    def collect(
        self,
        partner_id: int,
        order_id: int,
        via: CollectedVia,
        correlation_id: str | None = None,
    ) -> Payment:
        """The driver says the customer has paid them, in cash or over UPI.

        Taken on the driver's word — that is the choice made until a provider
        can confirm UPI payments itself. What makes it accountable is that it
        is recorded: who collected, how, and when, on a row nobody edits.

        A repeat of the same collection (a double tap, a retry after a timeout)
        returns the payment unchanged rather than failing.
        """
        if via not in DRIVER_COLLECTABLE:
            raise NotCollectableError("A driver can only record cash or UPI")

        payment = self.for_partner(partner_id, order_id)
        if payment.status == PaymentStatus.PAID.value and payment.collected_via == via.value:
            return payment

        payment = self._collectable(partner_id, order_id)
        self._mark_paid(payment, via, correlation_id)
        self.db.commit()
        self.db.refresh(payment)

        logger.info(
            "partner %s collected %s %s by %s for order %s",
            partner_id, payment.amount, payment.currency, via.value, order_id,
        )
        return payment

    # ── What other services cause ─────────────────────────────────────────

    def apply_partner_assigned(self, order_id: int, partner_id: int) -> None:
        """Remember which driver may collect. Stages; does not commit."""
        payment = self.repository.find_for_order(order_id)
        if payment is None:
            logger.warning("partner.assigned for order %s with no payment", order_id)
            return
        payment.partner_id = partner_id

    def apply_order_cancelled(self, order_id: int, correlation_id: str | None) -> None:
        """Payment's leg of the cancellation saga. Stages; does not commit.

        Unpaid: there is nothing to give back, so the payment is voided. Paid
        online: refunded — simulated, like the online payment it reverses, until
        a provider exists. Collected by a driver cannot happen here, because an
        order cannot be cancelled after pickup.
        """
        payment = self.repository.find_for_order(order_id)
        if payment is None:
            return  # cancelled before it was ever confirmed: never had one

        now = datetime.now(timezone.utc)
        if payment.status == PaymentStatus.AWAITING.value:
            payment.status = PaymentStatus.VOIDED.value
            payment.closed_at = now
        elif (
            payment.status == PaymentStatus.PAID.value
            and payment.collected_via == CollectedVia.ONLINE.value
        ):
            payment.status = PaymentStatus.REFUNDED.value
            payment.closed_at = now
            publisher.publish(self.db, publisher.PAYMENT_REFUNDED, payment, correlation_id)

    # ── Internals ─────────────────────────────────────────────────────────

    def _collectable(self, partner_id: int, order_id: int) -> Payment:
        payment = self.for_partner(partner_id, order_id)
        if payment.method != PaymentMethod.ON_DELIVERY.value:
            raise NotCollectableError("This delivery was paid online — nothing to collect")
        if payment.status == PaymentStatus.PAID.value:
            raise NotCollectableError("This delivery has already been paid")
        if payment.status != PaymentStatus.AWAITING.value:
            raise NotCollectableError("This order was cancelled — nothing to collect")
        return payment

    def _mark_paid(
        self, payment: Payment, via: CollectedVia, correlation_id: str | None
    ) -> None:
        payment.status = PaymentStatus.PAID.value
        payment.collected_via = via.value
        payment.paid_at = datetime.now(timezone.utc)
        # The id is needed for the event's payload; flush assigns it.
        self.db.flush()
        publisher.publish(self.db, publisher.PAYMENT_CAPTURED, payment, correlation_id)
