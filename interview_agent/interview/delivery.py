"""Recover validated questions without another model decision or automatic replay."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, literal, select
from sqlalchemy.dialects.postgresql import JSONB

from interview_agent.interview import db


@dataclass(frozen=True)
class QuestionSpeech:
    text: str
    attempt_id: uuid.UUID


async def save_question(session, conversation_id, run_id, text):
    # Called inside the decision transaction. Audio has not been attempted yet.
    order = (
        await session.scalar(
            select(func.max(db.CapturedTurn.capture_order)).where(
                db.CapturedTurn.conversation_id == conversation_id
            )
        )
        or 0
    )
    session.add(
        db.QuestionDelivery(
            id=run_id,
            conversation_id=conversation_id,
            text=text,
            capture_order=order,
        )
    )


async def latest_question(session, conversation_id):
    return await session.scalar(
        select(db.QuestionDelivery)
        .where(db.QuestionDelivery.conversation_id == conversation_id)
        .order_by(db.QuestionDelivery.created_at.desc(), db.QuestionDelivery.id.desc())
        .limit(1)
    )


async def answered(session, question):
    return bool(
        await session.scalar(
            select(db.Message.id)
            .join(db.CapturedTurn, db.CapturedTurn.message_id == db.Message.id)
            .where(
                db.CapturedTurn.conversation_id == question.conversation_id,
                db.CapturedTurn.capture_order > question.capture_order,
                db.Message.metrics["stt_confirmed"] == literal(True, type_=JSONB),
            )
            .limit(1)
        )
    )


async def request_replay(session, conversation_id, question_id, request_id):
    conversation = await session.scalar(
        select(db.Conversation).where(db.Conversation.id == conversation_id).with_for_update()
    )
    question = await latest_question(session, conversation_id)
    if (
        conversation is None
        or conversation.status != "interviewing"
        or conversation.capture_integrity_pending
        or question is None
        or question.id != question_id
        or await answered(session, question)
    ):
        raise ValueError("This question is no longer available for replay")
    previous = await session.get(db.QuestionAttempt, request_id)
    if previous is not None:
        if previous.question_id != question.id or not previous.explicit:
            raise ValueError("Replay identity belongs to a different request")
        return previous.id
    # Coalesce simultaneous clicks into the same still-pending explicit request.
    pending = await session.scalar(
        select(db.QuestionAttempt).where(
            db.QuestionAttempt.question_id == question.id, db.QuestionAttempt.status == "requested"
        )
    )
    if pending is not None:
        return pending.id
    session.add(
        db.QuestionAttempt(
            id=request_id, question_id=question.id, status="requested", explicit=True
        )
    )
    await session.commit()
    return request_id


class QuestionDeliveries:
    def __init__(self, sessionmaker, conversation_id):
        self.sessions = sessionmaker
        self.conversation_id = conversation_id

    async def claim(self, *, turn_id=None):
        async with self.sessions() as session:
            conversation = await session.scalar(
                select(db.Conversation)
                .where(db.Conversation.id == self.conversation_id)
                .with_for_update()
            )
            if (
                conversation is None
                or conversation.status != "interviewing"
                or conversation.capture_integrity_pending
            ):
                return None
            question = await latest_question(session, self.conversation_id)
            if question is None or await answered(session, question):
                return None
            if turn_id is not None:
                run = await session.get(db.TurnRun, question.id)
                if run.turn_id != turn_id:
                    return None
            attempts = list(
                await session.scalars(
                    select(db.QuestionAttempt)
                    .where(db.QuestionAttempt.question_id == question.id)
                    .order_by(db.QuestionAttempt.created_at)
                )
            )
            pending = next((a for a in attempts if a.status == "requested"), None)
            if pending is None and attempts:
                # A started attempt could already have been heard. Never infer
                # non-delivery from a crash, missing SDK callback or reconnection.
                return None
            attempt = pending or db.QuestionAttempt(
                id=uuid.uuid4(),
                question_id=question.id,
                explicit=False,
            )
            if pending is None:
                session.add(attempt)
            attempt.status = "started"
            attempt.owner_id = conversation.worker_owner_id
            attempt.owner_epoch = conversation.worker_epoch
            attempt.started_at = await session.scalar(select(func.clock_timestamp()))
            await session.commit()
            return QuestionSpeech(question.text, attempt.id)

    async def observed(self, attempt_id, *, interrupted):
        async with self.sessions() as session:
            conversation = await session.scalar(
                select(db.Conversation)
                .where(db.Conversation.id == self.conversation_id)
                .with_for_update()
            )
            attempt = await session.get(db.QuestionAttempt, attempt_id)
            if attempt is None or conversation is None:
                return
            question = await session.get(db.QuestionDelivery, attempt.question_id)
            if (
                question.conversation_id != self.conversation_id
                or attempt.owner_id != conversation.worker_owner_id
                or attempt.owner_epoch != conversation.worker_epoch
                or attempt.status != "started"
            ):
                return
            # SDK completion is evidence of forwarding, not browser audibility.
            attempt.status = "interrupted" if interrupted else "sdk_completed"
            attempt.finished_at = await session.scalar(select(func.clock_timestamp()))
            await session.commit()
