"""Pricing, until the Pricing service exists.

A stub, and deliberately shaped like the thing it will become: it takes two
points and returns a quote. When Pricing is real this file makes an HTTP call
instead of a calculation, and nothing above it changes.

**Delete-me markers**, so this cannot quietly become permanent:
  * the fare is a flat base plus a per-km rate on the *straight-line* distance;
  * there is no surge, no vehicle multiplier, no tariff by city;
  * `quote_id` is generated here rather than issued by anyone.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.services.orderServices.config import settings
from src.services.orderServices.domain.pricing import haversine_km, quote_amount


class Quote:
    __slots__ = ("quote_id", "amount", "currency", "distance_km", "expires_at")

    def __init__(
        self,
        quote_id: str,
        amount: Decimal,
        currency: str,
        distance_km: Decimal,
        expires_at: datetime,
    ):
        self.quote_id = quote_id
        self.amount = amount
        self.currency = currency
        self.distance_km = distance_km
        self.expires_at = expires_at


def get_quote(
    pickup_latitude: float,
    pickup_longitude: float,
    drop_latitude: float,
    drop_longitude: float,
    vehicle_type: str,
    weight_kg: Decimal,
) -> Quote:
    """What this delivery costs.

    `vehicle_type` and `weight_kg` are accepted and ignored. They are in the
    signature because the real service needs them, and a stub whose signature
    differs from its replacement is a refactor waiting to happen.
    """
    distance_km = haversine_km(
        pickup_latitude, pickup_longitude, drop_latitude, drop_longitude
    )

    amount = quote_amount(
        distance_km,
        base_fare=settings.pricing_base_fare,
        per_km=settings.pricing_per_km,
        minimum_fare=settings.pricing_minimum_fare,
    )

    return Quote(
        quote_id=str(uuid.uuid4()),
        amount=amount,
        currency=settings.pricing_currency,
        distance_km=distance_km,
        expires_at=datetime.now(timezone.utc)
        + timedelta(minutes=settings.quote_ttl_minutes),
    )
