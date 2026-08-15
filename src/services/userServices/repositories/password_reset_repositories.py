from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from src.services.userServices.models.password_reset_model import PasswordReset


class PasswordResetRepository:
    """Data access for password reset tokens. Holds no business rules."""

    def __init__(self, db: Session):
        self.db = db

    def create(self, user_id: int, token_hash: str, expires_at: datetime):
        reset = PasswordReset(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
        self.db.add(reset)
        self.db.commit()
        self.db.refresh(reset)
        return reset

    def find_usable(self, token_hash: str) -> PasswordReset | None:
        """Only unused, unrevoked, unexpired tokens count."""
        return self.db.scalar(
            select(PasswordReset).where(
                PasswordReset.token_hash == token_hash,
                PasswordReset.used_at.is_(None),
                PasswordReset.is_revoked.is_(False),
                PasswordReset.expires_at > datetime.now(timezone.utc),
            )
        )

    def mark_used(self, reset: PasswordReset) -> None:
        reset.used_at = datetime.now(timezone.utc)
        self.db.commit()

    def revoke_outstanding(self, user_id: int) -> None:
        """Invalidate a user's other pending tokens.

        Requesting a new link should retire the previous one, and completing a
        reset should retire them all — otherwise an older email still works.
        """
        self.db.execute(
            update(PasswordReset)
            .where(
                PasswordReset.user_id == user_id,
                PasswordReset.used_at.is_(None),
                PasswordReset.is_revoked.is_(False),
            )
            .values(is_revoked=True)
        )
        self.db.commit()
