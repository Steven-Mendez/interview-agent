"""Native agent playout and historical browser playback evidence."""

from __future__ import annotations

import math
import uuid
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from interview_agent.interview import db

MAX_CLIP_BYTES = 5_000_000
NATIVE_AUDIO_MIME = "audio/rtc"


def confirmation_source(conv) -> str | None:
    """The evidence policy, not a claim that audio reached a physical speaker."""
    if conv.closing_audio_mime == NATIVE_AUDIO_MIME:
        return "agent_playout"
    if conv.closing_audio_mime == "audio/wav":
        return "browser_playback"
    return None


# Why a browser could not play the farewell: a DOMException name, a media
# element error code, a broken byte stream, or "other". Categories the browser
# maps to, never an error message, which can describe the media.
PlaybackErrorKind = Literal[
    "NotAllowedError",
    "NotSupportedError",
    "AbortError",
    "NotFoundError",
    "EncodingError",
    "InvalidStateError",
    "SecurityError",
    "MEDIA_ERR_ABORTED",
    "MEDIA_ERR_NETWORK",
    "MEDIA_ERR_DECODE",
    "MEDIA_ERR_SRC_NOT_SUPPORTED",
    "stream",
    "other",
]


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
    # Only meaningful with `failed`. Not stored: the closing record has no
    # place for it; the route turns it into a metric category.
    error_kind: PlaybackErrorKind | None = None


def _matches(conv, ack):
    return (
        conv.closing_id == ack.closing_id
        and conv.closing_stream_id == ack.stream_id
        and conv.closing_attempt_id == ack.attempt_id
    )


def _promote(conv):
    if conv.closing_audio_mime == NATIVE_AUDIO_MIME:
        return
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


async def acknowledge_playback(
    sessionmaker, conversation_id, ack: PlaybackAck
) -> tuple[dict, bool]:
    """The response, and whether this call stored the attempt's first ACK: the
    browser repeats it until one is accepted."""
    async with sessionmaker() as session:
        conv = await session.scalar(
            select(db.Conversation).where(db.Conversation.id == conversation_id).with_for_update()
        )
        if conv is None:
            raise LookupError("Interview does not exist")
        if not _matches(conv, ack):
            raise ValueError("Playback acknowledgement does not match the closure attempt")
        if conv.closing_audio_mime == NATIVE_AUDIO_MIME:
            raise ValueError("Native speech is confirmed by the agent, not a clip acknowledgement")
        if conv.farewell_status == "played" and conv.closing_ack_status == "played":
            return {"accepted": True, "status": "played", "provisional": False}, False
        now = await session.scalar(select(func.clock_timestamp()))
        if conv.closing_ack_deadline_at is None or now > conv.closing_ack_deadline_at:
            return {"accepted": False, "reason": "expired"}, False
        first = conv.closing_ack_received_at is None
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
        }, first


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
            or conv.closing_audio_mime == NATIVE_AUDIO_MIME
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


async def record_agent_playout(
    sessionmaker, conversation_id, owner_id, closing_id, attempt_id, duration, *, source_id
) -> bool:
    """Persist successful native playout without fabricating a browser ACK."""
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration <= 0
        or not isinstance(source_id, str)
        or not source_id
    ):
        raise ValueError("Native speech needs a finite positive playout duration")
    async with sessionmaker() as session:
        conv = await session.scalar(
            select(db.Conversation).where(db.Conversation.id == conversation_id).with_for_update()
        )
        now = await session.scalar(select(func.clock_timestamp()))
        if (
            conv is None
            or conv.status != "closing"
            or conv.closing_owner_id != owner_id
            or conv.closing_id != closing_id
            or conv.closing_attempt_id != attempt_id
            or conv.closing_audio_mime != NATIVE_AUDIO_MIME
            or conv.closing_deadline_at is None
            or conv.closing_deadline_at <= now
            or conv.closing_ack_deadline_at is None
            or conv.closing_ack_deadline_at <= now
            or conv.closing_acquired_at is None
            or conv.closing_audio_timeout_seconds is None
            or now
            > conv.closing_acquired_at + timedelta(seconds=conv.closing_audio_timeout_seconds)
        ):
            return False
        item = await session.scalar(
            select(db.Message).where(
                db.Message.conversation_id == conversation_id,
                db.Message.source_id == source_id,
                db.Message.role == "assistant",
                db.Message.interrupted.is_not(True),
            )
        )
        if item is None:
            return False
        conv.closing_delivery_at = conv.closing_delivery_at or now
        conv.closing_stream_id = source_id
        conv.closing_playback_seconds = duration
        conv.closing_playback_exceeded_budget = False
        conv.farewell_status = "played"
        await session.commit()
        return True


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
            "farewell_confirmation_source": confirmation_source(conv),
            "transcript_sealed": conv.transcript_sealed_at is not None,
            "transcript_integrity": conv.transcript_integrity,
            "remaining_seconds": remaining,
            "playback_exceeded_budget": conv.closing_playback_exceeded_budget,
        }
