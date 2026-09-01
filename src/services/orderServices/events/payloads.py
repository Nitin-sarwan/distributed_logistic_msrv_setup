"""What each event carries.

The payload is what consumers need, not the whole order row. `order.confirmed`
carries coordinates because Dispatch matches on them; it does not carry the
customer's phone number, because an event is read by every consumer group
including Analytics and Audit, and none of them have business with it.

Building them here rather than inline at each call site keeps that judgement in
one reviewable place — and makes it obvious when a new field starts leaking into
a topic.
"""

from typing import Any

from src.services.orderServices.models.order_model import Order


def _point(latitude, longitude, city: str) -> dict[str, Any]:
    return {"latitude": latitude, "longitude": longitude, "city": city}


def order_created(order: Order) -> dict[str, Any]:
    """For Pricing, Audit, Analytics: enough to quote and to count."""
    return {
        "order_id": order.id,
        "public_ref": order.public_ref,
        "customer_id": order.customer_id,
        "pickup": _point(order.pickup_latitude, order.pickup_longitude, order.pickup_city),
        "drop": _point(order.drop_latitude, order.drop_longitude, order.drop_city),
        "vehicle_type": order.vehicle_type,
        "package_weight_kg": order.package_weight_kg,
        "distance_km": order.distance_km,
    }


def order_confirmed(order: Order) -> dict[str, Any]:
    """For Dispatch above all: everything needed to find a partner.

    Coordinates, what the vehicle must be able to carry, and the fare — Dispatch
    ranks on distance, filters on capacity, and shows the driver what the job
    pays.
    """
    return {
        "order_id": order.id,
        "public_ref": order.public_ref,
        "customer_id": order.customer_id,
        "pickup": _point(order.pickup_latitude, order.pickup_longitude, order.pickup_city),
        "drop": _point(order.drop_latitude, order.drop_longitude, order.drop_city),
        "vehicle_type": order.vehicle_type,
        "package_weight_kg": order.package_weight_kg,
        "distance_km": order.distance_km,
        "amount": order.quoted_amount,
        "currency": order.currency,
    }


def order_cancelled(order: Order, previous_status: str) -> dict[str, Any]:
    """For Payment above all.

    `previous_status` is the load-bearing field: it is what tells Payment
    whether there is nothing to do, an authorisation to void, or a capture to
    refund — without Payment having to ask Order.
    """
    return {
        "order_id": order.id,
        "public_ref": order.public_ref,
        "customer_id": order.customer_id,
        "partner_id": order.partner_id,
        "previous_status": previous_status,
        "reason": order.cancellation_reason,
        "amount": order.quoted_amount,
        "currency": order.currency,
    }


def order_picked_up(order: Order) -> dict[str, Any]:
    """For Notification above all: the customer wants to know it is moving."""
    return {
        "order_id": order.id,
        "public_ref": order.public_ref,
        "customer_id": order.customer_id,
        "partner_id": order.partner_id,
        "picked_up_at": order.picked_up_at,
    }


def order_delivered(order: Order) -> dict[str, Any]:
    """For Payment above all: this is what it captures on.

    Carries the amount so Payment does not have to ask what to charge — the
    order is the record of what was agreed, and this is that number.
    """
    return {
        "order_id": order.id,
        "public_ref": order.public_ref,
        "customer_id": order.customer_id,
        "partner_id": order.partner_id,
        "amount": order.quoted_amount,
        "currency": order.currency,
        "distance_km": order.distance_km,
        "delivered_at": order.delivered_at,
    }
