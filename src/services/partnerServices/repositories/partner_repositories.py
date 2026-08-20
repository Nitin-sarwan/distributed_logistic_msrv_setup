import math
from decimal import Decimal

from sqlalchemy import Float, Row, and_, cast, func, or_, select
from sqlalchemy.dialects.postgresql import INTERVAL
from sqlalchemy.orm import Session

from src.services.partnerServices.models.partner_model import Partner
from src.services.partnerServices.models.vehicle_model import Vehicle
from src.services.partnerServices.utils.enums import PartnerStatus, VehicleStatus

# Mean Earth radius. The Haversine formula treats the planet as a sphere, which
# is wrong by up to about 0.5% — irrelevant next to the error in a phone's GPS
# fix, and the alternative (a proper geodesic, or PostGIS) is a dependency this
# project does not need to answer "who is within 5km".
EARTH_RADIUS_KM = 6371.0

# One degree of latitude, everywhere. Longitude degrees shrink towards the
# poles, which is why the box below divides by cos(latitude).
KM_PER_DEGREE_LATITUDE = 111.045


class PartnerRepository:
    """Data access for the partners table. Holds no business rules."""

    def __init__(self, db: Session):
        self.db = db

    # ── Lookups ───────────────────────────────────────────────────────────

    def find_by_id(self, partner_id: int) -> Partner | None:
        return self.db.scalar(
            select(Partner).where(
                Partner.id == partner_id,
                Partner.is_deleted.is_(False),
            )
        )

    def find_by_phone(self, phone: str) -> Partner | None:
        return self.db.scalar(
            select(Partner).where(
                Partner.phone == phone,
                Partner.is_deleted.is_(False),
            )
        )

    def find_by_email(self, email: str) -> Partner | None:
        return self.db.scalar(
            select(Partner).where(
                Partner.email == email,
                Partner.is_deleted.is_(False),
            )
        )

    def find_by_phone_or_email(self, phone: str, email: str | None) -> Partner | None:
        """One query for the duplicate check at registration.

        `email` is skipped when absent rather than compared to NULL: in SQL
        `email = NULL` is never true, so including it would be dead weight, and
        it would also read as though a second partner without an email could
        collide with this one. They cannot.
        """
        conditions = [Partner.phone == phone]
        if email is not None:
            conditions.append(Partner.email == email)

        return self.db.scalar(
            select(Partner).where(
                or_(*conditions),
                Partner.is_deleted.is_(False),
            )
        )

    # ── Writes ────────────────────────────────────────────────────────────

    def create(self, partner: Partner) -> Partner:
        self.db.add(partner)
        self.db.commit()
        self.db.refresh(partner)
        return partner

    def save(self, partner: Partner) -> Partner:
        """Persist changes to an already-loaded partner."""
        self.db.commit()
        self.db.refresh(partner)
        return partner

    def delete(self, partner: Partner) -> None:
        self.db.delete(partner)
        self.db.commit()

    # ── The Dispatch query ────────────────────────────────────────────────

    def find_available(
        self,
        latitude: float,
        longitude: float,
        radius_km: float,
        stale_after_minutes: int,
        limit: int,
        vehicle_type: str | None = None,
        min_capacity: Decimal | None = None,
    ) -> list[Row]:
        """Partners who could take an order at this point, nearest first.

        Returns `(Partner, Vehicle, distance_km)` rows — the vehicle is part of
        the answer, not a follow-up lookup, because "is a partner available" is
        not separable from "on what". A free driver whose only cleared vehicle
        is a bike is not available for a 400kg load.

        Five conditions have to hold at once, and each one exists because
        dropping it produces a specific bad assignment:

        * `Partner.status == ONLINE` — not merely "not offline". ON_TRIP is
          mid-delivery and SUSPENDED is barred.
        * `is_verified` — KYC cleared. An unverified partner may hold an account
          and a vehicle, and must never receive an order.
        * a vehicle at `VehicleStatus.ACTIVE` — which, by the way that enum is
          built, already implies the vehicle passed verification.
        * a location fresher than `stale_after_minutes` — a status column says
          what the partner last chose, not where their phone currently is. Ten
          minutes without a heartbeat means they are in a tunnel, out of
          battery, or gone.
        * inside the radius.

        The `is_deleted` filter is the sixth, and is the one that would be
        easiest to forget: a soft-deleted partner keeps their last known status
        and coordinates, so without it a deactivated account keeps getting work.
        """
        latitude_rad = math.radians(latitude)
        longitude_rad = math.radians(longitude)

        # Constant across the whole scan, so compute it once in Python rather
        # than making Postgres evaluate it per row.
        cos_latitude = math.cos(latitude_rad)

        partner_latitude = func.radians(cast(Partner.current_latitude, Float))
        partner_longitude = func.radians(cast(Partner.current_longitude, Float))

        # The Haversine formula proper — half-angle sines, then asin — rather
        # than the spherical law of cosines, which is the shorter expression and
        # is wrong here.
        #
        # Both are algebraically exact. The difference is conditioning: the
        # cosine form ends in `acos(x)` with x approaching 1 for nearby points,
        # and acos has an infinite derivative there, so the last bits of
        # floating-point error get amplified without limit. In practice it
        # reports about 13cm for a partner standing exactly on the pickup pin.
        # Harmless at that magnitude, but the error grows as the *inverse* of
        # the distance, which is precisely backwards for a query whose whole
        # purpose is finding the closest partner. Haversine stays accurate at
        # short range, which is the range that matters.
        half_chord = func.power(
            func.sin((partner_latitude - latitude_rad) / 2), 2
        ) + cos_latitude * func.cos(partner_latitude) * func.power(
            func.sin((partner_longitude - longitude_rad) / 2), 2
        )

        # asin is defined on [-1, 1]. The expression above cannot go negative —
        # it is a sum of squares times a cosine that is positive for any real
        # latitude — so only the upper end needs clamping, and only against
        # rounding at the antipode.
        distance_km = (
            2 * EARTH_RADIUS_KM * func.asin(func.least(1.0, func.sqrt(half_chord)))
        )

        # A bounding box first, so the index on (status, is_verified, lat, lng)
        # can do the work and the trigonometry only runs on rows that already
        # plausibly qualify. It over-selects slightly — a box always contains
        # its inscribed circle's corners — which is exactly why the precise
        # distance filter below still applies.
        latitude_delta = radius_km / KM_PER_DEGREE_LATITUDE
        # Guard the divisor: at the poles cos(lat) reaches 0 and this would be a
        # division by zero. Nobody delivers there, but a crash is a poor way to
        # find that out.
        longitude_delta = radius_km / (
            KM_PER_DEGREE_LATITUDE * max(abs(cos_latitude), 1e-6)
        )

        # Postgres' clock, matching the one `update_location` writes with. Doing
        # the subtraction in SQL rather than in Python keeps both sides of the
        # comparison on the same clock, so a drifted application container
        # cannot quietly change what "stale" means.
        #
        # The interval goes through a bind parameter rather than being formatted
        # into the SQL text; `int()` on the way in means the value cannot be
        # anything but a number regardless.
        fresh_since = func.now() - cast(f"{int(stale_after_minutes)} minutes", INTERVAL)

        query = (
            select(Partner, Vehicle, distance_km.label("distance_km"))
            # An inner join, so a partner with no active vehicle simply does not
            # appear. An outer join would return them with a NULL vehicle and
            # push the decision onto Dispatch, which is precisely the judgement
            # this service is supposed to own.
            .join(
                Vehicle,
                and_(
                    Vehicle.partner_id == Partner.id,
                    Vehicle.status == VehicleStatus.ACTIVE.value,
                ),
            )
            .where(
                Partner.is_deleted.is_(False),
                Partner.is_verified.is_(True),
                Partner.status == PartnerStatus.ONLINE.value,
                Partner.current_latitude.is_not(None),
                Partner.current_longitude.is_not(None),
                Partner.location_updated_at.is_not(None),
                Partner.location_updated_at >= fresh_since,
                Partner.current_latitude.between(
                    latitude - latitude_delta, latitude + latitude_delta
                ),
                Partner.current_longitude.between(
                    longitude - longitude_delta, longitude + longitude_delta
                ),
                distance_km <= radius_km,
            )
            # Nearest first, then best rated. Distance dominates because it is
            # what the customer feels; rating only separates partners who are
            # effectively equidistant.
            .order_by(distance_km.asc(), Partner.rating.desc())
            .limit(limit)
        )

        if vehicle_type is not None:
            query = query.where(Vehicle.vehicle_type == vehicle_type)

        if min_capacity is not None:
            query = query.where(Vehicle.capacity >= min_capacity)

        return list(self.db.execute(query))
