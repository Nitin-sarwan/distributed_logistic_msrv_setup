from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from src.common.request_auth import extract_token, extract_user_id
from src.database.session_store import get_active_session, touch_session
from src.services.userServices.database.connection import get_db
from src.services.userServices.models.user_model import User
from src.services.userServices.repositories.user_repositories import UserRepository
from src.services.userServices.utils.security import decrypt_data

UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """Authoritative authentication for this service.

    The gateway performs a cheaper session check first, but this must not rely
    on that: a service reachable directly has to authenticate on its own, and
    headers set by an upstream proxy are not evidence of anything.
    """
    token = extract_token(request)
    if token is None:
        raise UNAUTHORIZED

    repository = UserRepository(db)

    # A supplied user id is a lookup shortcut, not a claim we trust. If it is
    # absent or wrong, fall back to resolving the user from the session.
    user = None
    claimed_id = extract_user_id(request)
    if claimed_id is not None:
        user = repository.find_by_id(claimed_id)

    if user is None:
        session = get_active_session(token)
        if session is None:
            raise UNAUTHORIZED
        user = repository.find_by_id(session["user"])

    if user is None:
        raise UNAUTHORIZED

    # The real check. Only this user's token_secret decrypts this token, so a
    # successful decrypt is proof the token was issued to them — whatever the
    # request claimed.
    payload = decrypt_data(token, user.token_secret)
    if payload is None:
        raise UNAUTHORIZED

    if payload.get("id") != user.id:
        raise UNAUTHORIZED

    # A refresh token must never authenticate a request — it is long-lived and
    # only exists to mint access tokens.
    if payload.get("type") != "access":
        raise UNAUTHORIZED

    expires = payload.get("exp")
    if expires is None or datetime.now(timezone.utc).timestamp() >= expires:
        raise UNAUTHORIZED

    # Checked even when the user came from the id hint: decryption proves the
    # token is authentic, but only the session store knows if it was revoked.
    if get_active_session(token) is None:
        raise UNAUTHORIZED

    touch_session(token)
    return user
