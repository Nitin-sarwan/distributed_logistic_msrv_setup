"""Who is calling, and which app they are calling from.

Same arrangement as orderServices and dispatchServices: this service cannot
decrypt a token, so it authenticates against the shared session store directly,
exactly as the gateway does, and re-checks the session's `app_type` itself.

Here that check does more work than anywhere else in the platform. Every row in
this database is keyed by the pair `(recipient_type, recipient_id)`, because
customer 5 and partner 5 are different people with the same integer. A route
that resolved the id without also resolving the type would happily show a
delivery partner a customer's entire message history.

That is why the two dependencies below exist as separate functions returning a
`Recipient`, rather than one function returning an int.
"""

import hmac
import logging
from typing import NamedTuple

from fastapi import Header, HTTPException, Request, status
from pymongo.errors import PyMongoError

from src.common.request_auth import (
    PARTNER_SESSION_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    extract_token,
)
from src.database.session_store import get_active_session
from src.services.notificationServices.config import settings
from src.services.notificationServices.domain.vocabulary import RecipientType

logger = logging.getLogger(__name__)

# Session discriminators, matching userServices.user_app_type and
# partnerServices.partner_app_type.
CUSTOMER_APP_TYPE = 1
PARTNER_APP_TYPE = 2

UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


class Recipient(NamedTuple):
    """Who the messages belong to. Never an id on its own.

    Passing the pair around as one value is what stops a handler from taking the
    id from the session and the type from somewhere else - the two can never
    disagree if they are never apart.
    """

    type: RecipientType
    id: int


def _recipient(
    request: Request,
    cookie_name: str,
    app_type: int,
    recipient_type: RecipientType,
) -> Recipient:
    token = extract_token(request, cookie_name=cookie_name)
    if token is None:
        raise UNAUTHORIZED

    try:
        session = get_active_session(token)
    except PyMongoError as error:
        # Fail closed. Unable to prove the caller is authenticated means the
        # request must not proceed - 503, because nothing here is wrong.
        logger.warning("Session lookup failed: %s", error)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session store unavailable",
        ) from error

    if session is None:
        raise UNAUTHORIZED

    # The check the gateway does not make. A live session is not enough; it has
    # to be the right kind, and here that decides whose messages are returned.
    if session.get("app_type") != app_type:
        logger.warning(
            "rejected a %s session on a %s route",
            session.get("app_type"),
            recipient_type.value,
        )
        raise UNAUTHORIZED

    subject = session.get("user")
    if not isinstance(subject, int):
        raise UNAUTHORIZED

    return Recipient(type=recipient_type, id=subject)


def get_customer(request: Request) -> Recipient:
    """The signed-in customer, for /api/notifications/*."""
    return _recipient(
        request, SESSION_COOKIE_NAME, CUSTOMER_APP_TYPE, RecipientType.CUSTOMER
    )


def get_partner(request: Request) -> Recipient:
    """The signed-in driver, for /api/deliveries/notifications/*."""
    return _recipient(
        request, PARTNER_SESSION_COOKIE_NAME, PARTNER_APP_TYPE, RecipientType.PARTNER
    )


def require_internal_key(x_internal_key: str = Header(default="")) -> None:
    """Guard `/internal/*`, which the gateway does not route.

    Same contract as the other services: an empty INTERNAL_API_KEY disables the
    check for local development, and the comparison is constant-time because a
    plain `!=` leaks the matching prefix through timing.
    """
    expected = settings.internal_api_key
    if not expected:
        return

    if not hmac.compare_digest(x_internal_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid internal API key",
        )
