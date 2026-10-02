"""User profiles: who signed in, from where and when last seen.

Revision ID: 9c3d5e7f1a20
Revises: 6a1f0c3e9b27
"""

from alembic import op
import sqlalchemy as sa

revision = "9c3d5e7f1a20"
down_revision = "6a1f0c3e9b27"
branch_labels = None
depends_on = None


def upgrade():
    # One row per user, written by GET /me from the sign-in's claims. No
    # foreign key to conversations: the retention purge deletes interviews,
    # never the people who had them.
    op.create_table(
        "user_profiles",
        sa.Column("owner_id", sa.Text(), primary_key=True),
        sa.Column("auth_provider", sa.Text(), nullable=False),
        sa.Column("email", sa.Text()),
        sa.Column("name", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("auth_provider IN ('neon', 'local')", name="user_profile_provider"),
    )
    op.create_index("user_profiles_last_seen_idx", "user_profiles", ["last_seen_at"])
    # Everyone the accounts tables already know. Nobody has been seen yet, so
    # nobody counts as active until their next visit; a user is as old as
    # their first interview still stored.
    op.execute(
        "INSERT INTO user_profiles (owner_id, auth_provider, created_at) "
        "SELECT owners.owner_id, "
        "CASE WHEN owners.owner_id = 'local-dev' OR starts_with(owners.owner_id, 'local:') "
        "THEN 'local' ELSE 'neon' END, "
        "COALESCE((SELECT min(created_at) FROM conversations "
        "WHERE conversations.owner_id = owners.owner_id), now()) "
        "FROM (SELECT owner_id FROM conversations WHERE owner_id IS NOT NULL "
        "UNION SELECT owner_id FROM user_interview_quotas "
        "UNION SELECT owner_id FROM user_settings) AS owners"
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM user_profiles)")):
        raise RuntimeError(
            "Downgrade would destroy user profiles (emails, names, sign-in dates); "
            "retain the additive schema"
        )
    op.drop_index("user_profiles_last_seen_idx", table_name="user_profiles")
    op.drop_table("user_profiles")
