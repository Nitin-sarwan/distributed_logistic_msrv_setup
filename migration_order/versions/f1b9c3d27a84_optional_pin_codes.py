"""make order PIN codes optional

Revision ID: f1b9c3d27a84
Revises: e4c7a2f91b05
Create Date: 2026-10-11

Many map results have no postcode — a park, a whole neighbourhood, the device's
own location — and asking the customer to type one blocked bookings. The
coordinates are what a driver navigates to; the PIN code is a label, so it may
now be absent. Existing rows all have one and are untouched.

Downgrading fails if any order was stored without a PIN code, which is correct:
the old schema has nowhere to put one.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f1b9c3d27a84"
down_revision: Union[str, Sequence[str], None] = "e4c7a2f91b05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column("orders", "pickup_pin_code", existing_type=sa.String(length=6), nullable=True)
    op.alter_column("orders", "drop_pin_code", existing_type=sa.String(length=6), nullable=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column("orders", "drop_pin_code", existing_type=sa.String(length=6), nullable=False)
    op.alter_column("orders", "pickup_pin_code", existing_type=sa.String(length=6), nullable=False)
