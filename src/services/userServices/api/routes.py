import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from src.common.request_auth import extract_token
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


@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(
    request: Request,
    payload: RegisterUser,
    service: UserService = Depends(get_user_service),
):
    try:
        return service.register(request, payload)
    except RegistrationFailedError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error.message,
        ) from error


@router.post(
    "/login",
    response_model=AuthResponse,
    # 200, not 201: logging in creates a session but not a resource at this URL.
    status_code=status.HTTP_200_OK,
)
def login(
    request: Request,
    payload: LoginUser,
    service: UserService = Depends(get_user_service),
):
    # Bad credentials raise a 401 from the service; nothing to map here.
    return service.login(request, payload)


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
    # Depends on get_current_user so an invalid token cannot revoke anything.
    user: User = Depends(get_current_user),
):
    revoke_session(extract_token(request))
    return {"detail": "Logged out"}


@router.post(
    "/refresh",
    response_model=RefreshResponse,
    status_code=status.HTTP_200_OK,
)
def refresh(
    request: Request,
    payload: RefreshRequest,
    service: UserService = Depends(get_user_service),
):
    # Public: the access token is expected to be expired by the time this is
    # called, so requiring one would defeat the purpose.
    return service.refresh(request, payload.refresh_token)


@router.post("/logout-all", status_code=status.HTTP_200_OK)
def logout_all(
    user: User = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
):
    revoked = service.logout_everywhere(user)
    return {"detail": "Logged out everywhere", "sessions_revoked": revoked}


@router.post("/change-password", status_code=status.HTTP_200_OK)
def change_password(
    payload: ChangePassword,
    user: User = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
):
    service.change_password(user, payload.current_password, payload.new_password)
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
    payload: ResetPassword,
    service: UserService = Depends(get_user_service),
):
    service.reset_password(payload.token, payload.new_password)
    return {"detail": "Password reset. Sign in again."}