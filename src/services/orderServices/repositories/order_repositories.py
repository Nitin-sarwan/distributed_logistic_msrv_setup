from sqlalchemy import select
from sqlalchemy.orm import Session

from src.services.orderServices.domain.states import OrderStatus
from src.services.orderServices.models.order_model import Order
from src.services.orderServices.models.status_history_model import OrderStatusHistory


class OrderRepository:
    """Data access for orders. Holds no business rules.

    Every customer-facing read is scoped by `customer_id` in the WHERE clause
    rather than fetched and checked afterwards. That ordering is the point: a
    query that cannot return someone else's order makes the ownership check
    impossible to forget, whereas `find_by_id` followed by `if order.customer_id
    != caller` is one early return away from leaking.
    """

    def __init__(self, db: Session):
        self.db = db

    def find_for_customer(self, order_id: int, customer_id: int) -> Order | None:
        """One order belonging to this customer, or None.

        None covers both "no such order" and "not yours" — the caller answers
        404 either way, so the two are deliberately not distinguished.
        """
        return self.db.scalar(
            select(Order).where(Order.id == order_id, Order.customer_id == customer_id)
        )

    def list_for_customer(
        self, customer_id: int, limit: int = 50, offset: int = 0
    ) -> list[Order]:
        return list(
            self.db.scalars(
                select(Order)
                .where(Order.customer_id == customer_id)
                # Newest first: an order list is read to find the recent one.
                .order_by(Order.created_at.desc(), Order.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )

    def find_by_idempotency_key(self, customer_id: int, key: str) -> Order | None:
        """The order a previous, possibly half-delivered request already made."""
        return self.db.scalar(
            select(Order).where(
                Order.customer_id == customer_id, Order.idempotency_key == key
            )
        )

    def list_for_partner(self, partner_id: int, statuses: list[str]) -> list[Order]:
        """Deliveries assigned to this partner, in one of the given states.

        Scoped by `partner_id` in the WHERE clause for the same reason the
        customer reads are: an ownership check that lives in the query cannot be
        forgotten. `partner_id` is the snapshot written at assignment, so this
        needs nothing from partnerServices.
        """
        return list(
            self.db.scalars(
                select(Order)
                .where(Order.partner_id == partner_id, Order.status.in_(statuses))
                # Oldest first: a driver works the queue in the order it arrived,
                # unlike a customer reading their most recent booking.
                .order_by(Order.created_at)
            )
        )

    def find_for_partner(self, order_id: int, partner_id: int) -> Order | None:
        return self.db.scalar(
            select(Order).where(Order.id == order_id, Order.partner_id == partner_id)
        )

    def find_by_id(self, order_id: int) -> Order | None:
        """Unscoped: for internal callers, and for partner-facing routes that do
        their own ownership check against the assignment snapshot."""
        return self.db.scalar(select(Order).where(Order.id == order_id))

    def history(self, order_id: int) -> list[OrderStatusHistory]:
        return list(
            self.db.scalars(
                select(OrderStatusHistory)
                .where(OrderStatusHistory.order_id == order_id)
                .order_by(OrderStatusHistory.occurred_at, OrderStatusHistory.id)
            )
        )

    def add(self, order: Order) -> None:
        """Stage an order. The caller commits — usually alongside an outbox row."""
        self.db.add(order)

    def record_transition(
        self,
        order_id: int,
        from_status: str | None,
        to_status: OrderStatus,
        actor: str,
        caused_by_event_id: str | None = None,
    ) -> None:
        self.db.add(
            OrderStatusHistory(
                order_id=order_id,
                from_status=from_status,
                to_status=to_status.value,
                actor=actor,
                caused_by_event_id=caused_by_event_id,
            )
        )
