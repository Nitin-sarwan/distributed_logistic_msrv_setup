from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.services.partnerServices.database.base import Base
from src.services.partnerServices.utils.enums import PartnerStatus


class Partner(Base):
    """A delivery partner — the person who moves the goods.

    This table answers "who are the delivery partners?" and nothing else. Which
    partner should get a particular order is Dispatch's question; the only thing
    this service owes it is an honest answer about who is verified, free, and
    nearby.
    """

    __tablename__ = "partners"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # ── Identity ──────────────────────────────────────────────────────────
    #
    # Phone is the login identity, not email. A driver signs into the partner
    # app with the number their SIM already has; many will never give an email
    # at all, which is why email is nullable here and required in userServices.

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    phone: Mapped[str] = mapped_column(
        String(10), nullable=False, unique=True, index=True
    )
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)

    password_hash: Mapped[str] = mapped_column(Text, nullable=False)

    # Per-partner token key — not part of the profile, but the column the whole
    # auth scheme rests on. Access tokens are AES-encrypted with it, so rotating
    # this one row's value invalidates only this partner's tokens, and a
    # `partners` table stolen without `.env` cannot be used to mint any.
    token_secret: Mapped[str] = mapped_column(Text, nullable=False)

    # ── Working state ─────────────────────────────────────────────────────

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=PartnerStatus.OFFLINE.value,
        server_default=PartnerStatus.OFFLINE.value,
    )

    # KYC cleared by operations. The single gate on receiving work: a partner
    # can register, add a vehicle and go online, and still appear to nobody
    # until this is true.
    is_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # ── Live location ─────────────────────────────────────────────────────
    #
    # NUMERIC(9,6) matches the `address` table in userServices — six decimal
    # places is roughly 10cm, the precision a driver's pin actually needs.
    #
    # Nullable because a partner who has never opened the app has no position.
    # `location_updated_at` is what makes the pair trustworthy: coordinates with
    # no timestamp cannot be told apart from coordinates recorded last Tuesday,
    # and Dispatch would route to a phone that has been in a tunnel for an hour.

    current_latitude: Mapped[Decimal | None] = mapped_column(
        Numeric(9, 6), nullable=True
    )
    current_longitude: Mapped[Decimal | None] = mapped_column(
        Numeric(9, 6), nullable=True
    )
    location_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Reputation ────────────────────────────────────────────────────────

    rating: Mapped[Decimal] = mapped_column(
        Numeric(2, 1), nullable=False, default=Decimal("5.0"), server_default="5.0"
    )

    # The sample count behind `rating`. Not decoration: without it the average
    # cannot be updated at all — a new score has to be weighted against how many
    # came before, and recomputing from a ratings table this service does not
    # own is not an option. It is also the honest number, since 5.0 from two
    # trips and 5.0 from two thousand are not the same claim.
    rating_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # ── Bookkeeping ───────────────────────────────────────────────────────

    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
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
        # The vocabulary from utils/enums.py, enforced by the database as well
        # as by Pydantic. Validation at the edge protects the API; the
        # constraint protects the table from a migration, a backfill, or a psql
        # session that never goes through the API at all.
        CheckConstraint(
            "status IN ('offline','online','on_trip','suspended')",
            name="ck_partners_status",
        ),
        CheckConstraint("rating >= 0 AND rating <= 5", name="ck_partners_rating_range"),
        CheckConstraint("rating_count >= 0", name="ck_partners_rating_count"),
        CheckConstraint(
            "current_latitude IS NULL OR (current_latitude BETWEEN -90 AND 90)",
            name="ck_partners_latitude_range",
        ),
        CheckConstraint(
            "current_longitude IS NULL OR (current_longitude BETWEEN -180 AND 180)",
            name="ck_partners_longitude_range",
        ),
        # The Dispatch query in one index: it filters status, then verification,
        # then a latitude/longitude bounding box. Leading with the two
        # low-cardinality columns is right here because they are extremely
        # selective in practice — most rows are offline at any moment.
        Index(
            "ix_partners_availability",
            "status",
            "is_verified",
            "current_latitude",
            "current_longitude",
        ),
    )
