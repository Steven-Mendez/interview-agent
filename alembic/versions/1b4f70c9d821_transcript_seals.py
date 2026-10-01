"""Immutable transcript seals and explicit incident reviews.

Revision ID: 1b4f70c9d821
Revises: f084ac76d119
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "1b4f70c9d821"
down_revision = "f084ac76d119"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "transcript_seals",
        sa.Column("id", pg.UUID(), primary_key=True),
        sa.Column(
            "conversation_id",
            pg.UUID(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_id", pg.UUID(), sa.ForeignKey("transcript_seals.id", ondelete="CASCADE")),
        sa.Column("records", pg.JSONB(), nullable=False),
        sa.Column("provenance", pg.JSONB(), nullable=False),
        sa.Column("transcript_hash", sa.Text(), nullable=False),
        sa.Column("integrity", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.UniqueConstraint("conversation_id", "version", name="transcript_seal_version_unique"),
        sa.CheckConstraint("version >= 1", name="transcript_seal_version_positive"),
    )
    op.create_index("ix_transcript_seals_conversation_id", "transcript_seals", ["conversation_id"])
    op.add_column("conversations", sa.Column("transcript_seal_id", pg.UUID()))
    op.create_foreign_key(
        "conversation_seal_fk",
        "conversations",
        "transcript_seals",
        ["transcript_seal_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "capture_incidents",
        sa.Column(
            "seal_id",
            pg.UUID(),
            sa.ForeignKey("transcript_seals.id", ondelete="CASCADE", name="incident_seal_fk"),
        ),
    )
    op.add_column(
        "evaluation_requests",
        sa.Column(
            "seal_id",
            pg.UUID(),
            sa.ForeignKey(
                "transcript_seals.id", ondelete="CASCADE", name="evaluation_request_seal_fk"
            ),
        ),
    )
    op.create_table(
        "incident_resolutions",
        sa.Column("id", pg.UUID(), primary_key=True),
        sa.Column(
            "conversation_id",
            pg.UUID(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "incident_id",
            pg.UUID(),
            sa.ForeignKey("capture_incidents.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("reviewer", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.CheckConstraint(
            "decision IN ('duplicate','post_cut','omission')", name="incident_resolution_decision"
        ),
    )
    op.execute("""CREATE FUNCTION protect_sealed_history() RETURNS trigger AS $$
        BEGIN
            IF TG_OP='UPDATE' OR EXISTS(SELECT 1 FROM conversations WHERE id=OLD.conversation_id) THEN
                RAISE EXCEPTION 'Sealed history is immutable';
            END IF;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql""")
    for table in ("transcript_seals", "incident_resolutions"):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION protect_sealed_history()"
        )


def downgrade():
    if op.get_bind().scalar(
        sa.text("""SELECT EXISTS(SELECT 1 FROM transcript_seals)
        OR EXISTS(SELECT 1 FROM incident_resolutions)
        OR EXISTS(SELECT 1 FROM conversations WHERE transcript_seal_id IS NOT NULL)
        OR EXISTS(SELECT 1 FROM capture_incidents WHERE seal_id IS NOT NULL)
        OR EXISTS(SELECT 1 FROM evaluation_requests WHERE seal_id IS NOT NULL)""")
    ):
        raise RuntimeError(
            "Downgrade would destroy sealed transcript history; retain additive schema"
        )
    for table in ("transcript_seals", "incident_resolutions"):
        op.execute(f"DROP TRIGGER {table}_immutable ON {table}")
    op.execute("DROP FUNCTION protect_sealed_history()")
    op.drop_table("incident_resolutions")
    for table, constraint in (
        ("evaluation_requests", "evaluation_request_seal_fk"),
        ("capture_incidents", "incident_seal_fk"),
    ):
        op.drop_constraint(constraint, table, type_="foreignkey")
        op.drop_column(table, "seal_id")
    op.drop_constraint("conversation_seal_fk", "conversations", type_="foreignkey")
    op.drop_column("conversations", "transcript_seal_id")
    op.drop_table("transcript_seals")
