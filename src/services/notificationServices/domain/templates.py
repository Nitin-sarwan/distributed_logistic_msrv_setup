"""Event in, messages out. The only place in the platform that writes prose.

This is the whole service in one file, and the boundary it defends is worth
stating plainly: **Notification decides channel, template and audience. It never
decides that something happened.** `order.confirmed` is a fact produced by the
service that owns orders; what a customer reads about it, in which app, over
which channel, is this file's business and nobody else's.

The inverse matters just as much. Nothing here calls Order, Dispatch or User to
find out more. Every message below is rendered from the event payload alone -
which is why those payloads carry `public_ref` and city names rather than only
ids. A template that needed a lookup would put another service's uptime in the
path of a push notification.

One event can produce several messages, to different people, on different
channels. `partner.assigned` tells the customer their driver is coming *and*
tells the driver they have a job; both are the same fact and neither is the
other's copy.

Pure functions of a dict. No database, no session, no I/O - so a template can be
checked in a REPL, which is what you want from the code that decides what four
thousand people read.
"""

from typing import Any, Callable, NamedTuple

from src.common.events.types import (
    DISPATCH_SEARCH_FAILED,
    ORDER_CANCELLED,
    ORDER_CONFIRMED,
    ORDER_CREATED,
    ORDER_DELIVERED,
    ORDER_PICKED_UP,
    PARTNER_ASSIGNED,
    PARTNER_OFFERED,
)
from src.services.notificationServices.domain.vocabulary import (
    Category,
    Channel,
    RecipientType,
)


class Message(NamedTuple):
    """One thing to say to one person.

    `action_path` is a route in the app rather than a URL. The two front ends
    live at different origins and a link that hardcoded one would be broken in
    the other; the app prefixes its own base.
    """

    recipient_type: RecipientType
    recipient_id: int
    category: Category
    title: str
    body: str
    channels: tuple[Channel, ...]
    action_path: str | None = None


# ── Helpers ───────────────────────────────────────────────────────────────
#
# Every value below comes off an event payload, where numbers arrive as strings
# (see `json_safe` in the envelope: money must not pass through a float). These
# format them for a person without pretending they are numbers again.


def _money(amount: Any, currency: str = "INR") -> str:
    if amount is None:
        return "the fare"
    symbol = "₹" if currency == "INR" else f"{currency} "
    # Whole rupees. Nobody reads paise on a notification.
    return f"{symbol}{float(amount):,.0f}"


def _km(value: Any) -> str:
    if value is None:
        return ""
    return f"{float(value):.1f}km"


def _route(data: dict[str, Any]) -> str:
    """"Koramangala to Indiranagar", or a graceful less-than-that.

    City is all Notification is given, deliberately: a full address on a lock
    screen is a privacy leak, and the person reading it already knows where they
    asked for the parcel to go.
    """
    pickup = (data.get("pickup") or {}).get("city") or data.get("pickup_city")
    drop = (data.get("drop") or {}).get("city") or data.get("drop_city")
    if pickup and drop:
        return f"{pickup} to {drop}"
    return pickup or drop or "your delivery"


def _ref(data: dict[str, Any]) -> str:
    return data.get("public_ref") or f"#{data.get('order_id')}"


def _order_path(data: dict[str, Any]) -> str:
    return f"/orders/{data.get('order_id')}"


# ── Customer templates ────────────────────────────────────────────────────


def _order_created(data: dict[str, Any]) -> list[Message]:
    """In-app only, and no push.

    The customer is looking at the screen that just created it - a push here
    would buzz a phone that is already in their hand. It is stored so the feed
    has a complete history, which is the other thing a feed is for.
    """
    return [
        Message(
            recipient_type=RecipientType.CUSTOMER,
            recipient_id=int(data["customer_id"]),
            category=Category.ORDER,
            title=f"Booking {_ref(data)} created",
            body=f"{_route(data)}. We are working out your fare.",
            channels=(Channel.IN_APP,),
            action_path=_order_path(data),
        )
    ]


def _order_confirmed(data: dict[str, Any]) -> list[Message]:
    """The first message worth interrupting someone for."""
    return [
        Message(
            recipient_type=RecipientType.CUSTOMER,
            recipient_id=int(data["customer_id"]),
            category=Category.ORDER,
            title=f"Order {_ref(data)} confirmed",
            body=(
                f"{_money(data.get('amount'), data.get('currency', 'INR'))} for "
                f"{_route(data)}. We are finding you a delivery partner."
            ),
            channels=(Channel.IN_APP, Channel.PUSH, Channel.EMAIL),
            action_path=_order_path(data),
        )
    ]


