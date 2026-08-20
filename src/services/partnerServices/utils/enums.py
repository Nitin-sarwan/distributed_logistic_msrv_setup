"""The closed vocabularies this service uses.

Stored as plain `varchar` columns with a CHECK constraint rather than a Postgres
ENUM type. A native enum needs `ALTER TYPE ... ADD VALUE` to grow, which cannot
run inside a transaction and therefore cannot be reverted by a downgrade — a
painful property for a set of statuses that will certainly gain members.

They subclass `str`, so a Pydantic response serialises them as their value and
comparisons against a raw string from the database still work.
"""

from decimal import Decimal
from enum import Enum


class PartnerStatus(str, Enum):
    """Where a partner is in the work cycle.

    Only OFFLINE <-> ONLINE is the partner's own to set. ON_TRIP is written by
    Dispatch when an order is assigned, and SUSPENDED by operations — letting a
    partner clear either one from their phone would let them dodge a live
    delivery or a suspension.
    """

    OFFLINE = "offline"
    ONLINE = "online"
    ON_TRIP = "on_trip"
    SUSPENDED = "suspended"


# What a partner may set on themselves. Everything else is a state someone else
# put them in.
PARTNER_SETTABLE_STATUSES = {PartnerStatus.OFFLINE, PartnerStatus.ONLINE}


class VehicleStatus(str, Enum):
    """A vehicle's life cycle, in one column.

    This replaces the pair of booleans it is tempting to write instead
    (`is_verified` + `is_active`). Two booleans admit four combinations and only
    three are meaningful — "active but unverified" is a vehicle taking orders
    without its papers checked, which is the exact state the verification step
    exists to prevent. A single column cannot express it.

    PENDING  — registered by the partner, documents not yet checked. Unusable.
    INACTIVE — cleared, but parked. The partner owns it and is not driving it.
    ACTIVE   — cleared and currently being driven. At most one per partner.
    REJECTED — documents refused. Unusable, and not silently retried.

    Only ACTIVE makes a partner available to Dispatch.
    """

    PENDING = "pending"
    INACTIVE = "inactive"
    ACTIVE = "active"
    REJECTED = "rejected"


# A vehicle that has cleared verification. PENDING has not been looked at yet
# and REJECTED was looked at and refused, so neither may be put on the road.
VEHICLE_USABLE_STATUSES = {VehicleStatus.ACTIVE, VehicleStatus.INACTIVE}


class VehicleType(str, Enum):
    TWO_WHEELER = "two_wheeler"
    THREE_WHEELER = "three_wheeler"
    MINI_TRUCK = "mini_truck"
    TRUCK = "truck"


# Upper bound per class of vehicle, in kilograms. A partner declares the actual
# capacity of their vehicle; this stops a declaration that is physically absurd
# — a bike rated for 900kg would win every Dispatch query for heavy loads.
#
# These are the ceilings Dispatch will eventually match order weight against, so
# they belong next to the type they qualify rather than in the matching service.
VEHICLE_MAX_CAPACITY_KG: dict[VehicleType, Decimal] = {
    VehicleType.TWO_WHEELER: Decimal("30"),
    VehicleType.THREE_WHEELER: Decimal("500"),
    VehicleType.MINI_TRUCK: Decimal("1500"),
    VehicleType.TRUCK: Decimal("10000"),
}
