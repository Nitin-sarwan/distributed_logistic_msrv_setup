from sqlalchemy.orm import Session

from src.services.userServices.api.schema import AddressCreate, AddressUpdate
from src.services.userServices.models.address_model import Address
from src.services.userServices.models.user_model import User
from src.services.userServices.repositories.address_repositories import (
    AddressRepository,
)
from src.services.userServices.utils.exceptions import AddressNotFoundError


class AddressService:
    """Business rules for saved addresses.

    Every method takes the authenticated `User` rather than a user id, so a
    caller cannot pass an id it merely claims to own. The identity comes from
    `get_current_user`, which has already decrypted the token and confirmed the
    session is live.
    """

    def __init__(self, db: Session):
        self.db = db
        self.address_repository = AddressRepository(db)

    def list_addresses(self, user: User) -> list[Address]:
        return self.address_repository.list_for_user(user.id)

    def get_address(self, user: User, address_id: int) -> Address:
        address = self.address_repository.find_for_user(address_id, user.id)
        if address is None:
            raise AddressNotFoundError()
        return address

    def create_address(self, user: User, payload: AddressCreate) -> Address:
        address = Address(
            # From the session, never from the request body.
            user_id=user.id,
            address_line1=payload.address_line1,
            address_line2=payload.address_line2,
            city=payload.city,
            pin_code=payload.pin_code,
            latitude=payload.latitude,
            longitude=payload.longitude,
        )
        return self.address_repository.create(address)

    def update_address(
        self,
        user: User,
        address_id: int,
        payload: AddressUpdate,
    ) -> Address:
        # Raises if it does not exist or is not theirs, before anything is
        # written.
        address = self.get_address(user, address_id)

        # exclude_unset, not exclude_none: it is what separates "field omitted"
        # from "field explicitly set to null". Without it, a PATCH changing only
        # the city would blank every other column.
        changes = payload.model_dump(exclude_unset=True)

        for field, value in changes.items():
            setattr(address, field, value)

        return self.address_repository.save(address)

    def delete_address(self, user: User, address_id: int) -> None:
        address = self.get_address(user, address_id)
        self.address_repository.delete(address)
