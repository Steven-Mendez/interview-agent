"""Durable logical evaluation requests and bounded leased attempts.

Revision ID: f084ac76d119
Revises: ef293b0d18a7
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "f084ac76d119"
down_revision = "ef293b0d18a7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("conversations", sa.Column("evaluation_request_id", pg.UUID()))
    op.create_table(
        "evaluation_requests",
        sa.Column("id", pg.UUID(), primary_key=True),
        sa.Column(
            "conversation_id",
            pg.UUID(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("automatic", sa.Boolean(), nullable=False),
        sa.Column("transcript_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("attempts BETWEEN 0 AND 3", name="evaluation_request_budget"),
    )
    op.create_index(
        "ix_evaluation_requests_conversation_id", "evaluation_requests", ["conversation_id"]
    )
    op.create_index(
        "evaluation_automatic_once",
        "evaluation_requests",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("automatic IS TRUE"),
    )
    op.add_column(
        "evaluation_runs",
        sa.Column(
            "request_id",
            pg.UUID(),
            sa.ForeignKey(
                "evaluation_requests.id", ondelete="CASCADE", name="evaluation_run_request_fk"
            ),
        ),
    )
    op.create_index("ix_evaluation_runs_request_id", "evaluation_runs", ["request_id"])
    op.add_column("evaluation_runs", sa.Column("ordinal", sa.Integer()))
    for name in ("started_at", "lease_until", "finished_at"):
        op.add_column("evaluation_runs", sa.Column(name, sa.DateTime(timezone=True)))


def downgrade():
    if op.get_bind().scalar(
        sa.text("""SELECT EXISTS(SELECT 1 FROM evaluation_requests)
        OR EXISTS(SELECT 1 FROM conversations WHERE evaluation_request_id IS NOT NULL)
        OR EXISTS(SELECT 1 FROM evaluation_runs WHERE request_id IS NOT NULL OR ordinal IS NOT NULL
            OR started_at IS NOT NULL OR lease_until IS NOT NULL OR finished_at IS NOT NULL)""")
    ):
        raise RuntimeError(
            "Downgrade would destroy evaluation request history; retain additive schema"
        )
    op.drop_index("ix_evaluation_runs_request_id", "evaluation_runs")
    op.drop_constraint("evaluation_run_request_fk", "evaluation_runs", type_="foreignkey")
    for name in ("request_id", "ordinal", "started_at", "lease_until", "finished_at"):
        op.drop_column("evaluation_runs", name)
    op.drop_table("evaluation_requests")
    op.drop_column("conversations", "evaluation_request_id")