def _search_failed(data: dict[str, Any]) -> list[Message]:
    """Three different failures, three different things to say.

    This is what `SearchFailureReason` is carried on the event for. "No partners
    are on duty near you" and "we asked eight partners and none accepted" are
    not the same situation, and telling somebody the first when the second
    happened is a small lie that produces a support ticket.
    """
    reason = data.get("reason")
    offers = data.get("offers_made") or 0

    if reason == "no_candidates":
        body = (
            "No delivery partners are on duty near your pickup point right now. "
            "It is worth trying again shortly."
        )
    elif offers:
        body = (
            f"We asked {offers} nearby partner{'s' if offers != 1 else ''} and "
            "none were able to take it. Please try again."
        )
    else:
        body = "We could not find a delivery partner in time. Please try again."

    return [
        Message(
            recipient_type=RecipientType.CUSTOMER,
            recipient_id=int(data["customer_id"]),
            category=Category.ORDER,
            title="No partner found",
            body=body,
            channels=(Channel.IN_APP, Channel.PUSH),
            action_path=_order_path(data),
        )
    ]


def _partner_assigned(data: dict[str, Any]) -> list[Message]:
    """One event, two people, two entirely different messages.

    The customer gets the driver's name, vehicle and number, because those are
    what you need at the kerb. The driver gets the job reference, because they
    are about to open it. Neither is a variant of the other, which is why this
    returns a list rather than one message with a template switch.
    """
    name = data.get("partner_name") or "Your delivery partner"
    vehicle = data.get("vehicle_number")
    phone = data.get("partner_phone")

    customer_body = f"{name} is on the way to collect your parcel."
    if vehicle:
        customer_body += f" Vehicle {vehicle}."
    if phone:
        customer_body += f" Call {phone}."

    messages = [
        Message(
            recipient_type=RecipientType.CUSTOMER,
            recipient_id=int(data["customer_id"]),
            category=Category.ORDER,
            title="Your partner is on the way",
            body=customer_body,
            channels=(Channel.IN_APP, Channel.PUSH),
            action_path=_order_path(data),
        )
    ]

    partner_id = data.get("partner_id")
    if partner_id is not None:
        messages.append(
            Message(
                recipient_type=RecipientType.PARTNER,
                recipient_id=int(partner_id),
                category=Category.ORDER,
                title=f"Delivery {_ref(data)} is yours",
                body="Head to the pickup point. Tap the job for the address.",
                channels=(Channel.IN_APP, Channel.PUSH),
                action_path=f"/deliveries/{data.get('order_id')}",
            )
        )

    return messages


def _order_picked_up(data: dict[str, Any]) -> list[Message]:
    return [
        Message(
            recipient_type=RecipientType.CUSTOMER,
            recipient_id=int(data["customer_id"]),
            category=Category.ORDER,
            title="Your parcel is on its way",
            body=f"{_ref(data)} has been collected and is in transit.",
            channels=(Channel.IN_APP, Channel.PUSH),
            action_path=_order_path(data),
        )
    ]


def _order_delivered(data: dict[str, Any]) -> list[Message]:
    """The one place email earns its place.

    A receipt is the message people go looking for weeks later, and a feed row
    inside an app is not where anybody looks. The push tells them now; the email
    is the copy that survives.
    """
    amount = _money(data.get("amount"), data.get("currency", "INR"))

    messages = [
        Message(
            recipient_type=RecipientType.CUSTOMER,
            recipient_id=int(data["customer_id"]),
            category=Category.ORDER,
            title="Delivered",
            body=f"{_ref(data)} has been delivered. {amount} for {_km(data.get('distance_km'))}.",
            channels=(Channel.IN_APP, Channel.PUSH, Channel.EMAIL),
            action_path=_order_path(data),
        )
    ]

    partner_id = data.get("partner_id")
    if partner_id is not None:
        messages.append(
            Message(
                recipient_type=RecipientType.PARTNER,
                recipient_id=int(partner_id),
                category=Category.ORDER,
                title="Delivery completed",
                body=f"{_ref(data)} is done. You are back online for new jobs.",
                channels=(Channel.IN_APP,),
                action_path=f"/deliveries/{data.get('order_id')}",
            )
        )

    return messages


