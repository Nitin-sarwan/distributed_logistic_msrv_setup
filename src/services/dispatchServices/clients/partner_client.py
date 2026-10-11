"""The only service Dispatch talks to over HTTP, and the two things it asks.

Both calls are synchronous on purpose, and for different reasons.

**The availability search** cannot be an event. Dispatch cannot rank candidates
it does not have, and a read model fed by `location.updated` would happily offer
work to a driver who went offline two minutes ago. PLATFORM_WORKFLOW.md records
the trade: the read model is an optimisation for when this query gets hot, not
the starting point.

**The claim** cannot be an event either, and this one is the more important of
the two. `PATCH /internal/partners/{id}/status` is where two dispatches racing
for the same driver are resolved: partnerServices' state machine lets exactly
one ONLINE -> ON_TRIP through and answers 409 to the loser. An event would make
the claim asynchronous, and an asynchronous claim is not a claim - both
dispatches would believe they had the driver and both would tell a customer so.

Nothing here retries. The offer loop is the retry: a search that fails is tried
again on the next tick, and a claim that fails moves to the next candidate. A
retry inside the client would just make the loop's timing harder to reason about.
"""

import logging
from decimal import Decimal

import httpx

from src.services.dispatchServices.config import settings
from src.services.dispatchServices.utils.exceptions import (
    PartnerClaimConflictError,
    PartnerServiceUnavailableError,
)

logger = logging.getLogger(__name__)

ONLINE = "online"
ON_TRIP = "on_trip"


class Candidate:
    """One row of the availability answer, flattened.

    partnerServices returns `{partner, vehicle, distance_km}`; the loop wants
    the handful of fields it ranks on and snapshots. Flattening here means the
    shape of that response is known in exactly one file.
    """

    __slots__ = (
        "partner_id",
        "name",
        "phone",
        "rating",
        "vehicle_id",
        "vehicle_number",
        "vehicle_type",
        "distance_km",
    )

    def __init__(self, row: dict):
        partner = row["partner"]
        vehicle = row.get("vehicle") or {}

        self.partner_id: int = int(partner["id"])
        self.name: str = partner.get("name") or "Delivery partner"
        self.phone: str = partner.get("phone") or ""
        self.rating = partner.get("rating")
        self.vehicle_id: int | None = vehicle.get("id")
        self.vehicle_number: str | None = vehicle.get("vehicle_number")
        self.vehicle_type: str | None = vehicle.get("vehicle_type")
        self.distance_km: Decimal = Decimal(str(row["distance_km"]))


def _url(path: str) -> str:
    return f"{settings.partner_service_url.rstrip('/')}{path}"


def _headers() -> dict[str, str]:
    return {"X-Internal-Key": settings.internal_api_key}


def find_available(
    latitude: Decimal | float,
    longitude: Decimal | float,
    vehicle_type: str,
    min_capacity: Decimal | float,
    radius_km: float,
    limit: int,
) -> list[Candidate]:
    """Who could take an order from here, nearest first.

    partnerServices does the filtering that only it can do - KYC verified, an
    ACTIVE vehicle, status ONLINE, and a location heartbeat fresh enough to
    believe. Dispatch does the ranking, because which of those candidates should
    be asked first is policy, and policy is what Dispatch is for.

    An empty list is a valid answer and a common one at 4am. It means "nobody",
    not "error", so it is a 200 and comes back as `[]`.
    """
    params = {
        "lat": float(latitude),
        "lng": float(longitude),
        "vehicle_type": vehicle_type,
        "min_capacity": float(min_capacity),
        "radius_km": radius_km,
        "limit": limit,
    }

    try:
        response = httpx.get(
            _url("/internal/partners/available"),
            params=params,
            headers=_headers(),
            timeout=settings.partner_service_timeout_seconds,
        )
    except httpx.HTTPError as error:
        logger.warning("partnerServices unreachable for availability: %s", error)
        raise PartnerServiceUnavailableError() from error

    if response.status_code >= 400:
        logger.warning(
            "partnerServices answered %s to the availability search: %s",
            response.status_code,
            response.text[:300],
        )
        raise PartnerServiceUnavailableError()

    return [Candidate(row) for row in response.json()]


def claim(partner_id: int) -> None:
    """ONLINE -> ON_TRIP. Raises PartnerClaimConflictError on 409.

    The 409 is not a failure to handle away - it is the answer. Somebody else
    claimed this driver between the search and now, which at any real volume
    happens constantly, and the correct response is to offer the job to the next
    candidate.

    Claiming a partner who is already ON_TRIP *for this same delivery* succeeds
    and changes nothing, which is what makes a retry after a network timeout
    safe: see partnerServices' `set_dispatch_status`.
    """
    _set_status(partner_id, ON_TRIP)


def release(partner_id: int) -> None:
    """ON_TRIP -> ONLINE, when the delivery ends or the order is cancelled.

    A conflict here is swallowed rather than raised. If the driver is already
    ONLINE, or went OFFLINE, or was suspended while delivering, then "put them
    back to work" has either already happened or is no longer the right thing -
    and none of those should make a cancellation fail.
    """
    try:
        _set_status(partner_id, ONLINE)
    except PartnerClaimConflictError:
        logger.info(
            "partner %s could not be returned to online; leaving their status alone",
            partner_id,
        )


def _set_status(partner_id: int, status: str) -> None:
    try:
        response = httpx.patch(
            _url(f"/internal/partners/{partner_id}/status"),
            json={"status": status},
            headers=_headers(),
            timeout=settings.partner_service_timeout_seconds,
        )
    except httpx.HTTPError as error:
        logger.warning("partnerServices unreachable to set status: %s", error)
        raise PartnerServiceUnavailableError() from error

    if response.status_code == 409:
        raise PartnerClaimConflictError()

    if response.status_code == 404:
        # The partner does not exist. Not retryable, and not a conflict either -
        # but from the loop's point of view it is the same decision: this
        # candidate is unusable, try the next.
        logger.warning("partnerServices has no partner %s", partner_id)
        raise PartnerClaimConflictError("That partner no longer exists")

    if response.status_code >= 400:
        logger.warning(
            "partnerServices answered %s setting partner %s to %s: %s",
            response.status_code,
            partner_id,
            status,
            response.text[:300],
        )
        raise PartnerServiceUnavailableError()
