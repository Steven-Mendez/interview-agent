"""Persist explicit final-input drain evidence without altering old transcripts.

Revision ID: c632ad97e120
Revises: 0ad71283bce4
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c632ad97e120"
down_revision = "0ad71283bce4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "conversations",
        sa.Column("stt_drain", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS(SELECT 1 FROM conversations WHERE stt_drain IS NOT NULL)")
    ):
        raise RuntimeError("Downgrade would destroy STT drain evidence; retain the additive schema")
    op.drop_column("conversations", "stt_drain")
