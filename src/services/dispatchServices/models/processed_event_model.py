from src.common.events.models import ProcessedEventMixin
from src.services.dispatchServices.database.base import Base


class ProcessedEvent(ProcessedEventMixin, Base):
    """Events dispatch_db has already acted on.

    Load-bearing here in a way it is not everywhere. A redelivered
    `order.confirmed` that got past this table would start a second search for a
    delivery that already has one — and the unique constraint on
    `dispatch_attempts.order_id` would then turn a duplicate message into a
    handler that fails, retries, and lands in the DLQ.

    Shape and reasoning: `src/common/events/models.py`.
    """
