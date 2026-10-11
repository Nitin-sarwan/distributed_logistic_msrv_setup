"""Binding quotes for Order, and the rate card for operations.

Mounted at `/internal/pricing`, deliberately outside `/api`. The gateway routes
`/api/*` and nothing else, so a customer cannot mint a quote for themselves —
if they could, they could quote a cheap trip and attach it to an expensive one.
`require_internal_key` is the second lock, for anything that can reach this
port directly.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from src.services.pricingServices.api.dependencies import require_internal_key
from src.services.pricingServices.api.schema import (
    QuoteRequest,
    QuoteResponse,
    TariffResponse,
    TariffUpdate,
    to_quote_response,
)
from src.services.pricingServices.database.connection import get_db
from src.services.pricingServices.services.pricing_services import PricingService, Trip
from src.services.pricingServices.utils.exceptions import (
    QuoteNotFoundError,
    UnknownVehicleTypeError,
    VehicleUnavailableError,
    WeightExceedsVehicleError,
)

router = APIRouter(
    prefix="/internal/pricing",
    tags=["internal"],
    # Applied to every route in the file rather than to each one, so a new
    # endpoint is protected by default instead of by memory.
    dependencies=[Depends(require_internal_key)],
)


def get_service(db: Session = Depends(get_db)) -> PricingService:
    return PricingService(db)


@router.post("/quotes", response_model=QuoteResponse, status_code=status.HTTP_201_CREATED)
def issue_quote(payload: QuoteRequest, service: PricingService = Depends(get_service)):
    """Price one delivery, record the price, and hand back its id.

    **422** when the delivery cannot be priced as asked — an unknown or
    switched-off vehicle, or a parcel too heavy for it. Order relays the detail
    to the customer, because it is something they can fix by choosing again.
    """
    try:
        quote = service.issue_quote(
            Trip(
                pickup_latitude=payload.pickup.latitude,
                pickup_longitude=payload.pickup.longitude,
                drop_latitude=payload.drop.latitude,
                drop_longitude=payload.drop.longitude,
            ),
            vehicle_type=payload.vehicle_type,
            weight_kg=payload.weight_kg,
            customer_id=payload.customer_id,
        )
    except (
        UnknownVehicleTypeError,
        VehicleUnavailableError,
        WeightExceedsVehicleError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=error.message
        ) from error

    return to_quote_response(quote)


@router.get("/quotes/{quote_id}", response_model=QuoteResponse)
def get_quote(quote_id: str, service: PricingService = Depends(get_service)):
    """One quote, as issued. The answer to "why was I charged that?"."""
    try:
        return to_quote_response(service.get_quote(quote_id))
    except QuoteNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=error.message
        ) from error


@router.get("/tariffs", response_model=list[TariffResponse])
def list_tariffs(service: PricingService = Depends(get_service)):
    """Every rate card, including the switched-off ones the public cannot see."""
    return service.tariffs(active_only=False)


@router.patch("/tariffs/{vehicle_type}", response_model=TariffResponse)
def update_tariff(
    vehicle_type: str,
    payload: TariffUpdate,
    service: PricingService = Depends(get_service),
):
    """Change a rate card. Affects new estimates and quotes, never issued ones."""
    changes = payload.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Nothing to change",
        )

    try:
        return service.update_tariff(vehicle_type, changes)
    except UnknownVehicleTypeError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=error.message
        ) from error
