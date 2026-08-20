"""The HTTP contract for partnerServices.

Kept separate from `models/` on purpose: those are the tables, these are what
clients send and receive. Columns that exist only for this service's own
bookkeeping — `token_secret`, `password_hash`, `is_deleted` — appear in no
response model here, and there is no request model anywhere in this file with a
`partner_id` field. Identity always comes from the authenticated session; a
`partner_id` in a body is a field an attacker gets to choose.
"""

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from src.services.partnerServices.utils.enums import (
    PARTNER_SETTABLE_STATUSES,
    VEHICLE_MAX_CAPACITY_KG,
    PartnerStatus,
    VehicleStatus,
    VehicleType,
)

# NUMERIC(9,6) — six decimal places is about 10cm, which is the precision a
# driver's pin actually needs. Matches userServices' address columns.
COORDINATE_PLACES = Decimal("0.000001")


def _quantize(value: Decimal) -> Decimal:
    """Round a coordinate to the column's six decimal places.

    Done here rather than left to Postgres so the value that is validated is the
    value that gets stored. A raw GPS reading with nine decimals would otherwise
    pass the range check and then be rounded on insert — and for longitude it
    could exceed NUMERIC(9,6)'s nine total digits and fail outright.
    """
    return value.quantize(COORDINATE_PLACES, rounding=ROUND_HALF_UP)


def _validate_phone(value: str) -> str:
    cleaned = value.strip()
    if not cleaned.isdigit():
        raise ValueError("Phone must contain only digits")
    if len(cleaned) != 10:
        raise ValueError("Phone must be 10 digits")
    return cleaned


def _normalize_vehicle_number(value: str) -> str:
    """Strip a number plate down to its letters and digits, uppercased.

    "DL 01 AB 1234", "dl-01-ab-1234" and "DL01AB1234" are one vehicle. Without
    this the unique constraint is trivially defeated by a space, and the same
    van gets registered to two partners.
    """
    cleaned = "".join(ch for ch in value if ch.isalnum()).upper()
    if not 4 <= len(cleaned) <= 20:
        raise ValueError("Vehicle number must be 4-20 alphanumeric characters")
    return cleaned


# ── Auth ───────────────────────────────────────────────────────────────────


class RegisterPartner(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    phone: str
    password: str = Field(min_length=8)
    # Optional, unlike userServices. A driver signs in with the number on their
    # SIM; requiring an address many of them do not have would be a barrier for
    # no gain.
    email: EmailStr | None = None

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        return _validate_phone(value)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr | None) -> str | None:
        return None if value is None else str(value).strip().lower()

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Name cannot be blank")
        return cleaned


class LoginPartner(BaseModel):
    # Phone, not email — the partner app's sign-in field.
    phone: str
    password: str

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        # Must match RegisterPartner's normalisation, or a number stored without
        # its stray whitespace could never be signed in with.
        return _validate_phone(value)


class ChangePassword(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class RefreshRequest(BaseModel):
    refresh_token: str


# ── Partner views ──────────────────────────────────────────────────────────


class PartnerResponse(BaseModel):
    """What a partner sees about themselves. Never returned to anyone else."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    phone: str
    email: str | None

    status: PartnerStatus
    is_verified: bool

    # Serialised as JSON numbers rather than Decimal, which Pydantic would emit
    # as a string. Six decimal places is well within float64's exact range.
    current_latitude: float | None
    current_longitude: float | None
    location_updated_at: datetime | None

    rating: float
    rating_count: int

    created_at: datetime
    updated_at: datetime | None


class PartnerPublicResponse(BaseModel):
    """What another service may learn about a partner.

    Deliberately narrower than PartnerResponse. An Order service showing "your
    driver is on the way" needs a name, a number to call, and a rating. It has
    no use for the partner's email or their exact live coordinates, so those are
    absent from the shape rather than filtered out by whoever consumes it.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    phone: str
    status: PartnerStatus
    is_verified: bool
    rating: float
    rating_count: int


class PartnerUpdate(BaseModel):
    """Partial update of a partner's own profile.

    Only fields the partner may set on themselves. Notably absent: `status`
    (its own endpoint, with rules), `is_verified` (operations decides), `rating`
    (customers decide), and `phone` — changing the login identity needs fresh
    proof of ownership of the new number, and an OTP flow does not exist yet.
    """

    name: str | None = Field(default=None, min_length=1, max_length=100)
    email: EmailStr | None = None

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr | None) -> str | None:
        return None if value is None else str(value).strip().lower()

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Name cannot be blank")
        return cleaned


class StatusUpdate(BaseModel):
    status: PartnerStatus

    @field_validator("status")
    @classmethod
    def only_self_settable(cls, value: PartnerStatus) -> PartnerStatus:
        # ON_TRIP belongs to Dispatch and SUSPENDED to operations. Rejecting
        # them here rather than only in the service keeps the rule visible in
        # the OpenAPI schema — and the service re-checks anyway.
        if value not in PARTNER_SETTABLE_STATUSES:
            allowed = ", ".join(sorted(s.value for s in PARTNER_SETTABLE_STATUSES))
            raise ValueError(f"A partner may only set: {allowed}")
        return value


