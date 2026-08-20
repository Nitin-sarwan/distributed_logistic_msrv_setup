import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.database.session_store import (
    create_session,
    get_active_session,
    revoke_children,
    revoke_user_sessions,
)
from src.services.userServices.api.schema import LoginUser, RegisterUser
from src.services.userServices.config import settings
from src.services.userServices.models.user_model import User
from src.services.userServices.repositories.password_reset_repositories import (
    PasswordResetRepository,
)
from src.services.userServices.repositories.user_repositories import UserRepository
from src.services.userServices.utils.exceptions import (
    EmailAlreadyExistsError,
    InvalidCredentialsError,
    PhoneAlreadyExistsError,
    RegistrationFailedError,
)
from src.services.userServices.utils.security import (
    create_access_token,
    create_refresh_token,
    decrypt_data,
    generate_reset_token,
    hash_password,
    hash_reset_token,
    verify_password,
)


class UserService:
    """Business rules for users. Talks to the DB only through the repository."""

    def __init__(self, db: Session):
        self.db = db
        self.user_repository = UserRepository(db)
        self.password_reset_repository = PasswordResetRepository(db)

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
        refresh_token, _rjti, refresh_expires = create_refresh_token(user)
        token, _jti, expires_at = create_access_token(user)

        # Sessions are shared infrastructure, so they go to Mongo rather than
        # this service's Postgres database.
        refresh_session = create_session(
            request,
            refresh_token,
            user.id,
            app_type=settings.user_app_type,
            expires_at=refresh_expires,
            token_type="refresh",
        )

        # The access session points at the refresh token that minted it, so
        # revoking the refresh token takes every access token with it.
        create_session(
            request,
            token,
            user.id,
            app_type=settings.user_app_type,
            expires_at=expires_at,
            token_type="auth",
            parent_token=refresh_token,
            device_session=refresh_session["device_session"],
            device_id=refresh_session["device_id"],
        )

        return {
            "access_token": token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
            "refresh_expires_at": refresh_expires,
            "device_session": refresh_session["device_session"],
            "device_id": refresh_session["device_id"],
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

    def refresh(self, request, refresh_token: str) -> dict:
        """Exchange a refresh token for a new access token.

        The refresh token stays valid — only the access token is replaced. The
        old access session is revoked so a stolen one dies at the next refresh.
        """
        session = get_active_session(refresh_token)
        if session is None or session.get("token_type") != "refresh":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid refresh token",
            )

        user = self.user_repository.find_by_id(session["user"])
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid refresh token",
            )

        payload = decrypt_data(refresh_token, user.token_secret)
        if payload is None or payload.get("type") != "refresh":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid refresh token",
            )

        # Retire the access tokens this refresh token previously minted, so only
        # the newest one works.
        revoke_children(refresh_token)

        token, _jti, expires_at = create_access_token(user)
        create_session(
            request,
            token,
            user.id,
            app_type=settings.user_app_type,
            expires_at=expires_at,
            token_type="auth",
            parent_token=refresh_token,
            device_session=session.get("device_session"),
            device_id=session.get("device_id"),
        )

        return {
            "user": user,
            "access_token": token,
            "expires_at": expires_at,
            "device_session": session.get("device_session", ""),
            "device_id": session.get("device_id", ""),
        }

    def logout_everywhere(self, user: User) -> int:
        """Revoke every session on every device.

        token_secret is rotated as well, so even a session record that somehow
        survived could not have its token decrypted.
        """
        revoked = revoke_user_sessions(user.id, app_type=settings.user_app_type)
        user.token_secret = secrets.token_hex(32)
        self.user_repository.save(user)
        return revoked

    def change_password(self, user: User, current: str, new: str) -> None:
        """Change a password for a signed-in user.

        Requires the current password: an unattended logged-in browser must not
        be enough to take over the account permanently.
        """
        if not verify_password(current, user.password):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Current password is incorrect",
            )

        user.password = hash_password(new)
        user.token_secret = secrets.token_hex(32)
        self.user_repository.save(user)

        # Rotating token_secret already invalidates every token; revoking the
        # sessions makes that visible in the store rather than only at decrypt.
        revoke_user_sessions(user.id, app_type=settings.user_app_type)

    def request_password_reset(self, email: str) -> str | None:
        """Issue a reset token, or quietly do nothing if the email is unknown.

        Returns the token only so the caller can deliver it. The caller must not
        reveal whether an account existed — that would leak which addresses are
        registered.
        """
        user = self.user_repository.find_by_email(email)
        if user is None:
            return None

        # A new link retires any previous one.
        self.password_reset_repository.revoke_outstanding(user.id)

        token, token_hash = generate_reset_token()
        expires_at = datetime.now(timezone.utc) + timedelta(
            minutes=settings.password_reset_expire_minutes
        )
        self.password_reset_repository.create(user.id, token_hash, expires_at)
        return token

    def reset_password(self, token: str, new_password: str) -> None:
        reset = self.password_reset_repository.find_usable(hash_reset_token(token))
        if reset is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Reset token is invalid or expired",
            )

        user = self.user_repository.find_by_id(reset.user_id)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Reset token is invalid or expired",
            )

        user.password = hash_password(new_password)
        user.token_secret = secrets.token_hex(32)
        self.user_repository.save(user)

        # Single use, and every other pending link dies too.
        self.password_reset_repository.mark_used(reset)
        self.password_reset_repository.revoke_outstanding(user.id)

        # A reset means the password may have been compromised — evict every
        # existing session rather than leaving an attacker signed in.
        revoke_user_sessions(user.id, app_type=settings.user_app_type)

