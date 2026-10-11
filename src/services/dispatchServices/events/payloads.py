"""What each event Dispatch emits carries.

The payload is what consumers need, not the whole row. `partner.assigned`
carries the driver's name and number because Order snapshots them and shows them
to a customer; it does not carry the driver's rating history or their live
coordinates, because an event is read by every consumer group including
Analytics and Audit and none of them have business with it.

Building them here rather than inline at each call site keeps that judgement in
one reviewable place - and makes it obvious when a new field starts leaking onto
a topic.
"""

from typing import Any

from src.services.dispatchServices.models.attempt_model import DispatchAttempt
from src.services.dispatchServices.models.offer_model import DispatchOffer


def search_started(attempt: DispatchAttempt) -> dict[str, Any]:
    """For Order above all: CONFIRMED -> SEARCHING_PARTNER.

    Also the marker analytics measures time-to-assign from, which is why it
    carries the radius and the deadline: "we took four minutes" reads
    differently once you know the search started at 5km and had 180 seconds.
    """
    return {
        "order_id": attempt.order_id,
        "public_ref": attempt.public_ref,
        "customer_id": attempt.customer_id,
        "attempt_id": attempt.id,
        "vehicle_type": attempt.vehicle_type,
        "radius_km": attempt.radius_km,
        "deadline_at": attempt.deadline_at,
    }


def partner_offered(attempt: DispatchAttempt, offer: DispatchOffer) -> dict[str, Any]:
    """For Notification above all: push the offer to the driver's phone.

    This is the event that becomes a `push.send` job. Dispatch does not know how
    a driver is reached and must not - it announces that an offer exists and for
    how long, and Notification owns channel, template and opt-out.
    """
    return {
        "order_id": attempt.order_id,
        "public_ref": attempt.public_ref,
        "attempt_id": attempt.id,
        "offer_id": offer.id,
        "partner_id": offer.partner_id,
        "rank": offer.rank,
        "distance_km": offer.distance_km,
        "pickup_city": attempt.pickup_city,
        "drop_city": attempt.drop_city,
        "amount": attempt.amount,
        "currency": attempt.currency,
        "expires_at": offer.expires_at,
    }


def partner_rejected(
    attempt: DispatchAttempt, offer: DispatchOffer, reason: str
) -> dict[str, Any]:
    """For analytics, and for Dispatch's own read of its history.

    **Rejection is an ordinary outcome, not an error.** A driver who declines is
    doing exactly what the offer asked them to do, and the three reasons -
    declined, expired, lost_race - are three different facts that must not be
    collapsed. Only one of them is even about the driver's choice.
    """
    return {
        "order_id": attempt.order_id,
        "public_ref": attempt.public_ref,
        "attempt_id": attempt.id,
        "offer_id": offer.id,
        "partner_id": offer.partner_id,
        "rank": offer.rank,
        "reason": reason,
        "offered_at": offer.offered_at,
    }


def partner_assigned(attempt: DispatchAttempt, offer: DispatchOffer) -> dict[str, Any]:
    """The event the whole service exists to emit.

    Everything Order needs to snapshot the driver onto the delivery, so that
    "your driver is on the way" never needs partnerServices to be up. Dispatch
    has just read these values in order to make the offer; passing them on is
    free, and asking for them again would put a second service in the path of a
    customer opening their app.

    Consumers: Order (snapshot and transition), Notification (tell the
    customer), Location (start tracking this pair), Audit.
    """
    return {
        "order_id": attempt.order_id,
        "public_ref": attempt.public_ref,
        "customer_id": attempt.customer_id,
        "attempt_id": attempt.id,
        "offer_id": offer.id,
        "partner_id": offer.partner_id,
        "partner_name": offer.partner_name,
        "partner_phone": offer.partner_phone,
        "vehicle_id": offer.vehicle_id,
        "vehicle_number": offer.vehicle_number,
        "vehicle_type": offer.vehicle_type,
        "distance_km": offer.distance_km,
        "offers_made": attempt.offers_made,
        "assigned_at": attempt.assigned_at,
    }


def search_failed(attempt: DispatchAttempt, reason: str) -> dict[str, Any]:
    """Dispatch gave up. Order decides what happens next.

    `reason` is one of `SearchFailureReason` and is load-bearing in the same way
    `previous_status` is on `order.cancelled`: it is what lets a consumer act
    without asking Dispatch anything. "Nobody was on duty" and "eight drivers
    declined" lead to different messages and different operational responses.
    """
    return {
        "order_id": attempt.order_id,
        "public_ref": attempt.public_ref,
        "customer_id": attempt.customer_id,
        "attempt_id": attempt.id,
        "reason": reason,
        "offers_made": attempt.offers_made,
        "radius_km": attempt.radius_km,
        "searched_for_seconds": _elapsed_seconds(attempt),
    }


def partner_released(
    attempt: DispatchAttempt, partner_id: int, reason: str
) -> dict[str, Any]:
    """A claimed driver handed back to the pool.

    Emitted after partnerServices has actually been told, not before - this
    announces a change that has happened, which is what past tense means.
    """
    return {
        "order_id": attempt.order_id,
        "public_ref": attempt.public_ref,
        "attempt_id": attempt.id,
        "partner_id": partner_id,
        "reason": reason,
    }


def _elapsed_seconds(attempt: DispatchAttempt) -> int | None:
    """How long the search ran, for the failure event.

    Computed rather than stored: it is only ever wanted at the moment the search
    ends, and a column would be one more thing that can disagree with the
    timestamps it is derived from.
    """
    if attempt.ended_at is None or attempt.created_at is None:
        return None
    return int((attempt.ended_at - attempt.created_at).total_seconds())
