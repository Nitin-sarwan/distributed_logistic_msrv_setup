import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from src.common.request_auth import PARTNER_SESSION_COOKIE_NAME
from src.database.session_store import revoke_session
from src.services.partnerServices.api.dependencies import (
    get_current_partner,
    partner_token,
)
from src.services.partnerServices.api.schema import (
    AuthResponse,
    ChangePassword,
    LocationUpdate,
    LoginPartner,
    PartnerResponse,
    PartnerUpdate,
    RefreshRequest,
    RefreshResponse,
    RegisterPartner,
    StatusUpdate,
    VehicleCreate,
    VehicleResponse,
    VehicleUpdate,
)
from src.services.partnerServices.config import settings
from src.services.partnerServices.database.connection import get_db
from src.services.partnerServices.models.partner_model import Partner
from src.services.partnerServices.services.partner_services import PartnerService
from src.services.partnerServices.services.vehicle_services import VehicleService
from src.services.partnerServices.utils.exceptions import (
    InvalidStatusTransitionError,
    NoActiveVehicleError,
    NotVerifiedError,
    RegistrationFailedError,
    RegistrationNumberExistsError,
    VehicleNotFoundError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/partners", tags=["partners"])


def get_partner_service(db: Session = Depends(get_db)) -> PartnerService:
    return PartnerService(db)


def get_vehicle_service(db: Session = Depends(get_db)) -> VehicleService:
    return VehicleService(db)


def set_session_cookie(response: Response, token: str) -> None:
    """Hand the browser an HttpOnly session cookie.

    The same token is still in the response body, so the partner mobile app —
    which does not use cookies at all — is unaffected. What the cookie adds is a
    credential JavaScript cannot read, which matters for a partner web
    dashboard: a token the frontend must hold in order to set an Authorization
    header is reachable by any injected script, and one marked HttpOnly is not.

    The name differs from userServices' cookie on purpose. See
    PARTNER_SESSION_COOKIE_NAME.
    """
    response.set_cookie(
        key=PARTNER_SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        max_age=settings.access_token_expire_minutes * 60,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """Remove the session cookie.

    The attributes must match those it was set with, or the browser treats it as
    a different cookie and leaves the original in place.

    Always paired with a server-side revocation, never used alone: deleting the
    cookie only stops this browser from sending the token, while revoking the
    session is what stops the token working for anyone holding it.
    """
    response.delete_cookie(
        key=PARTNER_SESSION_COOKIE_NAME,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        path="/",
    )


# ── Authentication ─────────────────────────────────────────────────────────


@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(
    request: Request,
    response: Response,
    payload: RegisterPartner,
    service: PartnerService = Depends(get_partner_service),
):
    try:
        result = service.register(request, payload)
    except RegistrationFailedError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error.message,
        ) from error

    # Registration issues a session, so the partner is signed in immediately.
    # They still cannot receive work: `is_verified` is false until operations
    # says otherwise.
    set_session_cookie(response, result["access_token"])
    return result


@router.post(
    "/login",
    response_model=AuthResponse,
    # 200, not 201: logging in creates a session but not a resource at this URL.
    status_code=status.HTTP_200_OK,
)
def login(
    request: Request,
    response: Response,
    payload: LoginPartner,
    service: PartnerService = Depends(get_partner_service),
):
    # Bad credentials raise a 401 from the service; nothing to map here, and no
    # cookie is set because the exception propagates before this returns.
    result = service.login(request, payload)
    set_session_cookie(response, result["access_token"])
    return result


@router.post(
    "/refresh",
    response_model=RefreshResponse,
    status_code=status.HTTP_200_OK,
)
def refresh(
    request: Request,
    response: Response,
    payload: RefreshRequest,
    service: PartnerService = Depends(get_partner_service),
):
    # Public: the access token is expected to be expired by the time this is
    # called, so requiring one would defeat the purpose.
    result = service.refresh(request, payload.refresh_token)

    # The previous access token was just revoked, so the cookie must carry the
    # new one — leaving the old value would sign the browser out at the next
    # request despite a successful refresh.
    set_session_cookie(response, result["access_token"])
    return result


@router.post("/logout", status_code=status.HTTP_200_OK)
def logout(
    request: Request,
    response: Response,
    # Depends on get_current_partner so an invalid token cannot revoke anything.
    partner: Partner = Depends(get_current_partner),
):
    # Revoking is the part that matters: it marks the session inactive, so the
    # token stops working for anyone holding it. Clearing the cookie only stops
    # this browser from sending it.
    revoke_session(partner_token(request))
    clear_session_cookie(response)
    return {"detail": "Logged out"}


@router.post("/logout-all", status_code=status.HTTP_200_OK)
def logout_all(
    response: Response,
    partner: Partner = Depends(get_current_partner),
    service: PartnerService = Depends(get_partner_service),
):
    revoked = service.logout_everywhere(partner)
    clear_session_cookie(response)
    return {"detail": "Logged out everywhere", "sessions_revoked": revoked}


@router.post("/change-password", status_code=status.HTTP_200_OK)
def change_password(
    response: Response,
    payload: ChangePassword,
    partner: Partner = Depends(get_current_partner),
    service: PartnerService = Depends(get_partner_service),
):
    service.change_password(partner, payload.current_password, payload.new_password)
    # Every session was just revoked, this browser's included, so the cookie it
    # holds is already dead. Clearing it keeps the browser from sending a
    # credential that can only produce 401s.
    clear_session_cookie(response)
    return {"detail": "Password changed. Sign in again."}


# ── Profile ────────────────────────────────────────────────────────────────
#
# No endpoint below takes a partner id. Identity comes from
# get_current_partner and every query is scoped by it, so one partner cannot
# read or change another's data whatever they put in the URL.


@router.get(
    "/me",
    response_model=PartnerResponse,
    status_code=status.HTTP_200_OK,
)
def me(partner: Partner = Depends(get_current_partner)):
    # get_current_partner has already authenticated and loaded the row.
    return partner


@router.patch(
    "/me",
    response_model=PartnerResponse,
    status_code=status.HTTP_200_OK,
)
def update_me(
    payload: PartnerUpdate,
    partner: Partner = Depends(get_current_partner),
    service: PartnerService = Depends(get_partner_service),
):
    return service.update_profile(partner, payload)


@router.patch(
    "/me/status",
    response_model=PartnerResponse,
    status_code=status.HTTP_200_OK,
)
def update_status(
    payload: StatusUpdate,
    partner: Partner = Depends(get_current_partner),
    service: PartnerService = Depends(get_partner_service),
):
    """Go on or off duty."""
    try:
        return service.set_status(partner, payload.status)
    except NotVerifiedError as error:
        # 403, not 409: the request is well formed and the state is fine, the
        # caller simply is not allowed to do this yet.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=error.message,
        ) from error
    except (NoActiveVehicleError, InvalidStatusTransitionError) as error:
        # 409: this conflicts with the partner's current state, and the fix is
        # to change that state rather than to resend a different body.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error.message,
        ) from error


