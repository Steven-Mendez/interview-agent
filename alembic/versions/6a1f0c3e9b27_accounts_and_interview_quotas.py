"""User accounts: interview owners, per-user settings and interview quotas.

Revision ID: 6a1f0c3e9b27
Revises: 2d4e8adaf02e
"""

from alembic import op
import sqlalchemy as sa

revision = "6a1f0c3e9b27"
down_revision = "2d4e8adaf02e"
branch_labels = None
depends_on = None


def upgrade():
    # The JWT `sub` of whoever created it. Rows from before accounts stay NULL
    # (nobody's) until scripts/claim_interviews.py assigns them.
    op.add_column("conversations", sa.Column("owner_id", sa.Text()))
    op.create_index("conversations_owner_created_idx", "conversations", ["owner_id", "created_at"])
    # Same columns and defaults as app_settings, one row per user; a user
    # without one reads app_settings.
    op.create_table(
        "user_settings",
        sa.Column("owner_id", sa.Text(), primary_key=True),
        sa.Column("agent_name", sa.Text(), nullable=False, server_default="Emma"),
        sa.Column("language", sa.Text(), nullable=False, server_default="en"),
        sa.Column("voice", sa.Text(), nullable=False, server_default="en_female"),
        sa.Column("persona", sa.Text()),
        sa.Column("custom_instructions", sa.Text()),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    # Interviews a user ever started. No foreign key to conversations: the
    # retention purge deletes interviews, never the count of them.
    op.create_table(
        "user_interview_quotas",
        sa.Column("owner_id", sa.Text(), primary_key=True),
        sa.Column("interviews_used", sa.Integer(), nullable=False, server_default="0"),
    )
    # Interviews all non-admin users started per calendar month (UTC).
    op.create_table(
        "guest_interview_months",
        sa.Column("month", sa.Date(), primary_key=True),
        sa.Column("interviews_started", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM conversations WHERE owner_id IS NOT NULL) "
            "OR EXISTS(SELECT 1 FROM user_settings) "
            "OR EXISTS(SELECT 1 FROM user_interview_quotas) "
            "OR EXISTS(SELECT 1 FROM guest_interview_months)"
        )
    ):
        raise RuntimeError(
            "Downgrade would destroy interview owners, user settings or interview quotas; "
            "retain the additive schema"
        )
    op.drop_table("guest_interview_months")
    op.drop_table("user_interview_quotas")
    op.drop_table("user_settings")
    op.drop_index("conversations_owner_created_idx", table_name="conversations")
    op.drop_column("conversations", "owner_id")
