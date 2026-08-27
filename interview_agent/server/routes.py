"""Interview API routes.

Flow: POST /interviews (upload + plan) → GET /interviews/{id}/token (join
room, agent dispatched) → interview happens → POST /interviews/{id}/evaluate
(auto-triggered by the worker, re-invocable) → GET /interviews/{id} (poll).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio.to_thread
from fastapi import APIRouter, Form, HTTPException, Request, UploadFile
from langchain_core.callbacks import UsageMetadataCallbackHandler
from livekit import api
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from interview_agent.config import settings
from interview_agent.interview import db, rag
from interview_agent.interview.evaluator import run_evaluator
from interview_agent.interview.planner import run_planner
from interview_agent.llm import summarize_usage
from interview_agent.voices import (
    DEFAULT_AGENT_NAME,
    DEFAULT_VOICE,
    SUPPORTED_LANGUAGES,
    VOICES,
    voices_by_language,
)

logger = logging.getLogger("interview_agent.server")

router = APIRouter()

# `resume.read()` buffers the upload in memory; cap it so a huge (or hostile)
# file cannot exhaust the process. Real resumes are well under this.
_MAX_RESUME_BYTES = 10 * 1024 * 1024  # 10 MB


def _sessionmaker(request: Request):
    return request.app.state.sessionmaker


@router.get("/healthz")
async def healthz(request: Request):
    """Liveness + DB reachability, for deploys and uptime checks."""
    try:
        async with _sessionmaker(request)() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("healthz failed: %s", exc)
        raise HTTPException(status_code=503, detail="database unreachable") from exc
    return {"status": "ok"}


# ---- Settings ---------------------------------------------------------------


class SettingsUpdate(BaseModel):
    """PUT /settings body: the global agent configuration."""

    agent_name: str = DEFAULT_AGENT_NAME
    language: str
    voice: str
    persona: str | None = None
    custom_instructions: str | None = None

    @field_validator("agent_name")
    @classmethod
    def _default_name(cls, value: str) -> str:
        return value.strip() or DEFAULT_AGENT_NAME

    @field_validator("persona", "custom_instructions")
    @classmethod
    def _empty_to_none(cls, value: str | None) -> str | None:
        # Empty form fields arrive as "" — normalize to NULL, same convention
        # as the old upload form.
        return (value or "").strip() or None

    @field_validator("language")
    @classmethod
    def _known_language(cls, value: str) -> str:
        if value not in SUPPORTED_LANGUAGES:
            raise ValueError(f"language must be one of {SUPPORTED_LANGUAGES}")
        return value

    @model_validator(mode="after")
    def _voice_matches_language(self) -> SettingsUpdate:
        voice = VOICES.get(self.voice)
        if voice is None:
            raise ValueError(f"unknown voice '{self.voice}'")
        if voice["language"] != self.language:
            raise ValueError(
                f"voice '{self.voice}' is not available for language '{self.language}'"
            )
        return self


def _serialize_settings(app_settings: db.AppSettings) -> dict[str, Any]:
    return {
        "agent_name": app_settings.agent_name,
        "language": app_settings.language,
        "voice": app_settings.voice,
        "persona": app_settings.persona,
        "custom_instructions": app_settings.custom_instructions,
        # The catalog rides along so one fetch renders the whole screen.
        "voices": voices_by_language(),
    }


@router.get("/settings")
async def get_settings(request: Request):
    async with _sessionmaker(request)() as session:
        app_settings = await db.get_app_settings(session)
        return _serialize_settings(app_settings)


@router.put("/settings")
async def update_settings(request: Request, body: SettingsUpdate):
    async with _sessionmaker(request)() as session:
        app_settings = await db.upsert_app_settings(session, body.model_dump())
        logger.info(
            "settings updated",
            extra={"language": app_settings.language, "voice": app_settings.voice},
        )
        return _serialize_settings(app_settings)


# ---- Interviews ---------------------------------------------------------------


def _serialize(conversation: db.Conversation) -> dict[str, Any]:
    evaluation = conversation.evaluation
    return {
        "id": str(conversation.id),
        "status": conversation.status,
        "ended_reason": conversation.ended_reason,
        "plan": conversation.plan,
        "milestones": [
            {
                "id": str(m.id),
                "position": m.position,
                "title": m.title,
                "description": m.description,
                "completed": m.completed,
                "notes": m.notes,
            }
            for m in conversation.milestones
        ],
        "evaluation": (
            {
                "hired": evaluation.hired,
                "score": evaluation.score,
                "strengths": evaluation.strengths,
                "weaknesses": evaluation.weaknesses,
                "rationale": evaluation.rationale,
                "ended_by": evaluation.ended_by,
            }
            if evaluation
            else None
        ),
        "token_usage": conversation.token_usage,
    }


async def _load_or_404(session: AsyncSession, interview_id: uuid.UUID) -> db.Conversation:
    conversation = await db.get_conversation(session, interview_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Interview not found")
    return conversation


@router.post("/interviews")
async def create_interview(
    request: Request,
    resume: UploadFile,
    job_offer: str = Form(),
):
    if not (resume.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="resume must be a PDF file")
    if not job_offer.strip():
        raise HTTPException(status_code=400, detail="job_offer must not be empty")

    # Read one byte past the cap: exactly-at-cap passes, anything larger 413s.
    data = await resume.read(_MAX_RESUME_BYTES + 1)
    if len(data) > _MAX_RESUME_BYTES:
        logger.warning("resume rejected: %s exceeds 10 MB", resume.filename)
        raise HTTPException(status_code=413, detail="Resume PDF exceeds the 10 MB limit")
    logger.info(
        "creating interview", extra={"resume": resume.filename, "bytes": len(data)}
    )
    try:
        # In a worker thread: the conversion is CPU-bound pure Python and
        # would otherwise stall every other request on this event loop.
        resume_markdown = await anyio.to_thread.run_sync(
            rag.pdf_to_markdown, data, resume.filename or "resume.pdf"
        )
    except Exception as exc:  # markitdown raises converter-specific errors
        raise HTTPException(status_code=400, detail=f"Could not read PDF: {exc}") from exc
    if not resume_markdown.strip():
        raise HTTPException(status_code=400, detail="PDF contained no extractable text")

    conversation_id = uuid.uuid4()
    async with _sessionmaker(request)() as session:
        # Snapshot the global agent settings NOW: the interview keeps this
        # persona/language/voice even if the settings change later.
        app_settings = await db.get_app_settings(session)
        voice_cfg = VOICES.get(app_settings.voice, VOICES[DEFAULT_VOICE])
        agent_settings = {
            "agent_name": app_settings.agent_name,
            "language": app_settings.language,
            "voice": app_settings.voice,
            "tts_model": voice_cfg["tts_model"],
            "tts_voice": voice_cfg["tts_voice"],
        }
        session.add(
            db.Conversation(
                id=conversation_id,
                status="created",
                job_offer=job_offer,
                resume_markdown=resume_markdown,
                resume_filename=resume.filename,
                persona=app_settings.persona,
                custom_instructions=app_settings.custom_instructions,
                agent_settings=agent_settings,
            )
        )
        await session.commit()

        try:
            n_chunks = await rag.index_resume(
                request.app.state.qdrant,
                request.app.state.embeddings,
                settings,
                conversation_id,
                resume_markdown,
            )
            logger.info(
                "resume indexed",
                extra={"conversation": str(conversation_id), "chunks": n_chunks},
            )
            planner_usage = UsageMetadataCallbackHandler()
            plan = await run_planner(
                settings,
                resume_markdown,
                job_offer,
                language=app_settings.language,
                agent_name=app_settings.agent_name,
                persona=app_settings.persona,
                custom_instructions=app_settings.custom_instructions,
                usage_callback=planner_usage,
            )
        except Exception as exc:
            logger.exception("planning failed for %s", conversation_id)
            await db.set_status(session, conversation_id, "error")
            raise HTTPException(status_code=500, detail=f"Planning failed: {exc}") from exc

        await db.add_token_usage(
            session, conversation_id, "planner", summarize_usage(planner_usage.usage_metadata)
        )

        logger.info(
            "interview planned",
            extra={
                "conversation": str(conversation_id),
                "language": app_settings.language,
                "milestones": len(plan.milestones),
            },
        )
        conversation = await _load_or_404(session, conversation_id)
        # The language is injected server-side (the planner no longer decides
        # it), keeping the plan JSON shape every downstream reader expects.
        conversation.plan = {
            **plan.model_dump(exclude={"milestones"}),
            "language": app_settings.language,
        }
        conversation.status = "planned"
        for i, spec in enumerate(plan.milestones):
            session.add(
                db.Milestone(
                    id=uuid.uuid4(),
                    conversation_id=conversation_id,
                    position=i,
                    title=spec.title,
                    description=spec.description,
                )
            )
        await session.commit()

        await session.refresh(conversation)
        return _serialize(conversation)


@router.get("/interviews/{interview_id}")
async def get_interview(request: Request, interview_id: uuid.UUID):
    async with _sessionmaker(request)() as session:
        conversation = await _load_or_404(session, interview_id)
        return _serialize(conversation)


@router.get("/interviews/{interview_id}/token")
async def get_token(request: Request, interview_id: uuid.UUID):
    async with _sessionmaker(request)() as session:
        conversation = await _load_or_404(session, interview_id)
        if conversation.status not in ("planned", "interviewing"):
            raise HTTPException(
                status_code=409,
                detail=f"Interview is '{conversation.status}', expected 'planned'",
            )
        # Reconnect window: an "interviewing" row outlives its worker (a crash
        # never marks it completed), so without a bound a candidate could
        # rejoin days later. The worker charges the elapsed time against the
        # cap, so a stale resume would greet them and wrap up in the same
        # breath. Same window the capacity check uses — a live interview can
        # never outlast its time cap, and nothing else updates the row while
        # it runs, so updated_at is effectively the interview's start.
        if conversation.status == "interviewing":
            window = timedelta(minutes=settings.interview_max_minutes + 5)
            if conversation.updated_at < datetime.now(UTC) - window:
                logger.info(
                    "refusing stale reconnect",
                    extra={
                        "conversation": str(interview_id),
                        "updated_at": conversation.updated_at.isoformat(),
                    },
                )
                raise HTTPException(
                    status_code=409, detail="Interview is 'expired', expected 'planned'"
                )
        # Capacity check (soft cap): only for NEW interviews — an already
        # "interviewing" conversation is a reconnect of a counted session.
        # Soft because a token issued now only counts once the worker marks
        # the row "interviewing"; two simultaneous joins can exceed the cap
        # by one, which is acceptable for cost protection.
        if conversation.status == "planned":
            window = settings.interview_max_minutes + 5
            active = await db.count_active_interviews(session, window)
            if active >= settings.max_concurrent_interviews:
                logger.warning(
                    "capacity reached: %s active interviews, rejecting %s",
                    active,
                    interview_id,
                )
                raise HTTPException(
                    status_code=429,
                    detail="Too many interviews in progress. Try again in a few minutes.",
                    headers={"Retry-After": "120"},
                )

    room = f"interview-{interview_id}"
    token = (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity("candidate")
        .with_grants(api.VideoGrants(room_join=True, room=room))
        # Explicit dispatch: the interviewer agent joins this room when the
        # browser creates it, carrying the conversation id as job metadata.
        .with_room_config(
            api.RoomConfiguration(
                agents=[
                    api.RoomAgentDispatch(
                        agent_name=settings.livekit_agent_name,
                        metadata=json.dumps({"conversation_id": str(interview_id)}),
                    )
                ]
            )
        )
        .to_jwt()
    )
    logger.info("token issued", extra={"conversation": str(interview_id), "room": room})
    return {"server_url": settings.livekit_url, "room": room, "token": token}


@router.post("/interviews/{interview_id}/evaluate")
async def evaluate_interview(request: Request, interview_id: uuid.UUID):
    sessionmaker = _sessionmaker(request)

    # Session 1 (short): load everything the evaluator needs, then release
    # the connection — the LLM call below can take minutes, and holding a
    # transaction open across it is pure waste.
    async with sessionmaker() as session:
        conversation = await _load_or_404(session, interview_id)
        # A live interview must never be evaluated. The worker only POSTs here
        # after marking the row "completed", so this only rejects an early
        # retry from the browser, or a crashed job's evaluation firing while a
        # reconnected job is still talking — which would score a half
        # transcript AND purge the resume chunks from Qdrant (see the cleanup
        # at the end of this handler), silently blinding search_resume for the
        # rest of the live interview.
        if conversation.status == "interviewing":
            raise HTTPException(
                status_code=409, detail="Interview is still in progress"
            )
        messages = await db.get_messages(session, interview_id)
        if not messages:
            raise HTTPException(status_code=409, detail="No transcript to evaluate yet")
        milestones = await db.get_milestones(session, interview_id)
        resume_markdown = conversation.resume_markdown
        job_offer = conversation.job_offer
        plan = conversation.plan or {}
        ended_reason = conversation.ended_reason or "unknown"
        custom_instructions = conversation.custom_instructions

    logger.info(
        "evaluating interview",
        extra={"conversation": str(interview_id), "messages": len(messages)},
    )
    evaluator_usage = UsageMetadataCallbackHandler()
    try:
        result = await run_evaluator(
            settings,
            resume_markdown=resume_markdown,
            job_offer=job_offer,
            plan=plan,
            milestones=[
                {
                    "title": m.title,
                    "description": m.description,
                    "completed": m.completed,
                    "notes": m.notes,
                }
                for m in milestones
            ],
            transcript=[(m.role, m.content) for m in messages],
            ended_reason=ended_reason,
            custom_instructions=custom_instructions,
            usage_callback=evaluator_usage,
        )
    except Exception as exc:
        # Surface the failure: the frontend polls status and offers a retry
        # (this endpoint is re-invocable) instead of spinning forever.
        logger.exception("evaluation failed for %s", interview_id)
        async with sessionmaker() as session:
            await db.set_status(session, interview_id, "evaluation_failed")
        raise HTTPException(status_code=502, detail=f"Evaluation failed: {exc}") from exc

    # Session 2 (write): upsert instead of delete+insert — two overlapping
    # invocations (worker auto-trigger + manual retry) must not race into a
    # duplicate-key 500; last commit wins and the result stays consistent.
    values = {
        "hired": result.hired,
        "score": result.score,
        "strengths": result.strengths,
        "weaknesses": result.weaknesses,
        "rationale": result.rationale,
        "ended_by": ended_reason,
    }
    async with sessionmaker() as session:
        await session.execute(
            pg_insert(db.Evaluation)
            .values(conversation_id=interview_id, **values)
            .on_conflict_do_update(index_elements=["conversation_id"], set_=values)
        )
        conversation = await _load_or_404(session, interview_id)
        conversation.status = "evaluated"
        await session.commit()
        # Re-evaluations accumulate on purpose: those tokens were spent.
        await db.add_token_usage(
            session, interview_id, "evaluator", summarize_usage(evaluator_usage.usage_metadata)
        )
        await session.refresh(conversation)
        serialized = _serialize(conversation)

    logger.info(
        "interview evaluated",
        extra={
            "conversation": str(interview_id),
            "hired": result.hired,
            "score": result.score,
        },
    )

    # The resume chunks only exist for the interviewer's search_resume; the
    # evaluation is done, so drop them (PII). Best-effort: the purge job
    # sweeps anything missed here.
    try:
        await rag.delete_resume_points(
            request.app.state.qdrant, settings, [interview_id]
        )
    except Exception:
        logger.exception("failed to delete resume points for %s", interview_id)

    return serialized
