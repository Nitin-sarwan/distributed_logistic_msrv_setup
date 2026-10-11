"""The two tables every service in the event flow needs, as mixins.

Mixins rather than shared models, because each service owns its own database
and therefore its own `Base`. A single concrete `Outbox` class bound to one Base
could only ever live in one database; these produce an identical table in each.

Identical is the point. The relay in `relay.py` and the deduplication in
`consumer.py` are written once against these column names, and work unchanged
for any service that mixes them in.
"""

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
from sqlalchemy.orm import Mapped, declared_attr, mapped_column


class OutboxMixin:
    """Events waiting to reach the broker.

    A service commits to Postgres and publishes to Kafka — two systems, no
    shared transaction. Publishing directly leaves a window where the state
    change exists and nobody was told: nothing retries, nothing alerts, and a
    customer waits for a driver who was never requested.

    A row here is written **in the same transaction as the state change it
    describes**, so either both happened or neither did. The relay reads
    unpublished rows and pushes them, retrying until it succeeds.

    The cost is at-least-once delivery: a relay can publish and crash before
    marking the row sent. That is why every event carries `event_id` and every
    consumer keeps a processed-events table.
    """

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # What consumers deduplicate on. Generated once, at publish, never reused.
    event_id: Mapped[str] = mapped_column(Uuid, nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)

    # The Kafka partition key: the order id. Kafka guarantees ordering only
    # within a partition, so every event for one order must land on the same
    # one, or a consumer processes delivered before picked_up.
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

    @declared_attr.directive
    def __table_args__(cls):
        # Partial: the relay only ever asks for unpublished rows, and this keeps
        # that query the same speed on day one and after a million orders.
        return (
            Index(
                "ix_outbox_unpublished",
                "id",
                postgresql_where=text("published_at IS NULL"),
            ),
        )


class ProcessedEventMixin:
    """Events this service has already acted on.

    Delivery is at-least-once, so duplicates are certain rather than
    hypothetical. A handler checks this table first and writes to it **in the
    same transaction as the work it did** — a separate commit leaves a window
    where the work is done and the marker is not, and the redelivery does it
    twice.

    Note what is *not* here: an expiry. Rows accumulate, and that is correct
    until the table is large enough to matter — a pruning job that deletes
    anything the broker could still redeliver reintroduces the duplicate it was
    built to stop.
    """

    __tablename__ = "processed_events"

    event_id: Mapped[str] = mapped_column(Uuid, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
