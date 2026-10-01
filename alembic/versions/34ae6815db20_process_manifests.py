"""Effective API and worker startup manifests.

Revision ID: 34ae6815db20
Revises: 23c4d8e190af
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "34ae6815db20"
down_revision = "23c4d8e190af"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "process_manifests",
        sa.Column("id", pg.UUID(), primary_key=True),
        sa.Column(
            "conversation_id", pg.UUID(), sa.ForeignKey("conversations.id", ondelete="CASCADE")
        ),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("snapshot", pg.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
    )
    for field in ("conversation_id", "created_at"):
        op.create_index("ix_process_manifests_" + field, "process_manifests", [field])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM process_manifests)")):
        raise RuntimeError("Downgrade would destroy execution manifests; retain additive schema")
    op.drop_table("process_manifests")
