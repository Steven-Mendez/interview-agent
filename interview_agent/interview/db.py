"""Async SQLAlchemy layer: ORM models and the query helpers both processes
(FastAPI server and LiveKit worker) share.

Schema changes go through Alembic (`uv run alembic revision --autogenerate`),
never through create_all.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    # Plain `datetime` annotations become TIMESTAMPTZ, not naive TIMESTAMP.
    type_annotation_map: ClassVar = {datetime: DateTime(timezone=True)}


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Bumped on every UPDATE (status changes included); the capacity check
    # uses it to ignore orphaned "interviewing" rows from crashed workers.
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
    # created | planned | interviewing | completed | evaluation_failed
    # | evaluated | error
    status: Mapped[str] = mapped_column(Text, default="created", server_default="created")
    job_offer: Mapped[str] = mapped_column(Text)
    resume_markdown: Mapped[str] = mapped_column(Text)
    resume_filename: Mapped[str | None] = mapped_column(Text)
    # Optional user inputs: desired interviewer personality (adopted by the
    # planner) and free-form requests (practice topics, question limits...).
    persona: Mapped[str | None] = mapped_column(Text)
    custom_instructions: Mapped[str | None] = mapped_column(Text)
    # Planner output minus milestones: persona, language, summary, focus_areas.
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # plan_complete | timeout | candidate_left | idle_timeout
    ended_reason: Mapped[str | None] = mapped_column(Text)
    # Per-component LLM spend, accumulated over the conversation's lifecycle:
    # {"planner" | "interviewer" | "evaluator":
    #   {"input_tokens": int, "output_tokens": int, "total_tokens": int}}
    token_usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # lazy="selectin": async sessions cannot lazy-load on attribute access
    # (MissingGreenlet), so both relationships load eagerly with the parent.
    milestones: Mapped[list[Milestone]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Milestone.position",
        lazy="selectin",
    )
    evaluation: Mapped[Evaluation | None] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", lazy="selectin"
    )


class Milestone(Base):
    __tablename__ = "milestones"
    __table_args__ = (Index("milestones_conv_idx", "conversation_id", "position"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    position: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    completed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    completed_at: Mapped[datetime | None] = mapped_column()
    notes: Mapped[str | None] = mapped_column(Text)

    conversation: Mapped[Conversation] = relationship(back_populates="milestones")


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("messages_conv_idx", "conversation_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(Text)  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    # Turn order, assigned synchronously in the worker's event handler. The
    # autoincrement id reflects COMMIT order, and the persist tasks are
    # fire-and-forget — under latency two inserts can commit out of order.
    # Nullable for rows that predate the column; get_messages falls back to id.
    seq: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Evaluation(Base):
    __tablename__ = "evaluations"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    hired: Mapped[bool] = mapped_column(Boolean)
    score: Mapped[int] = mapped_column(Integer)  # 0..100
    strengths: Mapped[list[str]] = mapped_column(JSONB)
    weaknesses: Mapped[list[str]] = mapped_column(JSONB)
    rationale: Mapped[str] = mapped_column(Text)
    ended_by: Mapped[str] = mapped_column(Text)  # same values as ended_reason
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    conversation: Mapped[Conversation] = relationship(back_populates="evaluation")


# --- Engine / session helpers -------------------------------------------------


def create_engine_and_sessionmaker(
    database_url: str,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(database_url, pool_size=5, max_overflow=5)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


# --- Query helpers ------------------------------------------------------------
# Each takes an AsyncSession and commits itself, so callers in the worker's
# event handlers can fire-and-forget them inside one `async with` block.


async def get_conversation(
    session: AsyncSession, conversation_id: uuid.UUID
) -> Conversation | None:
    return await session.get(Conversation, conversation_id)


async def get_milestones(
    session: AsyncSession, conversation_id: uuid.UUID
) -> list[Milestone]:
    result = await session.scalars(
        select(Milestone)
        .where(Milestone.conversation_id == conversation_id)
        .order_by(Milestone.position)
    )
    return list(result)


async def get_messages(
    session: AsyncSession, conversation_id: uuid.UUID
) -> list[Message]:
    result = await session.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        # seq is the true turn order; id breaks ties and covers any row
        # without one (the migration backfills seq from id order).
        .order_by(Message.seq, Message.id)
    )
    return list(result)


async def insert_message(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    role: str,
    content: str,
    seq: int | None = None,
) -> None:
    session.add(
        Message(conversation_id=conversation_id, role=role, content=content, seq=seq)
    )
    await session.commit()


async def count_active_interviews(session: AsyncSession, window_minutes: int) -> int:
    """Conversations in 'interviewing' whose updated_at falls in the window.

    A live interview can never outlast its time cap, so the window (cap plus
    some slack) auto-excludes stale rows left behind by crashed workers."""
    result = await session.scalar(
        select(func.count())
        .select_from(Conversation)
        .where(
            Conversation.status == "interviewing",
            Conversation.updated_at >= func.now() - timedelta(minutes=window_minutes),
        )
    )
    return int(result or 0)


async def set_status(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    status: str,
    ended_reason: str | None = None,
) -> None:
    values: dict[str, Any] = {"status": status}
    if ended_reason is not None:
        values["ended_reason"] = ended_reason
    await session.execute(
        update(Conversation).where(Conversation.id == conversation_id).values(**values)
    )
    await session.commit()


async def complete_milestone(
    session: AsyncSession, milestone_id: uuid.UUID, notes: str
) -> Milestone | None:
    """Mark one milestone done; returns it (or None if the id is unknown)."""
    milestone = await session.get(Milestone, milestone_id)
    if milestone is None:
        return None
    milestone.completed = True
    milestone.completed_at = datetime.now(UTC)
    milestone.notes = notes
    await session.commit()
    return milestone


async def add_token_usage(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    component: str,
    usage: dict[str, int],
) -> None:
    """Merge one component's token counts into conversations.token_usage.

    Read-modify-write without a lock is safe here: the three writers never
    run concurrently for one conversation — planning precedes the interview,
    and the worker flushes interviewer usage before triggering evaluation.
    """
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        return
    current = dict(conversation.token_usage or {})
    existing = current.get(component, {})
    current[component] = {
        key: int(existing.get(key, 0)) + int(usage.get(key, 0))
        for key in ("input_tokens", "output_tokens", "total_tokens")
    }
    # Reassign the whole dict: JSONB columns have no mutation tracking, so
    # in-place updates would never be flushed (same pattern as `.plan`).
    conversation.token_usage = current
    await session.commit()


async def delete_conversations_older_than(
    session: AsyncSession, days: int
) -> list[uuid.UUID]:
    """Purge conversations (and, via CASCADE, their milestones, messages and
    evaluations) older than `days`. Returns the deleted ids so the caller can
    clean up the matching Qdrant points."""
    cutoff = func.now() - timedelta(days=days)
    ids = list(
        await session.scalars(
            select(Conversation.id).where(Conversation.created_at < cutoff)
        )
    )
    if ids:
        await session.execute(delete(Conversation).where(Conversation.id.in_(ids)))
        await session.commit()
    return ids
