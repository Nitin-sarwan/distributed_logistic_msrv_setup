"""Service-to-service and operations endpoints.

Mounted at `/internal/partners`, deliberately outside `/api`. The gateway only
routes prefixes it has been told about, and it has been told about `/api/users`
and `/api/partners` — nothing else. A request to `/internal/...` from the public
internet therefore gets a 404 at the edge and never reaches this process at all.

That is the primary control. `require_internal_key` is the second one, for
callers that can reach the service port directly: Dispatch, an operations
console, or anything else inside the deployment.

This is where "which partner should get this order?" is *answered*, not decided.
The distinction is the whole reason these are two services: this file reports
who is verified, free, and nearby; Dispatch picks one.
"""

import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from src.services.partnerServices.api.dependencies import require_internal_key
from src.services.partnerServices.api.schema import (
    AssignStatusUpdate,
    AvailablePartner,
    PartnerDetail,
    PartnerPublicResponse,
    RatingCreate,
    SuspensionDecision,
    VehicleResponse,
    VehicleVerificationDecision,
    VerificationDecision,
)
from src.services.partnerServices.config import settings
from src.services.partnerServices.database.connection import get_db
from src.services.partnerServices.repositories.partner_repositories import (
    PartnerRepository,
)
from src.services.partnerServices.services.partner_services import PartnerService
from src.services.partnerServices.services.vehicle_services import VehicleService
from src.services.partnerServices.utils.enums import VehicleType
from src.services.partnerServices.utils.exceptions import (
    InvalidStatusTransitionError,
    VehicleNotFoundError,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/internal/partners",
    tags=["internal"],
    # Applied to every route in the file rather than to each one, so a new
    # endpoint added below is protected by default instead of by memory.
    dependencies=[Depends(require_internal_key)],
)

PARTNER_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail="Partner not found",
)


def get_partner_service(db: Session = Depends(get_db)) -> PartnerService:
    return PartnerService(db)


def get_vehicle_service(db: Session = Depends(get_db)) -> VehicleService:
    return VehicleService(db)


def get_partner_repository(db: Session = Depends(get_db)) -> PartnerRepository:
    return PartnerRepository(db)


@router.get(
    "/available",
    response_model=list[AvailablePartner],
    status_code=status.HTTP_200_OK,
)
def available(
    lat: float = Query(ge=-90, le=90, description="Pickup latitude"),
    lng: float = Query(ge=-180, le=180, description="Pickup longitude"),
    radius_km: float | None = Query(default=None, gt=0, le=50),
    limit: int | None = Query(default=None, ge=1, le=100),
    vehicle_type: VehicleType | None = Query(default=None),
    min_capacity: Decimal | None = Query(
        default=None, gt=0, description="Minimum load capacity in kilograms"
    ),
    service: PartnerService = Depends(get_partner_service),
):
    """The question Dispatch asks: who could take an order from here?

    Returns partners who are simultaneously verified, ONLINE, driving a
    verified active vehicle, sending fresh location heartbeats, and inside the
    radius — nearest first, with the distance included so Dispatch can rank on
    its own terms rather than being forced to accept this ordering.

    An empty list is a valid answer and a common one at 4am. It means "nobody",
    not "error", so it is a 200.
    """
    return service.find_available(
        latitude=lat,
        longitude=lng,
        radius_km=radius_km or settings.partner_search_radius_km,
        limit=limit or settings.partner_search_limit,
        vehicle_type=vehicle_type.value if vehicle_type else None,
        min_capacity=min_capacity,
    )


@router.get(
    "/{partner_id}",
    response_model=PartnerDetail,
    status_code=status.HTTP_200_OK,
)
def get_partner(
    partner_id: int,
    service: PartnerService = Depends(get_partner_service),
):
    """One partner plus the vehicle they are driving.

    What an Order service needs after an assignment, to show the customer who is
    coming and what to look for at the kerb. Narrower than what the partner sees
    about themselves — see PartnerPublicResponse.
    """
    detail = service.get_detail(partner_id)
    if detail is None:
        raise PARTNER_NOT_FOUND
    return detail


