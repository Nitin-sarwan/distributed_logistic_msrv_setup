"""user_address

Revision ID: 38383cea81f5
Revises: 9d7c2a5904e4
Create Date: 2026-08-14 22:40:22.389343

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '38383cea81f5'
down_revision: Union[str, Sequence[str], None] = '9d7c2a5904e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('address',
    sa.Column('id',sa.Integer(),autoincrement=True,nullable=False),
    sa.Column('user_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
    sa.Column('address_line1',sa.Text(),nullable=False),
    sa.Column('address_line2',sa.Text(),nullable=True),
    sa.Column('pin_code',sa.String(length=6),nullable=False),
    sa.Column('city',sa.String(length=255),nullable=False),
    sa.Column(
        "latitude",
        sa.Numeric(9, 6),
        nullable=False,
    ),

    sa.Column(
        "longitude",
        sa.Numeric(9, 6),
        nullable=False,
    ),
    )



def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('address')
   
