"""Request and response shapes.

Money leaves this service as a string. Pydantic serialises `Decimal` that way in
JSON, and it is the same convention orderServices uses for `quoted_amount`: a
float has already lost the cents by the time a browser parses it.
"""

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

COORDINATE_PLACES = Decimal("0.000001")


class Point(BaseModel):
    latitude: Decimal = Field(ge=-90, le=90)
    longitude: Decimal = Field(ge=-180, le=180)

    @field_validator("latitude", "longitude")
    @classmethod
    def round_coordinate(cls, value: Decimal) -> Decimal:
        # Six places is what the quote columns store. Rounding here means the
        # value priced is the value saved.
        return value.quantize(COORDINATE_PLACES, rounding=ROUND_HALF_UP)


class FareBreakdown(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    base_fare: Decimal
    distance_charge: Decimal
    weight_charge: Decimal
    minimum_fare_adjustment: Decimal


# ── Public: the estimate ──────────────────────────────────────────────────


class EstimateRequest(BaseModel):
    pickup: Point
    drop: Point
    # Optional because the home page asks for a price before it asks what is
    # being sent. Absent means "a light parcel" — see ESTIMATE_DEFAULT_WEIGHT_KG.
    weight_kg: Decimal | None = Field(default=None, gt=0, le=10_000)


class EstimateOptionResponse(BaseModel):
    vehicle_type: str
    available: bool
    unavailable_reason: str | None
    max_weight_kg: Decimal
    amount: Decimal
    currency: str
    breakdown: FareBreakdown


class EstimateResponse(BaseModel):
    """Every vehicle's price for one trip. Not a quote, and says so.

    `distance_km` is what fares are charged on — the straight line scaled for
    road detours — and `straight_line_km` is sent alongside it so the UI can
    explain the gap rather than look like it has measured wrong.
    """

    is_estimate: bool = True
    straight_line_km: Decimal
    distance_km: Decimal
    weight_kg: Decimal
    options: list[EstimateOptionResponse]


class TariffResponse(BaseModel):
    """The public rate card."""

    model_config = ConfigDict(from_attributes=True)

    vehicle_type: str
    base_fare: Decimal
    per_km: Decimal
    minimum_fare: Decimal
    included_weight_kg: Decimal
    per_kg: Decimal
    max_weight_kg: Decimal
    currency: str
    is_active: bool


# ── Internal: the quote ───────────────────────────────────────────────────


class QuoteRequest(BaseModel):
    pickup: Point
    drop: Point
    vehicle_type: str = Field(max_length=20)
    weight_kg: Decimal = Field(gt=0, le=10_000)
    # Recorded, never trusted for anything. Order passes the id its own session
    # check resolved; this service has no session check on /internal at all.
    customer_id: int | None = None


class QuoteResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    quote_id: str
    vehicle_type: str
    weight_kg: Decimal
    straight_line_km: Decimal
    distance_km: Decimal
    amount: Decimal
    currency: str
    breakdown: FareBreakdown
    expires_at: datetime
    created_at: datetime


class TariffUpdate(BaseModel):
    """Partial. Anything omitted is left alone."""

    base_fare: Decimal | None = Field(default=None, ge=0)
    per_km: Decimal | None = Field(default=None, ge=0)
    minimum_fare: Decimal | None = Field(default=None, ge=0)
    included_weight_kg: Decimal | None = Field(default=None, ge=0)
    per_kg: Decimal | None = Field(default=None, ge=0)
    max_weight_kg: Decimal | None = Field(default=None, gt=0)
    is_active: bool | None = None


def to_quote_response(quote) -> QuoteResponse:
    return QuoteResponse(
        quote_id=quote.id,
        vehicle_type=quote.vehicle_type,
        weight_kg=quote.weight_kg,
        straight_line_km=quote.straight_line_km,
        distance_km=quote.distance_km,
        amount=quote.amount,
        currency=quote.currency,
        breakdown=FareBreakdown.model_validate(quote),
        expires_at=quote.expires_at,
        created_at=quote.created_at,
    )
