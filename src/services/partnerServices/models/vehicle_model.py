from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.services.partnerServices.database.base import Base
from src.services.partnerServices.utils.enums import VehicleStatus


class Vehicle(Base):
    """A vehicle belonging to a partner.

    Its own table rather than columns on `partners` because the relationship is
    genuinely one-to-many over time: a partner replaces a bike with a van, or
    keeps both and switches between them by season. Flattening it would mean
    losing the old vehicle's record — including which vehicle carried a past
    delivery — every time one changed.
    """

    __tablename__ = "vehicles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    partner_id: Mapped[int] = mapped_column(
        ForeignKey("partners.id"),
        nullable=False,
        index=True,
    )

    vehicle_type: Mapped[str] = mapped_column(String(20), nullable=False)

    # The number plate, stored normalised (uppercase, no spaces or hyphens) so
    # "DL 01 AB 1234" and "dl01ab1234" cannot both be registered as separate
    # vehicles. Unique across the whole table, not per partner: a plate
    # identifies one physical vehicle in the world, and two partners claiming
    # the same one means one of them is lying.
    vehicle_number: Mapped[str] = mapped_column(
        String(20), nullable=False, unique=True
    )

    model_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # What the partner declares this vehicle can carry, in kilograms. Dispatch
    # matches order weight against it, so it is required — a NULL would either
    # exclude the vehicle from every query or be silently read as unlimited, and
    # both are worse than making the partner type a number.
    capacity: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)

    # Verification state and in-use state in one column. See VehicleStatus for
    # why this is not a pair of booleans: only ACTIVE puts a vehicle on the
    # road, and ACTIVE is unreachable without passing verification first.
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=VehicleStatus.PENDING.value,
        server_default=VehicleStatus.PENDING.value,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "vehicle_type IN ('two_wheeler','three_wheeler','mini_truck','truck')",
            name="ck_vehicles_type",
        ),
        CheckConstraint(
            "status IN ('pending','inactive','active','rejected')",
            name="ck_vehicles_status",
        ),
        CheckConstraint("capacity > 0", name="ck_vehicles_capacity_positive"),
        # "One active vehicle per partner", enforced by the database rather than
        # by the service remembering to stand the previous one down. A partial
        # unique index is the right tool: it constrains only the rows where
        # status is 'active', so a partner may own any number of parked ones.
        #
        # This is why VehicleRepository.set_active() clears and sets within a
        # single transaction — two statements that would each be legal alone are
        # not legal in between.
        Index(
            "uq_vehicles_one_active_per_partner",
            "partner_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )
