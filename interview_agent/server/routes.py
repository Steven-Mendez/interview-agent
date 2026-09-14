"""Interview API routes.

Flow: POST /interviews (upload + plan) → GET /interviews/{id}/token (join
room, agent dispatched) → interview happens → POST /interviews/{id}/evaluate
(auto-triggered by the worker; claims the row and answers 202 while the
evaluation runs in the background, see server.evaluations) →
GET /interviews/{id} (poll until evaluated / evaluation_failed).

Past interviews are browsable through GET /interviews (paginated history) and
GET /interviews/{id}/transcript, and re-runnable through
POST /interviews/{id}/repeat.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio.to_thread
from fastapi import APIRouter, Form, HTTPException, Query, Request, UploadFile
from langchain_core.callbacks import UsageMetadataCallbackHandler
from livekit import api
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from interview_agent.config import settings
from interview_agent.interview import db, rag
from interview_agent.interview.models import InterviewLength, Seniority
from interview_agent.interview.planner import run_planner
from interview_agent.llm import summarize_usage
from interview_agent.prompts import DEFAULT_SENIORITY, length_for
from interview_agent.server import evaluations
from interview_agent.voices import (
    DEFAULT_AGENT_NAME,
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


def _parse_seniority(value: str) -> Seniority | None:
    """None means "auto": let the planner classify it, once."""
    if value.strip().lower() in ("", "auto"):
        return None
    try:
        return Seniority(value.strip().lower())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"seniority must be 'auto' or one of {[s.value for s in Seniority]}",
        ) from None


def _parse_length(value: str) -> InterviewLength:
    try:
        return InterviewLength((value or "").strip().lower() or "standard")
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"interview_length must be one of {[v.value for v in InterviewLength]}",
        ) from None


def _validate_voice(language: str, voice: str) -> dict[str, Any]:
    """The catalog entry for `voice`, or a 400 explaining what is wrong.

    Shared by the global settings (via SettingsUpdate) and by the
    per-interview override, so both reject the same impossible pairs.
    """
    if language not in SUPPORTED_LANGUAGES:
        raise HTTPException(
            status_code=400, detail=f"language must be one of {SUPPORTED_LANGUAGES}"
        )
    voice_cfg = VOICES.get(voice)
    if voice_cfg is None:
        raise HTTPException(status_code=400, detail=f"unknown voice '{voice}'")
    if voice_cfg["language"] != language:
        raise HTTPException(
            status_code=400,
            detail=f"voice '{voice}' is not available for language '{language}'",
        )
    return voice_cfg


def _default_voice_for(language: str, like: str | None = None) -> str:
    """The catalog voice for `language` when none was chosen: the one with
    the same gender as `like` (the voice being replaced) when there is one,
    else the first listed. `language` must be supported."""
    candidates = [key for key, cfg in VOICES.items() if cfg["language"] == language]
    gender = VOICES.get(like or "", {}).get("gender")
    same_gender = [key for key in candidates if VOICES[key]["gender"] == gender]
    return (same_gender or candidates)[0]


def _resolve_interviewer(
    app_settings: db.AppSettings, overrides: dict[str, str | None] | None
) -> dict[str, Any]:
    """Who conducts THIS interview: the global settings with the per-interview
    overrides applied, voice already resolved to a concrete TTS pair.

    Override convention, so "leave it alone" and "clear it" stay distinct:
    `None` inherits the global value, a string wins. For persona and custom
    instructions an empty string is a real answer — this interview runs with
    none — while for the name, language and voice (which cannot be empty) it
    falls back to the global value too.

    Overriding the language without a voice inherits a voice that may speak
    another language; rather than rejecting the request for a pair the caller
    never chose, the voice falls back to one for the requested language.
    """
    values = overrides or {}

    def _pick(key: str, fallback: str) -> str:
        return (values.get(key) or "").strip() or fallback

    agent_name = _pick("agent_name", app_settings.agent_name)
    language = _pick("language", app_settings.language)
    voice_override = _pick("voice", "")
    voice = voice_override or app_settings.voice
    if (
        not voice_override
        and language in SUPPORTED_LANGUAGES
        and VOICES.get(voice, {}).get("language") != language
    ):
        voice = _default_voice_for(language, like=voice)
    voice_cfg = _validate_voice(language, voice)

    def _pick_optional(key: str, fallback: str | None) -> str | None:
        override = values.get(key)
        return fallback if override is None else (override.strip() or None)

    return {
        # The snapshot the worker reads back; keep these five keys stable.
        "agent_name": agent_name,
        "language": language,
        "voice": voice,
        "tts_model": voice_cfg["tts_model"],
        "tts_voice": voice_cfg["tts_voice"],
        # Stored in their own columns, not in the snapshot.
        "persona": _pick_optional("persona", app_settings.persona),
        "custom_instructions": _pick_optional(
            "custom_instructions", app_settings.custom_instructions
        ),
    }


class InterviewerOverride(BaseModel):
    """The `interviewer` JSON field of POST /interviews. Every key optional:
    absent inherits the global setting, a value (including "") wins."""

    agent_name: str | None = None
    language: str | None = None
    voice: str | None = None
    persona: str | None = None
    custom_instructions: str | None = None


def _parse_interviewer(raw: str | None) -> dict[str, str | None] | None:
    """None (nothing sent) means "use the global settings wholesale"."""
    if raw is None or not raw.strip():
        return None
    try:
        payload = json.loads(raw)
        override = InterviewerOverride.model_validate(payload)
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"interviewer must be a JSON object: {exc}"
        ) from None
    # exclude_unset drops the keys that were not sent. An explicit null does
    # survive the dump, but _resolve_interviewer treats None as "inherit" —
    # so null and absent both inherit the global value; only "" clears.
    return override.model_dump(exclude_unset=True)


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
        # Same rules as the per-interview override, raised as a 422 here
        # because this body is validated by pydantic.
        try:
            _validate_voice(self.language, self.voice)
        except HTTPException as exc:
            raise ValueError(exc.detail) from None
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


def _offer_title(job_offer: str, limit: int = 90) -> str:
    """First non-empty line of the offer — the closest thing to a role title.

    The history list needs a label per row and nothing stores one, so it is
    derived here (leading markdown hashes stripped) instead of asking the LLM
    for a field only the list would read.
    """
    for raw in job_offer.splitlines():
        line = raw.strip().lstrip("#").strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"
    return "Untitled role"


def _reconnect_deadline(conversation: db.Conversation) -> datetime:
    """Until when an "interviewing" row can be rejoined.

    A crash never marks the row completed, so without a bound a candidate
    could rejoin days later — and since the worker charges the elapsed time
    against the cap, a stale resume would greet them and wrap up in the same
    breath. Nothing else updates the row while the interview runs, so
    updated_at is effectively its start; the same window the capacity check
    uses (a live interview can never outlast its cap)."""
    cap = conversation.max_minutes or settings.interview_max_minutes
    return conversation.updated_at + timedelta(minutes=cap + 5)


def _can_reconnect(conversation: db.Conversation) -> bool:
    return datetime.now(UTC) < _reconnect_deadline(conversation)


def _lifecycle_fields(conversation: db.Conversation) -> dict[str, Any]:
    """What the UI needs to know whether a row can be (re)joined, without
    re-deriving the reconnect window from a cap it may not have (legacy rows
    fall back to server config): `can_start` for planned rows and for
    interviewing ones still inside the window, and the window's end."""
    if conversation.status == "interviewing":
        deadline = _reconnect_deadline(conversation)
        return {
            "can_start": datetime.now(UTC) < deadline,
            "reconnect_until": deadline.isoformat(),
        }
    return {"can_start": conversation.status == "planned", "reconnect_until": None}


