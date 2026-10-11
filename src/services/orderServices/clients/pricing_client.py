"""Pricing: the second synchronous call Order makes while a customer waits.

It replaced a stub that computed the fare in-process, and nothing above this
file changed when it did — `get_quote` kept its signature and its `Quote`, which
is what the stub was shaped for.

Synchronous on purpose. The customer is on a spinner waiting to see a price,
and an order without one is not an order: there is nothing to confirm and
nothing to pay. PLATFORM_WORKFLOW.md lists this as one of the few REST edges in
the order flow for exactly that reason.

Two different failures, and they must not be confused:

* **422 from Pricing** — the delivery cannot be priced as asked (a parcel too
  heavy for the vehicle). The customer's to fix, so it surfaces as 422 with
  Pricing's wording.
* **Anything else** — Pricing is down or broken. Ours to fix, so it surfaces as
  503 and the customer is told to try again.
"""

import logging
from datetime import datetime
from decimal import Decimal

import httpx

from src.services.orderServices.config import settings
from src.services.orderServices.utils.exceptions import (
    PricingServiceUnavailableError,
    QuoteRefusedError,
)

logger = logging.getLogger(__name__)


class Quote:
    __slots__ = ("quote_id", "amount", "currency", "distance_km", "expires_at")

    def __init__(
        self,
        quote_id: str,
        amount: Decimal,
        currency: str,
        distance_km: Decimal,
        expires_at: datetime,
    ):
        self.quote_id = quote_id
        self.amount = amount
        self.currency = currency
        self.distance_km = distance_km
        self.expires_at = expires_at


def get_quote(
    pickup_latitude: float,
    pickup_longitude: float,
    drop_latitude: float,
    drop_longitude: float,
    vehicle_type: str,
    weight_kg: Decimal,
    customer_id: int | None = None,
) -> Quote:
    """What this delivery costs, as a binding quote with an id."""
    url = f"{settings.pricing_service_url.rstrip('/')}/internal/pricing/quotes"
    body = {
        "pickup": {"latitude": str(pickup_latitude), "longitude": str(pickup_longitude)},
        "drop": {"latitude": str(drop_latitude), "longitude": str(drop_longitude)},
        "vehicle_type": vehicle_type,
        "weight_kg": str(weight_kg),
        "customer_id": customer_id,
    }

    try:
        response = httpx.post(
            url,
            json=body,
            headers={"X-Internal-Key": settings.internal_api_key},
            timeout=settings.pricing_service_timeout_seconds,
        )
    except httpx.HTTPError as error:
        # No retry, for the same reason as the address lookup: a retried timeout
        # doubles a wait the customer is already watching.
        logger.warning("Pricing service unreachable: %s", error)
        raise PricingServiceUnavailableError() from error

    if response.status_code == 422:
        detail = response.json().get("detail")
        # A string detail is Pricing refusing the delivery in words meant for
        # the customer. A list is a schema error, which means this client sent
        # something malformed — our bug, not theirs.
        if isinstance(detail, str):
            raise QuoteRefusedError(detail)
        logger.error("Pricing rejected the quote request shape: %s", detail)
        raise PricingServiceUnavailableError()

    if response.status_code >= 400:
        logger.warning(
            "Pricing service answered %s: %s", response.status_code, response.text[:300]
        )
        raise PricingServiceUnavailableError()

    data = response.json()
    return Quote(
        quote_id=data["quote_id"],
        amount=Decimal(str(data["amount"])),
        currency=data["currency"],
        distance_km=Decimal(str(data["distance_km"])),
        expires_at=datetime.fromisoformat(data["expires_at"]),
    )
