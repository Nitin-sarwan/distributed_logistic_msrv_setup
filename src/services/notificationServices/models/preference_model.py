from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.services.notificationServices.database.base import Base
from src.services.notificationServices.domain.vocabulary import RecipientType

RECIPIENT_VALUES = ", ".join(f"'{value.value}'" for value in RecipientType)


class NotificationPreference(Base):
    """Who asked not to be told, and how.

    The blueprint gives Notification one thing to decide that nobody else may:
    *which channel and template, and who has opted out*. This table is the
    second half of that. Without it, "opted out" would have to live in
    userServices or partnerServices, and every service that emitted an event
    would need to know about consent.

    **A missing row means every default.** Preferences are created on the first
    change, never on registration, so a person who has never opened the settings
    screen has no row and gets everything. That is the correct default and it
    also means this table stays small - it holds decisions, not people.

    In-app is absent from these switches by design. It is the stored row itself:
    turning it off would not mean "do not tell me", it would mean "keep no
    record of what you told me", which nobody wants and support cannot work
    without.
    """

    __tablename__ = "notification_preferences"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    recipient_type: Mapped[str] = mapped_column(String(16), nullable=False)
    recipient_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    email_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    push_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # Comma-separated category values, e.g. "payment,account". A text column
    # rather than a join table because it is read on every single message and
    # written about twice in an account's lifetime - and because the set it
    # draws from has four members, three of which are mutable.
    #
    # OFFER can never appear here: `MUTABLE_CATEGORIES` excludes it, and a
    # driver who silenced job offers would be on duty and unreachable.
    muted_categories: Mapped[str | None] = mapped_column(String(200), nullable=True)

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
            f"recipient_type IN ({RECIPIENT_VALUES})",
            name="ck_notification_preferences_recipient_type",
        ),
        # One row per person. The pair, not the id: customer 5 and partner 5 are
        # different people.
        UniqueConstraint(
            "recipient_type",
            "recipient_id",
            name="uq_notification_preferences_recipient",
        ),
    )

    # ── Read helpers ──────────────────────────────────────────────────────

    @property
    def muted(self) -> set[str]:
        if not self.muted_categories:
            return set()
        return {part.strip() for part in self.muted_categories.split(",") if part.strip()}
