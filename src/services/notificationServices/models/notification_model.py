from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.services.notificationServices.database.base import Base
from src.services.notificationServices.domain.vocabulary import Category, RecipientType

RECIPIENT_VALUES = ", ".join(f"'{value.value}'" for value in RecipientType)
CATEGORY_VALUES = ", ".join(f"'{value.value}'" for value in Category)


class Notification(Base):
    """One message, to one person, as they will read it.

    The title and body are **rendered at write time and stored as text**, not
    assembled from a template id and a JSON blob when the feed is read. That is
    the important decision in this table and it is worth defending: a message is
    a record of what somebody was actually told. Change a template next month
    and every historical notification rendered lazily would silently change with
    it, so a customer and a support agent looking at the same row would read
    different things.

    The cost is that a typo in a template cannot be retroactively fixed. That is
    the correct cost.

    `recipient_type` + `recipient_id` is a pair, always. Customer 5 and partner
    5 are different people - each service numbers its own subjects from 1 - so
    every index, every query and every preference lookup carries both.
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    recipient_type: Mapped[str] = mapped_column(String(16), nullable=False)
    recipient_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    category: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(150), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    # A route in the app, not a URL: the two front ends live at different
    # origins and a hardcoded one would be broken in the other.
    action_path: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # ── Provenance ────────────────────────────────────────────────────────
    #
    # Which event produced this, and about which order. Both are what make a
    # support question answerable: "why did they get this message?" is a lookup
    # rather than a reconstruction.
    event_id: Mapped[str] = mapped_column(Uuid, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    order_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Which outbound channels were attempted, comma separated, e.g. "push,email".
    # A record of intent rather than of receipt: the workers behind these are
    # logging stubs, and even when they are real, "handed to the queue" is the
    # most this service can honestly claim to know.
    channels: Mapped[str | None] = mapped_column(String(64), nullable=True)

    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            f"recipient_type IN ({RECIPIENT_VALUES})",
            name="ck_notifications_recipient_type",
        ),
        CheckConstraint(
            f"category IN ({CATEGORY_VALUES})", name="ck_notifications_category"
        ),
        # **The deduplication that matters most.** One event can produce several
        # messages - `partner.assigned` tells the customer and the driver - so
        # `event_id` alone is not unique. The triple is: one event says one
        # thing to one person once.
        #
        # `processed_events` already stops a redelivery reaching the handler.
        # This is the second line, and it is here because the visible symptom of
        # the first one failing is a person's phone buzzing twice.
        UniqueConstraint(
            "event_id",
            "recipient_type",
            "recipient_id",
            name="uq_notifications_event_recipient",
        ),
        # The feed: this person's messages, newest first. The only read the app
        # makes on any screen.
        Index(
            "ix_notifications_feed",
            "recipient_type",
            "recipient_id",
            "created_at",
        ),
        # The badge count, polled far more often than the feed is opened.
        # Partial, so it stays the size of what is unread rather than the size
        # of everything ever sent.
        Index(
            "ix_notifications_unread",
            "recipient_type",
            "recipient_id",
            postgresql_where=text("read_at IS NULL"),
        ),
        Index("ix_notifications_order", "order_id"),
    )
