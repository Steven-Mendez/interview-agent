"""Reserve logical model invocations before the provider request.

Revision ID: f41b987cad12
Revises: e79a42c608bd
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "f41b987cad12"
down_revision = "e79a42c608bd"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "turn_executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("invocations_reserved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("decision_deadline_at", sa.DateTime(timezone=True)),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("last_owner_id", postgresql.UUID(as_uuid=True)),
        sa.Column("notice_claimed_at", sa.DateTime(timezone=True)),
        sa.Column("waiters", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("conversation_id", "turn_id", name="turn_executions_unique"),
        sa.CheckConstraint("invocations_reserved BETWEEN 0 AND 2", name="turn_execution_budget"),
    )
    op.create_index(
        "turn_executions_queue_idx", "turn_executions", ["conversation_id", "status", "created_at"]
    )
    op.create_table(
        "turn_invocations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state_revision", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("outcome", sa.Text()),
        sa.ForeignKeyConstraint(["execution_id"], ["turn_executions.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("execution_id", "ordinal", name="turn_invocations_unique"),
        sa.CheckConstraint("ordinal BETWEEN 1 AND 2", name="turn_invocation_ordinal"),
    )
    op.create_index("ix_turn_invocations_execution_id", "turn_invocations", ["execution_id"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM turn_executions)")):
        raise RuntimeError(
            "Downgrade would destroy durable turn reservations; retain the additive schema"
        )
    op.drop_table("turn_invocations")
    op.drop_table("turn_executions")
