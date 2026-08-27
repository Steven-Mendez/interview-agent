"""seniority and interview_length calibration axes

Revision ID: a1c7f3b90d24
Revises: b7c1a9e02d41
Create Date: 2026-08-26 19:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a1c7f3b90d24'
down_revision: Union[str, Sequence[str], None] = 'b7c1a9e02d41'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # server_default (not just a Python-side default) so the rows that already
    # exist land on 'mid'/'standard' instead of NULL and keep working.
    op.add_column(
        'conversations',
        sa.Column('seniority', sa.Text(), nullable=False, server_default='mid'),
    )
    op.add_column(
        'conversations',
        sa.Column(
            'seniority_source', sa.Text(), nullable=False, server_default='fallback'
        ),
    )
    op.add_column(
        'conversations', sa.Column('seniority_evidence', sa.Text(), nullable=True)
    )
    op.add_column(
        'conversations',
        sa.Column(
            'interview_length', sa.Text(), nullable=False, server_default='standard'
        ),
    )
    # Nullable on purpose: legacy rows fall back to INTERVIEW_MAX_MINUTES.
    op.add_column(
        'conversations', sa.Column('max_minutes', sa.Integer(), nullable=True)
    )
    op.add_column(
        'milestones', sa.Column('expected_evidence', sa.Text(), nullable=True)
    )
    op.add_column(
        'evaluations', sa.Column('seniority_evaluated', sa.Text(), nullable=True)
    )
    op.add_column(
        'evaluations',
        sa.Column('calibration_notes', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('evaluations', 'calibration_notes')
    op.drop_column('evaluations', 'seniority_evaluated')
    op.drop_column('milestones', 'expected_evidence')
    op.drop_column('conversations', 'max_minutes')
    op.drop_column('conversations', 'interview_length')
    op.drop_column('conversations', 'seniority_evidence')
    op.drop_column('conversations', 'seniority_source')
    op.drop_column('conversations', 'seniority')
