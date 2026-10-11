"""Operations reads.

Mounted at `/internal/notifications`, deliberately outside `/api`. The gateway
routes `/api/*` and nothing else, so these paths do not exist as far as the
public internet is concerned. `require_internal_key` is the second lock, for
anything that can reach the service port directly.

There is deliberately **no endpoint here that sends a notification.** It is the
obvious thing to add and it would be the single worst change anyone could make
to this service: a service that can be told to send a message is a service that
contains none of the reasons messages are sent, and the moment one exists, every
other service starts calling it instead of emitting an event. Notification's
whole value is that it reads facts and decides what they mean to a person.
"""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from src.services.notificationServices.api.dependencies import require_internal_key
from src.services.notificationServices.api.schema import NotificationResponse
from src.services.notificationServices.database.connection import get_db
from src.services.notificationServices.models.notification_model import Notification
from sqlalchemy import select

router = APIRouter(
    prefix="/internal/notifications",
    tags=["internal"],
    # Applied to every route in the file rather than to each one, so a new
    # endpoint is protected by default instead of by memory.
    dependencies=[Depends(require_internal_key)],
)


@router.get(
    "/orders/{order_id}",
    response_model=list[NotificationResponse],
    status_code=status.HTTP_200_OK,
)
def for_order(
    order_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Everything anybody was told about one delivery, oldest first.

    The support answer, and the reason `order_id` is a column rather than
    something to be parsed back out of `action_path`. When a customer says "I
    was never told my order was cancelled", this is the call that settles it -
    including which channels were attempted.

    Unscoped by recipient: it deliberately returns both the customer's messages
    and the driver's, because the question being asked is about the delivery
    rather than about a person. That is exactly why it is not reachable from the
    gateway.
    """
    rows = db.scalars(
        select(Notification)
        .where(Notification.order_id == order_id)
        .order_by(Notification.created_at, Notification.id)
        .limit(limit)
    )
    return [NotificationResponse.model_validate(row) for row in rows]
