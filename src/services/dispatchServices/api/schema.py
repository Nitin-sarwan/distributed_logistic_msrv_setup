"""Request and response shapes.

Two audiences with very different needs, so two families:

* **The driver** gets an offer card. Everything on it is something a person
  reads before deciding in thirty seconds: what, where, how far, how much, how
  long left. Notably absent is the customer's phone number - that is on the
  order, and it belongs to the driver only once the job is theirs.
* **Operations** gets the attempt and its whole offer list, which is the answer
  to "why did this delivery take so long to get a driver?"
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from src.services.dispatchServices.domain.states import AttemptStatus, OfferStatus


class OfferCard(BaseModel):
    """One live offer, as the driver's app renders it.

    `seconds_remaining` is computed and sent rather than leaving the app to
    subtract `expires_at` from its own clock. A phone whose time is thirty
    seconds fast would show a countdown that ends before the offer does, and the
    driver would stop trying.
    """

    model_config = ConfigDict(from_attributes=True)

    offer_id: int
    order_id: int
    public_ref: str

    pickup_city: str | None
    drop_city: str | None
    vehicle_type: str
    package_weight_kg: Decimal
    trip_distance_km: Decimal | None

    # How far the driver is from the pickup, not the length of the trip. Two
    # different numbers that are easy to confuse, so both are named.
    pickup_distance_km: Decimal

    amount: Decimal | None
    currency: str

    expires_at: datetime
    seconds_remaining: int


class OfferResponse(BaseModel):
    """What the driver gets back from accepting or declining.

    Carries the order id on accept, because that is the driver's next call:
    `GET /api/deliveries` on the Order service, with the delivery now assigned
    to them.
    """

    model_config = ConfigDict(from_attributes=True)

    offer_id: int
    order_id: int
    status: OfferStatus
    responded_at: datetime | None


class DeclineRequest(BaseModel):
    """Optional colour on a decline.

    Free text, never parsed, and not used for any decision. It is here because
    "too far" and "wrong vehicle" are worth knowing in aggregate, and asking
    later means never having asked.
    """

    reason: str | None = Field(default=None, max_length=200)


class OfferSummary(BaseModel):
    """One row of an attempt's history, for operations."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    partner_id: int
    partner_name: str
    rank: int
    distance_km: Decimal
    status: OfferStatus
    offered_at: datetime | None
    expires_at: datetime | None
    responded_at: datetime | None


class AttemptResponse(BaseModel):
    """One order's search, start to finish.

    The whole point of Dispatch owning a database. Without this, "we asked six
    drivers over ninety seconds and the first four never answered" is not
    recorded anywhere - and that is exactly what someone needs when a customer
    complains.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    public_ref: str
    customer_id: int
    status: AttemptStatus

    vehicle_type: str
    package_weight_kg: Decimal
    radius_km: Decimal
    offers_made: int

    deadline_at: datetime
    assigned_partner_id: int | None
    assigned_at: datetime | None
    failure_reason: str | None
    ended_at: datetime | None

    correlation_id: str | None
    created_at: datetime

    offers: list[OfferSummary] = []
