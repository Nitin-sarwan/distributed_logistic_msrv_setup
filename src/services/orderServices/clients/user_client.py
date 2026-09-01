"""The one synchronous call this service makes to another service's data.

Order needs the customer's address to snapshot it, and it needs it now: there is
no valid order without one, and the copy has to be the authoritative version at
that instant. Everything else Order does with the outside world is an event.

Why not the three alternatives, briefly — the long form is in
docs/ORDER_SERVICE.md:

* **Trust an address in the request body.** An order is the record of what was
  agreed, and a client-supplied lat/lng cannot be checked against anything.
* **Store `address_id` and read it later.** Editing a saved address would
  rewrite where past deliveries went.
* **Consume `user.address_*` into a local replica.** Mirroring a table this
  service does not own, for data it needs once per order.
"""

import logging

import httpx

from src.services.orderServices.config import settings
from src.services.orderServices.utils.exceptions import (
    AddressNotFoundError,
    AddressServiceUnavailableError,
)

logger = logging.getLogger(__name__)


class ResolvedAddress:
    """An address plus the person at it, flattened for snapshotting."""

    __slots__ = (
        "line1",
        "line2",
        "city",
        "pin_code",
        "latitude",
        "longitude",
        "contact_name",
        "contact_phone",
    )

    def __init__(self, address: dict, contact: dict):
        self.line1: str = address["address_line1"]
        self.line2: str | None = address.get("address_line2")
        self.city: str = address["city"]
        self.pin_code: str = address["pin_code"]
        self.latitude: float = float(address["latitude"])
        self.longitude: float = float(address["longitude"])
        self.contact_name: str = contact["name"]
        self.contact_phone: str = contact["phone"]


def fetch_address(customer_id: int, address_id: int) -> ResolvedAddress:
    """Resolve one of this customer's saved addresses.

    Called with the id the session authenticated, and userServices scopes its
    query by it as well — so a customer cannot snapshot someone else's address
    by guessing a number, whatever this service passes.
    """
    url = (
        f"{settings.user_service_url.rstrip('/')}"
        f"/internal/users/{customer_id}/addresses/{address_id}"
    )

    try:
        response = httpx.get(
            url,
            headers={"X-Internal-Key": settings.internal_api_key},
            timeout=settings.user_service_timeout_seconds,
        )
    except httpx.HTTPError as error:
        # No retry. A retried timeout doubles a wait the customer is already
        # watching, and a retried 500 is usually a second 500.
        logger.warning("User service unreachable for address %s: %s", address_id, error)
        raise AddressServiceUnavailableError() from error

    if response.status_code == 404:
        # 404 covers both "no such address" and "not yours" — userServices does
        # not distinguish them, and neither should this.
        raise AddressNotFoundError()

    if response.status_code >= 400:
        logger.warning(
            "User service answered %s for address %s", response.status_code, address_id
        )
        raise AddressServiceUnavailableError()

    body = response.json()
    return ResolvedAddress(body["address"], body["contact"])
