from sqlalchemy import select
from sqlalchemy.orm import Session

from src.services.userServices.models.address_model import Address


class AddressRepository:
    """Data access for the address table. Holds no business rules.

    Every read and write is scoped to a user_id in the WHERE clause rather than
    fetched first and checked afterwards. That ordering matters: a query that
    cannot return another user's row makes the ownership check impossible to
    forget, whereas a `find_by_id` followed by an `if row.user_id != user.id`
    is one early return away from leaking.
    """

    def __init__(self, db: Session):
        self.db = db

    def list_for_user(self, user_id: int) -> list[Address]:
        return list(
            self.db.scalars(
                select(Address)
                .where(Address.user_id == user_id)
                # Stable ordering, so the list does not reshuffle between loads.
                .order_by(Address.id)
            )
        )

    def find_for_user(self, address_id: int, user_id: int) -> Address | None:
        """Fetch one address belonging to this user, or None.

        None covers both "no such address" and "not yours" — the caller must
        answer 404 either way, so the two cases are deliberately not
        distinguished here.
        """
        return self.db.scalar(
            select(Address).where(
                Address.id == address_id,
                Address.user_id == user_id,
            )
        )

    def create(self, address: Address) -> Address:
        self.db.add(address)
        self.db.commit()
        self.db.refresh(address)
        return address

    def save(self, address: Address) -> Address:
        """Persist changes to an already-loaded address."""
        self.db.commit()
        self.db.refresh(address)
        return address

    def delete(self, address: Address) -> None:
        self.db.delete(address)
        self.db.commit()

    def count_for_user(self, user_id: int) -> int:
        return len(self.list_for_user(user_id))
