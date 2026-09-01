from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from src.services.orderServices.database.base import Base


class OrderStatusHistory(Base):
    """Every transition an order made, and what caused it.

    Not derivable from the order row: `orders.status` holds the current value
    and nothing else, so without this table "when did it get assigned, and did
    it bounce back to searching first?" is unanswerable — which is exactly the
    question asked when a delivery goes wrong.

    `caused_by_event_id` is the link back to the event that triggered the move,
    which is what makes a support investigation a join rather than a guess.
    """

    __tablename__ = "order_status_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("orders.id"), nullable=False, index=True
    )

    # Null for the first row: nothing preceded CREATED.
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)

    # Null when a person did it rather than an event.
    caused_by_event_id: Mapped[str | None] = mapped_column(Uuid, nullable=True)

    # customer | partner | dispatch | payment | system
    actor: Mapped[str] = mapped_column(String(20), nullable=False)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
