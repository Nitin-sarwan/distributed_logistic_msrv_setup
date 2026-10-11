"""Straight-line distance, for "your driver is 1.2 km away".

A straight line, not a route, so the app labels it as approximate. Same Earth
radius as partnerServices' search and pricingServices, so the three never
disagree about how far apart two points are.
"""

import math

EARTH_RADIUS_KM = 6371.0


def haversine_km(
    from_latitude: float,
    from_longitude: float,
    to_latitude: float,
    to_longitude: float,
) -> float:
    from_lat, to_lat = math.radians(from_latitude), math.radians(to_latitude)
    delta_lat = to_lat - from_lat
    delta_lng = math.radians(to_longitude - from_longitude)

    half_chord = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(from_lat) * math.cos(to_lat) * math.sin(delta_lng / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(half_chord)))
