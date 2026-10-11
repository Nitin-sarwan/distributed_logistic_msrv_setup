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
    text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.services.dispatchServices.database.base import Base
from src.services.dispatchServices.domain.states import AttemptStatus

STATUS_VALUES = ", ".join(f"'{status.value}'" for status in AttemptStatus)


class DispatchAttempt(Base):
    """One order's search for a driver.

    Created when `order.confirmed` arrives and never deleted. It is the answer
    to "why did this delivery take eleven minutes to get a driver?", which is a
    question asked of Dispatch and of nothing else.

    **Everything the search needs is copied here from the event.** Dispatch does
    not call Order back for the pickup coordinates, the weight, or the fare — an
    event that carries them is what lets the two services be independently
    restartable. The corollary is that these values are a snapshot: if the
    customer somehow changed the order, this row still describes the search that
    actually ran.

    `order_id` is unique, and that is the idempotency guarantee. A redelivered
    `order.confirmed` — certain, under at-least-once — cannot start a second
    search for the same delivery.
    """

    __tablename__ = "dispatch_attempts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Unique: one search per order, enforced by the database rather than by a
    # check in the handler that a concurrent redelivery could race past.
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)

    # Shown in the driver's offer card and in the ops console. Copied so neither
    # has to call Order to render a screen.
    public_ref: Mapped[str] = mapped_column(String(12), nullable=False)
    customer_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=AttemptStatus.SEARCHING.value,
        server_default=AttemptStatus.SEARCHING.value,
    )

    # ── What the search matches on ────────────────────────────────────────
    pickup_latitude: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)
    pickup_longitude: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)
    pickup_city: Mapped[str | None] = mapped_column(String(255), nullable=True)
    drop_city: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vehicle_type: Mapped[str] = mapped_column(String(20), nullable=False)
    package_weight_kg: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    distance_km: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)

    # What the job pays. Not Dispatch's business except that the driver is shown
    # it — an offer without a number on it is one no sensible person accepts.
    amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="INR", server_default="INR"
    )

    # ── How the search is going ───────────────────────────────────────────
    #
    # Widens on an empty result, up to SEARCH_MAX_RADIUS_KM.
    radius_km: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    offers_made: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )

    # When to stop trying. Absolute rather than a duration, so a restart does
    # not hand the search a fresh clock.
    deadline_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Set when an availability search comes back empty: do not ask again before
    # this. Without it, an order placed at 4am re-queries partnerServices every
    # tick for three minutes.
    retry_search_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── How it ended ──────────────────────────────────────────────────────
    assigned_partner_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    assigned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # One of domain.states.SearchFailureReason. Kept as text rather than a
    # constrained column because it is diagnostic, not a state.
    failure_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The gateway's request id, carried in from the event envelope and back out
    # on every event this attempt emits. One delivery touches six services;
    # without it, tracing a bad dispatch means reading timestamps.
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

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
            f"status IN ({STATUS_VALUES})", name="ck_dispatch_attempts_status"
        ),
        # The worker's only query: everything still searching, oldest first.
        # Partial, because the terminal rows are the ones that accumulate and
        # the loop never looks at them again — so this index stays the size of
        # the live workload rather than the size of the table.
        Index(
            "ix_dispatch_attempts_searching",
            "created_at",
            postgresql_where=text(f"status = '{AttemptStatus.SEARCHING.value}'"),
        ),
        Index("ix_dispatch_attempts_partner", "assigned_partner_id"),
    )
