"""Data access for notifications and preferences. Holds no business rules.

Every read is scoped by the `(recipient_type, recipient_id)` pair in the WHERE
clause rather than fetched and checked afterwards. That ordering is the point:
a query that cannot return somebody else's messages makes the ownership check
impossible to forget - and here the pair matters as much as the id, because
customer 5 and partner 5 are different people.
"""

from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from src.services.notificationServices.domain.vocabulary import RecipientType
from src.services.notificationServices.models.notification_model import Notification
from src.services.notificationServices.models.preference_model import (
    NotificationPreference,
)


class NotificationRepository:
    def __init__(self, db: Session):
        self.db = db

    # ── Feed ──────────────────────────────────────────────────────────────

    def list_for(
        self,
        recipient_type: RecipientType,
        recipient_id: int,
        unread_only: bool = False,
        limit: int = 30,
        offset: int = 0,
    ) -> list[Notification]:
        query = select(Notification).where(
            Notification.recipient_type == recipient_type.value,
            Notification.recipient_id == recipient_id,
        )
        if unread_only:
            query = query.where(Notification.read_at.is_(None))

        return list(
            self.db.scalars(
                # Newest first: a feed is read to find out what just happened.
                # `id` breaks ties, because several messages from one event land
                # in the same transaction and therefore share a timestamp.
                query.order_by(Notification.created_at.desc(), Notification.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )

    def unread_count(
        self, recipient_type: RecipientType, recipient_id: int, cap: int = 99
    ) -> int:
        """How many unread, counted no further than `cap`.

        The badge is polled constantly and "99+" is what the app shows past the
        cap anyway, so counting the whole backlog of a neglected account every
        few seconds would be work done to be thrown away. The subquery limit is
        what turns an unbounded count into a bounded one.
        """
        capped = (
            select(Notification.id)
            .where(
                Notification.recipient_type == recipient_type.value,
                Notification.recipient_id == recipient_id,
                Notification.read_at.is_(None),
            )
            .limit(cap + 1)
            .subquery()
        )
        return self.db.scalar(select(func.count()).select_from(capped)) or 0

    def mark_read(
        self, recipient_type: RecipientType, recipient_id: int, notification_id: int
    ) -> bool:
        """Mark one as read. False if it is not this person's.

        Ownership is in the WHERE clause, so the update simply affects no rows
        rather than needing a fetch-then-compare that could be skipped.

        Already-read rows keep their original timestamp - `read_at IS NULL` in
        the predicate - because when somebody first saw a message is a fact, and
        a second tap should not rewrite it.
        """
        result = self.db.execute(
            update(Notification)
            .where(
                Notification.id == notification_id,
                Notification.recipient_type == recipient_type.value,
                Notification.recipient_id == recipient_id,
                Notification.read_at.is_(None),
            )
            .values(read_at=datetime.now(timezone.utc))
        )
        return result.rowcount > 0

    def mark_all_read(self, recipient_type: RecipientType, recipient_id: int) -> int:
        result = self.db.execute(
            update(Notification)
            .where(
                Notification.recipient_type == recipient_type.value,
                Notification.recipient_id == recipient_id,
                Notification.read_at.is_(None),
            )
            .values(read_at=datetime.now(timezone.utc))
        )
        return result.rowcount

    def add(self, notification: Notification) -> None:
        """Stage a notification. The caller commits."""
        self.db.add(notification)

    # ── Preferences ───────────────────────────────────────────────────────

    def find_preference(
        self, recipient_type: RecipientType, recipient_id: int
    ) -> NotificationPreference | None:
        """This person's settings, or None if they have never changed any.

        None is the common case and is not a missing row to be filled in: a
        person who has not opted out of anything gets everything, and creating
        a row at registration would mean this table grew with the user base
        rather than with the number of decisions made.
        """
        return self.db.scalar(
            select(NotificationPreference).where(
                NotificationPreference.recipient_type == recipient_type.value,
                NotificationPreference.recipient_id == recipient_id,
            )
        )

    def upsert_preference(
        self, recipient_type: RecipientType, recipient_id: int
    ) -> NotificationPreference:
        """The row to edit, creating it on first use. Caller commits."""
        preference = self.find_preference(recipient_type, recipient_id)
        if preference is not None:
            return preference

        preference = NotificationPreference(
            recipient_type=recipient_type.value,
            recipient_id=recipient_id,
        )
        self.db.add(preference)
        self.db.flush()
        return preference
