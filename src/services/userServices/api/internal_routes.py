"""Service-to-service reads of user data.

Mounted at `/internal/users`, outside `/api`. The gateway routes `/api/*` and
nothing else, so nothing here is reachable from the public internet — the same
arrangement partnerServices uses.

There is exactly one endpoint, and it exists for exactly one caller: the Order
service resolving a saved address into the snapshot it stores on the order. That
narrowness is the design. A general "give me this user" endpoint would be used
by everyone for everything within a month, and every one of those uses would be
a service depending on User's uptime.
"""

import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from src.services.userServices.config import settings
from src.services.userServices.database.connection import get_db
from src.services.userServices.repositories.address_repositories import (
    AddressRepository,
)
from src.services.userServices.repositories.user_repositories import UserRepository

logger = logging.getLogger(__name__)


def require_internal_key(x_internal_key: str = Header(default="")) -> None:
    """Guard the /internal routes.

    Those paths are not in the gateway's routing table, so nothing reaching them
    arrived through the front door. This is the second lock: anything that can
    reach the service port directly still has to know the shared secret.

    An empty INTERNAL_API_KEY disables the check — a deliberate convenience on a
    local machine, logged as a warning at startup.
    """
    expected = settings.internal_api_key
    if not expected:
        return

    # Constant-time: a plain `!=` leaks the length of the matching prefix
    # through timing, which is enough to recover a secret one character at a
    # time given enough requests.
    if not hmac.compare_digest(x_internal_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid internal API key",
        )


router = APIRouter(
    prefix="/internal/users",
    tags=["internal"],
    dependencies=[Depends(require_internal_key)],
)

NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail="Address not found",
)


@router.get("/{user_id}/addresses/{address_id}")
def get_address_for_snapshot(
    user_id: int,
    address_id: int,
    db: Session = Depends(get_db),
):
    """One of this user's addresses, plus the person to call at it.

    Both ids are in the path and the query is scoped by both, so a caller that
    passes an address id belonging to someone else gets 404 — the ownership
    check lives in the WHERE clause rather than in an `if` a caller could skip.

    The contact comes back in the same response deliberately. A driver needs a
    name and a number, and a second round trip for two columns would double the
    failure surface of what is meant to be Order's only synchronous dependency.

    404 covers both "no such address" and "not that user's". The two are not
    distinguished, for the same reason the customer-facing routes do not
    distinguish them.
    """
    address = AddressRepository(db).find_for_user(address_id, user_id)
    if address is None:
        raise NOT_FOUND

    user = UserRepository(db).find_by_id(user_id)
    if user is None:
        # An address whose owner is gone. Not reachable in practice, and a 404
        # is the honest answer rather than a half-filled snapshot.
        raise NOT_FOUND

    return {
        "address": {
            "id": address.id,
            "address_line1": address.address_line1,
            "address_line2": address.address_line2,
            "city": address.city,
            "pin_code": address.pin_code,
            # Numeric(9,6) — as floats, which is exactly representable at six
            # decimal places and is what the caller stores.
            "latitude": float(address.latitude),
            "longitude": float(address.longitude),
        },
        "contact": {
            "name": user.name,
            "phone": user.phone,
        },
    }
