from src.common.events.models import OutboxMixin
from src.services.paymentServices.database.base import Base


class Outbox(OutboxMixin, Base):
    """payment_db's copy of the outbox. Columns and reasoning: src/common/events/models.py.

    Capturing a payment and announcing `payment.captured` are one transaction,
    so a crash can never leave money recorded and the order never told.
    """
