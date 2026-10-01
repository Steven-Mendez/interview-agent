"""Versioned interview state, evidence, evaluation runs, and telemetry.

Revision ID: d2f604b8c901
Revises: c5e28d41f7a3
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "d2f604b8c901"
down_revision = "c5e28d41f7a3"
branch_labels = None
depends_on = None


def upgrade():
    for column in (
        sa.Column("run_config", pg.JSONB(), nullable=True),
        sa.Column("question_limit", sa.Integer(), nullable=True),
        sa.Column("followup_limit", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closing_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("closing_owner_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("closing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closing_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closing_stream_id", sa.Text(), nullable=True),
        sa.Column("closing_attempt_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("transcript_sealed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("transcript_integrity", sa.Text(), nullable=True),
        sa.Column("farewell_status", sa.Text(), nullable=True),
        sa.Column("evaluation_claim_id", pg.UUID(as_uuid=True), nullable=True),
    ):
        op.add_column("conversations", column)
    for column in (
        sa.Column("lifecycle", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("close_reason", sa.Text(), nullable=True),
        sa.Column("essential", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("competency", sa.Text(), nullable=True),
        sa.Column("primary_questions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("followups", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("clarifications", sa.Integer(), nullable=False, server_default="0"),
    ):
        op.add_column("milestones", column)
    op.execute(
        "UPDATE milestones SET lifecycle='closed', close_reason='legacy_unspecified' WHERE completed"
    )
    op.execute(
        """UPDATE conversations c SET started_at=(SELECT min(created_at) FROM messages m WHERE m.conversation_id=c.id)"""
    )
    for column in (
        sa.Column("source_id", sa.Text(), nullable=True),
        sa.Column("turn_id", sa.Text(), nullable=True),
        sa.Column("interrupted", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("metrics", pg.JSONB(), nullable=True),
    ):
        op.add_column("messages", column)
    op.create_unique_constraint(
        "messages_source_unique", "messages", ["conversation_id", "source_id"]
    )
    op.alter_column("evaluations", "score", existing_type=sa.Integer(), nullable=True)
    op.alter_column("evaluations", "hired", existing_type=sa.Boolean(), nullable=True)
    op.add_column("evaluations", sa.Column("result", pg.JSONB(), nullable=True))

    op.create_table(
        "evaluation_runs",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("transcript_hash", sa.Text(), nullable=False),
        sa.Column("config", pg.JSONB(), nullable=False),
        sa.Column("result", pg.JSONB(), nullable=True),
        sa.Column("usage", pg.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_evaluation_runs_conversation_id", "evaluation_runs", ["conversation_id"])
    op.create_table(
        "turn_runs",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("turn_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("decision", pg.JSONB(), nullable=False),
        sa.UniqueConstraint("conversation_id", "turn_id", name="turn_runs_unique"),
    )
    op.create_index("ix_turn_runs_conversation_id", "turn_runs", ["conversation_id"])
    op.create_table(
        "evidence",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "milestone_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("milestones.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "message_id",
            sa.BigInteger(),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("milestone_id", "message_id", "quote", name="evidence_unique"),
    )
    op.create_index("ix_evidence_conversation_id", "evidence", ["conversation_id"])
    op.create_table(
        "metric_events",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
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


def downgrade():
    connection = op.get_bind()
    has_v2_data = connection.scalar(
        sa.text("""
        SELECT EXISTS(SELECT 1 FROM conversations WHERE run_config IS NOT NULL AND run_config <> 'null'::jsonb)
            OR EXISTS(SELECT 1 FROM evaluations WHERE result IS NOT NULL AND result <> 'null'::jsonb)
            OR EXISTS(SELECT 1 FROM evaluation_runs)
            OR EXISTS(SELECT 1 FROM turn_runs)
            OR EXISTS(SELECT 1 FROM evidence)
            OR EXISTS(SELECT 1 FROM metric_events)
            OR EXISTS(SELECT 1 FROM metric_aggregates)
    """)
    )
    if has_v2_data:
        raise RuntimeError("Downgrade would destroy v2 interview evidence or metrics.")
    for table in ("metric_aggregates", "metric_events", "evidence", "turn_runs", "evaluation_runs"):
        op.drop_table(table)
    # Preserve absence of evidence as NULL; do not invent scores on downgrade.
    op.drop_column("evaluations", "result")
    op.drop_constraint("messages_source_unique", "messages", type_="unique")
    for name in ("metrics", "interrupted", "turn_id", "source_id"):
        op.drop_column("messages", name)
    for name in (
        "clarifications",
        "followups",
        "primary_questions",
        "competency",
        "essential",
        "close_reason",
        "lifecycle",
    ):
        op.drop_column("milestones", name)
    for name in (
        "evaluation_claim_id",
        "farewell_status",
        "closing_attempt_id",
        "transcript_integrity",
        "transcript_sealed_at",
        "closing_stream_id",
        "closing_deadline_at",
        "closing_started_at",
        "closing_owner_id",
        "closing_id",
        "started_at",
        "followup_limit",
        "question_limit",
        "run_config",
    ):
        op.drop_column("conversations", name)
