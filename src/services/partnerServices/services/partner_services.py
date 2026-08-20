import secrets
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.database.session_store import (
    create_session,
    get_active_session,
    revoke_children,
    revoke_user_sessions,
)
from src.services.partnerServices.api.schema import (
    LocationUpdate,
    LoginPartner,
    PartnerUpdate,
    RegisterPartner,
)
from src.services.partnerServices.config import settings
from src.services.partnerServices.models.partner_model import Partner
from src.services.partnerServices.repositories.partner_repositories import (
    PartnerRepository,
)
from src.services.partnerServices.repositories.vehicle_repositories import (
    VehicleRepository,
)
from src.services.partnerServices.utils.enums import (
    PARTNER_SETTABLE_STATUSES,
    PartnerStatus,
)
from src.services.partnerServices.utils.exceptions import (
    EmailAlreadyExistsError,
    InvalidCredentialsError,
    InvalidStatusTransitionError,
    NoActiveVehicleError,
    NotVerifiedError,
    PhoneAlreadyExistsError,
    RegistrationFailedError,
)
from src.services.partnerServices.utils.security import (
    TOKEN_SUBJECT,
    create_access_token,
    create_refresh_token,
    decrypt_data,
    hash_password,
    verify_password,
)

# The stored average carries one decimal place, matching NUMERIC(2,1).
RATING_PLACES = Decimal("0.1")


