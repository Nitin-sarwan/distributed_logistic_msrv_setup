"""Business rules for orders.

Talks to the database only through the repository, to other services only
through `clients/`, and to the rest of the platform only through the outbox. It
raises domain errors and never HTTPException — the routes own that translation.

The invariant this file exists to protect: **a state change and the event that
announces it are one transaction.** Every method below ends with a single
commit covering the order row, its history row, and its outbox row.
"""

import logging
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.services.orderServices.clients import payment_client, pricing_client
from src.services.orderServices.clients.user_client import ResolvedAddress, fetch_address
from src.services.orderServices.config import settings
from src.services.orderServices.domain.states import OrderStatus, is_cancellable
from src.services.orderServices.events import payloads, publisher
from src.services.orderServices.models.order_model import Order
from src.services.orderServices.repositories.order_repositories import OrderRepository
from src.services.orderServices.utils.exceptions import (
    InvalidTransitionError,
    OrderNotFoundError,
)
from src.services.orderServices.utils.reference import generate_public_ref

logger = logging.getLogger(__name__)


class OrderService:
    def __init__(self, db: Session):
        self.db = db
        self.repository = OrderRepository(db)

    # ── Reads ─────────────────────────────────────────────────────────────

    def list_orders(self, customer_id: int, limit: int = 50, offset: int = 0):
        return self.repository.list_for_customer(customer_id, limit, offset)

    def get_order(self, customer_id: int, order_id: int) -> Order:
        order = self.repository.find_for_customer(order_id, customer_id)
        if order is None:
            raise OrderNotFoundError()
        return order

    def get_history(self, customer_id: int, order_id: int):
        # Through get_order first, so history cannot be read for an order the
        # caller does not own.
        order = self.get_order(customer_id, order_id)
        return self.repository.history(order.id)

    # ── Create ────────────────────────────────────────────────────────────

    def create_order(
        self,
        customer_id: int,
        pickup: ResolvedAddress,
        drop: ResolvedAddress,
        vehicle_type: str,
        weight_kg: Decimal,
        description: str | None,
        idempotency_key: str,
        correlation_id: str | None = None,
    ) -> Order:
        """Snapshot both ends, quote the fare, and record the order.

        Lands at QUOTED rather than CREATED: the customer asked what this costs,
        and an order they have not agreed to pay for yet is exactly what a quote
        is. Confirmation is a separate, deliberate act — see `confirm_order`.
        """
        existing = self.repository.find_by_idempotency_key(customer_id, idempotency_key)
        if existing is not None:
            # A retry of a request that already succeeded. Returning the first
            # order is the whole point of the key.
            logger.info("idempotent replay for customer %s", customer_id)
            return existing

        quote = pricing_client.get_quote(
            pickup.latitude,
            pickup.longitude,
            drop.latitude,
            drop.longitude,
            vehicle_type,
            weight_kg,
        )

        order = Order(
            public_ref=generate_public_ref(),
            customer_id=customer_id,
            status=OrderStatus.QUOTED.value,
            pickup_line1=pickup.line1,
            pickup_line2=pickup.line2,
            pickup_city=pickup.city,
            pickup_pin_code=pickup.pin_code,
            pickup_latitude=Decimal(str(pickup.latitude)),
            pickup_longitude=Decimal(str(pickup.longitude)),
            pickup_contact_name=pickup.contact_name,
            pickup_contact_phone=pickup.contact_phone,
            drop_line1=drop.line1,
            drop_line2=drop.line2,
            drop_city=drop.city,
            drop_pin_code=drop.pin_code,
            drop_latitude=Decimal(str(drop.latitude)),
            drop_longitude=Decimal(str(drop.longitude)),
            drop_contact_name=drop.contact_name,
            drop_contact_phone=drop.contact_phone,
            package_weight_kg=weight_kg,
            package_description=description,
            vehicle_type=vehicle_type,
            distance_km=quote.distance_km,
            quoted_amount=quote.amount,
            currency=quote.currency,
            quote_expires_at=quote.expires_at,
            idempotency_key=idempotency_key,
        )

        self.repository.add(order)
        # Flush, not commit: the order needs its id for the history and event
        # rows, and all three still have to land or fail together.
        self.db.flush()

        self.repository.record_transition(
            order.id, None, OrderStatus.CREATED, actor="customer"
        )
        self.repository.record_transition(
            order.id, OrderStatus.CREATED.value, OrderStatus.QUOTED, actor="system"
        )
        publisher.publish(
            self.db,
            publisher.ORDER_CREATED,
            str(order.id),
            payloads.order_created(order),
            correlation_id,
        )

        try:
            self.db.commit()
        except IntegrityError:
            # Two identical requests raced past the idempotency read. The unique
            # constraint is what actually enforces it; this turns the loser into
            # the same answer the winner got.
            self.db.rollback()
            duplicate = self.repository.find_by_idempotency_key(
                customer_id, idempotency_key
            )
            if duplicate is None:
                raise
            return duplicate

        self.db.refresh(order)
        return order

    # ── Confirm ───────────────────────────────────────────────────────────

    def confirm_order(
        self, customer_id: int, order_id: int, correlation_id: str | None = None
    ) -> Order:
        """The customer accepts the fare and pays.

        Passes through PAYMENT_PENDING even while payment is stubbed, so the
        state machine does not change shape when the real service lands — only
        who answers changes.
        """
        order = self.get_order(customer_id, order_id)

        if order.status != OrderStatus.QUOTED.value:
            raise InvalidTransitionError(
                "This order has already been confirmed or cancelled"
            )

        intent = payment_client.create_intent(
            order.id, order.quoted_amount, order.currency
        )
        order.payment_intent_id = intent.intent_id
        order.status = OrderStatus.PAYMENT_PENDING.value
        self.repository.record_transition(
            order.id,
            OrderStatus.QUOTED.value,
            OrderStatus.PAYMENT_PENDING,
            actor="customer",
        )

        if intent.authorized:
            self._mark_confirmed(order, correlation_id)

        self.db.commit()
        self.db.refresh(order)
        return order

    def _mark_confirmed(self, order: Order, correlation_id: str | None) -> None:
        """What consuming `payment.authorized` will do, once that event exists.

        Kept as its own method so the Kafka handler added in the next phase
        calls exactly this, and confirmation cannot drift into two versions.
        """
        from datetime import datetime, timezone

        order.status = OrderStatus.CONFIRMED.value
        order.confirmed_at = datetime.now(timezone.utc)
        self.repository.record_transition(
            order.id,
            OrderStatus.PAYMENT_PENDING.value,
            OrderStatus.CONFIRMED,
            actor="payment",
        )
        # The event Dispatch is waiting for. Order does not know Dispatch exists.
        publisher.publish(
            self.db,
            publisher.ORDER_CONFIRMED,
            str(order.id),
            payloads.order_confirmed(order),
            correlation_id,
        )

    # ── Cancel ────────────────────────────────────────────────────────────

    def cancel_order(
        self,
        customer_id: int,
        order_id: int,
        reason: str | None = None,
        correlation_id: str | None = None,
    ) -> Order:
        """Call it off, up to pickup.

        Order does not refund anything and does not wait for Payment to. It
        records the cancellation, announces it with the state the order was in,
        and stops — `previous_status` is what lets Payment decide between doing
        nothing, voiding an authorisation, and refunding a capture.
        """
        from datetime import datetime, timezone

        order = self.get_order(customer_id, order_id)
        previous = order.status

        if not is_cancellable(OrderStatus(previous)):
            raise InvalidTransitionError(
                "This delivery has already been picked up and cannot be cancelled"
            )

        order.status = OrderStatus.CANCELLED.value
        order.cancelled_at = datetime.now(timezone.utc)
        order.cancellation_reason = reason

        self.repository.record_transition(
            order.id, previous, OrderStatus.CANCELLED, actor="customer"
        )
        publisher.publish(
            self.db,
            publisher.ORDER_CANCELLED,
            str(order.id),
            payloads.order_cancelled(order, previous),
            correlation_id,
        )

        self.db.commit()
        self.db.refresh(order)
        return order


