from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.services.paymentServices.database.base import Base
from src.services.paymentServices.domain.states import (
    CollectedVia,
    PaymentMethod,
    PaymentStatus,
)


def _values(enum) -> str:
    return ", ".join(f"'{member.value}'" for member in enum)


class Payment(Base):
    """One order's money: how it was meant to arrive, and whether it has.

    One row per order, created when the customer confirms. The amount is a copy
    of the quoted fare taken at that moment — Payment never asks Order or
    Pricing again, which is the snapshot rule applied to money.

    `order_id`, `customer_id` and `partner_id` are bare integers: they live in
    other services' databases.
    """

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    public_ref: Mapped[str] = mapped_column(String(12), nullable=False)
    customer_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    # Learned from `partner.assigned`. Who may collect, and who did.
    partner_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)

    method: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    collected_via: Mapped[str | None] = mapped_column(String(10), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(f"method IN ({_values(PaymentMethod)})", name="ck_payments_method"),
        CheckConstraint(f"status IN ({_values(PaymentStatus)})", name="ck_payments_status"),
        CheckConstraint(
            f"collected_via IS NULL OR collected_via IN ({_values(CollectedVia)})",
            name="ck_payments_collected_via",
        ),
        CheckConstraint("amount >= 0", name="ck_payments_amount"),
        # Paid means we know how. A PAID row with no `collected_via` is a
        # payment nobody can account for.
        CheckConstraint(
            "status <> 'paid' OR (collected_via IS NOT NULL AND paid_at IS NOT NULL)",
            name="ck_payments_paid_has_how",
        ),
    )
