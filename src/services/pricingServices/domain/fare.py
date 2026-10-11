"""Fare calculation, and the distance it rests on.

Pure: no database, no settings, no clock. Everything a fare depends on is an
argument, which is what lets the public estimate and the binding quote call the
same function and never disagree about a price.

Moved here whole from orderServices' `domain/pricing.py`, which was written to
be moved. What changed on the way is that rates now come from a tariff per
vehicle type instead of three numbers in Order's `.env`.
"""

import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

# Mean Earth radius, matching partnerServices' availability search so the two
# never disagree about how far apart two points are.
EARTH_RADIUS_KM = 6371.0

MONEY = Decimal("0.01")
DISTANCE = Decimal("0.01")
ZERO = Decimal("0.00")


# Matches partnerServices' VehicleType. Duplicated as a tuple rather than
# imported, because a service must not import another service's code — the
# vocabulary is a contract, and the CHECK constraint on `tariffs` enforces it.
VEHICLE_TYPES = ("two_wheeler", "three_wheeler", "mini_truck", "truck")

VEHICLE_LABELS = {
    "two_wheeler": "two wheeler",
    "three_wheeler": "three wheeler",
    "mini_truck": "mini truck",
    "truck": "truck",
}


def haversine_km(
    from_latitude: float,
    from_longitude: float,
    to_latitude: float,
    to_longitude: float,
) -> Decimal:
    """Great-circle distance in kilometres. A straight line, not a route."""
    from_lat, to_lat = math.radians(from_latitude), math.radians(to_latitude)
    delta_lat = to_lat - from_lat
    delta_lng = math.radians(to_longitude - from_longitude)

    half_chord = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(from_lat) * math.cos(to_lat) * math.sin(delta_lng / 2) ** 2
    )
    distance = 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(half_chord)))

    return Decimal(distance).quantize(DISTANCE, rounding=ROUND_HALF_UP)


def road_distance_km(straight_line_km: Decimal, factor: float) -> Decimal:
    """What the trip is charged on: the straight line, scaled for detours.

    An estimate, and named as one everywhere it is shown. Every real trip is
    longer than the straight line, so charging on the straight line would
    undercharge every delivery by a predictable amount.
    """
    return (straight_line_km * Decimal(str(factor))).quantize(
        DISTANCE, rounding=ROUND_HALF_UP
    )


@dataclass(frozen=True)
class Rates:
    """The numbers from one tariff row, detached from the ORM.

    A quote is computed from these and then stores its own copy of the result,
    so editing a tariff tomorrow cannot change what a customer was quoted today.
    """

    base_fare: Decimal
    per_km: Decimal
    minimum_fare: Decimal
    included_weight_kg: Decimal
    per_kg: Decimal
    max_weight_kg: Decimal


@dataclass(frozen=True)
class Fare:
    """A price and how it was reached.

    The breakdown is returned to the customer, not just stored. "Why is this
    ₹212?" has an answer on the screen rather than in a support ticket.
    """

    base_fare: Decimal
    distance_charge: Decimal
    weight_charge: Decimal
    # Positive when the minimum fare lifted the total; zero otherwise. Shown as
    # its own line so a short trip's price does not look like arithmetic error.
    minimum_fare_adjustment: Decimal
    amount: Decimal


def format_kg(weight: Decimal) -> str:
    """'30', not '30.00' — for messages a person reads."""
    return format(weight.normalize(), "f")


def exceeds_capacity(rates: Rates, weight_kg: Decimal) -> bool:
    return weight_kg > rates.max_weight_kg


def compute_fare(rates: Rates, distance_km: Decimal, weight_kg: Decimal) -> Fare:
    """base + distance x rate + weight over the allowance x rate, floored.

    Decimal throughout: a fare is money, and money computed in float eventually
    charges someone 149.99999999. Each line is rounded on its own so the lines
    shown to the customer add up to the total shown to the customer.

    Does not check capacity — the caller decides whether an overweight parcel is
    an error (a quote) or an "unavailable" option (an estimate).
    """
    base = rates.base_fare.quantize(MONEY, rounding=ROUND_HALF_UP)
    distance_charge = (distance_km * rates.per_km).quantize(
        MONEY, rounding=ROUND_HALF_UP
    )

    excess_kg = max(ZERO, weight_kg - rates.included_weight_kg)
    weight_charge = (excess_kg * rates.per_kg).quantize(MONEY, rounding=ROUND_HALF_UP)

    subtotal = base + distance_charge + weight_charge
    minimum = rates.minimum_fare.quantize(MONEY, rounding=ROUND_HALF_UP)
    adjustment = max(ZERO, minimum - subtotal)

    return Fare(
        base_fare=base,
        distance_charge=distance_charge,
        weight_charge=weight_charge,
        minimum_fare_adjustment=adjustment,
        amount=subtotal + adjustment,
    )