def _serialize(conversation: db.Conversation) -> dict[str, Any]:
    evaluation = conversation.evaluation
    return {
        "id": str(conversation.id),
        "created_at": conversation.created_at.isoformat(),
        "updated_at": conversation.updated_at.isoformat(),
        "status": conversation.status,
        "ended_reason": conversation.ended_reason,
        **_lifecycle_fields(conversation),
        "title": _offer_title(conversation.job_offer),
        "job_offer": conversation.job_offer,
        "resume_filename": conversation.resume_filename,
        # Root of the re-run chain, NULL on a first attempt.
        "repeat_of_id": (str(conversation.repeat_of_id) if conversation.repeat_of_id else None),
        "plan": conversation.plan,
        "seniority": conversation.seniority,
        "seniority_source": conversation.seniority_source,
        "seniority_evidence": conversation.seniority_evidence,
        "interview_length": conversation.interview_length,
        "max_minutes": conversation.max_minutes,
        # Who conducted it, as snapshotted at creation. NULL on legacy rows.
        "interviewer": (
            {
                key: conversation.agent_settings.get(key)
                for key in ("agent_name", "language", "voice")
            }
            if conversation.agent_settings
            else None
        ),
        "milestones": [
            {
                "id": str(m.id),
                "position": m.position,
                "title": m.title,
                "description": m.description,
                "expected_evidence": m.expected_evidence,
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
                "seniority_evaluated": evaluation.seniority_evaluated,
                "calibration_notes": evaluation.calibration_notes or [],
                "ended_by": evaluation.ended_by,
            }
            if evaluation
            else None
        ),
        "token_usage": conversation.token_usage,
    }


def _serialize_summary(conversation: db.Conversation) -> dict[str, Any]:
    """One history row: enough to render the list, without the transcript, the
    plan, the resume or the full evaluation prose."""
    evaluation = conversation.evaluation
    milestones = conversation.milestones
    return {
        "id": str(conversation.id),
        "created_at": conversation.created_at.isoformat(),
        "updated_at": conversation.updated_at.isoformat(),
        "status": conversation.status,
        "ended_reason": conversation.ended_reason,
        **_lifecycle_fields(conversation),
        "title": _offer_title(conversation.job_offer),
        "resume_filename": conversation.resume_filename,
        "seniority": conversation.seniority,
        "seniority_source": conversation.seniority_source,
        "interview_length": conversation.interview_length,
        "max_minutes": conversation.max_minutes,
        "repeat_of_id": (str(conversation.repeat_of_id) if conversation.repeat_of_id else None),
        "milestones_total": len(milestones),
        "milestones_completed": sum(1 for m in milestones if m.completed),
        "evaluation": (
            {"hired": evaluation.hired, "score": evaluation.score} if evaluation else None
        ),
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
    # Depth axis. "auto" (the default) means the planner classifies the role
    # ONCE from the offer and the resume; anything else pins it outright.
    seniority: str = Form("auto"),
    # Volume axis, independent of the level: milestone count and minutes.
    interview_length: str = Form("standard"),
    # Who conducts it, as a JSON object: {"agent_name", "language", "voice",
    # "persona", "custom_instructions"}, every key optional. JSON rather than
    # five sibling form fields because an EMPTY multipart field arrives
    # indistinguishable from an absent one — and here the difference matters:
    # omitted inherits the global setting, "" runs this interview without one.
    interviewer: str | None = Form(None),
):
    requested_seniority = _parse_seniority(seniority)
    length = _parse_length(interview_length)
    interviewer_overrides = _parse_interviewer(interviewer)
    if not (resume.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="resume must be a PDF file")
    if not job_offer.strip():
        raise HTTPException(status_code=400, detail="job_offer must not be empty")

    # Read one byte past the cap: exactly-at-cap passes, anything larger 413s.
    data = await resume.read(_MAX_RESUME_BYTES + 1)
    if len(data) > _MAX_RESUME_BYTES:
        logger.warning("resume rejected: %s exceeds 10 MB", resume.filename)
        raise HTTPException(status_code=413, detail="Resume PDF exceeds the 10 MB limit")
    logger.info("creating interview", extra={"resume": resume.filename, "bytes": len(data)})
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

    return await _plan_and_persist(
        request,
        job_offer=job_offer,
        resume_markdown=resume_markdown,
        resume_filename=resume.filename,
        requested_seniority=requested_seniority,
        length=length,
        interviewer_overrides=interviewer_overrides,
    )


