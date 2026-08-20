from sqlalchemy import case, select, update
from sqlalchemy.orm import Session

from src.services.partnerServices.models.vehicle_model import Vehicle
from src.services.partnerServices.utils.enums import VehicleStatus


class VehicleRepository:
    """Data access for the vehicles table. Holds no business rules.

    Every read and write is scoped by `partner_id` in the WHERE clause rather
    than fetched first and checked afterwards — the same shape as
    `AddressRepository` in userServices, for the same reason. A query that
    cannot return another partner's row makes the ownership check impossible to
    forget; a `find_by_id` followed by `if row.partner_id != partner.id` is one
    early return away from leaking.
    """

    def __init__(self, db: Session):
        self.db = db

    def list_for_partner(self, partner_id: int) -> list[Vehicle]:
        return list(
            self.db.scalars(
                select(Vehicle)
                .where(Vehicle.partner_id == partner_id)
                # The vehicle the partner is actually driving sits at the top of
                # their list; the rest keep a stable order so the list does not
                # reshuffle between loads.
                .order_by(
                    case((Vehicle.status == VehicleStatus.ACTIVE.value, 0), else_=1),
                    Vehicle.id,
                )
            )
        )

    def find_for_partner(self, vehicle_id: int, partner_id: int) -> Vehicle | None:
        """Fetch one vehicle belonging to this partner, or None.

        None covers both "no such vehicle" and "not yours" — the caller answers
        404 either way, so the two are deliberately not distinguished.
        """
        return self.db.scalar(
            select(Vehicle).where(
                Vehicle.id == vehicle_id,
                Vehicle.partner_id == partner_id,
            )
        )

    def find_active_for_partner(self, partner_id: int) -> Vehicle | None:
        return self.db.scalar(
            select(Vehicle).where(
                Vehicle.partner_id == partner_id,
                Vehicle.status == VehicleStatus.ACTIVE.value,
            )
        )

    def find_by_number(self, vehicle_number: str) -> Vehicle | None:
        """Global lookup, not scoped to a partner — that is the point.

        A number plate is unique across the table, so this is how a second
        partner claiming an already-registered vehicle is caught before the
        insert raises an IntegrityError.
        """
        return self.db.scalar(
            select(Vehicle).where(Vehicle.vehicle_number == vehicle_number)
        )

    def find_by_id(self, vehicle_id: int) -> Vehicle | None:
        """Unscoped by partner — for the /internal verification endpoints only.

        Operations reviews a vehicle without being its owner, so this is the one
        lookup that does not carry a partner_id. Every partner-facing path must
        use `find_for_partner` instead.
        """
        return self.db.scalar(select(Vehicle).where(Vehicle.id == vehicle_id))

    def create(self, vehicle: Vehicle) -> Vehicle:
        self.db.add(vehicle)
        self.db.commit()
        self.db.refresh(vehicle)
        return vehicle

    def save(self, vehicle: Vehicle) -> Vehicle:
        """Persist changes to an already-loaded vehicle."""
        self.db.commit()
        self.db.refresh(vehicle)
        return vehicle

    def delete(self, vehicle: Vehicle) -> None:
        self.db.delete(vehicle)
        self.db.commit()

    def set_active(self, vehicle: Vehicle) -> Vehicle:
        """Make this the partner's active vehicle, atomically.

        `uq_vehicles_one_active_per_partner` is a partial unique index, so the
        vehicle currently on the road must be stood down and this one promoted
        within a single transaction. Committing in between would leave a moment
        with two active rows, which the index rejects outright.

        The previous one goes to INACTIVE rather than PENDING: it already passed
        verification, and sending it back into the review queue every time the
        partner switches vehicles would be absurd.

        `synchronize_session=False` is safe because the next statement re-reads
        the row it touched; a briefly stale identity map costs nothing here and
        avoids a needless second SELECT.
        """
        self.db.execute(
            update(Vehicle)
            .where(
                Vehicle.partner_id == vehicle.partner_id,
                Vehicle.status == VehicleStatus.ACTIVE.value,
                Vehicle.id != vehicle.id,
            )
            .values(status=VehicleStatus.INACTIVE.value),
            execution_options={"synchronize_session": False},
        )
        vehicle.status = VehicleStatus.ACTIVE.value
        self.db.commit()
        self.db.refresh(vehicle)
        return vehicle
