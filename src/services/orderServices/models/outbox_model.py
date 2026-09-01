from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.services.orderServices.database.base import Base


class Outbox(Base):
    """Events waiting to reach the broker.

    The service commits to Postgres and publishes to Kafka — two systems, no
    shared transaction. Publishing directly leaves a window where the order
    exists and nobody was told: nothing retries, nothing alerts, and a customer
    waits for a driver who was never requested.

    A row here is written **in the same transaction as the state change it
    describes**, so either both happened or neither did. A separate relay reads
    unpublished rows and pushes them, retrying until it succeeds.

    The cost is at-least-once delivery: a relay can publish and crash before
    marking the row sent. That is why every event carries `event_id` and every
    consumer keeps a processed-events table.
    """

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # What consumers deduplicate on. Generated once, here, and never reused.
    event_id: Mapped[str] = mapped_column(Uuid, nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)

    # The Kafka partition key: the order id. Kafka guarantees ordering only
    # within a partition, so every event for one order must land on the same
    # one, or a consumer will process delivered before picked_up.
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=False)

    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # Partial: the relay only ever asks for unpublished rows, and this keeps
        # that query the same speed on day one and after a million orders.
        Index(
            "ix_outbox_unpublished",
            "id",
            postgresql_where=text("published_at IS NULL"),
        ),
    )
