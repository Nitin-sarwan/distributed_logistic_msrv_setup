import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session

from src.services.orderServices.api.dependencies import (
    get_correlation_id,
    get_current_customer_id,
)
from src.services.orderServices.api.schema import (
    CancelOrder,
    CreateOrder,
    OrderEndpoint,
    OrderResponse,
    StatusHistoryEntry,
    to_order_response,
)
from src.services.orderServices.clients.user_client import ResolvedAddress, fetch_address
from src.services.orderServices.database.connection import get_db
from src.services.orderServices.services.order_services import OrderService
from src.services.orderServices.utils.exceptions import (
    AddressNotFoundError,
    AddressServiceUnavailableError,
    InvalidTransitionError,
    OrderNotFoundError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orders", tags=["orders"])


def get_order_service(db: Session = Depends(get_db)) -> OrderService:
    return OrderService(db)


def resolve_endpoint(endpoint: OrderEndpoint, customer_id: int) -> ResolvedAddress:
    """Turn one end of the trip into something snapshottable.

    A saved address is fetched from User — the only synchronous call this
    service makes for a customer's data. An inline address is taken as given:
    the customer typed it, so it *is* the agreement and there is nothing to
    look up. An order with both ends inline therefore needs User not at all.
    """
    if endpoint.address is not None:
        given = endpoint.address
        return ResolvedAddress(
            {
                "address_line1": given.address_line1,
                "address_line2": given.address_line2,
                "city": given.city,
                "pin_code": given.pin_code,
                "latitude": given.latitude,
                "longitude": given.longitude,
            },
            {"name": given.contact_name, "phone": given.contact_phone},
        )

    return fetch_address(customer_id, endpoint.address_id)


@router.post("", response_model=OrderResponse, status_code=status.HTTP_201_CREATED)
def create_order(
    payload: CreateOrder,
    idempotency_key: str = Header(alias="Idempotency-Key", max_length=64),
    customer_id: int = Depends(get_current_customer_id),
    correlation_id: str | None = Depends(get_correlation_id),
    service: OrderService = Depends(get_order_service),
):
    """Quote a delivery and record it.

    `Idempotency-Key` is required, not optional: a customer on a flaky
    connection taps Confirm twice, and the second request must return the first
    order rather than create another.

    Answers 201 with the order at `quoted`. Nothing is charged and no partner is
    sought until `/confirm`.
    """
    try:
        pickup = resolve_endpoint(payload.pickup, customer_id)
        drop = resolve_endpoint(payload.drop, customer_id)
    except AddressNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=error.message
        ) from error
    except AddressServiceUnavailableError as error:
        # 503, not 500: nothing here is broken, an upstream is — and the
        # distinction tells the client it is worth retrying.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=error.message
        ) from error

    order = service.create_order(
        customer_id=customer_id,
        pickup=pickup,
        drop=drop,
        vehicle_type=payload.vehicle_type,
        weight_kg=Decimal(payload.weight_kg),
        description=payload.description,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
    )
    return to_order_response(order)


@router.get("", response_model=list[OrderResponse])
def list_orders(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    customer_id: int = Depends(get_current_customer_id),
    service: OrderService = Depends(get_order_service),
):
    """The caller's own orders, newest first. There is no way to ask for anyone
    else's: the query is scoped by the session's id, not by a parameter."""
    orders = service.list_orders(customer_id, limit, offset)
    return [to_order_response(order) for order in orders]


@router.get("/{order_id}", response_model=OrderResponse)
def get_order(
    order_id: int,
    customer_id: int = Depends(get_current_customer_id),
    service: OrderService = Depends(get_order_service),
):
    try:
        return to_order_response(service.get_order(customer_id, order_id))
    except OrderNotFoundError as error:
        # 404 rather than 403 even when the order exists but belongs to someone
        # else: a 403 confirms the id is real, which the caller has no business
        # learning.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=error.message
        ) from error


@router.get("/{order_id}/history", response_model=list[StatusHistoryEntry])
def get_history(
    order_id: int,
    customer_id: int = Depends(get_current_customer_id),
    service: OrderService = Depends(get_order_service),
):
    """Every transition, for the tracking timeline."""
    try:
        return service.get_history(customer_id, order_id)
    except OrderNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=error.message
        ) from error


@router.post("/{order_id}/confirm", response_model=OrderResponse)
def confirm_order(
    order_id: int,
    customer_id: int = Depends(get_current_customer_id),
    correlation_id: str | None = Depends(get_correlation_id),
    service: OrderService = Depends(get_order_service),
):
    """Accept the fare and pay.

    While Payment is stubbed this returns a `confirmed` order directly. With a
    real provider it will return `payment_pending` and the confirmation will
    arrive as an event — which is why the client must render from the status it
    gets back rather than assuming.
    """
    try:
        order = service.confirm_order(customer_id, order_id, correlation_id)
    except OrderNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=error.message
        ) from error
    except InvalidTransitionError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=error.message
        ) from error

    return to_order_response(order)


@router.post("/{order_id}/cancel", response_model=OrderResponse)
def cancel_order(
    order_id: int,
    payload: CancelOrder,
    customer_id: int = Depends(get_current_customer_id),
    correlation_id: str | None = Depends(get_correlation_id),
    service: OrderService = Depends(get_order_service),
):
    """Call it off, up to pickup.

    Any refund is Payment's to make: this records the cancellation and announces
    it with the state the order was in, which is what tells Payment whether
    there is anything to reverse.
    """
    try:
        order = service.cancel_order(
            customer_id, order_id, payload.reason, correlation_id
        )
    except OrderNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=error.message
        ) from error
    except InvalidTransitionError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=error.message
        ) from error

    return to_order_response(order)
