"""Persist the revision used to reject stale dialogue decisions.

Revision ID: e79a42c608bd
Revises: d2f604b8c901
"""

from alembic import op
import sqlalchemy as sa

revision = "e79a42c608bd"
down_revision = "d2f604b8c901"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "conversations",
        sa.Column("state_revision", sa.BigInteger(), nullable=False, server_default="0"),
    )


def downgrade():
    op.drop_column("conversations", "state_revision")
