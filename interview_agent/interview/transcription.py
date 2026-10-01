"""Durable logical turns and immutable versions of candidate evidence."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select

from interview_agent.interview import db

PROVENANCE_KEYS = frozenset(
    {
        "stt_confirmed",
        "stt_turn_id",
        "stt_turn_version",
        "stt_segments",
        "stt_segmentation",
        "stt_capture_sequence",
        "stt_capture_session_id",
    }
)


def _provenance(metrics):
    return {key: value for key, value in metrics.items() if key in PROVENANCE_KEYS}


@dataclass(frozen=True)
class CaptureAdmission:
    message: db.Message | None
    turn_id: str
    accepted: bool
    incident: str | None = None


def _finite_metadata(value):
    if isinstance(value, float) and not math.isfinite(value):
        return {"invalid_float": repr(value)}, True
    if isinstance(value, (dict, list, tuple)):
        pairs = value.items() if isinstance(value, dict) else enumerate(value)
        result, invalid = {}, False
        for key, item in pairs:
            result[key], bad = _finite_metadata(item)
            invalid |= bad
        return (result if isinstance(value, dict) else list(result.values())), invalid
    return value, False


def _payload(content, source_id, version, interrupted, metrics):
    # JSON round-trip freezes the caller's mutable lists/dicts and normalizes
    # SDK tuples. No content is sent to external traces or log messages here.
    return json.loads(
        json.dumps(
            {
                "content": content,
                "source_id": source_id,
                "version": version,
                "interrupted": interrupted,
                "metrics": metrics,
            },
            allow_nan=False,
        )
    )


async def _incident(session, conversation, turn_id, kind, payload):
    fingerprint = hashlib.sha256(
        json.dumps(
            [turn_id, kind, payload],
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    existing = await session.scalar(
        select(db.CaptureIncident.id).where(
            db.CaptureIncident.conversation_id == conversation.id,
            db.CaptureIncident.fingerprint == fingerprint,
        )
    )
    if existing is None:
        session.add(
            db.CaptureIncident(
                id=uuid.uuid4(),
                conversation_id=conversation.id,
                turn_id=turn_id,
                kind=kind,
                payload=payload,
                fingerprint=fingerprint,
                seal_id=conversation.transcript_seal_id,
            )
        )
        conversation.capture_integrity_pending = True
        conversation.state_revision += 1


async def admit_candidate(
    session,
    conversation_id,
    *,
    content,
    source_id,
    metrics,
    interrupted=False,
    commit=True,
):
    """A provenance-identified correction keeps the original ID and decision budget.

    Never infer capture identity by matching text. Missing provider/audio
    metadata is retained explicitly; SDK IDs remain a conservative identity
    fallback when no logical capture ID was supplied by the voice boundary.
    """
    if not isinstance(source_id, str) or not source_id or len(source_id) > 256:
        raise ValueError("Candidate capture requires an explicit source ID")
    metrics = dict(metrics or {})
    turn_id = metrics.get("stt_turn_id") or source_id
    version = metrics.get("stt_turn_version", 1)
    if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 256:
        raise ValueError("Candidate capture requires a bounded logical turn ID")
    if type(version) is not int or version < 1:
        raise ValueError("Candidate capture version must be a positive integer")
    metrics.setdefault("stt_confirmed", False)
    metrics.setdefault("stt_segmentation", "unknown")
    metrics, invalid_metadata = _finite_metadata(metrics)
    payload = _payload(content, source_id, version, interrupted, metrics)
    metrics = payload["metrics"]
    conversation = await session.scalar(
        select(db.Conversation)
        .where(
            db.Conversation.id == conversation_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if conversation is None:
        raise ValueError("Candidate capture interview no longer exists")
    capture = await session.get(db.CapturedTurn, (conversation_id, turn_id))
    message = await session.get(db.Message, capture.message_id) if capture else None
    alias = await session.get(db.CaptureSource, (conversation_id, source_id))
    snapshot = await session.get(db.MessageVersion, (message.id, version)) if message else None
    if invalid_metadata:
        kind = "invalid_metadata"
    elif alias is not None and alias.turn_id != turn_id:
        kind = "source_identity_conflict"
    elif snapshot is not None and (
        snapshot.content != content
        or snapshot.interrupted != interrupted
        or _provenance(snapshot.metrics) != _provenance(metrics)
    ):
        kind = "version_conflict"
    elif snapshot is not None:
        # The immutable snapshot is the idempotency authority. A delayed old
        # ChatMessage must not downgrade a newer canonical version.
        if alias is None:
            session.add(
                db.CaptureSource(
                    conversation_id=conversation_id,
                    source_id=source_id,
                    turn_id=turn_id,
                    message_id=message.id,
                )
            )
        if commit:
            await session.commit()
        else:
            await session.flush()
        return CaptureAdmission(message, turn_id, True)
    elif conversation.transcript_sealed_at is not None or conversation.status not in (
        "planned",
        "interviewing",
        "closing",
    ):
        kind = "late_after_seal"
    elif version != ((message.version or 0) + 1 if message else 1):
        kind = "version_gap"
    else:
        kind = None
    if kind is not None:
        await _incident(session, conversation, turn_id, kind, payload)
        if commit:
            await session.commit()
        else:
            await session.flush()
        return CaptureAdmission(message, turn_id, False, kind)
    if message is None:
        # A capture never adopts an unversioned row (an interviewer message)
        # that happens to share its source ID; it records the conflict instead.
        existing = await session.scalar(
            select(db.Message).where(
                db.Message.conversation_id == conversation_id,
                db.Message.source_id == source_id,
            )
        )
        if existing is not None:
            await _incident(session, conversation, turn_id, "source_conflict", payload)
            if commit:
                await session.commit()
            else:
                await session.flush()
            return CaptureAdmission(existing, turn_id, False, "source_conflict")
        message = await db.insert_message(
            session,
            conversation_id,
            "user",
            content,
            source_id=source_id,
            turn_id=turn_id,
            interrupted=interrupted,
            metrics=metrics,
            commit=False,
        )
        capture_order = (
            await session.scalar(
                select(func.max(db.CapturedTurn.capture_order)).where(
                    db.CapturedTurn.conversation_id == conversation_id,
                )
            )
            or 0
        ) + 1
        session.add(
            db.CapturedTurn(
                conversation_id=conversation_id,
                turn_id=turn_id,
                message_id=message.id,
                capture_order=capture_order,
            )
        )
    else:
        message.content = content
        message.interrupted = interrupted
        message.metrics = metrics
        conversation.state_revision += 1
    message.version = version
    session.add(
        db.MessageVersion(
            message_id=message.id,
            version=version,
            content=content,
            source_id=source_id,
            interrupted=interrupted,
            metrics=metrics,
        )
    )
    if alias is None:
        session.add(
            db.CaptureSource(
                conversation_id=conversation_id,
                source_id=source_id,
                turn_id=turn_id,
                message_id=message.id,
            )
        )
    if commit:
        await session.commit()
    else:
        await session.flush()
    return CaptureAdmission(message, turn_id, True)


class OrderedCaptureWriter:
    """Snapshot EOT synchronously and commit in capture order before graph entry.

    The queue retains errors through shutdown. Cancellation of a caller waiting
    at the barrier does not cancel a capture already admitted by the SDK.
    """

    def __init__(self, sessionmaker, conversation_id):
        self.sessionmaker = sessionmaker
        self.conversation_id = conversation_id
        self.tail = None
        self.errors = []
        self.integrity_pending = False

    def submit(self, *, content, source_id, metrics, interrupted=False):
        payload = copy.deepcopy(
            {
                "content": content,
                "source_id": source_id,
                "metrics": metrics,
                "interrupted": interrupted,
            }
        )
        previous = self.tail

        async def write():
            if previous is not None:
                await previous
            try:
                async with self.sessionmaker() as session:
                    result = await admit_candidate(session, self.conversation_id, **payload)
                    self.integrity_pending |= not result.accepted
            except Exception as exc:
                self.errors.append(exc)

        self.tail = asyncio.create_task(write())
        return self.tail

    async def drain(self):
        # Include captures admitted while a preceding write was pending.
        while self.tail is not None:
            current = self.tail
            await asyncio.shield(current)
            if current is self.tail:
                break
        if self.errors:
            raise RuntimeError("Durable capture persistence failed") from self.errors[0]
