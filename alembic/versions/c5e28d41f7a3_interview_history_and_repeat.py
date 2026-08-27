"""interview history: created_at index and the repeat lineage column

Revision ID: c5e28d41f7a3
Revises: a1c7f3b90d24
Create Date: 2026-08-26 20:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c5e28d41f7a3'
down_revision: Union[str, Sequence[str], None] = 'a1c7f3b90d24'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Nullable: only re-runs carry it, and it always points at the ROOT of the
    # chain. SET NULL (not CASCADE) so deleting the original leaves the later
    # attempts standing — the purge job walks by age, not by lineage.
    op.add_column(
        'conversations',
        sa.Column('repeat_of_id', postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        'conversations_repeat_of_id_fkey',
        'conversations',
        'conversations',
        ['repeat_of_id'],
        ['id'],
        ondelete='SET NULL',
    )
    # The history list pages by created_at DESC.
    op.create_index(
        'conversations_created_at_idx', 'conversations', ['created_at']
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('conversations_created_at_idx', table_name='conversations')
    op.drop_constraint(
        'conversations_repeat_of_id_fkey', 'conversations', type_='foreignkey'
    )
    op.drop_column('conversations', 'repeat_of_id')
