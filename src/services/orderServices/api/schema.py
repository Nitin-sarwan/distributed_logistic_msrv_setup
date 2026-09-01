"""The HTTP contract for orderServices.

`customer_id` appears in no request model. Identity comes from the session, and
a customer id in a body is a field an attacker gets to choose.
"""

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.services.orderServices.domain.states import OrderStatus

COORDINATE_PLACES = Decimal("0.000001")


class AddressInput(BaseModel):
    """An address typed in rather than saved.

    The escape hatch that removes the User dependency: a one-off delivery to
    somewhere not in the address book is a real case, and these values *are* the
    agreement — the customer typed them, so there is nothing to look up.
    """

    address_line1: str = Field(min_length=1, max_length=500)
    address_line2: str | None = Field(default=None, max_length=500)
    city: str = Field(min_length=1, max_length=255)
    pin_code: str
    latitude: Decimal = Field(ge=-90, le=90)
    longitude: Decimal = Field(ge=-180, le=180)
    contact_name: str = Field(min_length=1, max_length=100)
    contact_phone: str

    @field_validator("pin_code")
    @classmethod
    def validate_pin_code(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned.isdigit() or len(cleaned) != 6:
            raise ValueError("PIN code must be exactly 6 digits")
        return cleaned

    @field_validator("contact_phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned.isdigit() or len(cleaned) != 10:
            raise ValueError("Phone must be 10 digits")
        return cleaned

    @field_validator("latitude", "longitude")
    @classmethod
    def round_coordinate(cls, value: Decimal) -> Decimal:
        # Six places is what the column stores. Rounding here means the value
        # validated is the value saved.
        return value.quantize(COORDINATE_PLACES, rounding=ROUND_HALF_UP)


class OrderEndpoint(BaseModel):
    """One end of the trip: a saved address, or one given inline.

    Exactly one of the two. Accepting both would leave the service choosing
    which the customer meant, and it has no way to know.
    """

    address_id: int | None = None
    address: AddressInput | None = None

    @model_validator(mode="after")
    def exactly_one(self) -> "OrderEndpoint":
        if (self.address_id is None) == (self.address is None):
            raise ValueError("Provide either address_id or address, not both")
        return self


class CreateOrder(BaseModel):
    pickup: OrderEndpoint
    drop: OrderEndpoint
    vehicle_type: str = Field(max_length=20)
    weight_kg: Decimal = Field(gt=0, le=10_000)
    description: str | None = Field(default=None, max_length=500)

    @field_validator("description")
    @classmethod
    def strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class CancelOrder(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class AddressSnapshot(BaseModel):
    """How an end of the trip is returned. Flat, because it is a snapshot of
    what was agreed rather than a live address anyone can follow."""

    model_config = ConfigDict(from_attributes=True)

    line1: str
    line2: str | None
    city: str
    pin_code: str
    latitude: float
    longitude: float
    contact_name: str
    contact_phone: str


class OrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    public_ref: str
    status: OrderStatus

    pickup: AddressSnapshot
    drop: AddressSnapshot

    vehicle_type: str
    package_weight_kg: float
    package_description: str | None

    distance_km: float | None
    # Money as a string: a float has already lost the cents by the time the
    # browser parses it.
    quoted_amount: str | None
    currency: str
    quote_expires_at: datetime | None

    partner_id: int | None
    partner_name: str | None
    partner_phone: str | None
    vehicle_number: str | None

    created_at: datetime
    confirmed_at: datetime | None
    picked_up_at: datetime | None
    delivered_at: datetime | None
    cancelled_at: datetime | None
    cancellation_reason: str | None


class StatusHistoryEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    from_status: str | None
    to_status: str
    actor: str
    occurred_at: datetime


def to_address_snapshot(order, prefix: str) -> AddressSnapshot:
    """Gather one end's flat columns back into a nested shape.

    The columns are flat because a snapshot has no identity of its own — there
    is no address row to join to — and the response is nested because that is
    what a client renders. This function is the only place the two shapes meet.
    """
    return AddressSnapshot(
        line1=getattr(order, f"{prefix}_line1"),
        line2=getattr(order, f"{prefix}_line2"),
        city=getattr(order, f"{prefix}_city"),
        pin_code=getattr(order, f"{prefix}_pin_code"),
        latitude=float(getattr(order, f"{prefix}_latitude")),
        longitude=float(getattr(order, f"{prefix}_longitude")),
        contact_name=getattr(order, f"{prefix}_contact_name"),
        contact_phone=getattr(order, f"{prefix}_contact_phone"),
    )


def to_order_response(order) -> OrderResponse:
    return OrderResponse(
        id=order.id,
        public_ref=order.public_ref,
        status=OrderStatus(order.status),
        pickup=to_address_snapshot(order, "pickup"),
        drop=to_address_snapshot(order, "drop"),
        vehicle_type=order.vehicle_type,
        package_weight_kg=float(order.package_weight_kg),
        package_description=order.package_description,
        distance_km=float(order.distance_km) if order.distance_km is not None else None,
        quoted_amount=str(order.quoted_amount) if order.quoted_amount is not None else None,
        currency=order.currency,
        quote_expires_at=order.quote_expires_at,
        partner_id=order.partner_id,
        partner_name=order.partner_name,
        partner_phone=order.partner_phone,
        vehicle_number=order.vehicle_number,
        created_at=order.created_at,
        confirmed_at=order.confirmed_at,
        picked_up_at=order.picked_up_at,
        delivered_at=order.delivered_at,
        cancelled_at=order.cancelled_at,
        cancellation_reason=order.cancellation_reason,
    )