@router.post(
    "/me/location",
    response_model=PartnerResponse,
    status_code=status.HTTP_200_OK,
)
def update_location(
    payload: LocationUpdate,
    partner: Partner = Depends(get_current_partner),
    service: PartnerService = Depends(get_partner_service),
):
    """The partner app's heartbeat.

    Called on a timer while the app is open. Dispatch treats a partner whose
    last heartbeat is older than PARTNER_LOCATION_STALE_MINUTES as unreachable,
    however green their status says they are.
    """
    return service.update_location(partner, payload)


# ── Vehicles ───────────────────────────────────────────────────────────────


@router.get(
    "/me/vehicles",
    response_model=list[VehicleResponse],
    status_code=status.HTTP_200_OK,
)
def list_vehicles(
    partner: Partner = Depends(get_current_partner),
    service: VehicleService = Depends(get_vehicle_service),
):
    return service.list_vehicles(partner)


@router.post(
    "/me/vehicles",
    response_model=VehicleResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_vehicle(
    payload: VehicleCreate,
    partner: Partner = Depends(get_current_partner),
    service: VehicleService = Depends(get_vehicle_service),
):
    """Add a vehicle. It arrives PENDING and cannot be driven until verified."""
    try:
        return service.create_vehicle(partner, payload)
    except RegistrationNumberExistsError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error.message,
        ) from error


@router.patch(
    "/me/vehicles/{vehicle_id}",
    response_model=VehicleResponse,
    status_code=status.HTTP_200_OK,
)
def update_vehicle(
    vehicle_id: int,
    payload: VehicleUpdate,
    partner: Partner = Depends(get_current_partner),
    service: VehicleService = Depends(get_vehicle_service),
):
    try:
        return service.update_vehicle(partner, vehicle_id, payload)
    except VehicleNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error.message,
        ) from error


@router.post(
    "/me/vehicles/{vehicle_id}/activate",
    response_model=VehicleResponse,
    status_code=status.HTTP_200_OK,
)
def activate_vehicle(
    vehicle_id: int,
    partner: Partner = Depends(get_current_partner),
    service: VehicleService = Depends(get_vehicle_service),
):
    """Pick which vehicle the partner is driving today.

    Whichever was active is stood down in the same transaction — the database
    enforces one active vehicle per partner.
    """
    try:
        return service.activate_vehicle(partner, vehicle_id)
    except VehicleNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error.message,
        ) from error
    except InvalidStatusTransitionError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error.message,
        ) from error


@router.delete(
    "/me/vehicles/{vehicle_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_vehicle(
    vehicle_id: int,
    partner: Partner = Depends(get_current_partner),
    service: VehicleService = Depends(get_vehicle_service),
):
    try:
        service.delete_vehicle(partner, vehicle_id)
    except VehicleNotFoundError as error:
        # 404 rather than 403 even when the vehicle exists but belongs to
        # someone else: a 403 would confirm the id is real, which is a fact the
        # caller has no business learning.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error.message,
        ) from error
    except InvalidStatusTransitionError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error.message,
        ) from error

    # 204 carries no body, so nothing is returned.
