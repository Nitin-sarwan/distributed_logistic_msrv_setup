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

from src.services.orderServices.clients import (
    partner_client,
    payment_client,
    pricing_client,
)
from src.services.orderServices.clients.user_client import ResolvedAddress, fetch_address
from src.services.orderServices.config import settings
from src.services.orderServices.domain.geo import haversine_km
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


# Between assignment and hand-over: the only window in which a customer may see
# where their driver is.
TRACKABLE_STATUSES = (
    OrderStatus.PARTNER_ASSIGNED,
    OrderStatus.PICKED_UP,
    OrderStatus.IN_TRANSIT,
)


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

    def tracking(self, customer_id: int, order_id: int) -> dict:
        """Where this order's driver is, if the customer may know.

        The privacy rule lives here, not in partnerServices: a customer sees a
        driver's position only while that driver is on *their* order — from
        assignment until the goods are handed over. Before that there is no
        driver to show; after it the driver is somebody else's business. The
        order id is the key, so there is no route that takes a partner id and
        nothing that can be walked.
        """
        from datetime import datetime, timezone

        order = self.get_order(customer_id, order_id)
        status = OrderStatus(order.status)

        if status in (OrderStatus.DELIVERED, OrderStatus.SETTLED, OrderStatus.CANCELLED):
            return {"available": False, "reason": "finished"}
        if status not in TRACKABLE_STATUSES or order.partner_id is None:
            return {"available": False, "reason": "not_assigned"}

        position = partner_client.fetch_position(order.partner_id)
        if position is None:
            return {"available": False, "reason": "no_position"}

        # Before pickup the driver is heading to the sender; after, to the drop.
        if status is OrderStatus.PARTNER_ASSIGNED:
            heading_to, target = "pickup", (order.pickup_latitude, order.pickup_longitude)
        else:
            heading_to, target = "drop", (order.drop_latitude, order.drop_longitude)

        age = (
            (datetime.now(timezone.utc) - position.updated_at).total_seconds()
            if position.updated_at
            else None
        )
        return {
            "available": True,
            "latitude": position.latitude,
            "longitude": position.longitude,
            "updated_at": position.updated_at,
            "is_stale": age is None or age > settings.tracking_stale_seconds,
            "heading_to": heading_to,
            "distance_km": round(
                haversine_km(
                    position.latitude, position.longitude, float(target[0]), float(target[1])
                ),
                2,
            ),
        }

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
        payment_method: str = "online",
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
            customer_id=customer_id,
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
            payment_method=payment_method,
            distance_km=quote.distance_km,
            quote_id=quote.quote_id,
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
        """The customer accepts the fare and chooses how to pay.

        Opens the order's payment with Payment, synchronously. Pay-on-delivery
        is confirmed straight away — a driver is sought, and the money is
        collected at the door. Online is confirmed only if the payment is
        already paid; otherwise the order waits at PAYMENT_PENDING.
        """
        order = self.get_order(customer_id, order_id)

        if order.status != OrderStatus.QUOTED.value:
            raise InvalidTransitionError(
                "This order has already been confirmed or cancelled"
            )

        payment = payment_client.open_payment(
            order_id=order.id,
            public_ref=order.public_ref,
            customer_id=order.customer_id,
            amount=order.quoted_amount,
            currency=order.currency,
            method=order.payment_method,
        )
        if payment.status == payment_client.PAID:
            order.payment_status = "paid"
        order.status = OrderStatus.PAYMENT_PENDING.value
        self.repository.record_transition(
            order.id,
            OrderStatus.QUOTED.value,
            OrderStatus.PAYMENT_PENDING,
            actor="customer",
        )

        if payment.confirms_order:
            self._mark_confirmed(order, correlation_id)

        self.db.commit()
        self.db.refresh(order)
        return order

    def _mark_confirmed(self, order: Order, correlation_id: str | None) -> None:
        """PAYMENT_PENDING -> CONFIRMED, and tell Dispatch.

        Kept as its own method so a future `payment.authorized` handler (a real
        provider confirming an online payment later) calls exactly this.
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
        # Already paid (online, or collected before the driver tapped
        # Delivered): goods and money are both done, so it is settled now.
        if order.payment_status == "paid":
            self._settle(order, event_id=None)

        self.db.commit()
        self.db.refresh(order)
        return order

    # ── What Payment causes ───────────────────────────────────────────────

    def apply_payment_captured(self, order_id: int, event_id: str | None = None) -> Order:
        """The money arrived. Stages; does not commit.

        Settles the order if the goods have arrived too. If not yet — the
        driver collected before tapping Delivered — `mark_delivered` settles it
        when they do. Idempotent: a redelivery finds it paid and changes nothing.
        """
        order = self.repository.find_by_id(order_id)
        if order is None:
            raise OrderNotFoundError()

        order.payment_status = "paid"
        if order.status == OrderStatus.DELIVERED.value:
            self._settle(order, event_id)
        return order

    def apply_payment_refunded(self, order_id: int) -> Order:
        """The money went back after a cancellation. Stages; does not commit."""
        order = self.repository.find_by_id(order_id)
        if order is None:
            raise OrderNotFoundError()
        order.payment_status = "refunded"
        return order

    def _settle(self, order: Order, event_id: str | None) -> None:
        """DELIVERED -> SETTLED: the goods arrived and the money moved."""
        order.status = OrderStatus.SETTLED.value
        self.repository.record_transition(
            order.id,
            OrderStatus.DELIVERED.value,
            OrderStatus.SETTLED,
            actor="payment",
            caused_by_event_id=event_id,
        )

    # ── What Dispatch causes ──────────────────────────────────────────────
    #
    # Each of the three below comes in two forms: an `apply_*` that stages the
    # change and does NOT commit, and a wrapper that commits.
    #
    # The split is not stylistic. A Kafka handler must write its work and its
    # `processed_events` marker in **one** transaction — commit them separately
    # and a crash in between leaves the work done and the marker missing, so the
    # redelivery does it twice. So the consumer calls `apply_*` and commits
    # once, itself. The HTTP routes, which have no marker to write, call the
    # committing wrapper.

    def apply_assignment(
        self,
        order_id: int,
        partner_id: int,
        partner_name: str,
        partner_phone: str,
        vehicle_number: str | None,
        event_id: str | None = None,
    ) -> Order:
        """Hand an order to a driver. Stages; does not commit.

        **This is what consuming `partner.assigned` does.** Dispatch decides
        who; Order only records the decision and snapshots what a customer needs
        to recognise them at the kerb.
        """
        order = self.repository.find_by_id(order_id)
        if order is None:
            raise OrderNotFoundError()

        # Idempotent in the one way that matters: a redelivery of the same
        # assignment, or a second copy of it through the internal endpoint,
        # finds the order already assigned to this partner and is a no-op rather
        # than a 409. A *different* partner is a genuine conflict and still is.
        if (
            order.status == OrderStatus.PARTNER_ASSIGNED.value
            and order.partner_id == partner_id
        ):
            return order

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
        """`apply_assignment`, committed.

        Kept for `POST /internal/orders/{id}/assignment`, which is now a manual
        override rather than the normal path: assignments arrive as
        `partner.assigned` on the topic. It stays because an operator sometimes
        has to place an order by hand, and because it is how the flow is tested
        without a broker running.
        """
        order = self.apply_assignment(
            order_id=order_id,
            partner_id=partner_id,
            partner_name=partner_name,
            partner_phone=partner_phone,
            vehicle_number=vehicle_number,
            event_id=event_id,
        )
        self.db.commit()
        self.db.refresh(order)
        return order

    def apply_search_started(self, order_id: int, event_id: str | None = None) -> Order:
        """CONFIRMED -> SEARCHING_PARTNER. Stages; does not commit.

        What consuming `dispatch.search_started` does. The state exists so the
        customer's app can say "finding you a driver" rather than leaving
        `confirmed` on screen for the length of an offer loop, and so a support
        question about a stuck order can be answered from the history table.

        A redelivery, or an order that has already been assigned while this
        event sat in a retry, is a no-op — never a step backwards.
        """
        order = self.repository.find_by_id(order_id)
        if order is None:
            raise OrderNotFoundError()

        if order.status != OrderStatus.CONFIRMED.value:
            logger.info(
                "order %s is %s, not confirmed — ignoring search_started",
                order_id,
                order.status,
            )
            return order

        order.status = OrderStatus.SEARCHING_PARTNER.value
        self.repository.record_transition(
            order.id,
            OrderStatus.CONFIRMED.value,
            OrderStatus.SEARCHING_PARTNER,
            actor="dispatch",
            caused_by_event_id=event_id,
        )
        return order

    def apply_search_failed(
        self,
        order_id: int,
        reason: str,
        event_id: str | None = None,
        correlation_id: str | None = None,
    ) -> Order:
        """Dispatch found nobody. Stages; does not commit.

        **Order decides what happens, not Dispatch** — which is the whole reason
        this is an event rather than Dispatch calling a cancel endpoint. Dispatch
        reports a fact about its search; the policy of what a customer with no
        available driver should experience belongs to the service that owns the
        order.

        The policy here is to cancel, because the alternative is an order that
        sits in SEARCHING_PARTNER forever with a payment authorised against it.
        Cancelling emits `order.cancelled` carrying `previous_status`, which is
        what lets Payment void the authorisation and Notification tell the
        customer to try again. Set CANCEL_ON_DISPATCH_FAILURE=false to
        leave the order searching instead and handle it by hand.
        """
        from datetime import datetime, timezone

        order = self.repository.find_by_id(order_id)
        if order is None:
            raise OrderNotFoundError()

        if not settings.cancel_on_dispatch_failure:
            logger.warning(
                "dispatch found no partner for order %s (%s); leaving it "
                "searching because CANCEL_ON_DISPATCH_FAILURE is false",
                order_id,
                reason,
            )
            return order

        if not is_cancellable(OrderStatus(order.status)):
            # It was assigned, picked up or already cancelled while this event
            # waited. Nothing to do — and nothing to complain about.
            logger.info(
                "order %s is %s — ignoring search_failed", order_id, order.status
            )
            return order

        previous = order.status
        order.status = OrderStatus.CANCELLED.value
        order.cancelled_at = datetime.now(timezone.utc)
        order.cancellation_reason = reason

        self.repository.record_transition(
            order.id,
            previous,
            OrderStatus.CANCELLED,
            actor="dispatch",
            caused_by_event_id=event_id,
        )
        publisher.publish(
            self.db,
            publisher.ORDER_CANCELLED,
            str(order.id),
            payloads.order_cancelled(order, previous),
            correlation_id,
        )
        return order
