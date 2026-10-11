from src.common.events.models import ProcessedEventMixin
from src.services.orderServices.database.base import Base


class ProcessedEvent(ProcessedEventMixin, Base):
    """Events order_db has already acted on.

    No longer empty: Order consumes `partner.assigned`, `dispatch.search_started`
    and `dispatch.search_failed` from Dispatch. Every one of those handlers
    writes its row here in the same transaction as the state change, which is
    what makes a redelivery a no-op instead of a second assignment.

    Shape and reasoning: `src/common/events/models.py`.
    """
