"""app_settings table and conversation agent_settings snapshot

Revision ID: b7c1a9e02d41
Revises: f4da256bb4a1
Create Date: 2026-07-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b7c1a9e02d41'
down_revision: Union[str, Sequence[str], None] = 'f4da256bb4a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'app_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('agent_name', sa.Text(), server_default='Emma', nullable=False),
        sa.Column('language', sa.Text(), server_default='en', nullable=False),
        sa.Column('voice', sa.Text(), server_default='en_female', nullable=False),
        sa.Column('persona', sa.Text(), nullable=True),
        sa.Column('custom_instructions', sa.Text(), nullable=True),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.CheckConstraint('id = 1', name='app_settings_singleton'),
        sa.PrimaryKeyConstraint('id'),
    )
    # Seed the singleton row so GET /settings always finds one; the server
    # defaults fill every other column.
    op.execute("INSERT INTO app_settings (id) VALUES (1)")
    op.add_column(
        'conversations',
        sa.Column('agent_settings', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('conversations', 'agent_settings')
    op.drop_table('app_settings')
