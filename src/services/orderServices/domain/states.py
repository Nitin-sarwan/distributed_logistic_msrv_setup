"""The order state machine.

Pure: no SQLAlchemy, no FastAPI, no database. It can be exercised in a REPL,
which is the point — this is the part of the service most likely to rot if it
gets scattered across route handlers, and the part whose bugs are most expensive
once orders exist in the wild.

Every transition here is announced as an event, and every event is a contract
another service depends on. Adding a state is therefore never a local change.
"""

from enum import Enum


class OrderStatus(str, Enum):
    """Where an order is in its life.

    Subclasses `str`, so a Pydantic response serialises the value and a
    comparison against a raw string from the database still works.
    """

    CREATED = "created"
    QUOTED = "quoted"
    PAYMENT_PENDING = "payment_pending"
    CONFIRMED = "confirmed"
    SEARCHING_PARTNER = "searching_partner"
    PARTNER_ASSIGNED = "partner_assigned"
    PICKED_UP = "picked_up"
    IN_TRANSIT = "in_transit"
    DELIVERED = "delivered"
    SETTLED = "settled"
    CANCELLED = "cancelled"


# The only legal moves. Anything absent is a bug in a caller, not a state to add
# a branch for at the call site.
TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset({OrderStatus.QUOTED, OrderStatus.CANCELLED}),
    OrderStatus.QUOTED: frozenset({OrderStatus.PAYMENT_PENDING, OrderStatus.CANCELLED}),
    OrderStatus.PAYMENT_PENDING: frozenset(
        {OrderStatus.CONFIRMED, OrderStatus.CANCELLED}
    ),
    OrderStatus.CONFIRMED: frozenset(
        {OrderStatus.SEARCHING_PARTNER, OrderStatus.CANCELLED}
    ),
    OrderStatus.SEARCHING_PARTNER: frozenset(
        {OrderStatus.PARTNER_ASSIGNED, OrderStatus.CANCELLED}
    ),
    # Back to SEARCHING_PARTNER is deliberate: a partner who accepts and then
    # drops the job returns the order to the pool. Without that edge the order
    # is stranded and someone rescues it with SQL.
    OrderStatus.PARTNER_ASSIGNED: frozenset(
        {
            OrderStatus.PICKED_UP,
            OrderStatus.SEARCHING_PARTNER,
            OrderStatus.CANCELLED,
        }
    ),
    # Nothing returns from PICKED_UP. Once goods are in a vehicle the resolution
    # is a return trip, which is a new order rather than a reversed status.
    OrderStatus.PICKED_UP: frozenset({OrderStatus.IN_TRANSIT}),
    OrderStatus.IN_TRANSIT: frozenset({OrderStatus.DELIVERED}),
    # DELIVERED is not the end; SETTLED is. Delivered means the goods arrived,
    # settled means the money moved — two facts, two owners, two failure modes.
    OrderStatus.DELIVERED: frozenset({OrderStatus.SETTLED}),
    OrderStatus.SETTLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
}

# States a customer may still call off. Past pickup the answer is a return trip.
CANCELLABLE: frozenset[OrderStatus] = frozenset(
    status for status, targets in TRANSITIONS.items() if OrderStatus.CANCELLED in targets
)

# Nothing further will happen to an order in one of these.
TERMINAL: frozenset[OrderStatus] = frozenset(
    status for status, targets in TRANSITIONS.items() if not targets
)

# What the customer's app should keep polling.
ACTIVE: frozenset[OrderStatus] = frozenset(OrderStatus) - TERMINAL - {
    OrderStatus.DELIVERED
}


def can_transition(current: OrderStatus, target: OrderStatus) -> bool:
    return target in TRANSITIONS[current]


def is_cancellable(current: OrderStatus) -> bool:
    return current in CANCELLABLE