class DeliveryService:
    """The partner's view of an order.

    A separate class from OrderService because the two answer to different
    people: everything here is authorised against `orders.partner_id` — the
    snapshot written at assignment — rather than against a customer session, and
    none of it may be reachable from a customer's routes.

    Nothing here calls partnerServices. The driver's name, phone, and vehicle
    were copied onto the order when they were assigned, and that copy is what a
    customer sees for the rest of the order's life.
    """

    ACTIVE_STATUSES = [
        OrderStatus.PARTNER_ASSIGNED.value,
        OrderStatus.PICKED_UP.value,
        OrderStatus.IN_TRANSIT.value,
    ]
    COMPLETED_STATUSES = [
        OrderStatus.DELIVERED.value,
        OrderStatus.SETTLED.value,
    ]

    def __init__(self, db: Session):
        self.db = db
        self.repository = OrderRepository(db)

    def list_for_partner(self, partner_id: int, completed: bool = False):
        statuses = self.COMPLETED_STATUSES if completed else self.ACTIVE_STATUSES
        return self.repository.list_for_partner(partner_id, statuses)

    def _assigned_order(self, order_id: int, partner_id: int) -> Order:
        """This partner's delivery, or 404.

        `find_for_partner` scopes by both ids, so a driver acting on someone
        else's delivery gets the same answer as one acting on an order that does
        not exist — which is the only thing they should be able to learn.
        """
        order = self.repository.find_for_partner(order_id, partner_id)
        if order is None:
            raise OrderNotFoundError()
        return order

    def mark_picked_up(
        self, partner_id: int, order_id: int, correlation_id: str | None = None
    ) -> Order:
        from datetime import datetime, timezone

        order = self._assigned_order(order_id, partner_id)

        if order.status != OrderStatus.PARTNER_ASSIGNED.value:
            raise InvalidTransitionError(
                "This delivery is not waiting to be picked up"
            )

        order.status = OrderStatus.PICKED_UP.value
        order.picked_up_at = datetime.now(timezone.utc)
        self.repository.record_transition(
            order.id,
            OrderStatus.PARTNER_ASSIGNED.value,
            OrderStatus.PICKED_UP,
            actor="partner",
        )
        publisher.publish(
            self.db,
            publisher.ORDER_PICKED_UP,
            str(order.id),
            payloads.order_picked_up(order),
            correlation_id,
        )

        self.db.commit()
        self.db.refresh(order)
        return order

    def mark_delivered(
        self, partner_id: int, order_id: int, correlation_id: str | None = None
    ) -> Order:
        """Goods handed over.

        Lands at DELIVERED, not SETTLED. Delivered means the goods arrived;
        settled means the money moved, and that is Payment's to say — it
        consumes the event this emits and answers with `payment.captured`.
        """
        from datetime import datetime, timezone

        order = self._assigned_order(order_id, partner_id)

        if order.status not in (
            OrderStatus.PICKED_UP.value,
            OrderStatus.IN_TRANSIT.value,
        ):
            raise InvalidTransitionError("This delivery has not been picked up yet")

        previous = order.status
        order.status = OrderStatus.DELIVERED.value
        order.delivered_at = datetime.now(timezone.utc)
        self.repository.record_transition(
            order.id, previous, OrderStatus.DELIVERED, actor="partner"
        )
        publisher.publish(
            self.db,
            publisher.ORDER_DELIVERED,
            str(order.id),
            payloads.order_delivered(order),
            correlation_id,
        )

        self.db.commit()
        self.db.refresh(order)
        return order

    def assign_partner(
        self,
        order_id: int,
        partner_id: int,
        partner_name: str,
        partner_phone: str,
        vehicle_number: str | None,
        event_id: str | None = None,
    ) -> Order:
        """Hand an order to a driver.

        **This is what consuming `partner.assigned` will do.** Dispatch decides
        who; Order only records the decision and snapshots what a customer needs
        to recognise them at the kerb.

        Exposed as an internal endpoint until Kafka exists, so the flow can be
        driven end to end — when the consumer lands it calls this same method,
        and nothing about the assignment changes shape.
        """
        order = self.repository.find_by_id(order_id)
        if order is None:
            raise OrderNotFoundError()

        if order.status not in (
            OrderStatus.CONFIRMED.value,
            OrderStatus.SEARCHING_PARTNER.value,
        ):
            raise InvalidTransitionError(
                "This order is not waiting for a partner"
            )

        previous = order.status
        order.partner_id = partner_id
        order.partner_name = partner_name
        order.partner_phone = partner_phone
        order.vehicle_number = vehicle_number
        order.status = OrderStatus.PARTNER_ASSIGNED.value

        self.repository.record_transition(
            order.id,
            previous,
            OrderStatus.PARTNER_ASSIGNED,
            actor="dispatch",
            caused_by_event_id=event_id,
        )

        self.db.commit()
        self.db.refresh(order)
        return order


def payment_is_stubbed() -> bool:
    """For the health endpoint, so a deployment cannot hide free deliveries."""
    return settings.payment_autoconfirm