async def _plan_and_persist(
    request: Request,
    *,
    job_offer: str,
    resume_markdown: str,
    resume_filename: str | None,
    requested_seniority: Seniority | None,
    length: InterviewLength,
    repeat_of_id: uuid.UUID | None = None,
    pinned_source: str | None = None,
    pinned_evidence: str | None = None,
    interviewer_overrides: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """Index the resume, run the planner and store the planned interview.

    Shared by POST /interviews (fresh upload) and POST /interviews/{id}/repeat
    (same resume text and offer, replanned) — the only difference between the
    two is where the inputs come from, so everything after them lives here.

    `pinned_source` / `pinned_evidence` only apply when `requested_seniority`
    is given: a re-run carries the ORIGINAL provenance forward ("auto" stays
    "auto") instead of masquerading as a level the user just picked.

    `interviewer_overrides` follows the convention in `_resolve_interviewer`:
    None inherits the global settings, a value wins for this interview only.
    """
    conversation_id = uuid.uuid4()
    # This interview's own time cap: the requested length's minutes, clamped
    # by the global setting. Stored on the row AND handed to the planner, so
    # a "deep" request under a 15-minute cap is planned for 15 minutes
    # instead of being cut off mid-plan (`interview_length` stays "deep").
    max_minutes = min(length_for(length)["minutes"], settings.interview_max_minutes)
    async with _sessionmaker(request)() as session:
        # Snapshot the interviewer NOW: this interview keeps this
        # persona/language/voice even if the settings change later.
        app_settings = await db.get_app_settings(session)
        interviewer = _resolve_interviewer(app_settings, interviewer_overrides)
        agent_settings = {
            key: interviewer[key]
            for key in ("agent_name", "language", "voice", "tts_model", "tts_voice")
        }
        session.add(
            db.Conversation(
                id=conversation_id,
                status="created",
                job_offer=job_offer,
                resume_markdown=resume_markdown,
                resume_filename=resume_filename,
                repeat_of_id=repeat_of_id,
                persona=interviewer["persona"],
                custom_instructions=interviewer["custom_instructions"],
                agent_settings=agent_settings,
                # Provisional: when the planner classifies, the detected value
                # overwrites this a few lines below.
                seniority=(requested_seniority or DEFAULT_SENIORITY).value,
                seniority_source=(
                    (pinned_source or "explicit") if requested_seniority else "fallback"
                ),
                seniority_evidence=pinned_evidence if requested_seniority else None,
                interview_length=length.value,
                max_minutes=max_minutes,
            )
        )
        await session.commit()

        planner_usage = UsageMetadataCallbackHandler()
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
            plan = await run_planner(
                settings,
                resume_markdown,
                job_offer,
                language=interviewer["language"],
                agent_name=interviewer["agent_name"],
                seniority=requested_seniority,
                interview_length=length,
                max_minutes=max_minutes,
                persona=interviewer["persona"],
                custom_instructions=interviewer["custom_instructions"],
                usage_callback=planner_usage,
            )
        except Exception as exc:
            logger.exception("planning failed for %s", conversation_id)
            await db.set_status(session, conversation_id, "error")
            # The attempts inside with_retry were real spend even though no
            # plan came out of them.
            await evaluations.record_spent_usage(session, conversation_id, "planner", planner_usage)
            raise HTTPException(status_code=500, detail=f"Planning failed: {exc}") from exc

        await db.add_token_usage(
            session, conversation_id, "planner", summarize_usage(planner_usage.usage_metadata)
        )

        logger.info(
            "interview planned",
            extra={
                "conversation": str(conversation_id),
                "language": interviewer["language"],
                "seniority": (
                    requested_seniority or plan.detected_seniority or DEFAULT_SENIORITY
                ).value,
                "milestones": len(plan.milestones),
            },
        )
        conversation = await _load_or_404(session, conversation_id)
        # The language is injected server-side (the planner no longer decides
        # it), keeping the plan JSON shape every downstream reader expects.
        # The seniority fields stay OUT of the plan JSON on purpose: they live
        # in columns, as the single source of truth for every later stage.
        conversation.plan = {
            **plan.model_dump(exclude={"milestones", "detected_seniority", "seniority_evidence"}),
            "language": interviewer["language"],
        }
        # Resolution, once and for all: explicit beats detected beats fallback.
        if requested_seniority is None and plan.detected_seniority is not None:
            conversation.seniority = plan.detected_seniority.value
            conversation.seniority_source = "detected"
            conversation.seniority_evidence = plan.seniority_evidence
        conversation.status = "planned"
        for i, spec in enumerate(plan.milestones):
            session.add(
                db.Milestone(
                    id=uuid.uuid4(),
                    conversation_id=conversation_id,
                    position=i,
                    title=spec.title,
                    description=spec.description,
                    expected_evidence=spec.expected_evidence,
                )
            )
        await session.commit()

        await session.refresh(conversation)
        return _serialize(conversation)


_HISTORY_STATUSES = (
    "created",
    "planned",
    "interviewing",
    "completed",
    "evaluating",
    "evaluated",
    "evaluation_failed",
    "error",
)


@router.get("/interviews")
async def list_interviews(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None),
):
    """Paginated history, newest first. `status` narrows it to one state;
    empty (a form's "all" option) is the same as absent."""
    status = status or None
    if status is not None and status not in _HISTORY_STATUSES:
        raise HTTPException(
            status_code=400, detail=f"status must be one of {list(_HISTORY_STATUSES)}"
        )
    async with _sessionmaker(request)() as session:
        conversations, total = await db.list_conversations(
            session, limit=limit, offset=offset, status=status
        )
        return {
            "items": [_serialize_summary(c) for c in conversations],
            "total": total,
            "limit": limit,
            "offset": offset,
        }


