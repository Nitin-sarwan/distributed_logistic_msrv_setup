"""add quote_id to orders

Revision ID: a8d4e61f2c90
Revises: c3a91d5f7e02
Create Date: 2026-10-11

Pricing became its own service, and every quote it issues has an id. Order
keeps that id beside the amount it already snapshots: the amount is what was
agreed, the id is where to look up how it was reached.

Nullable, because orders priced by the old in-process stub have no quote to
point at, and inventing one would be worse than admitting it.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a8d4e61f2c90"
down_revision: Union[str, Sequence[str], None] = "c3a91d5f7e02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("orders", sa.Column("quote_id", sa.String(length=36), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("orders", "quote_id")
