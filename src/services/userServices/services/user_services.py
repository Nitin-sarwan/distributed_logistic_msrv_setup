import secrets

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.database.session_store import create_session
from src.services.userServices.api.schema import LoginUser, RegisterUser
from src.services.userServices.models.user_model import User
from src.services.userServices.repositories.user_repositories import UserRepository
from src.services.userServices.utils.exceptions import (
    EmailAlreadyExistsError,
    InvalidCredentialsError,
    PhoneAlreadyExistsError,
    RegistrationFailedError,
)
from src.services.userServices.utils.security import (
    create_access_token,
    hash_password,
    verify_password,
)


class UserService:
    """Business rules for users. Talks to the DB only through the repository."""

    def __init__(self, db: Session):
        self.db = db
        self.user_repository = UserRepository(db)

    def validate_and_get_user(self, email: str, password: str) -> User:
        user = self.user_repository.find_by_email(email)

        if not user:
            raise InvalidCredentialsError()

        if not verify_password(password, user.password):
            raise InvalidCredentialsError()

        return user

    def register_user(self, payload: RegisterUser) -> User:
        # phone is unique too, so check both up front rather than letting the
        # insert fail with an IntegrityError.
        existing = self.user_repository.find_by_email_or_phone(
            payload.email, payload.phone
        )
        if existing:
            if existing.email == payload.email:
                raise EmailAlreadyExistsError()
            raise PhoneAlreadyExistsError()

        user = User(
            name=payload.name,
            email=payload.email,
            phone=payload.phone,
            password=hash_password(payload.password),
            token_secret=secrets.token_hex(32),
        )
        return self.user_repository.create(user)

    def create_user_session(self, request, user: User) -> dict:
        """Issue a token for a user and record the session.

        Shared by register and login, so the session shape stays identical
        however the user arrived.
        """
        token, _jti, expires_at = create_access_token(user)

        # Sessions are shared infrastructure, so they go to Mongo rather than
        # this service's Postgres database.
        session = create_session(request, token, user.id)

        return {
            "access_token": token,
            "expires_at": expires_at,
            "device_session": session["device_session"],
            "device_id": session["device_id"],
        }

    def register(self, request, payload: RegisterUser) -> dict:
        """Full registration flow: persist, verify, then issue a session."""
        try:
            user = self.register_user(payload)
        except (EmailAlreadyExistsError, PhoneAlreadyExistsError) as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=error.message,
            ) from error

        # Confirm the stored hash actually verifies before handing out a token —
        # otherwise the account would be created but impossible to log into.
        try:
            user = self.validate_and_get_user(user.email, payload.password)
        except InvalidCredentialsError as error:
            self.user_repository.delete(user)
            raise RegistrationFailedError() from error

        return {"user": user, **self.create_user_session(request, user)}
    
    def login(self, request, payload: LoginUser) -> dict:
        """Authenticate, then issue a session — the same one register issues."""
        try:
            user = self.validate_and_get_user(payload.email, payload.password)
        except InvalidCredentialsError as error:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=error.message,
            ) from error

        return {"user": user, **self.create_user_session(request, user)}

