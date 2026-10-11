"""Where the assigned driver is, for the customer's live map.

The one read Order makes of partnerServices, and a narrow one: a location-only
endpoint that exists for this, rather than the partner record. Order decides
whether the customer may see it — only while that driver is on that customer's
delivery — and partnerServices only answers where the driver is.

Failures return None instead of raising. A tracking map with no driver pin for
one refresh is a non-event; a tracking request that errors is a broken page.
"""

import logging
from datetime import datetime

import httpx

from src.services.orderServices.config import settings

logger = logging.getLogger(__name__)


class PartnerPosition:
    __slots__ = ("latitude", "longitude", "updated_at")

    def __init__(self, latitude: float, longitude: float, updated_at: datetime | None):
        self.latitude = latitude
        self.longitude = longitude
        self.updated_at = updated_at


def fetch_position(partner_id: int) -> PartnerPosition | None:
    url = (
        f"{settings.partner_service_url.rstrip('/')}"
        f"/internal/partners/{partner_id}/location"
    )
    try:
        response = httpx.get(
            url,
            headers={"X-Internal-Key": settings.internal_api_key},
            timeout=settings.partner_service_timeout_seconds,
        )
    except httpx.HTTPError as error:
        logger.warning("partnerServices unreachable for location: %s", error)
        return None

    if response.status_code >= 400:
        logger.warning(
            "partnerServices answered %s for partner %s location",
            response.status_code,
            partner_id,
        )
        return None

    data = response.json()
    if data.get("latitude") is None or data.get("longitude") is None:
        return None

    updated = data.get("updated_at")
    return PartnerPosition(
        latitude=float(data["latitude"]),
        longitude=float(data["longitude"]),
        updated_at=datetime.fromisoformat(updated) if updated else None,
    )
