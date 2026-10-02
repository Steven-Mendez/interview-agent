"""Metrics leave Postgres for OTLP; a per-interview counter caps browser onset samples.

Revision ID: dad9ce0068bd
Revises: 34ae6815db20
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "dad9ce0068bd"
down_revision = "34ae6815db20"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table("metric_events")
    op.drop_table("metric_aggregates")
    op.add_column(
        "conversations",
        sa.Column("response_onset_samples", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade():
    # The tables as 34ae6815db20 left them, empty: their samples are in OTLP now.
    op.drop_column("conversations", "response_onset_samples")
    op.create_table(
        "metric_events",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("turn_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("component", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("dimensions", pg.JSONB(), nullable=False),
    )
    op.create_index("ix_metric_events_conversation_id", "metric_events", ["conversation_id"])
    op.create_index("ix_metric_events_created_at", "metric_events", ["created_at"])
    op.create_table(
        "metric_aggregates",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("bucket_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dimensions", pg.JSONB(), nullable=False),
        sa.Column("component", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("total", sa.Float(), nullable=False),
        sa.Column("minimum", sa.Float(), nullable=True),
        sa.Column("maximum", sa.Float(), nullable=True),
        sa.Column("unknown_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("histogram", pg.JSONB(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_metric_aggregates_bucket_date", "metric_aggregates", ["bucket_date"])
