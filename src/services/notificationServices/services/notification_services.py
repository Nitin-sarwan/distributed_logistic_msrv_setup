"""Turning one event into the messages people actually receive.

The sequence, and every step of it is a decision this service owns:

    event -> templates.render()   what to say, to whom, on which channels
          -> preference check     who asked not to hear it
          -> store the row        the feed, and the permanent record
          -> hand to channels     push and email, out to their workers

What is *not* here is any judgement about whether the underlying thing happened
or mattered. `order.delivered` is a fact from the service that owns orders. This
file decides that a customer sees "Delivered" with a receipt by email and the
driver sees a line in their feed - and nothing else in the platform gets a vote
on that.

Transactions follow the same rule as everywhere else in the event flow:
`apply_*` stages and does not commit, because a Kafka handler's work and its
`processed_events` marker have to land together.
"""

import logging
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.services.notificationServices.channels import (
    EmailChannel,
    InAppChannel,
    PushChannel,
)
from src.services.notificationServices.channels.base import Channel as ChannelImpl
from src.services.notificationServices.config import settings
from src.services.notificationServices.domain.templates import Message, render
from src.services.notificationServices.domain.vocabulary import (
    MUTABLE_CATEGORIES,
    Category,
    Channel,
    RecipientType,
)
from src.services.notificationServices.models.notification_model import Notification
from src.services.notificationServices.repositories.notification_repositories import (
    NotificationRepository,
)

logger = logging.getLogger(__name__)

# Built once at import. They are stateless - the stubs log, and the real
# versions will publish to a queue - so a fresh instance per message would be
# allocation for nothing.
CHANNELS: dict[str, ChannelImpl] = {
    Channel.IN_APP.value: InAppChannel(),
    Channel.EMAIL.value: EmailChannel(),
    Channel.PUSH.value: PushChannel(),
}

# Which of the config switches gates which channel. IN_APP is absent because it
# cannot be switched off: it is the stored row.
GLOBAL_SWITCHES: dict[str, str] = {
    Channel.EMAIL.value: "email_enabled",
    Channel.PUSH.value: "push_enabled",
}


