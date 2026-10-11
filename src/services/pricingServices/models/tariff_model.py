from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Numeric,
    String,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.services.pricingServices.database.base import Base
from src.services.pricingServices.domain.fare import VEHICLE_TYPES, Rates

VEHICLE_TYPE_VALUES = ", ".join(f"'{value}'" for value in VEHICLE_TYPES)


class Tariff(Base):
    """The rate card for one class of vehicle.

    One row per vehicle type, edited in place by operations. Editing it never
    changes a quote already issued — each quote stores the amounts it was
    computed to, which is the snapshot rule applied to money.

    `max_weight_kg` lives here, not just in partnerServices, because refusing a
    120kg parcel on a two-wheeler is a pricing answer ("we will not quote that")
    and has to happen before the customer pays, not when Dispatch finds nobody.
    """

    __tablename__ = "tariffs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    vehicle_type: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)

    base_fare: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    per_km: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    minimum_fare: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    # Weight up to this is in the base price; every kilogram past it costs
    # `per_kg`. Zero allowance plus a zero rate means weight is free.
    included_weight_kg: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    per_kg: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    max_weight_kg: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="INR", server_default="INR"
    )

    # Off means "we do not offer this right now" — estimates show it greyed out
    # and quotes refuse it. Never deleted: old quotes still name it.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
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
            f"vehicle_type IN ({VEHICLE_TYPE_VALUES})", name="ck_tariffs_vehicle_type"
        ),
        CheckConstraint(
            "base_fare >= 0 AND per_km >= 0 AND minimum_fare >= 0 AND per_kg >= 0",
            name="ck_tariffs_rates_non_negative",
        ),
        CheckConstraint(
            "included_weight_kg >= 0 AND max_weight_kg > 0",
            name="ck_tariffs_weights",
        ),
    )

    def rates(self) -> Rates:
        return Rates(
            base_fare=self.base_fare,
            per_km=self.per_km,
            minimum_fare=self.minimum_fare,
            included_weight_kg=self.included_weight_kg,
            per_kg=self.per_kg,
            max_weight_kg=self.max_weight_kg,
        )