def _order_cancelled(data: dict[str, Any]) -> list[Message]:
    """What the customer is told depends on what was already paid.

    `previous_status` is on the event so Payment can decide whether to void or
    refund without asking Order. It serves the same purpose here: promising a
    refund on an order that was never charged is worse than saying nothing.
    """
    previous = data.get("previous_status")
    reason = data.get("reason")

    body = f"{_ref(data)} has been cancelled."
    if reason:
        # Reasons come from whoever cancelled - a customer's free text, or a
        # sentence Order wrote for a failed search - so the punctuation cannot
        # be assumed either way.
        body += f" {reason.rstrip('.')}."
    if previous in ("confirmed", "searching_partner", "partner_assigned"):
        body += " Any amount authorised for it will be released."

    messages = [
        Message(
            recipient_type=RecipientType.CUSTOMER,
            recipient_id=int(data["customer_id"]),
            category=Category.ORDER,
            title="Order cancelled",
            body=body,
            channels=(Channel.IN_APP, Channel.PUSH, Channel.EMAIL),
            action_path=_order_path(data),
        )
    ]

    # Only if somebody was already driving towards it. A cancellation before
    # assignment is not news to any partner.
    partner_id = data.get("partner_id")
    if partner_id is not None:
        messages.append(
            Message(
                recipient_type=RecipientType.PARTNER,
                recipient_id=int(partner_id),
                category=Category.ORDER,
                title=f"Delivery {_ref(data)} cancelled",
                # Not "the customer cancelled": Order also cancels an order
                # itself when Dispatch finds nobody, and telling a driver a
                # customer did it would be a guess presented as a fact.
                body="This delivery was cancelled. You are back online for new jobs.",
                channels=(Channel.IN_APP, Channel.PUSH),
                action_path="/deliveries",
            )
        )

    return messages


# ── Partner templates ─────────────────────────────────────────────────────


def _partner_offered(data: dict[str, Any]) -> list[Message]:
    """The one message on the platform with a deadline in it.

    Push only, and stored in the feed as well - but the feed copy is close to
    worthless, because by the time anybody scrolls to it the offer is gone. It
    is stored anyway so a driver disputing "I never got it" can be answered.

    Never mutable: OFFER is absent from `MUTABLE_CATEGORIES` because a driver
    who silenced these would be on duty and unreachable, which is worse for them
    than for anyone.
    """
    return [
        Message(
            recipient_type=RecipientType.PARTNER,
            recipient_id=int(data["partner_id"]),
            category=Category.OFFER,
            title=f"New delivery - {_money(data.get('amount'), data.get('currency', 'INR'))}",
            body=(
                f"{_route(data)}. Pickup is {_km(data.get('distance_km'))} away. "
                "Tap to accept before it expires."
            ),
            channels=(Channel.IN_APP, Channel.PUSH),
            action_path="/offers",
        )
    ]


# ── The registry ──────────────────────────────────────────────────────────
#
# One entry per event type this service reacts to. An event absent from here
# produces nothing, silently and correctly: every service reads the whole topic,
# and most of what arrives is not something to tell a person about.
#
# Notably absent, and deliberately:
#
#   dispatch.search_started  the customer was already told "finding you a
#                            partner" by order.confirmed, one second earlier
#   partner.rejected         a driver declining is not news to anybody; it is
#                            an analytics fact
#   partner.released         internal bookkeeping, invisible by design

TEMPLATES: dict[str, Callable[[dict[str, Any]], list[Message]]] = {
    ORDER_CREATED: _order_created,
    ORDER_CONFIRMED: _order_confirmed,
    DISPATCH_SEARCH_FAILED: _search_failed,
    PARTNER_OFFERED: _partner_offered,
    PARTNER_ASSIGNED: _partner_assigned,
    ORDER_PICKED_UP: _order_picked_up,
    ORDER_DELIVERED: _order_delivered,
    ORDER_CANCELLED: _order_cancelled,
}


def render(event_type: str, data: dict[str, Any]) -> list[Message]:
    """Every message one event should produce. Empty list is a valid answer."""
    template = TEMPLATES.get(event_type)
    if template is None:
        return []
    return template(data)
