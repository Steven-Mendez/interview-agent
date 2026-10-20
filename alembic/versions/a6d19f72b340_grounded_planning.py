"""Persist interview preferences and topic-to-requirement provenance.

Revision ID: a6d19f72b340
Revises: 9c3d5e7f1a20
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a6d19f72b340"
down_revision = "9c3d5e7f1a20"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "conversations", sa.Column("interview_preferences", postgresql.JSONB(), nullable=True)
    )
    op.add_column("milestones", sa.Column("planning_context", postgresql.JSONB(), nullable=True))


def downgrade():
    bind = op.get_bind()
    if bind.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM conversations WHERE interview_preferences IS NOT NULL) "
            "OR EXISTS (SELECT 1 FROM milestones WHERE planning_context IS NOT NULL)"
        )
    ):
        raise RuntimeError("Downgrade would destroy interview preferences or planning provenance")
    op.drop_column("milestones", "planning_context")
    op.drop_column("conversations", "interview_preferences")