@router.get("/interviews/{interview_id}")
async def get_interview(request: Request, interview_id: uuid.UUID):
    async with _sessionmaker(request)() as session:
        conversation = await _load_or_404(session, interview_id)
        return _serialize(conversation)


@router.get("/interviews/{interview_id}/transcript")
async def get_transcript(request: Request, interview_id: uuid.UUID):
    """The stored turns of a past interview, in turn order.

    Kept off /interviews/{id} on purpose: that one is polled every 2s while an
    interview runs, and the transcript grows without bound.
    """
    async with _sessionmaker(request)() as session:
        await _load_or_404(session, interview_id)
        messages = await db.get_messages(session, interview_id)
        return {
            "messages": [
                {
                    "role": m.role,
                    "content": m.content,
                    "created_at": m.created_at.isoformat(),
                }
                for m in messages
            ]
        }


class RepeatRequest(BaseModel):
    """POST /interviews/{id}/repeat body; both fields inherit when omitted.

    `seniority: "auto"` is not "inherit" — it asks the planner to classify the
    role again from scratch, exactly like it means on the upload form.
    """

    seniority: str | None = None
    interview_length: str | None = None


@router.post("/interviews/{interview_id}/repeat")
async def repeat_interview(
    request: Request, interview_id: uuid.UUID, body: RepeatRequest | None = None
):
    """Run the same role again: a NEW interview off the stored resume and offer.

    The resume PDF is long gone (only its markdown is kept) and so are the
    Qdrant chunks, which are purged after the evaluation — so this re-indexes
    the stored markdown and re-plans. Re-planning rather than cloning the old
    milestones is the point of a practice re-run: the same role and the same
    bar, different questions. The original is never touched.
    """
    body = body or RepeatRequest()
    async with _sessionmaker(request)() as session:
        source = await _load_or_404(session, interview_id)
        job_offer = source.job_offer
        resume_markdown = source.resume_markdown
        resume_filename = source.resume_filename
        # Inherit unless the caller overrides. Inheriting carries the level's
        # provenance with it, so a re-run of an auto-detected interview still
        # reads as "auto" instead of claiming the user picked the level.
        if body.seniority is None:
            requested_seniority = _parse_seniority(source.seniority)
            pinned_source = source.seniority_source
            pinned_evidence = source.seniority_evidence
        else:
            requested_seniority = _parse_seniority(body.seniority)
            pinned_source = None
            pinned_evidence = None
        length = _parse_length(body.interview_length or source.interview_length)
        # A root repeat_of_id (not a chain) keeps every attempt on this role
        # under one id, however many times it is repeated.
        root_id = source.repeat_of_id or source.id
        # Same interviewer as the original: repeating a run must not silently
        # swap the voice or the persona because the global settings moved on.
        # "" (not None) for persona/instructions so a source that ran WITHOUT
        # one does not inherit whatever the settings hold now. Legacy rows
        # have no snapshot — those fall back to the global settings.
        snapshot = source.agent_settings or {}
        interviewer_overrides = {
            "agent_name": snapshot.get("agent_name"),
            "language": snapshot.get("language"),
            "voice": snapshot.get("voice"),
            "persona": source.persona or "",
            "custom_instructions": source.custom_instructions or "",
        }

    logger.info(
        "repeating interview",
        extra={"conversation": str(interview_id), "root": str(root_id)},
    )
    return await _plan_and_persist(
        request,
        job_offer=job_offer,
        resume_markdown=resume_markdown,
        resume_filename=resume_filename,
        requested_seniority=requested_seniority,
        length=length,
        repeat_of_id=root_id,
        pinned_source=pinned_source,
        pinned_evidence=pinned_evidence,
        interviewer_overrides=interviewer_overrides,
    )


