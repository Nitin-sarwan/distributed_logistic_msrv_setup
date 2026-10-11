"""Every event on the platform, and the topic they travel on.

Listed in one module so a typo is an ImportError at startup rather than a topic
nobody subscribes to. A producer that invents a string and a consumer that
matches on a different string both work perfectly and never meet.

**Past tense, always.** `order.confirmed`, never `confirm_order`. A command on
the log is a boundary in the wrong place: it names who must act, which is
exactly the coupling the log is here to remove.
"""

from typing import Final

# ── One topic ─────────────────────────────────────────────────────────────
#
# Order produces to it, Dispatch produces to it, and both plus Notification
# consume from it. That is deliberate, and it is the alternative to the more
# conventional topic-per-producer.
#
# Kafka guarantees ordering only *within a partition of one topic*. Every event
# here is keyed by `order_id`, so one topic means every fact about one delivery
# lands on one partition in the order it happened — `order.confirmed` before
# `partner.assigned` before `order.picked_up`. Split those across an
# `order-events` and a `dispatch-events` topic and Notification, which reads
# both, can be told a driver arrived before it is told one was found.
#
# The cost is that every consumer group reads every event and discards most of
# them. At this volume that is free; the ordering guarantee is not.
TOPIC: Final = "order-events"

# Where a message goes when its handler has failed every retry. Nothing consumes
# it automatically — it is a place to look, not a queue that drains itself.
DLQ_TOPIC: Final = f"{TOPIC}.dlq"

# ── Producers ─────────────────────────────────────────────────────────────
#
# The `producer` field on every envelope. Since all four share a topic, this is
# how you tell at a glance who emitted what.
PRODUCER_ORDER: Final = "order"
PRODUCER_DISPATCH: Final = "dispatch"
PRODUCER_USER: Final = "user"
PRODUCER_PAYMENT: Final = "payment"

# ── order ─────────────────────────────────────────────────────────────────
ORDER_CREATED: Final = "order.created"
ORDER_CONFIRMED: Final = "order.confirmed"
ORDER_PICKED_UP: Final = "order.picked_up"
ORDER_DELIVERED: Final = "order.delivered"
ORDER_CANCELLED: Final = "order.cancelled"

# ── dispatch ──────────────────────────────────────────────────────────────
#
# `partner.*` are facts about a partner and an order together; `dispatch.*` are
# facts about the search itself. Both are produced by Dispatch — the prefix says
# what the event is about, not who sent it.
DISPATCH_SEARCH_STARTED: Final = "dispatch.search_started"
DISPATCH_SEARCH_FAILED: Final = "dispatch.search_failed"
PARTNER_OFFERED: Final = "partner.offered"
PARTNER_REJECTED: Final = "partner.rejected"
PARTNER_ASSIGNED: Final = "partner.assigned"
PARTNER_RELEASED: Final = "partner.released"

# Every type the platform knows about. A consumer registering a handler for a
# string absent from this set is a startup error — see consumer.py.
ALL_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        ORDER_CREATED,
        ORDER_CONFIRMED,
        ORDER_PICKED_UP,
        ORDER_DELIVERED,
        ORDER_CANCELLED,
        DISPATCH_SEARCH_STARTED,
        DISPATCH_SEARCH_FAILED,
        PARTNER_OFFERED,
        PARTNER_REJECTED,
        PARTNER_ASSIGNED,
        PARTNER_RELEASED,
    }
)

# ── Consumer groups ───────────────────────────────────────────────────────
#
# The group id is what Kafka tracks offsets against, so it is effectively part
# of the deployment's state: rename one and that service re-reads the topic from
# `consumer_auto_offset_reset`. Pinned here rather than derived from a service
# name for exactly that reason.
GROUP_ORDER: Final = "order-service"
GROUP_DISPATCH: Final = "dispatch-service"
GROUP_NOTIFICATION: Final = "notification-service"
