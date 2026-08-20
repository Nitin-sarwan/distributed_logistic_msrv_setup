from sqlalchemy.orm import Session

from src.services.partnerServices.api.schema import VehicleCreate, VehicleUpdate
from src.services.partnerServices.models.partner_model import Partner
from src.services.partnerServices.models.vehicle_model import Vehicle
from src.services.partnerServices.repositories.partner_repositories import (
    PartnerRepository,
)
from src.services.partnerServices.repositories.vehicle_repositories import (
    VehicleRepository,
)
from src.services.partnerServices.utils.enums import (
    VEHICLE_USABLE_STATUSES,
    PartnerStatus,
    VehicleStatus,
)
from src.services.partnerServices.utils.exceptions import (
    InvalidStatusTransitionError,
    RegistrationNumberExistsError,
    VehicleNotFoundError,
)


class VehicleService:
    """Business rules for a partner's vehicles.

    Every partner-facing method takes the authenticated `Partner` rather than an
    id, so a caller cannot pass an id it merely claims to own. Identity comes
    from `get_current_partner`, which has already decrypted the token and
    confirmed the session is live.
    """

    def __init__(self, db: Session):
        self.db = db
        self.vehicle_repository = VehicleRepository(db)
        self.partner_repository = PartnerRepository(db)

    # ── Partner-facing ────────────────────────────────────────────────────

    def list_vehicles(self, partner: Partner) -> list[Vehicle]:
        return self.vehicle_repository.list_for_partner(partner.id)

    def get_vehicle(self, partner: Partner, vehicle_id: int) -> Vehicle:
        vehicle = self.vehicle_repository.find_for_partner(vehicle_id, partner.id)
        if vehicle is None:
            raise VehicleNotFoundError()
        return vehicle

    def create_vehicle(self, partner: Partner, payload: VehicleCreate) -> Vehicle:
        # The plate is unique table-wide, so a clash here usually means another
        # partner already registered this vehicle. Checking first turns an
        # IntegrityError into a clean 409 that names the actual problem.
        if self.vehicle_repository.find_by_number(payload.vehicle_number):
            raise RegistrationNumberExistsError()

        vehicle = Vehicle(
            # From the session, never from the request body.
            partner_id=partner.id,
            vehicle_type=payload.vehicle_type.value,
            vehicle_number=payload.vehicle_number,
            capacity=payload.capacity,
            model_name=payload.model_name,
            # PENDING by default. A partner cannot put a vehicle on the road by
            # adding it — operations has to look at the papers first.
            status=VehicleStatus.PENDING.value,
        )
        return self.vehicle_repository.create(vehicle)

    def update_vehicle(
        self, partner: Partner, vehicle_id: int, payload: VehicleUpdate
    ) -> Vehicle:
        # Raises if it does not exist or is not theirs, before anything is
        # written.
        vehicle = self.get_vehicle(partner, vehicle_id)

        # exclude_unset, not exclude_none: it is what separates "field omitted"
        # from "field explicitly set to null". Without it, a PATCH changing only
        # the model name would blank the capacity.
        changes = payload.model_dump(exclude_unset=True)

        for field, value in changes.items():
            setattr(vehicle, field, value)

        return self.vehicle_repository.save(vehicle)

    def activate_vehicle(self, partner: Partner, vehicle_id: int) -> Vehicle:
        """Make this the vehicle the partner is currently driving.

        Refused for PENDING and REJECTED, which is the whole point of the status
        column: a vehicle whose documents have not been cleared must not become
        the one Dispatch matches orders against.
        """
        vehicle = self.get_vehicle(partner, vehicle_id)

        if VehicleStatus(vehicle.status) not in VEHICLE_USABLE_STATUSES:
            raise InvalidStatusTransitionError(
                "This vehicle has not been verified yet"
            )

        return self.vehicle_repository.set_active(vehicle)

    def delete_vehicle(self, partner: Partner, vehicle_id: int) -> None:
        """Remove a vehicle from the partner's list.

        A hard delete, unlike partners, which are soft-deleted. A partner has a
        rating and a history worth keeping a tombstone for; a vehicle they added
        by mistake does not. If deliveries later reference a vehicle id, this
        becomes a soft delete — at which point the foreign key will say so.
        """
        vehicle = self.get_vehicle(partner, vehicle_id)

        if VehicleStatus(vehicle.status) is VehicleStatus.ACTIVE:
            status = PartnerStatus(partner.status)

            if status is PartnerStatus.ON_TRIP:
                raise InvalidStatusTransitionError(
                    "Cannot remove the vehicle currently on a delivery"
                )

            # Going online required an active vehicle, so removing it has to
            # take the partner back offline in the same operation. Skipping this
            # leaves them showing `online` with nothing Dispatch can match,
            # which is exactly the silent dead end set_status() refuses to
            # create at the other end.
            if status is PartnerStatus.ONLINE:
                partner.status = PartnerStatus.OFFLINE.value
                self.partner_repository.save(partner)

        self.vehicle_repository.delete(vehicle)

    # ── Called by operations ──────────────────────────────────────────────

    def set_verification(self, vehicle_id: int, approve: bool) -> Vehicle:
        """Clear or refuse a vehicle's documents.

        Approving lands it at INACTIVE, never ACTIVE: cleared to be driven is
        not the same as currently being driven, and which vehicle is on the road
        is the partner's call.

        Refusing an ACTIVE vehicle also takes its partner offline — the same
        invariant `delete_vehicle` maintains, reached from the other direction.
        """
        vehicle = self.vehicle_repository.find_by_id(vehicle_id)
        if vehicle is None:
            raise VehicleNotFoundError()

        was_active = VehicleStatus(vehicle.status) is VehicleStatus.ACTIVE

        vehicle.status = (
            VehicleStatus.INACTIVE.value if approve else VehicleStatus.REJECTED.value
        )
        vehicle = self.vehicle_repository.save(vehicle)

        if not approve and was_active:
            partner = self.partner_repository.find_by_id(vehicle.partner_id)
            if partner is not None and PartnerStatus(partner.status) in (
                PartnerStatus.ONLINE,
                PartnerStatus.ON_TRIP,
            ):
                partner.status = PartnerStatus.OFFLINE.value
                self.partner_repository.save(partner)

        return vehicle
