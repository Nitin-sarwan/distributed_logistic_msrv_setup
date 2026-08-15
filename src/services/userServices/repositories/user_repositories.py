from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from src.services.userServices.models.user_model import User


class UserRepository:
    """Data access for the users table. Holds no business rules."""

    def __init__(self, db: Session):
        self.db = db

    def find_by_email(self, email: str) -> User | None:
        return self.db.scalar(
            select(User).where(
                User.email == email,
                User.is_deleted.is_(False),
            )
        )

    def find_by_phone(self, phone: str) -> User | None:
        return self.db.scalar(
            select(User).where(
                User.phone == phone,
                User.is_deleted.is_(False),
            )
        )

    def find_by_email_or_phone(self, email: str, phone: str | None) -> User | None:
        conditions = [User.email == email]
        if phone is not None:
            conditions.append(User.phone == phone)

        return self.db.scalar(
            select(User).where(
                or_(*conditions),
                User.is_deleted.is_(False),
            )
        )

    def create(self, user: User) -> User:
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        return user

    def delete(self, user: User) -> None:
        self.db.delete(user)
        self.db.commit()

    def save(self, user: User) -> User:
        """Persist changes to an already-loaded user."""
        self.db.commit()
        self.db.refresh(user)
        return user


    def find_by_id(self,id:int)->User|None:
        return self.db.scalar(
            select(User)
            .where(User.id==id,
             User.is_deleted.is_(False)
            )
        )