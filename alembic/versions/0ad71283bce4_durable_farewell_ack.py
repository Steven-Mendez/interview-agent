"""Persist farewell delivery and participant ACK independently of the worker.

Revision ID: 0ad71283bce4
Revises: f41b987cad12
"""

from alembic import op
import sqlalchemy as sa

revision = "0ad71283bce4"
down_revision = "f41b987cad12"
branch_labels = None
depends_on = None


def upgrade():
    for name in (
        "closing_acquired_at",
        "closing_ack_deadline_at",
        "closing_delivery_at",
        "closing_ack_received_at",
    ):
        op.add_column("conversations", sa.Column(name, sa.DateTime(timezone=True)))
    for name in ("closing_audio_timeout_seconds", "closing_playback_seconds"):
        op.add_column("conversations", sa.Column(name, sa.Float()))
    op.add_column("conversations", sa.Column("closing_audio_size", sa.Integer()))
    for name in ("closing_audio_mime", "closing_ack_status"):
        op.add_column("conversations", sa.Column(name, sa.Text()))
    op.add_column("conversations", sa.Column("closing_playback_exceeded_budget", sa.Boolean()))


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM conversations WHERE closing_acquired_at IS NOT NULL)")
    ):
        raise RuntimeError("Downgrade would destroy farewell evidence; retain the additive schema")
    for name in (
        "closing_playback_exceeded_budget",
        "closing_ack_status",
        "closing_audio_mime",
        "closing_audio_size",
        "closing_playback_seconds",
        "closing_audio_timeout_seconds",
        "closing_ack_received_at",
        "closing_delivery_at",
        "closing_ack_deadline_at",
        "closing_acquired_at",
    ):
        op.drop_column("conversations", name)
