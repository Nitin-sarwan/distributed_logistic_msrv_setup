from src.common.events.models import ProcessedEventMixin
from src.services.paymentServices.database.base import Base


class ProcessedEvent(ProcessedEventMixin, Base):
    """Events payment_db has already acted on. Shape: src/common/events/models.py."""