@router.patch(
    "/{partner_id}/status",
    response_model=PartnerPublicResponse,
    status_code=status.HTTP_200_OK,
)
def set_dispatch_status(
    partner_id: int,
    payload: AssignStatusUpdate,
    service: PartnerService = Depends(get_partner_service),
    repository: PartnerRepository = Depends(get_partner_repository),
):
    """Dispatch claiming a partner for a delivery, or handing them back.

    ONLINE -> ON_TRIP on assignment, ON_TRIP -> ONLINE on completion. The 409 on
    any other transition is the safety property that matters: it is what stops
    two concurrent dispatches from both believing they claimed the same partner.

    Setting the status a partner already holds succeeds and changes nothing, so
    a retry after a network timeout is safe.
    """
    partner = repository.find_by_id(partner_id)
    if partner is None:
        raise PARTNER_NOT_FOUND

    try:
        return service.set_dispatch_status(partner, payload.status)
    except InvalidStatusTransitionError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error.message,
        ) from error


@router.post(
    "/{partner_id}/rating",
    response_model=PartnerPublicResponse,
    status_code=status.HTTP_200_OK,
)
def record_rating(
    partner_id: int,
    payload: RatingCreate,
    service: PartnerService = Depends(get_partner_service),
    repository: PartnerRepository = Depends(get_partner_repository),
):
    """Fold a completed delivery's score into the partner's running average.

    Internal because the caller has to be something that knows the delivery
    actually happened. Exposed to customers directly, this would be a free
    endpoint for tanking any partner's rating.
    """
    partner = repository.find_by_id(partner_id)
    if partner is None:
        raise PARTNER_NOT_FOUND

    return service.record_rating(partner, payload.rating)


@router.post(
    "/{partner_id}/verification",
    response_model=PartnerPublicResponse,
    status_code=status.HTTP_200_OK,
)
def set_verification(
    partner_id: int,
    payload: VerificationDecision,
    service: PartnerService = Depends(get_partner_service),
    repository: PartnerRepository = Depends(get_partner_repository),
):
    """Operations clearing or withdrawing a partner's KYC.

    This is the single gate on receiving work. A partner can register, add a
    vehicle and tap "go online" without ever appearing to Dispatch until this
    endpoint says so.
    """
    partner = repository.find_by_id(partner_id)
    if partner is None:
        raise PARTNER_NOT_FOUND

    return service.set_verification(partner, payload.approve)


@router.post(
    "/{partner_id}/suspension",
    response_model=PartnerPublicResponse,
    status_code=status.HTTP_200_OK,
)
def set_suspension(
    partner_id: int,
    payload: SuspensionDecision,
    service: PartnerService = Depends(get_partner_service),
    repository: PartnerRepository = Depends(get_partner_repository),
):
    """Bar a partner from working, or let them back in.

    Lifting a suspension returns them to OFFLINE, not ONLINE: allowed to work is
    not the same as at work, and only the partner decides the second.
    """
    partner = repository.find_by_id(partner_id)
    if partner is None:
        raise PARTNER_NOT_FOUND

    return service.set_suspended(partner, payload.suspended)


@router.post(
    "/vehicles/{vehicle_id}/verification",
    response_model=VehicleResponse,
    status_code=status.HTTP_200_OK,
)
def set_vehicle_verification(
    vehicle_id: int,
    payload: VehicleVerificationDecision,
    service: VehicleService = Depends(get_vehicle_service),
):
    """Operations clearing or refusing a vehicle's documents.

    Note the path: `/internal/partners/vehicles/{id}`, not nested under a
    partner. Reviewing a vehicle does not require knowing whose it is, and
    demanding the partner id in the URL would only create a second thing that
    can be wrong.

    Approving lands the vehicle at INACTIVE, not ACTIVE — cleared to be driven
    is not the same as currently being driven.
    """
    try:
        return service.set_verification(vehicle_id, payload.approve)
    except VehicleNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error.message,
        ) from error
