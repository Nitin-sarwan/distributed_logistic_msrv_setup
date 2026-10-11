from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.services.pricingServices.database.base import Base


class Quote(Base):
    """A binding price, issued to Order for one delivery.

    Estimates are not stored — they are arithmetic on public inputs, asked for
    on every keystroke of the booking form. A quote is different: it is the
    number the customer agrees to pay, so it is written down with everything it
    was computed from, and Order keeps its id.

    Every amount is a snapshot. The tariff id is kept so an operator can see
    which rate card a quote came from, but nothing recomputes from it.
    """

    __tablename__ = "quotes"

    # A UUID, not a sequence. Order holds it, support quotes it to customers,
    # and a guessable id would let anyone count how many deliveries we price.
    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    # Who asked, if anyone. Bare integer for the usual reason: a real foreign
    # key would need user_db and pricing_db to be one database.
    customer_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    tariff_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tariffs.id"), nullable=False
    )
    vehicle_type: Mapped[str] = mapped_column(String(20), nullable=False)

    pickup_latitude: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)
    pickup_longitude: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)
    drop_latitude: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)
    drop_longitude: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)
    weight_kg: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    straight_line_km: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    distance_km: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)

    base_fare: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    distance_charge: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    weight_charge: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    minimum_fare_adjustment: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # "What have we quoted this customer?" — the one support question this
        # table is asked by anything other than its primary key.
        Index("ix_quotes_customer_created", "customer_id", "created_at"),
    )
