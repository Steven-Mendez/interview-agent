"""conversation updated_at

Revision ID: 34b9d346d5c7
Revises: 3492d138d0d6
Create Date: 2026-07-06 04:42:58.957450

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '34b9d346d5c7'
down_revision: Union[str, Sequence[str], None] = '3492d138d0d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'conversations',
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('conversations', 'updated_at')
