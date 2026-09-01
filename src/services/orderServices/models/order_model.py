from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.services.orderServices.database.base import Base
from src.services.orderServices.domain.states import OrderStatus

STATUS_VALUES = ", ".join(f"'{status.value}'" for status in OrderStatus)


class Order(Base):
    """One delivery, as agreed.

    Nearly every column here is a **snapshot**, not a reference. The addresses
    are copied from the customer's address book at creation, the fare from the
    quote, the partner's name and number from the assignment. Nothing refreshes
    them and nothing should: an order is the record of what was agreed at the
    time, and a customer editing their saved address must not silently rewrite
    where last month's delivery went.

    `customer_id` and `partner_id` are bare integers. A real foreign key would
    require a shared database, which is the arrangement this architecture exists
    to avoid.
    """

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Shown to people; used in URLs. See utils/reference.py for why the primary
    # key is not.
    public_ref: Mapped[str] = mapped_column(String(12), nullable=False, unique=True)

    customer_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=OrderStatus.CREATED.value,
        server_default=OrderStatus.CREATED.value,
    )

    # ── Pickup snapshot ───────────────────────────────────────────────────
    pickup_line1: Mapped[str] = mapped_column(Text, nullable=False)
    pickup_line2: Mapped[str | None] = mapped_column(Text, nullable=True)
    pickup_city: Mapped[str] = mapped_column(String(255), nullable=False)
    pickup_pin_code: Mapped[str] = mapped_column(String(6), nullable=False)
    pickup_latitude: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)
    pickup_longitude: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)
    pickup_contact_name: Mapped[str] = mapped_column(String(100), nullable=False)
    pickup_contact_phone: Mapped[str] = mapped_column(String(10), nullable=False)

    # ── Drop snapshot ─────────────────────────────────────────────────────
    drop_line1: Mapped[str] = mapped_column(Text, nullable=False)
    drop_line2: Mapped[str | None] = mapped_column(Text, nullable=True)
    drop_city: Mapped[str] = mapped_column(String(255), nullable=False)
    drop_pin_code: Mapped[str] = mapped_column(String(6), nullable=False)
    drop_latitude: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)
    drop_longitude: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)
    drop_contact_name: Mapped[str] = mapped_column(String(100), nullable=False)
    drop_contact_phone: Mapped[str] = mapped_column(String(10), nullable=False)

    # ── What is being moved ───────────────────────────────────────────────
    package_weight_kg: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    package_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Matches partnerServices' VehicleType vocabulary — it is what Dispatch
    # filters candidates on.
    vehicle_type: Mapped[str] = mapped_column(String(20), nullable=False)

    # ── Fare snapshot ─────────────────────────────────────────────────────
    distance_km: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    quoted_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="INR", server_default="INR"
    )
    # A quote is a price at a moment; past this it must be re-quoted rather than
    # silently honoured.
    quote_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Owned by Payment. Held here only so support can join the two by hand.
    payment_intent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ── Partner snapshot, written on partner.assigned ─────────────────────
    partner_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    partner_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    partner_phone: Mapped[str | None] = mapped_column(String(10), nullable=True)
    vehicle_number: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Client-supplied. A customer on a flaky connection taps Confirm twice, and
    # the second request must return the first order rather than create another.
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)

    # ── When each thing happened ──────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        server_default=func.now(),
        onupdate=func.now(),
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    picked_up_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # One order per (customer, idempotency key). This is what makes the
        # retry safe under a race, rather than only under a sequence.
        UniqueConstraint(
            "customer_id", "idempotency_key", name="uq_orders_idempotency"
        ),
        CheckConstraint(f"status IN ({STATUS_VALUES})", name="ck_orders_status"),
        CheckConstraint("package_weight_kg > 0", name="ck_orders_weight_positive"),
        CheckConstraint(
            "pickup_latitude BETWEEN -90 AND 90 AND drop_latitude BETWEEN -90 AND 90",
            name="ck_orders_latitude_range",
        ),
        CheckConstraint(
            "pickup_longitude BETWEEN -180 AND 180"
            " AND drop_longitude BETWEEN -180 AND 180",
            name="ck_orders_longitude_range",
        ),
        # The customer's order list, which is the most frequent read in the app.
        Index("ix_orders_customer_recent", "customer_id", "created_at"),
    )
