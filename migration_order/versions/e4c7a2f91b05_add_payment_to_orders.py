"""add payment method and status to orders

Revision ID: e4c7a2f91b05
Revises: a8d4e61f2c90
Create Date: 2026-10-11

Customers now choose to pay online or on delivery, and the driver needs to know
which before handing over the parcel. Order keeps both as snapshots: the method
from booking, the status from `payment.captured` / `payment.refunded`.

**Existing rows are backfilled**, which is why this revision writes data:

* every order already past confirmation was paid by the old in-process stub,
  so it becomes `online` / `paid`;
* everything else (quoted, never confirmed, or cancelled before payment) is
  `online` / `pending` — the method the stub implied, with nothing received.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e4c7a2f91b05"
down_revision: Union[str, Sequence[str], None] = "a8d4e61f2c90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PAID_BY_STUB = (
    "confirmed", "searching_partner", "partner_assigned",
    "picked_up", "in_transit", "delivered", "settled",
)


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "orders",
        sa.Column("payment_method", sa.String(length=20), server_default="online", nullable=False),
    )
    op.add_column(
        "orders",
        sa.Column("payment_status", sa.String(length=20), server_default="pending", nullable=False),
    )
    op.create_check_constraint(
        "ck_orders_payment_method", "orders", "payment_method IN ('online', 'on_delivery')"
    )
    op.create_check_constraint(
        "ck_orders_payment_status", "orders", "payment_status IN ('pending', 'paid', 'refunded')"
    )

    statuses = ", ".join(f"'{status}'" for status in PAID_BY_STUB)
    op.execute(f"UPDATE orders SET payment_status = 'paid' WHERE status IN ({statuses})")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("ck_orders_payment_status", "orders", type_="check")
    op.drop_constraint("ck_orders_payment_method", "orders", type_="check")
    op.drop_column("orders", "payment_status")
    op.drop_column("orders", "payment_method")
