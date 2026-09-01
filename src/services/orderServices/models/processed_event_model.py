from datetime import datetime

from sqlalchemy import DateTime, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from src.services.orderServices.database.base import Base


class ProcessedEvent(Base):
    """Events this service has already acted on.

    Delivery is at-least-once, so duplicates are certain rather than
    hypothetical. A handler checks this table first and writes to it **in the
    same transaction as the work it did** — a separate commit leaves a window
    where the work is done and the marker is not, and the redelivery does it
    twice.

    Empty until the first Kafka consumer lands. It exists now because the shape
    of a handler is decided by it, and retrofitting means revisiting every one.
    """

    __tablename__ = "processed_events"

    event_id: Mapped[str] = mapped_column(Uuid, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
