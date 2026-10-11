"""What Notification does when anything happens. One handler, registered eight times.

Unlike Order and Dispatch, this service has no per-event logic - it has a
template registry. Every handler is the same function, and the difference
between `order.delivered` and `partner.offered` lives in
`domain/templates.py` where a person can read all of it at once.

That uniformity is the design. A service whose job is "say the right thing to
the right person" should have exactly one code path for doing so; the moment one
event gets a special handler, the rules about muting or deduplication start
applying to seven events and not the eighth.

**Notification is a terminal consumer.** It produces no events, has no outbox,
and nothing downstream waits on it. That is what makes it safe to add
subscriptions here freely: a new template is a new message to somebody, never a
change to how a delivery works.
"""

import logging

from sqlalchemy.orm import Session

from src.common.events.consumer import EventConsumer
from src.common.events.types import GROUP_NOTIFICATION
from src.services.notificationServices.database.connection import SessionLocal
from src.services.notificationServices.domain.templates import TEMPLATES
from src.services.notificationServices.models.processed_event_model import ProcessedEvent
from src.services.notificationServices.services.notification_services import (
    NotificationService,
)

logger = logging.getLogger(__name__)


def handle(db: Session, envelope: dict) -> None:
    """Render one event into messages and store them. Does not commit.

    The consumer commits this and the `processed_events` row together, which is
    what makes a redelivery a no-op rather than a second push notification -
    the most visible possible symptom of a missing deduplication check.
    """
    written = NotificationService(db).apply_event(envelope)

    if written:
        logger.info(
            "%s -> %d notification(s): %s",
            envelope.get("event_type"),
            len(written),
            ", ".join(
                f"{note.recipient_type}:{note.recipient_id}" for note in written
            ),
        )


def build_consumer() -> EventConsumer:
    """Notification's consumer, wired from the template registry.

    Subscriptions are derived from `TEMPLATES` rather than listed again here.
    Two lists would be two places to edit, and the failure mode of forgetting
    one is silent: a template nobody subscribes to renders nothing, forever,
    with no error anywhere.
    """
    consumer = EventConsumer(
        group_id=GROUP_NOTIFICATION,
        session_factory=SessionLocal,
        processed_model=ProcessedEvent,
        service_name="notification",
    )

    for event_type in sorted(TEMPLATES):
        consumer.on(event_type, handle)

    return consumer
