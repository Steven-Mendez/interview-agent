"""Recover validated question delivery without another decision.

Revision ID: ef293b0d18a7
Revises: b84dc72e0519
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "ef293b0d18a7"
down_revision = "b84dc72e0519"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "question_deliveries",
        sa.Column(
            "id", pg.UUID(), sa.ForeignKey("turn_runs.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column(
            "conversation_id",
            pg.UUID(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("capture_order", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
    )
    op.create_index(
        "ix_question_deliveries_conversation_id", "question_deliveries", ["conversation_id"]
    )
    op.create_table(
        "question_attempts",
        sa.Column("id", pg.UUID(), primary_key=True),
        sa.Column(
            "question_id",
            pg.UUID(),
            sa.ForeignKey("question_deliveries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("explicit", sa.Boolean(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="requested"),
        sa.Column("owner_id", pg.UUID()),
        sa.Column("owner_epoch", sa.BigInteger()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_question_attempts_question_id", "question_attempts", ["question_id"])


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM question_deliveries) OR EXISTS(SELECT 1 FROM question_attempts)"
        )
    ):
        raise RuntimeError(
            "Downgrade would destroy question delivery history; retain additive schema"
        )
    op.drop_table("question_attempts")
    op.drop_table("question_deliveries")
