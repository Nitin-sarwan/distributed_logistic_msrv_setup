from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class RegisterUser(BaseModel):
    name: str
    email: EmailStr
    phone: str 
    password: str

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        return str(value).strip().lower()

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str ) -> str:
        if not value.isdigit():
            raise ValueError("Phone must contain only digits")
        if len(value) != 10:
            raise ValueError("Phone must of 10 digits")
        return value


class UserResponse(BaseModel):
    # Never expose password or token_secret.
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: EmailStr
    phone: str 
    created_at: datetime


class LoginUser(BaseModel):
    # No from_attributes here: this is client input, never built from an ORM row.
    email: EmailStr
    password: str

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        # Must match RegisterUser's normalisation, or a user who registered as
        # "Aer@Gmail.com" could never log in as "aer@gmail.com".
        return str(value).strip().lower()


class AuthResponse(BaseModel):
    """Returned by both register and login — same shape either way."""

    model_config = ConfigDict(from_attributes=True)

    user: UserResponse
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_at: datetime
    refresh_expires_at: datetime
    device_session: str
    device_id: str


class RefreshResponse(BaseModel):
    """A refresh returns a new access token only — the refresh token persists."""

    model_config = ConfigDict(from_attributes=True)

    user: UserResponse
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    device_session: str
    device_id: str


class RefreshRequest(BaseModel):
    refresh_token: str


class ChangePassword(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class ForgotPassword(BaseModel):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        return str(value).strip().lower()


class ResetPassword(BaseModel):
    token: str
    new_password: str = Field(min_length=8)


# ── Addresses ──────────────────────────────────────────────────────────────
#
# The HTTP contract for the `address` table. `user_id` is deliberately absent
# from every request model: it comes from the authenticated session. Accepting
# it from the client would let anyone write an address onto someone else's
# account, and no amount of checking afterwards is as safe as never taking it.

# NUMERIC(9,6) — six decimal places is about 10cm, which is the precision a
# driver's pin actually needs.
COORDINATE_PLACES = Decimal("0.000001")


def _quantize(value: Decimal) -> Decimal:
    """Round a coordinate to the column's six decimal places.

    Done here rather than left to Postgres so the value that is validated is
    the value that gets stored. A GPS reading with nine decimals would
    otherwise pass the range check and then be rounded on insert, and for
    longitude it could exceed NUMERIC(9,6)'s nine total digits and fail
    outright.
    """
    return value.quantize(COORDINATE_PLACES, rounding=ROUND_HALF_UP)


class AddressBase(BaseModel):
    address_line1: str = Field(min_length=1, max_length=500)
    address_line2: str | None = Field(default=None, max_length=500)
    city: str = Field(min_length=1, max_length=255)
    pin_code: str
    latitude: Decimal = Field(ge=-90, le=90)
    longitude: Decimal = Field(ge=-180, le=180)

    @field_validator("address_line1", "city")
    @classmethod
    def strip_required(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("This field cannot be blank")
        return cleaned

    @field_validator("address_line2")
    @classmethod
    def strip_optional(cls, value: str | None) -> str | None:
        # An empty second line is an absent one, so store NULL rather than "".
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("pin_code")
    @classmethod
    def validate_pin_code(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned.isdigit() or len(cleaned) != 6:
            raise ValueError("PIN code must be exactly 6 digits")
        return cleaned

    @field_validator("latitude", "longitude")
    @classmethod
    def round_coordinate(cls, value: Decimal) -> Decimal:
        return _quantize(value)


class AddressCreate(AddressBase):
    """Body for creating an address. Every field except line 2 is required."""


class AddressUpdate(BaseModel):
    """Body for a partial update. Only the fields sent are changed.

    A separate model rather than `AddressCreate` with defaults, so that
    "not sent" and "sent as null" stay distinguishable — otherwise a PATCH of
    only the city would silently blank the coordinates.
    """

    address_line1: str | None = Field(default=None, min_length=1, max_length=500)
    address_line2: str | None = Field(default=None, max_length=500)
    city: str | None = Field(default=None, min_length=1, max_length=255)
    pin_code: str | None = None
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)

    # Written out rather than reused from AddressBase: every field here is
    # optional, so each validator has to let None through untouched. A shared
    # validator would need the same guard anyway, and would read worse.

    @field_validator("address_line1", "city")
    @classmethod
    def strip_required(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("This field cannot be blank")
        return cleaned

    @field_validator("address_line2")
    @classmethod
    def strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("pin_code")
    @classmethod
    def validate_pin_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned.isdigit() or len(cleaned) != 6:
            raise ValueError("PIN code must be exactly 6 digits")
        return cleaned

    @field_validator("latitude", "longitude")
    @classmethod
    def round_coordinate(cls, value: Decimal | None) -> Decimal | None:
        return None if value is None else _quantize(value)


class AddressResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    address_line1: str
    address_line2: str | None
    city: str
    pin_code: str
    # Serialised as JSON numbers rather than Decimal, which Pydantic would emit
    # as a string. Six decimal places is well within float64's exact range, so
    # nothing is lost.
    latitude: float
    longitude: float


# ── Geocoding ──────────────────────────────────────────────────────────────
#
# The contract for /api/geo. Nothing here touches a table: these are places the
# provider knows about, not rows this service owns. The field names deliberately
# match AddressCreate's, so the frontend can fill the address form from a search
# result without a translation step in between.


class PlaceResponse(BaseModel):
    """One resolved location, in the shape the address form needs.

    `city` can come back empty and `pin_code` null: a pin dropped on an unnamed
    road genuinely has neither, and inventing values to keep the shape tidy
    would put wrong data in a column a driver relies on. The user fills the gap;
    the coordinates — the part that has to be right — are already correct.
    """

    latitude: float
    longitude: float

    # The human sentence for a suggestion list. Kept separate from
    # address_line1, which is the shorter thing that belongs in the form field.
    label: str

    address_line1: str
    address_line2: str | None
    city: str
    pin_code: str | None

    # Stable per result, so the frontend has a list key that does not shift when
    # the same query is re-run.
    place_id: str


class ReverseGeocodeResponse(BaseModel):
    """What is at a point.

    The coordinates are echoed back because they are the authoritative half of
    the answer — `place` is a convenience that may be null for a field, a new
    road, or the middle of a lake, and the caller still has a usable pin.
    """

    latitude: float
    longitude: float
    place: PlaceResponse | None
