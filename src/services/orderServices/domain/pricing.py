"""Fare calculation, and the distance it rests on.

Pure, and deliberately separate from the client that will one day call the
Pricing service: when that service exists this file moves there whole, and
`clients/pricing_client.py` is what changes.
"""

import math
from decimal import ROUND_HALF_UP, Decimal

# Mean Earth radius, matching partnerServices' availability search so the two
# never disagree about how far apart two points are.
EARTH_RADIUS_KM = 6371.0

MONEY = Decimal("0.01")
DISTANCE = Decimal("0.01")


def haversine_km(
    from_latitude: float,
    from_longitude: float,
    to_latitude: float,
    to_longitude: float,
) -> Decimal:
    """Great-circle distance in kilometres.

    A straight line, not a route. Nothing here has asked a routing engine which
    roads exist, so every real trip is longer — which is why the fare built on
    it is a stub, and why the UI must call it an estimate.
    """
    from_lat, to_lat = math.radians(from_latitude), math.radians(to_latitude)
    delta_lat = to_lat - from_lat
    delta_lng = math.radians(to_longitude - from_longitude)

    half_chord = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(from_lat) * math.cos(to_lat) * math.sin(delta_lng / 2) ** 2
    )
    distance = 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(half_chord)))

    return Decimal(distance).quantize(DISTANCE, rounding=ROUND_HALF_UP)


def quote_amount(
    distance_km: Decimal,
    base_fare: float,
    per_km: float,
    minimum_fare: float,
) -> Decimal:
    """base + distance x rate, floored at the minimum.

    Decimal throughout: a fare is money, and money computed in float eventually
    charges someone 149.99999999.
    """
    amount = Decimal(str(base_fare)) + distance_km * Decimal(str(per_km))
    amount = max(amount, Decimal(str(minimum_fare)))
    return amount.quantize(MONEY, rounding=ROUND_HALF_UP)
