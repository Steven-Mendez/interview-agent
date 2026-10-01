"""Durable delivery and browser playback evidence, independent of RTC job lifetime."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from interview_agent.interview import db

MAX_CLIP_BYTES = 5_000_000


class PlaybackAck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    closing_id: uuid.UUID
    stream_id: str = Field(min_length=1, max_length=128)
    attempt_id: uuid.UUID
    status: Literal["played", "failed", "timeout"]
    duration_seconds: float | None = Field(
        default=None, strict=True, ge=0, le=300, allow_inf_nan=False
    )
    # Which output the browser used, never a device name it cannot verify:
    # the device chosen before joining, or the browser's default output.
    audio_output: Literal["selected", "default"] | None = None


def _matches(conv, ack):
    return (
        conv.closing_id == ack.closing_id
        and conv.closing_stream_id == ack.stream_id
        and conv.closing_attempt_id == ack.attempt_id
    )


def _promote(conv):
    deadline = conv.closing_ack_deadline_at
    if (
        conv.closing_ack_status == "played"
        and deadline is not None
        and conv.closing_ack_received_at is not None
        and conv.closing_ack_received_at <= deadline
        and conv.closing_delivery_at is not None
        and conv.closing_delivery_at <= deadline
    ):
        # Playback and transcript integrity are separate. A late promotion
        # must never reopen a seal or launch another evaluation.
        conv.farewell_status = "played"
        if conv.closing_acquired_at is not None and conv.closing_audio_timeout_seconds is not None:
            completed_at = max(conv.closing_ack_received_at, conv.closing_delivery_at)
            conv.closing_playback_exceeded_budget = (
                completed_at - conv.closing_acquired_at
            ).total_seconds() > conv.closing_audio_timeout_seconds


async def acknowledge_playback(sessionmaker, conversation_id, ack: PlaybackAck) -> dict:
    async with sessionmaker() as session:
        conv = await session.scalar(
            select(db.Conversation).where(db.Conversation.id == conversation_id).with_for_update()
        )
        if conv is None:
            raise LookupError("Interview does not exist")
        if not _matches(conv, ack):
            raise ValueError("Playback acknowledgement does not match the closure attempt")
        if conv.farewell_status == "played" and conv.closing_ack_status == "played":
            return {"accepted": True, "status": "played", "provisional": False}
        now = await session.scalar(select(func.clock_timestamp()))
        if conv.closing_ack_deadline_at is None or now > conv.closing_ack_deadline_at:
            return {"accepted": False, "reason": "expired"}
        if conv.closing_ack_status != "played":
            conv.closing_ack_status = ack.status
            conv.closing_ack_received_at = now
            conv.closing_playback_seconds = ack.duration_seconds
        if ack.status != "played" and conv.closing_ack_status != "played":
            conv.farewell_status = ack.status
        _promote(conv)
        await session.commit()
        return {
            "accepted": True,
            "status": conv.farewell_status,
            "provisional": conv.closing_ack_status == "played" and conv.farewell_status != "played",
        }


async def record_delivery(
    sessionmaker, conversation_id, owner_id, closing_id, stream_id, attempt_id, size, mime
) -> str | None:
    if mime != "audio/wav" or not 0 < size <= MAX_CLIP_BYTES:
        raise ValueError("Invalid farewell clip format or size")
    async with sessionmaker() as session:
        conv = await session.scalar(
            select(db.Conversation).where(db.Conversation.id == conversation_id).with_for_update()
        )
        now = await session.scalar(select(func.clock_timestamp()))
        if (
            conv is None
            or conv.closing_owner_id != owner_id
            or conv.closing_id != closing_id
            or conv.closing_stream_id != stream_id
            or conv.closing_attempt_id != attempt_id
            or conv.closing_ack_deadline_at is None
            or now > conv.closing_ack_deadline_at
        ):
            return None
        conv.closing_delivery_at = conv.closing_delivery_at or now
        conv.closing_audio_size = size
        conv.closing_audio_mime = mime
        _promote(conv)
        await session.commit()
        return conv.farewell_status


async def closing_state(sessionmaker, conversation_id) -> dict:
    async with sessionmaker() as session:
        conv = await session.get(db.Conversation, conversation_id)
        if conv is None:
            raise LookupError("Interview does not exist")
        now = await session.scalar(select(func.clock_timestamp()))
        remaining = None
        if conv.closing_ack_deadline_at is not None:
            # Initial ownership + audio/cleanup/margin + two 5-second sweeps.
            remaining = max(0, (conv.closing_ack_deadline_at - now).total_seconds() + 10)
        return {
            "status": conv.status,
            "closing_id": str(conv.closing_id) if conv.closing_id else None,
            "farewell_status": conv.farewell_status,
            "transcript_sealed": conv.transcript_sealed_at is not None,
            "transcript_integrity": conv.transcript_integrity,
            "remaining_seconds": remaining,
            "playback_exceeded_budget": conv.closing_playback_exceeded_budget,
        }
