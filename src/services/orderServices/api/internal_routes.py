"""Service-to-service reads.

Mounted at `/internal/orders`, outside `/api`. The gateway routes `/api/*` and
nothing else, so these paths do not exist as far as the public internet is
concerned — the same arrangement partnerServices uses for the availability
search Dispatch will call.

Dispatch and Notification are the callers: both are handed an order id in an
event and sometimes need more than the event carried.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from src.services.orderServices.api.dependencies import require_internal_key
from pydantic import BaseModel, Field

from src.services.orderServices.api.schema import OrderResponse, to_order_response
from src.services.orderServices.database.connection import get_db
from src.services.orderServices.repositories.order_repositories import OrderRepository
from src.services.orderServices.services.order_services import DeliveryService
from src.services.orderServices.utils.exceptions import (
    InvalidTransitionError,
    OrderNotFoundError,
)

router = APIRouter(
    prefix="/internal/orders",
    tags=["internal"],
    # Applied to every route in the file rather than to each one, so a new
    # endpoint is protected by default instead of by memory.
    dependencies=[Depends(require_internal_key)],
)


@router.get("/{order_id}", response_model=OrderResponse)
def get_order(order_id: int, db: Session = Depends(get_db)):
    """One order, unscoped by customer.

    The caller is a service, not a person, so there is no session to scope by —
    which is exactly why this route is unreachable from the gateway.
    """
    order = OrderRepository(db).find_by_id(order_id)
    if order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Order not found"
        )
    return to_order_response(order)


class AssignPartner(BaseModel):
    """Dispatch's decision, as a body.

    Every field except the id is a **snapshot** the customer will see for the
    rest of the order's life. Dispatch sends them because it has just read them
    from partnerServices; Order copies them so that showing "your driver is on
    the way" never needs a second service to be up.
    """

    partner_id: int
    partner_name: str = Field(min_length=1, max_length=100)
    partner_phone: str = Field(min_length=10, max_length=10)
    vehicle_number: str | None = Field(default=None, max_length=20)
    # The `partner.assigned` event this came from, recorded on the history row.
    event_id: str | None = None


@router.post("/{order_id}/assignment", response_model=OrderResponse)
def assign_partner(
    order_id: int,
    payload: AssignPartner,
    db: Session = Depends(get_db),
):
    """Hand an order to a driver.

    **This is the stand-in for consuming `partner.assigned`.** Dispatch does not
    exist yet and neither does Kafka, so the assignment arrives as a call
    instead of an event — but it lands in the same service method the consumer
    will call, so nothing about it changes shape when the broker arrives.

    Internal-only, like everything in this file: a driver cannot assign work to
    themselves, and a customer cannot choose their driver.
    """
    try:
        order = DeliveryService(db).assign_partner(
            order_id=order_id,
            partner_id=payload.partner_id,
            partner_name=payload.partner_name,
            partner_phone=payload.partner_phone,
            vehicle_number=payload.vehicle_number,
            event_id=payload.event_id,
        )
    except OrderNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Order not found"
        ) from error
    except InvalidTransitionError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=error.message
        ) from error

    return to_order_response(order)
