import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from src.common.request_auth import SESSION_COOKIE_NAME, extract_token
from src.database.session_store import revoke_session
from src.services.userServices.api.dependencies import get_current_user
from src.services.userServices.api.schema import (
    AuthResponse,
    ChangePassword,
    ForgotPassword,
    LoginUser,
    RefreshRequest,
    RefreshResponse,
    RegisterUser,
    ResetPassword,
    UserResponse,
)
from src.services.userServices.config import settings
from src.services.userServices.database.connection import get_db
from src.services.userServices.models.user_model import User
from src.services.userServices.services.user_services import UserService
from src.services.userServices.utils.exceptions import RegistrationFailedError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


def get_user_service(db: Session = Depends(get_db)) -> UserService:
    return UserService(db)


def set_session_cookie(response: Response, token: str) -> None:
    """Hand the browser an HttpOnly session cookie.

    The same token is still in the response body, so non-browser clients are
    unaffected. What the cookie adds is a credential JavaScript cannot read:
    a token the frontend must hold in order to set an Authorization header is
    reachable by any injected script, and one marked HttpOnly is not.

    max_age matches the token's own lifetime, so the browser drops the cookie
    at roughly the moment the token stops working. It is only a hint — the
    session store remains the authority, and a revoked token fails immediately
    however long the cookie survives.
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        max_age=settings.access_token_expire_minutes * 60,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """Remove the session cookie.

    The attributes must match those it was set with, or the browser treats it
    as a different cookie and leaves the original in place.

    Always paired with a server-side revocation, never used alone: deleting the
    cookie only stops this browser from sending the token, while revoking the
    session is what stops the token working for anyone holding it.
    """
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        path="/",
    )


@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(
    request: Request,
    response: Response,
    payload: RegisterUser,
    service: UserService = Depends(get_user_service),
):
    try:
        result = service.register(request, payload)
    except RegistrationFailedError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error.message,
        ) from error

    # Registration issues a session, so the client is logged in immediately.
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
    payload: LoginUser,
    service: UserService = Depends(get_user_service),
):
    # Bad credentials raise a 401 from the service; nothing to map here, and no
    # cookie is set because the exception propagates before this returns.
    result = service.login(request, payload)
    set_session_cookie(response, result["access_token"])
    return result


@router.get(
    "/profile",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
)
def profile(user: User = Depends(get_current_user)):
    # get_current_user has already authenticated and loaded the row.
    return user


@router.post("/logout", status_code=status.HTTP_200_OK)
def logout(
    request: Request,
    response: Response,
    # Depends on get_current_user so an invalid token cannot revoke anything.
    user: User = Depends(get_current_user),
):
    # Revoking is the part that matters: it marks the session inactive, so the
    # token stops working for anyone holding it. Clearing the cookie only stops
    # this browser from sending it.
    revoke_session(extract_token(request))
    clear_session_cookie(response)
    return {"detail": "Logged out"}


@router.post(
    "/refresh",
    response_model=RefreshResponse,
    status_code=status.HTTP_200_OK,
)
def refresh(
    request: Request,
    response: Response,
    payload: RefreshRequest,
    service: UserService = Depends(get_user_service),
):
    # Public: the access token is expected to be expired by the time this is
    # called, so requiring one would defeat the purpose.
    result = service.refresh(request, payload.refresh_token)

    # The previous access token was just revoked, so the cookie must carry the
    # new one — leaving the old value would log the browser out at the next
    # request despite a successful refresh.
    set_session_cookie(response, result["access_token"])
    return result


@router.post("/logout-all", status_code=status.HTTP_200_OK)
def logout_all(
    response: Response,
    user: User = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
):
    revoked = service.logout_everywhere(user)
    clear_session_cookie(response)
    return {"detail": "Logged out everywhere", "sessions_revoked": revoked}


@router.post("/change-password", status_code=status.HTTP_200_OK)
def change_password(
    response: Response,
    payload: ChangePassword,
    user: User = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
):
    service.change_password(user, payload.current_password, payload.new_password)
    # Every session was just revoked, this browser's included, so the cookie it
    # holds is already dead. Clearing it keeps the browser from sending a
    # credential that can only produce 401s.
    clear_session_cookie(response)
    return {"detail": "Password changed. Sign in again."}


@router.post("/forgot-password", status_code=status.HTTP_200_OK)
def forgot_password(
    payload: ForgotPassword,
    service: UserService = Depends(get_user_service),
):
    token = service.request_password_reset(payload.email)

    # Always the same answer, whether or not the account exists — otherwise this
    # endpoint tells an attacker which emails are registered.
    response = {"detail": "If that email is registered, a reset link has been sent."}

    if token is not None:
        logger.info("Password reset token issued for %s", payload.email)
        if settings.password_reset_expose_token:
            # Local dev only. See PASSWORD_RESET_EXPOSE_TOKEN in config.
            response["reset_token"] = token

    return response


@router.post("/reset-password", status_code=status.HTTP_200_OK)
def reset_password(
    response: Response,
    payload: ResetPassword,
    service: UserService = Depends(get_user_service),
):
    service.reset_password(payload.token, payload.new_password)
    # A reset revokes every session on the assumption the old password may be
    # compromised. If this browser happened to hold one, its cookie is now dead.
    clear_session_cookie(response)
    return {"detail": "Password reset. Sign in again."}