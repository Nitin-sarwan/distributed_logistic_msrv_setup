from src.common.events.models import ProcessedEventMixin
from src.services.notificationServices.database.base import Base


class ProcessedEvent(ProcessedEventMixin, Base):
    """Events notification_db has already acted on.

    The table that stops a customer being told twice that their driver is on the
    way. Delivery is at-least-once, so without it every redelivery is a second
    push notification - the most visible possible symptom of a missing
    deduplication check, and the reason this table is created with the service
    rather than added when someone complains.

    Note there is no outbox here. Notification is a terminal consumer: it reads
    domain facts and produces messages, and produces no events of its own.

    Shape and reasoning: `src/common/events/models.py`.
    """