@router.get("/interviews/{interview_id}/token")
async def get_token(request: Request, interview_id: uuid.UUID):
    async with _sessionmaker(request)() as session:
        conversation = await _load_or_404(session, interview_id)
        if conversation.status not in ("planned", "interviewing"):
            raise HTTPException(
                status_code=409,
                detail=f"Interview is '{conversation.status}', expected 'planned'",
            )
        # Reconnect window (see _reconnect_deadline): an "interviewing" row
        # past it is an orphan of a crashed worker, not a live interview.
        if conversation.status == "interviewing" and not _can_reconnect(conversation):
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


@router.post("/interviews/{interview_id}/evaluate", status_code=202)
async def evaluate_interview(request: Request, interview_id: uuid.UUID):
    """Start the evaluation in the background; 202 with the row as it stands.

    Idempotent: the row is CLAIMED with one atomic UPDATE into "evaluating"
    (db.claim_evaluation), so the worker's auto-trigger and a Retry from the
    browser racing here start exactly one run — the loser just gets the row
    back. A run whose process died (no heartbeat for evaluations.STALE_AFTER)
    is claimed again, which is what the UI's Retry does once its own clock,
    anchored on the same heartbeat, runs out. Poll GET /interviews/{id} for
    the outcome.
    """
    async with _sessionmaker(request)() as session:
        conversation = await _load_or_404(session, interview_id)
        if conversation.status == "interviewing":
            # A live interview must never be evaluated: it would score half a
            # transcript AND purge the resume chunks from Qdrant (see the
            # runner), blinding search_resume for the rest of it. But a row
            # past its reconnect window is not live — a crash never marks it
            # completed — and the transcript it left is still worth scoring.
            if _can_reconnect(conversation):
                raise HTTPException(status_code=409, detail="Interview is still in progress")
            logger.info(
                "closing an orphaned interview for evaluation",
                extra={
                    "conversation": str(interview_id),
                    "updated_at": conversation.updated_at.isoformat(),
                },
            )
            await db.set_status(session, interview_id, "completed", "connection_lost")
        if not await db.has_messages(session, interview_id):
            raise HTTPException(status_code=409, detail="No transcript to evaluate yet")
        if await db.claim_evaluation(session, interview_id, evaluations.STALE_AFTER):
            request.app.state.evaluations.start(interview_id)
            logger.info("evaluation scheduled", extra={"conversation": str(interview_id)})
        else:
            # Not claimable: a run is already on it, and still heartbeating.
            logger.info("evaluation already running", extra={"conversation": str(interview_id)})
        await session.refresh(conversation)
        return _serialize(conversation)
