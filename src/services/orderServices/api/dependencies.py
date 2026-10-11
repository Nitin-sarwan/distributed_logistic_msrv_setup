"""Who is calling.

**This service authenticates against the shared session store, not against the
gateway's headers** — a deliberate departure from the blueprint's "internal
services trust X-User-Id", and worth explaining because the reasoning decides
how every future service does it.

The blueprint assumes a JWT the gateway can verify with a local key. This system
does not have one: tokens are AES-encrypted with a per-subject secret held in the
owning service's private database, which is why userServices and partnerServices
each decrypt their own and why the gateway can only consult the session store.

Order has no such secret and cannot decrypt anything. That leaves two options:

1. **Trust `X-User-Id` from the gateway.** One forged header away from creating
   orders as anyone, for anything that can reach this service's port.
2. **Read the shared session store directly**, exactly as the gateway does.

Option 2 costs one Mongo lookup and depends on no service — the session store is
shared infrastructure, the same category as Kafka. It also lets this service
enforce something the gateway currently does not: that the session belongs to
the *right kind of subject*. A partner's token in a customer's cookie is a live
session, and without the `app_type` check below it would create orders under a
customer id that is actually a partner id.

Nothing here calls the User service. That rule — never validate a token by
asking User — still holds, and is what keeps User's uptime out of the request
path.
"""

import hmac
import logging

from fastapi import Header, HTTPException, Request, status
from pymongo.errors import PyMongoError

from src.common.request_auth import (
    PARTNER_SESSION_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    extract_token,
)
from src.database.session_store import get_active_session
from src.services.orderServices.config import settings

logger = logging.getLogger(__name__)

# Session discriminators, matching userServices.user_app_type and
# partnerServices.partner_app_type. One shared collection holds both, so this is
# what tells them apart.
CUSTOMER_APP_TYPE = 1
PARTNER_APP_TYPE = 2

UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def _subject_id(request: Request, cookie_name: str, app_type: int) -> int:
    token = extract_token(request, cookie_name=cookie_name)
    if token is None:
        raise UNAUTHORIZED

    try:
        session = get_active_session(token)
    except PyMongoError as error:
        # Fail closed. Unable to prove the caller is authenticated means the
        # request must not proceed — 503, because nothing here is wrong.
        logger.warning("Session lookup failed: %s", error)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session store unavailable",
        ) from error

    if session is None:
        raise UNAUTHORIZED

    # The check the gateway does not make. A live session is not enough; it has
    # to be the right kind.
    if session.get("app_type") != app_type:
        logger.warning(
            "rejected %s session on a %s route",
            session.get("app_type"),
            app_type,
        )
        raise UNAUTHORIZED

    subject = session.get("user")
    if not isinstance(subject, int):
        raise UNAUTHORIZED

    return subject


def get_current_customer_id(request: Request) -> int:
    """The signed-in customer. Every customer-facing route depends on this."""
    return _subject_id(request, SESSION_COOKIE_NAME, CUSTOMER_APP_TYPE)


def get_current_partner_id(request: Request) -> int:
    """The signed-in partner, for the two routes a driver calls on an order.

    Being a partner is not enough on its own — the route also checks that this
    partner is the one assigned to *that* order, against the snapshot taken at
    assignment. No call to partnerServices is involved.
    """
    return _subject_id(request, PARTNER_SESSION_COOKIE_NAME, PARTNER_APP_TYPE)


def get_correlation_id(
    x_correlation_id: str = Header(default=""),
    x_request_id: str = Header(default=""),
) -> str | None:
    """The id that ties one delivery's records together across six services.

    Generated at the gateway. Accepted from the caller because a missing one is
    worse than a forged one: this value is only ever logged and carried in event
    envelopes, never trusted for a decision.
    """
    return x_correlation_id or x_request_id or None


def require_internal_key(x_internal_key: str = Header(default="")) -> None:
    """Guard `/internal/*`, which the gateway does not route.

    Same contract as partnerServices: an empty INTERNAL_API_KEY disables the
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