class PartnerService:
    """Business rules for partners. Talks to the DB only through repositories."""

    def __init__(self, db: Session):
        self.db = db
        self.partner_repository = PartnerRepository(db)
        self.vehicle_repository = VehicleRepository(db)

    # ── Authentication ────────────────────────────────────────────────────

    def validate_and_get_partner(self, phone: str, password: str) -> Partner:
        partner = self.partner_repository.find_by_phone(phone)

        if not partner:
            raise InvalidCredentialsError()

        if not verify_password(password, partner.password_hash):
            raise InvalidCredentialsError()

        return partner

    def register_partner(self, payload: RegisterPartner) -> Partner:
        # Both columns are unique, so check them up front rather than letting
        # the insert fail with an IntegrityError — that turns a clean 409 into
        # a 500 and tells the caller nothing useful.
        existing = self.partner_repository.find_by_phone_or_email(
            payload.phone, payload.email
        )
        if existing:
            if existing.phone == payload.phone:
                raise PhoneAlreadyExistsError()
            raise EmailAlreadyExistsError()

        partner = Partner(
            name=payload.name,
            phone=payload.phone,
            email=payload.email,
            password_hash=hash_password(payload.password),
            token_secret=secrets.token_hex(32),
        )
        # Everything else takes its column default: OFFLINE, unverified, rating
        # 5.0 over zero samples. A new partner is deliberately invisible to
        # Dispatch until operations verifies them.
        return self.partner_repository.create(partner)

    def create_partner_session(self, request, partner: Partner) -> dict:
        """Issue tokens for a partner and record the sessions.

        Shared by register and login, so the session shape stays identical
        however the partner arrived.
        """
        refresh_token, _rjti, refresh_expires = create_refresh_token(partner)
        token, _jti, expires_at = create_access_token(partner)

        # Sessions are shared infrastructure, so they go to Mongo rather than
        # this service's Postgres. `app_type` is what keeps partner 5's sessions
        # distinct from user 5's in that shared collection.
        refresh_session = create_session(
            request,
            refresh_token,
            partner.id,
            app_type=settings.partner_app_type,
            expires_at=refresh_expires,
            token_type="refresh",
        )

        # The access session points at the refresh token that minted it, so
        # revoking the refresh token takes every access token with it.
        create_session(
            request,
            token,
            partner.id,
            app_type=settings.partner_app_type,
            expires_at=expires_at,
            token_type="auth",
            parent_token=refresh_token,
            device_session=refresh_session["device_session"],
            device_id=refresh_session["device_id"],
        )

        return {
            "access_token": token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
            "refresh_expires_at": refresh_expires,
            "device_session": refresh_session["device_session"],
            "device_id": refresh_session["device_id"],
        }

    def register(self, request, payload: RegisterPartner) -> dict:
        """Full registration flow: persist, verify, then issue a session."""
        try:
            partner = self.register_partner(payload)
        except (PhoneAlreadyExistsError, EmailAlreadyExistsError) as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=error.message,
            ) from error

        # Confirm the stored hash actually verifies before handing out a token —
        # otherwise the account exists but can never be signed into, and the
        # partner has no way to discover that except by failing to log in.
        try:
            partner = self.validate_and_get_partner(partner.phone, payload.password)
        except InvalidCredentialsError as error:
            self.partner_repository.delete(partner)
            raise RegistrationFailedError() from error

        return {"partner": partner, **self.create_partner_session(request, partner)}

    def login(self, request, payload: LoginPartner) -> dict:
        """Authenticate, then issue a session — the same one register issues."""
        try:
            partner = self.validate_and_get_partner(payload.phone, payload.password)
        except InvalidCredentialsError as error:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=error.message,
            ) from error

        return {"partner": partner, **self.create_partner_session(request, partner)}

    def refresh(self, request, refresh_token: str) -> dict:
        """Exchange a refresh token for a new access token.

        The refresh token stays valid — only the access token is replaced. The
        old access session is revoked, so a stolen one dies at the next refresh.
        """
        invalid = HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )

        session = get_active_session(refresh_token)
        if session is None or session.get("token_type") != "refresh":
            raise invalid

        # A user's refresh token must not mint a partner access token. Decryption
        # below would fail anyway — the secrets live in different databases — but
        # checking the discriminator first means the id lookup never runs against
        # the wrong table in the first place.
        if session.get("app_type") != settings.partner_app_type:
            raise invalid

        partner = self.partner_repository.find_by_id(session["user"])
        if partner is None:
            raise invalid

        payload = decrypt_data(refresh_token, partner.token_secret)
        if (
            payload is None
            or payload.get("type") != "refresh"
            or payload.get("subject") != TOKEN_SUBJECT
        ):
            raise invalid

        # Retire the access tokens this refresh token previously minted, so only
        # the newest one works.
        revoke_children(refresh_token)

        token, _jti, expires_at = create_access_token(partner)
        create_session(
            request,
            token,
            partner.id,
            app_type=settings.partner_app_type,
            expires_at=expires_at,
            token_type="auth",
            parent_token=refresh_token,
            device_session=session.get("device_session"),
            device_id=session.get("device_id"),
        )

        return {
            "partner": partner,
            "access_token": token,
            "expires_at": expires_at,
            "device_session": session.get("device_session", ""),
            "device_id": session.get("device_id", ""),
        }

    def logout_everywhere(self, partner: Partner) -> int:
        """Revoke every session on every device.

        token_secret is rotated as well, so even a session record that somehow
        survived could not have its token decrypted.
        """
        revoked = revoke_user_sessions(
            partner.id, app_type=settings.partner_app_type
        )
        partner.token_secret = secrets.token_hex(32)
        self.partner_repository.save(partner)
        return revoked

    def change_password(self, partner: Partner, current: str, new: str) -> None:
        """Change a password for a signed-in partner.

        Requires the current password: an unattended unlocked phone must not be
        enough to take over the account permanently.
        """
        if not verify_password(current, partner.password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Current password is incorrect",
            )

        partner.password_hash = hash_password(new)
        partner.token_secret = secrets.token_hex(32)
        self.partner_repository.save(partner)

        # Rotating token_secret already invalidates every token; revoking the
        # sessions makes that visible in the store rather than only at decrypt.
        revoke_user_sessions(partner.id, app_type=settings.partner_app_type)

    # ── Profile ───────────────────────────────────────────────────────────

    def update_profile(self, partner: Partner, payload: PartnerUpdate) -> Partner:
        # exclude_unset, not exclude_none: it is what separates "field omitted"
        # from "field explicitly set to null". Without it, a PATCH changing only
        # the name would blank the email.
        changes = payload.model_dump(exclude_unset=True)

        email = changes.get("email")
        if email is not None and email != partner.email:
            clash = self.partner_repository.find_by_email(email)
            if clash is not None and clash.id != partner.id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Email already registered",
                )

        for field, value in changes.items():
            setattr(partner, field, value)

        return self.partner_repository.save(partner)

    # ── Availability ──────────────────────────────────────────────────────

    def set_status(self, partner: Partner, new_status: PartnerStatus) -> Partner:
        """A partner switching themselves on or off.

        Three refusals, in order of how badly each would break something:

        1. **Mid-delivery.** ON_TRIP is not the partner's to clear. Letting them
           go offline with a live order would strand a customer with a delivery
           that no longer belongs to anybody.
        2. **Suspended.** A suspension a partner can lift is not a suspension.
        3. **Not verified, or no active vehicle.** Refused at the point of going
           online rather than silently filtered out of the Dispatch query — a
           driver sitting at a green "online" screen receiving nothing has no
           way to find out why.
        """
        if new_status not in PARTNER_SETTABLE_STATUSES:
            raise InvalidStatusTransitionError(
                f"A partner may not set status to {new_status.value}"
            )

        current = PartnerStatus(partner.status)

        if current is PartnerStatus.ON_TRIP:
            raise InvalidStatusTransitionError(
                "Finish or hand back the current delivery first"
            )

        if current is PartnerStatus.SUSPENDED:
            raise InvalidStatusTransitionError(
                "This account is suspended. Contact support."
            )

        if new_status is PartnerStatus.ONLINE:
            if not partner.is_verified:
                raise NotVerifiedError()
            if self.vehicle_repository.find_active_for_partner(partner.id) is None:
                raise NoActiveVehicleError()

        partner.status = new_status.value
        return self.partner_repository.save(partner)

    def update_location(self, partner: Partner, payload: LocationUpdate) -> Partner:
        """Record the partner's current position — the app's heartbeat.

        Accepted in every status, including OFFLINE. It is tempting to reject
        those, but the timestamp is what Dispatch uses to decide whether a pin
        is trustworthy, and a partner who goes online should be routable
        immediately rather than after their next heartbeat.
        """
        partner.current_latitude = payload.latitude
        partner.current_longitude = payload.longitude
        # The database's clock, not this process's. The availability query
        # measures staleness against `now()` inside Postgres, so a timestamp
        # written from a container whose time has drifted would make fresh pins
        # look stale — or, worse, stale ones look fresh.
        partner.location_updated_at = func.now()
        return self.partner_repository.save(partner)

    # ── Called by other services ──────────────────────────────────────────

    def find_available(
        self,
        latitude: float,
        longitude: float,
        radius_km: float,
        limit: int,
        vehicle_type: str | None = None,
        min_capacity: Decimal | None = None,
    ) -> list[dict]:
        rows = self.partner_repository.find_available(
            latitude=latitude,
            longitude=longitude,
            radius_km=radius_km,
            stale_after_minutes=settings.partner_location_stale_minutes,
            limit=limit,
            vehicle_type=vehicle_type,
            min_capacity=min_capacity,
        )
        return [
            {"partner": partner, "vehicle": vehicle, "distance_km": distance}
            for partner, vehicle, distance in rows
        ]

    def get_detail(self, partner_id: int) -> dict | None:
        partner = self.partner_repository.find_by_id(partner_id)
        if partner is None:
            return None
        return {
            "partner": partner,
            "vehicle": self.vehicle_repository.find_active_for_partner(partner.id),
        }

    def set_dispatch_status(
        self, partner: Partner, new_status: PartnerStatus
    ) -> Partner:
        """Dispatch claiming a partner for an order, or handing them back.

        ONLINE -> ON_TRIP is an assignment; ON_TRIP -> ONLINE is a release. Any
        other pair is refused, and the refusal is the point: claiming a partner
        who is already ON_TRIP would double-book them, and one who is OFFLINE or
        SUSPENDED is not available to claim at all.

        Re-setting the status a partner already holds is allowed and does
        nothing, so a retried request after a timeout is safe.
        """
        current = PartnerStatus(partner.status)

        if current is new_status:
            return partner

        allowed = {
            (PartnerStatus.ONLINE, PartnerStatus.ON_TRIP),
            (PartnerStatus.ON_TRIP, PartnerStatus.ONLINE),
        }
        if (current, new_status) not in allowed:
            raise InvalidStatusTransitionError(
                f"Cannot move a partner from {current.value} to {new_status.value}"
            )

        partner.status = new_status.value
        return self.partner_repository.save(partner)

    def set_verification(self, partner: Partner, approve: bool) -> Partner:
        """Operations clearing or refusing a partner's KYC."""
        partner.is_verified = approve

        # A partner who has just lost verification must stop appearing to
        # Dispatch immediately. The availability query already filters on
        # is_verified, so this is belt and braces — but leaving them showing
        # `online` in the partner app while they receive nothing is the exact
        # confusing state set_status() refuses to create.
        if not approve and PartnerStatus(partner.status) is PartnerStatus.ONLINE:
            partner.status = PartnerStatus.OFFLINE.value

        return self.partner_repository.save(partner)

    def set_suspended(self, partner: Partner, suspended: bool) -> Partner:
        """Operations barring a partner, or letting them back in.

        Lifting a suspension returns them to OFFLINE rather than ONLINE: being
        allowed to work again is not the same as being at work, and only the
        partner decides the latter.
        """
        current = PartnerStatus(partner.status)

        if suspended:
            partner.status = PartnerStatus.SUSPENDED.value
        elif current is PartnerStatus.SUSPENDED:
            partner.status = PartnerStatus.OFFLINE.value

        return self.partner_repository.save(partner)

    def record_rating(self, partner: Partner, rating: int) -> Partner:
        """Fold one delivery's score into the running average.

        The stored average is rounded to one decimal place, so each update
        carries a little of the previous rounding forward. The drift is bounded
        by half a decimal place and shrinks as the count grows — acceptable for
        a display rating, and the alternative is a full ratings table this
        service has no other reason to own.

        The 5.0 default over zero samples is a placeholder, not a rating, which
        is why `rating_count` and not the average drives this arithmetic: the
        first real score of 3 sets the average to exactly 3.0, rather than
        averaging against a five nobody gave.
        """
        previous_total = Decimal(partner.rating) * partner.rating_count
        new_count = partner.rating_count + 1

        partner.rating = ((previous_total + rating) / new_count).quantize(
            RATING_PLACES, rounding=ROUND_HALF_UP
        )
        partner.rating_count = new_count

        return self.partner_repository.save(partner)
