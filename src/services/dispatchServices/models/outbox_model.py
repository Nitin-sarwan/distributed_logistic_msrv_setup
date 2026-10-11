from src.common.events.models import OutboxMixin
from src.services.dispatchServices.database.base import Base


class Outbox(OutboxMixin, Base):
    """dispatch_db's copy of the outbox.

    Columns, index and reasoning: `src/common/events/models.py`.

    Dispatch is a producer as well as a consumer, and every event it emits goes
    through here — including the ones emitted from inside a Kafka handler. That
    is the point of the pattern being on both sides: consuming
    `order.cancelled`, releasing the partner and queueing `partner.released` is
    one transaction, so a crash between them is impossible.
    """
