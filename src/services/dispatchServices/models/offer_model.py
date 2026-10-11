from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.services.dispatchServices.database.base import Base
from src.services.dispatchServices.domain.states import OfferStatus

STATUS_VALUES = ", ".join(f"'{status.value}'" for status in OfferStatus)


class DispatchOffer(Base):
    """One job put in front of one driver.

    A row is written for **every candidate the search returned**, not only the
    one being asked right now. The rest sit at QUEUED. That is what makes the
    offer loop survive a restart: the ranked list, who has already been asked,
    what they said, and how long the live offer has left are all in the table
    rather than in a variable in a running process.

    It is also the record analytics reads. "How many drivers do we have to ask
    before someone accepts, and in which parts of the city?" is answerable from
    this table alone.

    The partner's name and phone are snapshotted here for the same reason
    everything else in this platform is: they travel onward on `partner.assigned`
    and get copied onto the order, and none of that should require
    partnerServices to be reachable at the moment a customer opens their app.
    """

    __tablename__ = "dispatch_offers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    attempt_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("dispatch_attempts.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Denormalised from the attempt. Every query a driver makes is "what is
    # offered to me?", and carrying the order id here saves joining to answer it.
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # ── Who ───────────────────────────────────────────────────────────────
    partner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    partner_name: Mapped[str] = mapped_column(String(100), nullable=False)
    partner_phone: Mapped[str] = mapped_column(String(10), nullable=False)
    vehicle_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    vehicle_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    vehicle_type: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # How far the driver is from the pickup, at the moment the search ran. The
    # ranking key, and the number shown on the offer card.
    distance_km: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)

    # Position in the ranked list, 1-based. Kept explicitly rather than derived
    # from the ordering, because the ordering is what analytics wants to ask
    # about: does the third-nearest driver accept as often as the nearest?
    rank: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=OfferStatus.QUEUED.value,
        server_default=OfferStatus.QUEUED.value,
    )

    # ── When ──────────────────────────────────────────────────────────────
    #
    # Null while QUEUED. Both are set at the moment the offer goes live.
    offered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The countdown on the driver's screen, as an absolute instant. The worker
    # expires the offer past this; the accept endpoint refuses past it too, so
    # a driver who taps as the timer hits zero cannot beat the sweep by a
    # fraction of a second and take a job already offered to someone else.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    responded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            f"status IN ({STATUS_VALUES})", name="ck_dispatch_offers_status"
        ),
        # A driver is asked about one order at most once. Without this, a search
        # that widens its radius and re-runs would re-offer the same job to
        # somebody who has already declined it.
        UniqueConstraint(
            "attempt_id", "partner_id", name="uq_dispatch_offers_attempt_partner"
        ),
        # **At most one live offer per attempt.** This is the constraint that
        # makes the loop a loop rather than a broadcast: two drivers must never
        # be looking at the same job, because both could accept and only one
        # claim can win.
        #
        # A partial unique index is the only way to say "unique among rows in
        # this state" in Postgres — a plain UNIQUE(attempt_id) would forbid the
        # queue, and a check in Python would be one concurrent tick away from
        # being wrong.
        Index(
            "uq_dispatch_offers_one_live",
            "attempt_id",
            unique=True,
            postgresql_where=text(f"status = '{OfferStatus.OFFERED.value}'"),
        ),
        # The driver's own query: "is anything offered to me right now?"
        Index(
            "ix_dispatch_offers_partner_live",
            "partner_id",
            postgresql_where=text(f"status = '{OfferStatus.OFFERED.value}'"),
        ),
        Index("ix_dispatch_offers_attempt", "attempt_id", "rank"),
        Index("ix_dispatch_offers_order", "order_id"),
    )
