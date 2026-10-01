"""Durable LangSmith deletion jobs and export tombstones.

Revision ID: 23c4d8e190af
Revises: 1b4f70c9d821
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "23c4d8e190af"
down_revision = "1b4f70c9d821"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "external_traces",
        sa.Column("id", pg.UUID(), primary_key=True),
        sa.Column(
            "conversation_id", pg.UUID(), sa.ForeignKey("conversations.id", ondelete="SET NULL")
        ),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("project_name", sa.Text(), nullable=False),
        sa.Column("project_id", pg.UUID()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="active"),
        sa.Column("deletion_requested_at", sa.DateTime(timezone=True)),
        sa.Column("deletion_submitted_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("lease_owner", pg.UUID()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
    )
    for field in ["conversation_id", "expires_at", "next_attempt_at"]:
        op.create_index("ix_external_traces_" + field, "external_traces", [field])
    op.execute("""CREATE FUNCTION tombstone_conversation_traces() RETURNS trigger AS $$
        BEGIN
            UPDATE external_traces SET
                conversation_id = NULL,
                state = CASE WHEN state='active' THEN 'pending' ELSE state END,
                deletion_requested_at = COALESCE(deletion_requested_at, clock_timestamp()),
                next_attempt_at = CASE WHEN state='active' THEN clock_timestamp() ELSE next_attempt_at END
            WHERE conversation_id=OLD.id;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER conversations_trace_tombstones BEFORE DELETE ON conversations
        FOR EACH ROW EXECUTE FUNCTION tombstone_conversation_traces()""")


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM external_traces)")):
        raise RuntimeError(
            "Downgrade would destroy external deletion jobs and tombstones; retain additive schema"
        )
    op.execute("DROP TRIGGER conversations_trace_tombstones ON conversations")
    op.execute("DROP FUNCTION tombstone_conversation_traces()")
    op.drop_table("external_traces")
