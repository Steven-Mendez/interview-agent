"""Add PostgreSQL worker ownership and leases.

Revision ID: a3e72bc19054
Revises: c632ad97e120
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a3e72bc19054"
down_revision = "c632ad97e120"
branch_labels = None
depends_on = None


def upgrade():
    # Server sweep health has no candidate/interview identity.
    op.alter_column("metric_events", "conversation_id", nullable=True)
    op.add_column("conversations", sa.Column("worker_owner_id", postgresql.UUID(as_uuid=True)))
    op.add_column(
        "conversations",
        sa.Column("worker_epoch", sa.BigInteger(), nullable=False, server_default="0"),
    )
    for name in (
        "worker_acquired_at",
        "worker_lease_until",
        "worker_activity_at",
        "worker_disconnected_at",
    ):
        op.add_column("conversations", sa.Column(name, sa.DateTime(timezone=True)))


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM conversations WHERE worker_epoch > 0) "
            "OR EXISTS(SELECT 1 FROM metric_events WHERE conversation_id IS NULL)"
        )
    ):
        raise RuntimeError("Downgrade would destroy worker ownership; retain the additive schema")
    for name in (
        "worker_disconnected_at",
        "worker_activity_at",
        "worker_lease_until",
        "worker_acquired_at",
        "worker_epoch",
        "worker_owner_id",
    ):
        op.drop_column("conversations", name)
    op.alter_column("metric_events", "conversation_id", nullable=False)
