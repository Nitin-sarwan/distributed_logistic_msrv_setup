"""The driver's half of an order.

Mounted at `/api/deliveries`, **a separate gateway prefix from `/api/orders`**,
and that separation is the point rather than a naming choice.

Sessions are per-audience: a customer holds `lp_session` and a partner holds
`lp_partner_session`, so the gateway has to know which cookie carries a route
before it can authenticate one. `/api/orders` is the customer's and
`/api/deliveries` is the driver's, which also means the frontend attaches the
right credential without any per-request cleverness.

The same rows, a different door, a different session, and an ownership check
against `orders.partner_id` rather than `orders.customer_id`.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from src.services.orderServices.api.dependencies import (
    get_correlation_id,
    get_current_partner_id,
)
from src.services.orderServices.api.schema import OrderResponse, to_order_response
from src.services.orderServices.database.connection import get_db
from src.services.orderServices.services.order_services import DeliveryService
from src.services.orderServices.utils.exceptions import (
    InvalidTransitionError,
    OrderNotFoundError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/deliveries", tags=["deliveries"])


def get_delivery_service(db: Session = Depends(get_db)) -> DeliveryService:
    return DeliveryService(db)


NOT_FOUND = "This delivery is not assigned to you"


@router.get("", response_model=list[OrderResponse])
def list_deliveries(
    completed: bool = Query(default=False),
    partner_id: int = Depends(get_current_partner_id),
    service: DeliveryService = Depends(get_delivery_service),
):
    """What this driver is carrying, or has carried.

    Active by default — assigned, picked up, in transit — because that is what a
    driver opens the app to see. `completed=true` is the day's history.
    """
    deliveries = service.list_for_partner(partner_id, completed=completed)
    return [to_order_response(order) for order in deliveries]


@router.post("/{order_id}/picked-up", response_model=OrderResponse)
def mark_picked_up(
    order_id: int,
    partner_id: int = Depends(get_current_partner_id),
    correlation_id: str | None = Depends(get_correlation_id),
    service: DeliveryService = Depends(get_delivery_service),
):
    """Goods collected.

    Authorised twice over: the session must be a partner's, and that partner
    must be the one on this order's assignment snapshot. Neither check involves
    partnerServices.
    """
    try:
        order = service.mark_picked_up(partner_id, order_id, correlation_id)
    except OrderNotFoundError as error:
        # 404 rather than 403: a driver poking at ids should not learn which
        # ones are real deliveries belonging to someone else.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND
        ) from error
    except InvalidTransitionError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=error.message
        ) from error

    return to_order_response(order)


@router.post("/{order_id}/delivered", response_model=OrderResponse)
def mark_delivered(
    order_id: int,
    partner_id: int = Depends(get_current_partner_id),
    correlation_id: str | None = Depends(get_correlation_id),
    service: DeliveryService = Depends(get_delivery_service),
):
    """Handed over.

    The order lands at `delivered`, not `settled`: the goods arriving and the
    money moving are two facts with two owners, and Payment says the second by
    consuming the event this emits.
    """
    try:
        order = service.mark_delivered(partner_id, order_id, correlation_id)
    except OrderNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND
        ) from error
    except InvalidTransitionError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=error.message
        ) from error

    return to_order_response(order)
