"""Who is calling.

Same arrangement as orderServices, and for the same reason: this service cannot
decrypt a token - the per-subject `token_secret` lives in partnerServices'
private database - so it authenticates against the shared session store
directly, exactly as the gateway does.

Trusting the gateway's `X-Partner-Id` instead would be one forged header away
from accepting a delivery as any driver on the platform, for anything that can
reach this service's port.

Reading the session store also lets this service enforce something the gateway
does not: that the session belongs to the *right kind of subject*. A customer's
token in a partner's cookie is a live session, and without the `app_type` check
below it would let a customer accept their own delivery.

Nothing here calls partnerServices. Validating a token by asking another service
would put its uptime in the request path of every offer response.
"""

import hmac
import logging

from fastapi import Header, HTTPException, Request, status
from pymongo.errors import PyMongoError

from src.common.request_auth import PARTNER_SESSION_COOKIE_NAME, extract_token
from src.database.session_store import get_active_session
from src.services.dispatchServices.config import settings

logger = logging.getLogger(__name__)

# Matches partnerServices.partner_app_type. One shared collection holds customer
# and partner sessions and `user` in the document is only an integer, so this is
# what tells partner 5 apart from customer 5.
PARTNER_APP_TYPE = 2

UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_partner_id(request: Request) -> int:
    """The signed-in driver. Every offer route depends on this.

    Being a partner is not enough on its own - each route also checks the offer
    is one made to *this* partner, and that check lives in the SQL rather than
    after the fetch. See the repository.
    """
    token = extract_token(request, cookie_name=PARTNER_SESSION_COOKIE_NAME)
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

    if session.get("app_type") != PARTNER_APP_TYPE:
        logger.warning(
            "rejected a %s session on a partner route", session.get("app_type")
        )
        raise UNAUTHORIZED

    partner_id = session.get("user")
    if not isinstance(partner_id, int):
        raise UNAUTHORIZED

    return partner_id


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