class LocationUpdate(BaseModel):
    latitude: Decimal = Field(ge=-90, le=90)
    longitude: Decimal = Field(ge=-180, le=180)

    @field_validator("latitude", "longitude")
    @classmethod
    def round_coordinate(cls, value: Decimal) -> Decimal:
        return _quantize(value)


# ── Vehicles ───────────────────────────────────────────────────────────────


class VehicleCreate(BaseModel):
    vehicle_type: VehicleType
    vehicle_number: str
    capacity: Decimal = Field(gt=0, description="Load capacity in kilograms")
    model_name: str | None = Field(default=None, max_length=100)

    @field_validator("vehicle_number")
    @classmethod
    def normalize_number(cls, value: str) -> str:
        return _normalize_vehicle_number(value)

    @field_validator("model_name")
    @classmethod
    def strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @model_validator(mode="after")
    def capacity_fits_type(self) -> "VehicleCreate":
        # Cross-field, so it cannot be a field_validator: neither value means
        # anything without the other. A two-wheeler declared at 900kg would
        # otherwise win every Dispatch query for heavy loads.
        maximum = VEHICLE_MAX_CAPACITY_KG[self.vehicle_type]
        if self.capacity > maximum:
            raise ValueError(
                f"A {self.vehicle_type.value} cannot carry more than {maximum}kg"
            )
        return self


class VehicleUpdate(BaseModel):
    """Partial update. Only the fields sent are changed.

    A separate model rather than VehicleCreate with defaults, so "not sent" and
    "sent as null" stay distinguishable — otherwise a PATCH of only the model
    name would blank the capacity.

    `vehicle_type`, `vehicle_number` and `status` are all absent. The first two
    are verified facts about a physical vehicle, and editing them after approval
    would carry that approval over to a different vehicle; `status` is set by
    the activate endpoint and by operations. Adding a second vehicle is the
    supported path.
    """

    capacity: Decimal | None = Field(default=None, gt=0)
    model_name: str | None = Field(default=None, max_length=100)

    @field_validator("model_name")
    @classmethod
    def strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class VehicleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    partner_id: int
    vehicle_type: VehicleType
    vehicle_number: str
    model_name: str | None
    capacity: float
    status: VehicleStatus
    created_at: datetime
    updated_at: datetime | None


# ── Auth responses ─────────────────────────────────────────────────────────


class AuthResponse(BaseModel):
    """Returned by both register and login — same shape either way."""

    model_config = ConfigDict(from_attributes=True)

    partner: PartnerResponse
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

    partner: PartnerResponse
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    device_session: str
    device_id: str


# ── Internal (service-to-service) ──────────────────────────────────────────


class AvailablePartner(BaseModel):
    """One row of Dispatch's answer.

    `distance_km` is included because Dispatch's ranking is its own business —
    it may weigh distance against rating, load balancing, or a partner's recent
    rejections. Returning the number rather than only the order lets it do that
    without asking again.
    """

    model_config = ConfigDict(from_attributes=True)

    partner: PartnerPublicResponse
    vehicle: VehicleResponse
    distance_km: float


class PartnerDetail(BaseModel):
    """A single partner plus the vehicle they are driving.

    What an Order service needs to render "your driver is on the way": who, and
    what to look for at the kerb.
    """

    model_config = ConfigDict(from_attributes=True)

    partner: PartnerPublicResponse
    vehicle: VehicleResponse | None


class AssignStatusUpdate(BaseModel):
    """Dispatch moving a partner between ONLINE and ON_TRIP."""

    status: PartnerStatus

    @field_validator("status")
    @classmethod
    def only_dispatch_settable(cls, value: PartnerStatus) -> PartnerStatus:
        if value not in (PartnerStatus.ONLINE, PartnerStatus.ON_TRIP):
            raise ValueError("Dispatch may only set: online, on_trip")
        return value


class RatingCreate(BaseModel):
    """A completed delivery's rating, submitted by whoever owns orders.

    Not an endpoint a partner or a customer calls directly: the caller has to be
    a service that knows the delivery actually happened, which is why this lives
    behind /internal.
    """

    rating: int = Field(ge=1, le=5)


class VerificationDecision(BaseModel):
    """An operations decision on a partner's KYC."""

    approve: bool


class SuspensionDecision(BaseModel):
    """Operations barring a partner from working, or letting them back in.

    Distinct from verification. Rejected KYC means "we have not established who
    you are"; suspension means "we know, and you may not work right now". They
    are lifted by different people for different reasons, so collapsing them
    into one flag would lose which one applies.
    """

    suspended: bool


class VehicleVerificationDecision(BaseModel):
    """An operations decision on a vehicle's documents.

    Approving moves a PENDING vehicle to INACTIVE, not ACTIVE — cleared to be
    driven is not the same as currently being driven, and which vehicle is on
    the road is the partner's choice to make.
    """

    approve: bool
