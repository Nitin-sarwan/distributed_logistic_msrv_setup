from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from src.services.userServices.api.schema import AuthResponse, LoginUser, RegisterUser
from src.services.userServices.database.connection import get_db
from src.services.userServices.services.user_services import UserService
from src.services.userServices.utils.exceptions import RegistrationFailedError

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
