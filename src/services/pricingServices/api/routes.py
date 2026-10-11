"""The public face of Pricing: what would this cost, and what are the rates.

Mounted at `/api/pricing`. Both endpoints are in the gateway's PUBLIC_PATHS,
because the home page lets a visitor describe a delivery and see the price
before it asks who they are — the same reasoning that makes `/api/geo` public.

Public is safe here in a way it would not be for most services: an estimate is
arithmetic on coordinates the caller already has, plus a rate card that is
published anyway. It writes nothing and calls nobody, so the worst an abusive
caller can do is make this process do sums.

Nothing here is binding. The price a customer pays comes from a **quote**, which
only Order can ask for — see internal_routes.py.
"""

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from src.services.pricingServices.api.schema import (
    EstimateOptionResponse,
    EstimateRequest,
    EstimateResponse,
    FareBreakdown,
    TariffResponse,
)
from src.services.pricingServices.database.connection import get_db
from src.services.pricingServices.services.pricing_services import PricingService, Trip

router = APIRouter(prefix="/pricing", tags=["pricing"])


def get_service(db: Session = Depends(get_db)) -> PricingService:
    return PricingService(db)


@router.post("/estimate", response_model=EstimateResponse, status_code=status.HTTP_200_OK)
def estimate(
    payload: EstimateRequest,
    response: Response,
    service: PricingService = Depends(get_service),
):
    """Every vehicle's price for this trip, cheapest first.

    POST rather than GET only because four coordinates and a weight make an
    unreadable query string; it is safe and idempotent all the same. Vehicles
    that cannot take this parcel are listed with a reason, not dropped.
    """
    # Rates change; a cached estimate would quote yesterday's price.
    response.headers["Cache-Control"] = "no-store"

    result = service.estimate(
        Trip(
            pickup_latitude=payload.pickup.latitude,
            pickup_longitude=payload.pickup.longitude,
            drop_latitude=payload.drop.latitude,
            drop_longitude=payload.drop.longitude,
        ),
        payload.weight_kg,
    )

    return EstimateResponse(
        straight_line_km=result.straight_line_km,
        distance_km=result.distance_km,
        weight_kg=result.weight_kg,
        options=[
            EstimateOptionResponse(
                vehicle_type=option.tariff.vehicle_type,
                available=option.available,
                unavailable_reason=option.unavailable_reason,
                max_weight_kg=option.tariff.max_weight_kg,
                amount=option.fare.amount,
                currency=option.tariff.currency,
                breakdown=FareBreakdown.model_validate(option.fare),
            )
            for option in result.options
        ],
    )


@router.get("/tariffs", response_model=list[TariffResponse])
def tariffs(service: PricingService = Depends(get_service)):
    """The rate card for every vehicle currently offered."""
    return service.tariffs(active_only=True)
