"""Durable voice captures and immutable message evidence versions.

Revision ID: b84dc72e0519
Revises: a3e72bc19054
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "b84dc72e0519"
down_revision = "a3e72bc19054"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("turn_executions", sa.Column("producer_owner_id", pg.UUID()))
    op.add_column("turn_executions", sa.Column("producer_epoch", sa.BigInteger()))
    op.add_column(
        "conversations",
        sa.Column(
            "capture_integrity_pending",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.add_column("messages", sa.Column("version", sa.Integer()))
    op.add_column("evidence", sa.Column("message_version", sa.Integer()))
    op.create_table(
        "captured_turns",
        sa.Column(
            "conversation_id",
            pg.UUID(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("turn_id", sa.Text(), primary_key=True),
        sa.Column(
            "message_id",
            sa.BigInteger(),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("capture_order", sa.BigInteger(), nullable=False),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.UniqueConstraint("message_id", name="captured_turn_message_unique"),
        sa.UniqueConstraint("conversation_id", "capture_order", name="captured_turn_order_unique"),
    )
    op.create_table(
        "message_versions",
        sa.Column(
            "message_id",
            sa.BigInteger(),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("interrupted", sa.Boolean(), nullable=False),
        sa.Column("metrics", pg.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.CheckConstraint("version >= 1", name="message_version_positive"),
    )
    op.execute("""CREATE FUNCTION immutable_message_version() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
        RAISE EXCEPTION 'Message evidence versions are immutable';
        END $$""")
    op.execute("""CREATE TRIGGER immutable_message_version_update
        BEFORE UPDATE ON message_versions FOR EACH ROW
        EXECUTE FUNCTION immutable_message_version()""")
    op.drop_constraint("evidence_unique", "evidence", type_="unique")
    op.create_unique_constraint(
        "evidence_unique",
        "evidence",
        ["milestone_id", "message_id", "message_version", "quote"],
        postgresql_nulls_not_distinct=True,
    )
    op.create_foreign_key(
        "evidence_message_version_fk",
        "evidence",
        "message_versions",
        ["message_id", "message_version"],
        ["message_id", "version"],
        ondelete="CASCADE",
    )
    op.create_table(
        "capture_sources",
        sa.Column(
            "conversation_id",
            pg.UUID(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("source_id", sa.Text(), primary_key=True),
        sa.Column("turn_id", sa.Text(), nullable=False),
        sa.Column(
            "message_id",
            sa.BigInteger(),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    op.create_table(
        "capture_incidents",
        sa.Column("id", pg.UUID(), primary_key=True),
        sa.Column(
            "conversation_id",
            pg.UUID(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("turn_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", pg.JSONB(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("conversation_id", "fingerprint", name="capture_incident_unique"),
    )
    op.create_index(
        "ix_capture_incidents_conversation_id", "capture_incidents", ["conversation_id"]
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text("""SELECT
        EXISTS(SELECT 1 FROM captured_turns) OR EXISTS(SELECT 1 FROM message_versions)
        OR EXISTS(SELECT 1 FROM capture_sources) OR EXISTS(SELECT 1 FROM capture_incidents)
        OR EXISTS(SELECT 1 FROM messages WHERE version IS NOT NULL)
        OR EXISTS(SELECT 1 FROM evidence WHERE message_version IS NOT NULL)
        OR EXISTS(SELECT 1 FROM conversations WHERE capture_integrity_pending)
        OR EXISTS(SELECT 1 FROM turn_executions
            WHERE producer_owner_id IS NOT NULL OR producer_epoch IS NOT NULL)
    """)
    ):
        raise RuntimeError(
            "Downgrade would destroy logical capture versions; retain additive schema"
        )
    op.drop_constraint("evidence_message_version_fk", "evidence", type_="foreignkey")
    op.drop_constraint("evidence_unique", "evidence", type_="unique")
    op.create_unique_constraint(
        "evidence_unique", "evidence", ["milestone_id", "message_id", "quote"]
    )
    for name in ("capture_incidents", "capture_sources", "message_versions", "captured_turns"):
        op.drop_table(name)
    op.execute("DROP FUNCTION immutable_message_version()")
    op.drop_column("evidence", "message_version")
    op.drop_column("messages", "version")
    op.drop_column("conversations", "capture_integrity_pending")
    op.drop_column("turn_executions", "producer_epoch")
    op.drop_column("turn_executions", "producer_owner_id")
