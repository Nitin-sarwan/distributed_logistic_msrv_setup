from src.common.events.models import OutboxMixin
from src.services.orderServices.database.base import Base


class Outbox(OutboxMixin, Base):
    """order_db's copy of the outbox.

    The columns, the partial index and the reasoning all live in
    `src/common/events/models.py`. Three services now run an outbox and the
    relay is written once against these exact column names, so the table cannot
    be allowed to drift between them - which is what a per-service definition
    would eventually do.

    It is still a table in *this* database, not a shared one. Same shape,
    separate rows: the outbox row is written in the same transaction as the
    order it describes, and that transaction can only span one database.
    """