class NotificationService:
    def __init__(self, db: Session):
        self.db = db
        self.repository = NotificationRepository(db)

    # ── What events cause ─────────────────────────────────────────────────

    def apply_event(self, envelope: dict[str, Any]) -> list[Notification]:
        """Render one event into stored messages and hand them to channels.

        Stages; does not commit. Returns what was written, which the consumer
        does nothing with and a test does everything with.

        An event with no template produces an empty list and that is a normal
        outcome, not a gap: every service reads the whole topic and most of what
        arrives is not something to tell a person about.
        """
        event_type = envelope.get("event_type", "")
        data = envelope.get("data") or {}

        messages = render(event_type, data)
        if not messages:
            return []

        written: list[Notification] = []
        for message in messages:
            notification = self._store(message, envelope, data)
            if notification is not None:
                written.append(notification)

        return written

    def _store(
        self, message: Message, envelope: dict[str, Any], data: dict[str, Any]
    ) -> Notification | None:
        """Write one message and dispatch its outbound channels.

        The row is written **before** the channels are called, and that order
        matters: a push worker that is handed a job referencing a notification
        id must be able to find it. It also means a channel that throws leaves
        the message in the feed rather than losing it entirely.
        """
        allowed = self._allowed_channels(message)

        notification = Notification(
            recipient_type=message.recipient_type.value,
            recipient_id=message.recipient_id,
            category=message.category.value,
            title=message.title,
            body=message.body,
            action_path=message.action_path,
            event_id=envelope["event_id"],
            event_type=envelope["event_type"],
            order_id=data.get("order_id"),
            correlation_id=envelope.get("correlation_id"),
            channels=",".join(allowed) if allowed else None,
        )

        try:
            # A SAVEPOINT, not a plain flush. One event can produce several
            # messages and they are all written in the consumer's single
            # transaction; a bare `rollback()` on the duplicate below would
            # throw away the siblings that succeeded, so `partner.assigned`
            # failing to re-notify the driver would silently un-notify the
            # customer too.
            #
            # Releasing the savepoint flushes, which is where the id the
            # channels are handed comes from, and where the unique constraint
            # fires if this event already spoke to this person.
            with self.db.begin_nested():
                self.repository.add(notification)
        except IntegrityError:
            # `uq_notifications_event_recipient`. A redelivery that got past
            # `processed_events` - possible if the marker table was reset, or if
            # two consumers in the group briefly overlapped during a rebalance.
            #
            # Nothing is wrong and nothing more should happen: the person was
            # already told. Critically, the channels below are NOT called, which
            # is the entire reason this constraint exists.
            logger.info(
                "already notified %s:%s about event %s",
                message.recipient_type.value,
                message.recipient_id,
                envelope.get("event_id"),
            )
            return None

        self._dispatch(message, notification, allowed)
        return notification

    def _allowed_channels(self, message: Message) -> list[str]:
        """Which channels this message may actually use.

        Three gates, in order of authority:

        1. **The category is muted.** Only IN_APP survives - the record is kept
           whatever happens, because "we never told you" and "you asked us not
           to" are different things and support needs to see which.
        2. **The person switched the channel off.**
        3. **The deployment switched the channel off.** Last, so that turning
           push off globally cannot be mistaken for a personal preference.

        IN_APP passes all three unconditionally. See the preference model.
        """
        preference = self.repository.find_preference(
            message.recipient_type, message.recipient_id
        )

        muted = preference.muted if preference is not None else set()
        # An unmutable category cannot be silenced even if its value somehow
        # ended up in the column - the check lives here as well as at write
        # time, because a stale row from before a category was reclassified
        # would otherwise silence job offers.
        category_muted = (
            message.category.value in muted
            and message.category in MUTABLE_CATEGORIES
        )

        allowed: list[str] = []
        for channel in message.channels:
            if channel == Channel.IN_APP:
                allowed.append(channel.value)
                continue

            if category_muted:
                continue

            if preference is not None and not getattr(
                preference, GLOBAL_SWITCHES[channel.value]
            ):
                continue

            if not getattr(settings, GLOBAL_SWITCHES[channel.value]):
                continue

            allowed.append(channel.value)

        return allowed

    def _dispatch(
        self, message: Message, notification: Notification, allowed: list[str]
    ) -> None:
        """Hand the message to each permitted channel.

        Failures are logged and swallowed. A dead email worker must not roll
        back the consumer's transaction, because a redelivered Kafka message
        replays *every* channel for this event - and the customer would get a
        second push notification because an email bounced.
        """
        for name in allowed:
            channel = CHANNELS.get(name)
            if channel is None:
                continue

            try:
                result = channel.send(message, notification.id)
            except Exception:
                logger.exception(
                    "channel %s failed for notification %s", name, notification.id
                )
                continue

            if not result.delivered:
                logger.warning(
                    "channel %s did not deliver notification %s: %s",
                    name,
                    notification.id,
                    result.detail,
                )

    # ── What people do ────────────────────────────────────────────────────

    def feed(
        self,
        recipient_type: RecipientType,
        recipient_id: int,
        unread_only: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Notification]:
        capped = min(limit or settings.feed_default_limit, settings.feed_max_limit)
        return self.repository.list_for(
            recipient_type, recipient_id, unread_only, capped, offset
        )

    def unread_count(self, recipient_type: RecipientType, recipient_id: int) -> int:
        return self.repository.unread_count(
            recipient_type, recipient_id, settings.unread_count_cap
        )

    def mark_read(
        self, recipient_type: RecipientType, recipient_id: int, notification_id: int
    ) -> bool:
        changed = self.repository.mark_read(
            recipient_type, recipient_id, notification_id
        )
        self.db.commit()
        return changed

    def mark_all_read(self, recipient_type: RecipientType, recipient_id: int) -> int:
        count = self.repository.mark_all_read(recipient_type, recipient_id)
        self.db.commit()
        return count

    # ── Preferences ───────────────────────────────────────────────────────

    def get_preferences(self, recipient_type: RecipientType, recipient_id: int) -> dict:
        """Effective settings, whether or not a row exists.

        Always answers with the full shape rather than 404-ing on somebody who
        has never changed anything - the settings screen needs to render either
        way, and "no row" means "all defaults", not "no such person".
        """
        preference = self.repository.find_preference(recipient_type, recipient_id)

        if preference is None:
            return {
                "email_enabled": True,
                "push_enabled": True,
                "muted_categories": [],
            }

        return {
            "email_enabled": preference.email_enabled,
            "push_enabled": preference.push_enabled,
            "muted_categories": sorted(preference.muted),
        }

    def set_preferences(
        self,
        recipient_type: RecipientType,
        recipient_id: int,
        email_enabled: bool | None = None,
        push_enabled: bool | None = None,
        muted_categories: list[str] | None = None,
    ) -> dict:
        """Partial update. Only what was sent is changed."""
        preference = self.repository.upsert_preference(recipient_type, recipient_id)

        if email_enabled is not None:
            preference.email_enabled = email_enabled
        if push_enabled is not None:
            preference.push_enabled = push_enabled
        if muted_categories is not None:
            # Filtered rather than rejected: a client sending `offer` is asking
            # for something the platform does not offer, and dropping it is
            # kinder than a 422 on a settings toggle. The unmutable set is the
            # authority, not the caller.
            keepable = sorted(
                {
                    value
                    for value in muted_categories
                    if value in {category.value for category in MUTABLE_CATEGORIES}
                }
            )
            preference.muted_categories = ",".join(keepable) if keepable else None

        self.db.commit()
        return self.get_preferences(recipient_type, recipient_id)


def known_categories() -> list[str]:
    """Every category, for the settings screen to render."""
    return [category.value for category in Category]
