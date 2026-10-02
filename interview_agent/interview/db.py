"""Async SQLAlchemy layer: ORM models and the query helpers both processes
(FastAPI server and LiveKit worker) share.

Schema changes go through Alembic (`uv run alembic revision --autogenerate`),
never through create_all.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, ClassVar, Literal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    delete,
    exists,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.orm.attributes import flag_modified

from interview_agent.voices import DEFAULT_AGENT_NAME, DEFAULT_LANGUAGE, DEFAULT_VOICE


class Base(DeclarativeBase):
    # Plain `datetime` annotations become TIMESTAMPTZ, not naive TIMESTAMP.
    type_annotation_map: ClassVar = {datetime: DateTime(timezone=True)}


class Conversation(Base):
    __tablename__ = "conversations"

    __table_args__ = (
        Index("conversations_created_at_idx", "created_at"),
        Index("conversations_owner_created_idx", "owner_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    # The JWT `sub` of whoever created it; NULL (nobody's) for rows from before
    # accounts, until scripts/claim_interviews.py assigns them.
    owner_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Bumped on every UPDATE (status changes included); the capacity check
    # uses it to ignore orphaned "interviewing" rows from crashed workers.
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    # created | planned | interviewing | closing | completed | evaluating
    # | evaluation_failed | evaluated | error
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
    # The two calibration axes, resolved ONCE at creation and never re-inferred
    # downstream. `seniority` sets DEPTH (what is asked and what counts as a
    # sufficient answer), `interview_length` sets VOLUME (milestone count and
    # minutes). Deliberately NOT mirrored into `plan`: one source of truth, no
    # drift. seniority_source: explicit | detected | fallback.
    seniority: Mapped[str] = mapped_column(Text, default="mid", server_default="mid")
    seniority_source: Mapped[str] = mapped_column(
        Text, default="fallback", server_default="fallback"
    )
    # Why the planner classified it this way; NULL when the user picked it.
    seniority_evidence: Mapped[str | None] = mapped_column(Text)
    interview_length: Mapped[str] = mapped_column(
        Text, default="standard", server_default="standard"
    )
    # Time cap for THIS interview, derived from interview_length and clamped by
    # the global setting. Set at creation; NULL only before planning.
    max_minutes: Mapped[int | None] = mapped_column(Integer)
    # Snapshot of the app settings taken at creation time, with the voice
    # already resolved to a concrete tts_model/tts_voice pair: {"agent_name",
    # "language", "voice", "tts_model", "tts_voice"}. Editing the settings
    # never affects an interview already planned. The worker refuses rows
    # without it.
    agent_settings: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Set when this interview was started as a re-run of an earlier one, and
    # always points at the ROOT of the chain (repeating a repeat re-points at
    # the original), so every attempt on the same resume/offer groups under
    # one id. SET NULL on delete: losing the original must not cascade away
    # the attempts that came after it.
    repeat_of_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL")
    )
    # question_limit | plan_exhausted | candidate_requested | timeout
    # | idle_timeout | candidate_left | abandoned | worker_lost | connection_lost
    ended_reason: Mapped[str | None] = mapped_column(Text)
    # Per-component LLM spend, accumulated over the conversation's lifecycle:
    # {"planner" | "interviewer" | "evaluator":
    #   {"input_tokens": int, "output_tokens": int, "total_tokens": int}}
    token_usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    run_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    state_revision: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    question_limit: Mapped[int | None] = mapped_column(Integer)
    followup_limit: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column()
    worker_owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    worker_epoch: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    worker_acquired_at: Mapped[datetime | None] = mapped_column()
    worker_lease_until: Mapped[datetime | None] = mapped_column()
    worker_activity_at: Mapped[datetime | None] = mapped_column()
    worker_disconnected_at: Mapped[datetime | None] = mapped_column()
    evaluation_request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    transcript_seal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "transcript_seals.id", name="conversation_seal_fk", use_alter=True, ondelete="SET NULL"
        )
    )
    capture_integrity_pending: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    closing_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    closing_owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    closing_started_at: Mapped[datetime | None] = mapped_column()
    closing_acquired_at: Mapped[datetime | None] = mapped_column()
    closing_ack_deadline_at: Mapped[datetime | None] = mapped_column()
    closing_audio_timeout_seconds: Mapped[float | None] = mapped_column(Float)
    closing_delivery_at: Mapped[datetime | None] = mapped_column()
    closing_audio_size: Mapped[int | None] = mapped_column(Integer)
    closing_audio_mime: Mapped[str | None] = mapped_column(Text)
    closing_ack_received_at: Mapped[datetime | None] = mapped_column()
    closing_ack_status: Mapped[str | None] = mapped_column(Text)
    closing_playback_seconds: Mapped[float | None] = mapped_column(Float)
    closing_playback_exceeded_budget: Mapped[bool | None] = mapped_column(Boolean)
    closing_deadline_at: Mapped[datetime | None] = mapped_column()
    closing_stream_id: Mapped[str | None] = mapped_column(Text)
    closing_attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    transcript_sealed_at: Mapped[datetime | None] = mapped_column()
    transcript_integrity: Mapped[str | None] = mapped_column(Text)
    stt_drain: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    farewell_status: Mapped[str | None] = mapped_column(Text)
    evaluation_claim_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Browser response-onset samples accepted so far; bounds what one
    # interview's participant token can send.
    response_onset_samples: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

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
    # The bar, materialized at planning time: what the candidate must say for
    # this milestone to count as covered AT THE PINNED LEVEL. Travels to the
    # evaluator so it judges against a written criterion instead of
    # re-deriving how deep the topic "should" go.
    expected_evidence: Mapped[str | None] = mapped_column(Text)
    completed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    completed_at: Mapped[datetime | None] = mapped_column()
    notes: Mapped[str | None] = mapped_column(Text)
    lifecycle: Mapped[str] = mapped_column(Text, default="pending", server_default="pending")
    close_reason: Mapped[str | None] = mapped_column(Text)
    essential: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    competency: Mapped[str | None] = mapped_column(Text)
    primary_questions: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    followups: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    clarifications: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    conversation: Mapped[Conversation] = relationship(back_populates="milestones")


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("messages_conv_idx", "conversation_id", "id"),
        UniqueConstraint("conversation_id", "source_id", name="messages_source_unique"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(Text)  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    # Turn order, assigned synchronously in the worker's event handler. The
    # autoincrement id reflects COMMIT order, and the persist tasks are
    # fire-and-forget — under latency two inserts can commit out of order.
    # Allocated under the conversation lock; get_messages breaks ties by id.
    seq: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    source_id: Mapped[str | None] = mapped_column(Text)
    turn_id: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int | None] = mapped_column(Integer)
    interrupted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class CapturedTurn(Base):
    """One logical voice turn, regardless of the SDK message IDs it receives."""

    __tablename__ = "captured_turns"
    __table_args__ = (
        UniqueConstraint("message_id", name="captured_turn_message_unique"),
        UniqueConstraint("conversation_id", "capture_order", name="captured_turn_order_unique"),
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    turn_id: Mapped[str] = mapped_column(Text, primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"))
    capture_order: Mapped[int] = mapped_column(BigInteger)
    captured_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())


class MessageVersion(Base):
    """Immutable text/provenance snapshots of each candidate capture version."""

    __tablename__ = "message_versions"
    __table_args__ = (CheckConstraint("version >= 1", name="message_version_positive"),)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    source_id: Mapped[str] = mapped_column(Text)
    interrupted: Mapped[bool] = mapped_column(Boolean)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())


class CaptureSource(Base):
    """Producer aliases survive restarts without becoming new logical turns."""

    __tablename__ = "capture_sources"
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    turn_id: Mapped[str] = mapped_column(Text)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"))


class CaptureIncident(Base):
    """Retain conflicting/late content without silently changing canonical evidence."""

    __tablename__ = "capture_incidents"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    turn_id: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    fingerprint: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    resolved_at: Mapped[datetime | None] = mapped_column()
    seal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transcript_seals.id", ondelete="CASCADE")
    )
    __table_args__ = (
        UniqueConstraint("conversation_id", "fingerprint", name="capture_incident_unique"),
    )


class TranscriptSeal(Base):
    __tablename__ = "transcript_seals"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transcript_seals.id", ondelete="CASCADE")
    )
    records: Mapped[list[dict]] = mapped_column(JSONB)
    provenance: Mapped[dict] = mapped_column(JSONB)
    transcript_hash: Mapped[str] = mapped_column(Text)
    integrity: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    __table_args__ = (
        UniqueConstraint("conversation_id", "version", name="transcript_seal_version_unique"),
        CheckConstraint("version >= 1", name="transcript_seal_version_positive"),
    )


class IncidentResolution(Base):
    __tablename__ = "incident_resolutions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("capture_incidents.id", ondelete="CASCADE"), unique=True
    )
    decision: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text)
    reviewer: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    __table_args__ = (
        CheckConstraint(
            "decision IN ('duplicate','post_cut','omission')", name="incident_resolution_decision"
        ),
    )


class Evaluation(Base):
    __tablename__ = "evaluations"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    hired: Mapped[bool | None] = mapped_column(Boolean)
    score: Mapped[int | None] = mapped_column(Integer)  # 0..100, or not assessable
    strengths: Mapped[list[str]] = mapped_column(JSONB)
    weaknesses: Mapped[list[str]] = mapped_column(JSONB)
    rationale: Mapped[str] = mapped_column(Text)
    # The level judged against, plus the expectations the evaluator discarded
    # for being above it. Forcing the discard into an output field is what
    # keeps above-level expectations out of `weaknesses`.
    seniority_evaluated: Mapped[str | None] = mapped_column(Text)
    calibration_notes: Mapped[list[str] | None] = mapped_column(JSONB)
    ended_by: Mapped[str] = mapped_column(Text)  # same values as ended_reason
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    conversation: Mapped[Conversation] = relationship(back_populates="evaluation")


class EvaluationRequest(Base):
    __tablename__ = "evaluation_requests"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    automatic: Mapped[bool] = mapped_column(Boolean)
    transcript_hash: Mapped[str] = mapped_column(Text)
    seal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transcript_seals.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    deadline_at: Mapped[datetime] = mapped_column()
    __table_args__ = (
        Index(
            "evaluation_automatic_once",
            "conversation_id",
            unique=True,
            postgresql_where=automatic.is_(True),
        ),
        CheckConstraint("attempts BETWEEN 0 AND 3", name="evaluation_request_budget"),
    )


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("evaluation_requests.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column()
    lease_until: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    status: Mapped[str] = mapped_column(Text)
    transcript_hash: Mapped[str] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)


class TurnRun(Base):
    __tablename__ = "turn_runs"
    __table_args__ = (UniqueConstraint("conversation_id", "turn_id", name="turn_runs_unique"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    turn_id: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    decision: Mapped[dict[str, Any]] = mapped_column(JSONB)


class QuestionDelivery(Base):
    __tablename__ = "question_deliveries"
    id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("turn_runs.id", ondelete="CASCADE"), primary_key=True
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    text: Mapped[str] = mapped_column(Text)
    capture_order: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())


class QuestionAttempt(Base):
    __tablename__ = "question_attempts"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("question_deliveries.id", ondelete="CASCADE"), index=True
    )
    explicit: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(Text, default="requested", server_default="requested")
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    owner_epoch: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()


class TurnExecution(Base):
    """A logical turn's durable budget and short exclusive decision lease."""

    __tablename__ = "turn_executions"
    __table_args__ = (
        UniqueConstraint("conversation_id", "turn_id", name="turn_executions_unique"),
        CheckConstraint("invocations_reserved BETWEEN 0 AND 2", name="turn_execution_budget"),
        Index("turn_executions_queue_idx", "conversation_id", "status", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    turn_id: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    status: Mapped[str] = mapped_column(Text, default="queued", server_default="queued")
    invocations_reserved: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # First reservation is atomic with the claim; recovery never moves its deadline.
    decision_deadline_at: Mapped[datetime | None] = mapped_column()
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    producer_owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    producer_epoch: Mapped[int | None] = mapped_column(BigInteger)
    lease_until: Mapped[datetime | None] = mapped_column()
    last_owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    notice_claimed_at: Mapped[datetime | None] = mapped_column()
    waiters: Mapped[dict[str, float]] = mapped_column(JSONB, default=dict, server_default="{}")


class TurnInvocation(Base):
    """Reservations are not evidence that an HTTP request or token was used."""

    __tablename__ = "turn_invocations"
    __table_args__ = (
        UniqueConstraint("execution_id", "ordinal", name="turn_invocations_unique"),
        CheckConstraint("ordinal BETWEEN 1 AND 2", name="turn_invocation_ordinal"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    execution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("turn_executions.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    state_revision: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column()
    outcome: Mapped[str | None] = mapped_column(Text)


class Evidence(Base):
    __tablename__ = "evidence"
    __table_args__ = (
        UniqueConstraint(
            "milestone_id",
            "message_id",
            "message_version",
            "quote",
            name="evidence_unique",
            postgresql_nulls_not_distinct=True,
        ),
        ForeignKeyConstraint(
            ["message_id", "message_version"],
            ["message_versions.message_id", "message_versions.version"],
            ondelete="CASCADE",
            name="evidence_message_version_fk",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    milestone_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("milestones.id", ondelete="CASCADE"))
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"))
    message_version: Mapped[int | None] = mapped_column(Integer)
    quote: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ProcessManifest(Base):
    """Effective process configuration; no source documents or credentials."""

    __tablename__ = "process_manifests"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(Text)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp(), index=True)


class AppSettings(Base):
    """Global agent configuration edited from the Settings screen.

    A singleton row (id = 1, enforced by the check constraint): the app has
    exactly one agent to configure. `voice` holds a catalog KEY from
    interview_agent.voices; the concrete TTS model/voice is resolved when an
    interview is created.
    """

    __tablename__ = "app_settings"
    __table_args__ = (CheckConstraint("id = 1", name="app_settings_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    agent_name: Mapped[str] = mapped_column(
        Text, default=DEFAULT_AGENT_NAME, server_default=DEFAULT_AGENT_NAME
    )
    language: Mapped[str] = mapped_column(
        Text, default=DEFAULT_LANGUAGE, server_default=DEFAULT_LANGUAGE
    )
    voice: Mapped[str] = mapped_column(Text, default=DEFAULT_VOICE, server_default=DEFAULT_VOICE)
    persona: Mapped[str | None] = mapped_column(Text)
    custom_instructions: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class UserSettings(Base):
    """One user's agent configuration: the same columns as AppSettings, whose
    values a user without a row of their own reads."""

    __tablename__ = "user_settings"

    owner_id: Mapped[str] = mapped_column(Text, primary_key=True)
    agent_name: Mapped[str] = mapped_column(
        Text, default=DEFAULT_AGENT_NAME, server_default=DEFAULT_AGENT_NAME
    )
    language: Mapped[str] = mapped_column(
        Text, default=DEFAULT_LANGUAGE, server_default=DEFAULT_LANGUAGE
    )
    voice: Mapped[str] = mapped_column(Text, default=DEFAULT_VOICE, server_default=DEFAULT_VOICE)
    persona: Mapped[str | None] = mapped_column(Text)
    custom_instructions: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class UserInterviewQuota(Base):
    """Interviews a user ever started. Deliberately not tied to conversations:
    the retention purge deletes interviews, never the count of them."""

    __tablename__ = "user_interview_quotas"

    owner_id: Mapped[str] = mapped_column(Text, primary_key=True)
    interviews_used: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class GuestInterviewMonth(Base):
    """Interviews all non-admin users started in one calendar month (UTC)."""

    __tablename__ = "guest_interview_months"

    month: Mapped[date] = mapped_column(Date, primary_key=True)
    interviews_started: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


# --- Engine / session helpers -------------------------------------------------


def create_engine_and_sessionmaker(
    database_url: str,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    # Neon closes idle connections when its compute suspends (after 5 minutes
    # on the Free plan): ping on checkout so a dead pooled one is replaced.
    engine = create_async_engine(database_url, pool_size=5, max_overflow=5, pool_pre_ping=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


# --- Query helpers ------------------------------------------------------------
# Each takes an AsyncSession and commits itself, so callers in the worker's
# event handlers can fire-and-forget them inside one `async with` block.


async def get_conversation(
    session: AsyncSession, conversation_id: uuid.UUID
) -> Conversation | None:
    return await session.get(Conversation, conversation_id)


async def list_conversations(
    session: AsyncSession,
    limit: int,
    offset: int,
    status: str | None = None,
    owner_id: str | None = None,
) -> tuple[list[Conversation], int]:
    """One page of the history, newest first, plus the unpaginated total.
    `owner_id` narrows it to one user's interviews.

    Both relationships are lazy="selectin", so the milestones and the
    evaluation of the whole page load in two extra queries — not one per row.
    """
    filters = [Conversation.status == status] if status else []
    if owner_id is not None:
        filters.append(Conversation.owner_id == owner_id)
    total = await session.scalar(select(func.count()).select_from(Conversation).where(*filters))
    rows = await session.scalars(
        select(Conversation)
        .where(*filters)
        # created_at ties (same-second seeds in tests) would otherwise page
        # non-deterministically; id breaks them.
        .order_by(Conversation.created_at.desc(), Conversation.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows), int(total or 0)


async def get_milestones(session: AsyncSession, conversation_id: uuid.UUID) -> list[Milestone]:
    result = await session.scalars(
        select(Milestone)
        .where(Milestone.conversation_id == conversation_id)
        .order_by(Milestone.position)
    )
    return list(result)


async def get_messages(session: AsyncSession, conversation_id: uuid.UUID) -> list[Message]:
    result = await session.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        # seq is the true turn order; id breaks ties and covers any row
        # without one (the migration backfills seq from id order).
        .order_by(Message.seq, Message.id)
    )
    return list(result)


async def has_messages(session: AsyncSession, conversation_id: uuid.UUID) -> bool:
    """Whether anything was said at all — the cheapest "is there a transcript"."""
    result = await session.scalar(
        select(exists().where(Message.conversation_id == conversation_id))
    )
    return bool(result)


async def insert_message(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    role: str,
    content: str,
    seq: int | None = None,
    *,
    source_id: str | None = None,
    turn_id: str | None = None,
    interrupted: bool = False,
    metrics: dict | None = None,
    commit: bool = True,
) -> Message:
    # Allocate order under the conversation lock; two jobs cannot interleave
    # their local counters after a reconnect. Tests may pin seq explicitly.
    conversation = await session.scalar(
        select(Conversation)
        .where(Conversation.id == conversation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if source_id is not None:
        existing = await session.scalar(
            select(Message).where(
                Message.conversation_id == conversation_id, Message.source_id == source_id
            )
        )
        if existing is not None:
            if existing.version is not None:
                raise ValueError("Versioned messages require durable capture admission")
            if metrics:
                previous_metrics = existing.metrics or {}
                previous = previous_metrics.get("stt_confirmed")
                merged_metrics = {**previous_metrics, **metrics}
                current = merged_metrics.get("stt_confirmed")
                provenance_changed = (
                    ("stt_confirmed" in previous_metrics) != ("stt_confirmed" in merged_metrics)
                    or type(current) is not type(previous)
                    or current != previous
                )
                if provenance_changed and conversation is not None:
                    if conversation.transcript_sealed_at is not None:
                        raise ValueError("Sealed STT eligibility cannot be changed")
                    conversation.state_revision += 1
                existing.metrics = merged_metrics
                if provenance_changed:
                    # JSON dirty checking uses Python equality: True == 1.
                    # Force persistence of a source-type change as well.
                    flag_modified(existing, "metrics")
            if interrupted and not existing.interrupted:
                if conversation is not None and conversation.transcript_sealed_at is not None:
                    raise ValueError("Sealed message interruption cannot be changed")
                existing.interrupted = True
                if conversation is not None:
                    conversation.state_revision += 1
            if commit:
                await session.commit()
            else:
                await session.flush()
            return existing
    if conversation is not None and (
        conversation.transcript_sealed_at is not None
        or (
            conversation.closing_id is not None
            and conversation.status
            not in (
                "planned",
                "interviewing",
                "closing",
            )
        )
    ):
        raise ValueError("The canonical transcript is sealed; new messages are rejected")
    if seq is None:
        highest = await session.scalar(
            select(func.max(Message.seq)).where(Message.conversation_id == conversation_id)
        )
        seq = (highest if highest is not None else -1) + 1
    message = Message(
        conversation_id=conversation_id,
        role=role,
        content=content,
        seq=seq,
        source_id=source_id,
        turn_id=turn_id,
        interrupted=interrupted,
        metrics=metrics,
    )
    session.add(message)
    if conversation is not None:
        conversation.state_revision += 1
    if commit:
        await session.commit()
    else:
        await session.flush()
    return message


async def begin_interview(session: AsyncSession, conversation_id: uuid.UUID) -> str | None:
    """Late dispatches never reopen closing or terminal conversations."""
    conversation = await session.scalar(
        select(Conversation)
        .where(Conversation.id == conversation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if conversation is None:
        return None
    if conversation.status in ("planned", "interviewing"):
        if conversation.status != "interviewing" or conversation.started_at is None:
            conversation.state_revision += 1
        conversation.status = "interviewing"
        if conversation.started_at is None:
            conversation.started_at = await session.scalar(select(func.clock_timestamp()))
        await session.commit()
    return conversation.status


async def count_active_interviews(session: AsyncSession, window_minutes: int) -> int:
    """Conversations in 'interviewing' whose updated_at falls in the window.

    A live interview can never outlast its time cap, so the window (cap plus
    some slack) auto-excludes stale rows left behind by crashed workers."""
    result = await session.scalar(
        select(func.count())
        .select_from(Conversation)
        .where(
            Conversation.status.in_(("interviewing", "closing")),
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
    values: dict[str, Any] = {
        "status": status,
        "state_revision": Conversation.state_revision + 1,
    }
    if ended_reason is not None:
        values["ended_reason"] = ended_reason
    await session.execute(
        update(Conversation).where(Conversation.id == conversation_id).values(**values)
    )
    await session.commit()


# Rows an evaluation can be started from. "evaluated" is included on purpose:
# re-running one upserts the result, and those tokens are spent knowingly.
EVALUATION_CLAIMABLE = ("completed", "evaluation_failed", "evaluated")


async def claim_evaluation(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    stale_after: timedelta,
    *,
    automatic: bool = False,
    recover: bool = False,
    request_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    from interview_agent.interview.evaluation_requests import claim

    return await claim(
        session,
        conversation_id,
        stale_after,
        automatic=automatic,
        recover=recover,
        request_id=request_id,
    )


async def heartbeat_evaluation(
    session: AsyncSession, conversation_id: uuid.UUID, claim_id: uuid.UUID | None = None
) -> bool:
    from interview_agent.interview.evaluation_requests import heartbeat

    return await heartbeat(session, conversation_id, claim_id, timedelta(minutes=2))


async def set_status_if(
    session: AsyncSession, conversation_id: uuid.UUID, expected: str, status: str
) -> bool:
    """`set_status` guarded on the current value: the transition happens only
    if the row is still in `expected`. Lets a run that lost its claim (a
    reclaim after it went stale) leave the newer run's outcome alone."""
    result = await session.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id, Conversation.status == expected)
        .values(status=status)
        .returning(Conversation.id)
    )
    changed = result.scalar_one_or_none() is not None
    await session.commit()
    return changed


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
    *,
    commit: bool = True,
) -> None:
    """Merge one component's token counts into conversations.token_usage.

    Read-modify-write without a lock is safe here: the three writers never
    run concurrently for one conversation — planning precedes the interview,
    and the worker flushes interviewer usage before triggering evaluation.
    """
    conversation = await session.scalar(
        select(Conversation).where(Conversation.id == conversation_id).with_for_update()
    )
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
    if commit:
        await session.commit()


async def get_app_settings(session: AsyncSession) -> AppSettings:
    """The singleton settings row, or an unsaved default instance if it is
    missing (fresh test schema without the migration seed, or a hand-deleted
    row) — callers always get usable values."""
    settings_row = await session.get(AppSettings, 1)
    if settings_row is not None:
        return settings_row
    # Column defaults only apply on INSERT — set them explicitly here.
    return AppSettings(
        id=1,
        agent_name=DEFAULT_AGENT_NAME,
        language=DEFAULT_LANGUAGE,
        voice=DEFAULT_VOICE,
    )


async def upsert_app_settings(session: AsyncSession, values: dict[str, Any]) -> AppSettings:
    """Write the singleton settings row (insert-or-update, race-safe)."""
    await session.execute(
        pg_insert(AppSettings)
        .values(id=1, **values)
        .on_conflict_do_update(index_elements=["id"], set_=values)
    )
    await session.commit()
    return await get_app_settings(session)


async def get_user_settings(session: AsyncSession, owner_id: str) -> UserSettings | AppSettings:
    """The user's own settings row, or the app-wide settings until they save one."""
    settings_row = await session.get(UserSettings, owner_id)
    if settings_row is not None:
        return settings_row
    return await get_app_settings(session)


async def upsert_user_settings(
    session: AsyncSession, owner_id: str, values: dict[str, Any]
) -> UserSettings:
    """Write one user's settings row (insert-or-update, race-safe)."""
    await session.execute(
        pg_insert(UserSettings)
        .values(owner_id=owner_id, **values)
        .on_conflict_do_update(
            index_elements=["owner_id"], set_={**values, "updated_at": func.now()}
        )
    )
    await session.commit()
    return await session.get(UserSettings, owner_id, populate_existing=True)


QuotaOutcome = Literal["ok", "lifetime", "monthly"]


def _current_month():
    """This calendar month in UTC, on the database clock."""
    return func.date_trunc("month", func.timezone("UTC", func.now())).cast(Date)


async def reserve_interview_slot(
    session: AsyncSession, owner_id: str, *, lifetime_limit: int, monthly_limit: int
) -> QuotaOutcome:
    """Count one more interview against the user's lifetime quota and this
    month's shared capacity, both or neither, in one transaction.

    Each counter moves with a conditional UPDATE: its row lock serializes
    concurrent reservations, and READ COMMITTED re-checks the WHERE against the
    row the lock holder committed, so neither limit can be exceeded. Running
    out of monthly capacity rolls the lifetime increment back with it.
    """
    await session.execute(
        pg_insert(UserInterviewQuota)
        .values(owner_id=owner_id)
        .on_conflict_do_nothing(index_elements=["owner_id"])
    )
    reserved = await session.scalar(
        update(UserInterviewQuota)
        .where(
            UserInterviewQuota.owner_id == owner_id,
            UserInterviewQuota.interviews_used < lifetime_limit,
        )
        .values(interviews_used=UserInterviewQuota.interviews_used + 1)
        .returning(UserInterviewQuota.owner_id)
    )
    if reserved is None:
        await session.rollback()
        return "lifetime"
    await session.execute(
        pg_insert(GuestInterviewMonth)
        .values(month=_current_month())
        .on_conflict_do_nothing(index_elements=["month"])
    )
    reserved = await session.scalar(
        update(GuestInterviewMonth)
        .where(
            GuestInterviewMonth.month == _current_month(),
            GuestInterviewMonth.interviews_started < monthly_limit,
        )
        .values(interviews_started=GuestInterviewMonth.interviews_started + 1)
        .returning(GuestInterviewMonth.month)
    )
    if reserved is None:
        await session.rollback()
        return "monthly"
    await session.commit()
    return "ok"


@dataclass(frozen=True)
class QuotaStatus:
    interviews_used: int
    monthly_capacity_available: bool


async def interview_quota_status(
    session: AsyncSession, owner_id: str, *, monthly_limit: int
) -> QuotaStatus:
    """How many interviews the user started, and whether this month's shared
    capacity has room for one more."""
    used = await session.scalar(
        select(UserInterviewQuota.interviews_used).where(UserInterviewQuota.owner_id == owner_id)
    )
    started = await session.scalar(
        select(GuestInterviewMonth.interviews_started).where(
            GuestInterviewMonth.month == _current_month()
        )
    )
    return QuotaStatus(
        interviews_used=int(used or 0),
        monthly_capacity_available=int(started or 0) < monthly_limit,
    )


async def seconds_until_next_month(session: AsyncSession) -> int:
    """Whole seconds until 00:00 UTC on the 1st of next month, on the database clock."""
    seconds = await session.scalar(
        text(
            "SELECT EXTRACT(EPOCH FROM date_trunc('month', timezone('UTC', now())) "
            "+ interval '1 month' - timezone('UTC', now()))"
        )
    )
    return max(1, math.ceil(seconds))


async def delete_conversations_older_than(session: AsyncSession, days: int) -> list[uuid.UUID]:
    """Purge conversations (and, via CASCADE, their milestones, messages and
    evaluations) older than `days`. Returns the deleted ids for logging."""
    cutoff = func.now() - timedelta(days=days)
    ids = list(
        await session.scalars(select(Conversation.id).where(Conversation.created_at < cutoff))
    )
    if ids:
        await session.execute(delete(Conversation).where(Conversation.id.in_(ids)))
        await session.commit()
    return ids


async def expire_manifests(session: AsyncSession, detail_days: int) -> None:
    """Startup manifests are technical detail: dropped after METRICS_DETAIL_DAYS."""
    now = await session.scalar(select(func.clock_timestamp()))
    await session.execute(
        delete(ProcessManifest).where(
            ProcessManifest.created_at < now - timedelta(days=detail_days)
        )
    )
    await session.commit()
