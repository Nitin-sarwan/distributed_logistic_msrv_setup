"""Who is calling.

The driver's routes authenticate against the shared session store, like
Dispatch and Order: this service cannot decrypt a token, and must not trust a
proxy header to say which driver is recording that they took someone's money.
The session's `app_type` is checked too — a customer's session must never be
able to mark their own order paid.
"""

import hmac
import logging

from fastapi import Header, HTTPException, Request, status
from pymongo.errors import PyMongoError

from src.common.request_auth import PARTNER_SESSION_COOKIE_NAME, extract_token
from src.database.session_store import get_active_session
from src.services.paymentServices.config import settings

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
    """The signed-in driver. Every collection route depends on this.

    Being a partner is not enough: each route also checks the payment belongs to
    a delivery assigned to *this* partner, in the SQL. See the repository.
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
