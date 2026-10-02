"""Interview API routes.

Flow: POST /interviews (upload + plan) → GET /interviews/{id}/token (join
room, agent dispatched) → interview happens → POST /interviews/{id}/evaluate
(auto-triggered by the worker; claims the row and answers 202 while the
evaluation runs in the background, see server.evaluations) →
GET /interviews/{id} (poll until evaluated / evaluation_failed).

Past interviews are browsable through GET /interviews (paginated history) and
GET /interviews/{id}/transcript, and re-runnable through
POST /interviews/{id}/repeat.

GET /me records who signed in (the user's profile); admins list everyone
through GET /admin/users.

Every interview belongs to the user who created it (server.auth): anyone
else, admins included, gets a 404. The closing routes are the exception: the
browser authenticates them with its LiveKit participant token, after the
interview. Creating or repeating an interview spends one of the user's
lifetime interviews and one of the month's shared ones; admins spend neither.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Annotated, Any

import anyio.to_thread
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, UploadFile
from langchain_core.callbacks import UsageMetadataCallbackHandler
from livekit import api
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from interview_agent import otel_metrics
from interview_agent.closing import FAREWELLS
from interview_agent.config import settings
from interview_agent.interview import db
from interview_agent.interview import resume as resume_ingestion
from interview_agent.interview.context import validate_source_documents
from interview_agent.interview.models import InterviewLength, Seniority
from interview_agent.interview.planner import run_planner
from interview_agent.interview.seals import result_invalid, seal_invalid
from interview_agent.interview.workers import reconnect_deadline
from interview_agent.llm import summarize_usage
from interview_agent.observability import (
    LLMObserver,
    Telemetry,
    content_hash,
    execution_config,
)
from interview_agent.playback import PlaybackAck, acknowledge_playback, closing_state
from interview_agent.prompts import DEFAULT_SENIORITY, fit_length, followup_budget, length_for
from interview_agent.server import evaluations
from interview_agent.server.account_metrics import publish_account_metrics
from interview_agent.server.auth import (
    CurrentUser,
    User,
    auth_provider,
    current_user,
    internal_caller,
    internal_caller_or_user,
    is_admin,
    require_admin,
)
from interview_agent.server.reconciliation import LifecycleSweeper, reconcile_interview
from interview_agent.server.retention import purge_expired
from interview_agent.voices import (
    DEFAULT_AGENT_NAME,
    SUPPORTED_LANGUAGES,
    VOICES,
    resolve_voice,
    voices_by_language,
)

logger = logging.getLogger("interview_agent.server")

router = APIRouter()


def _verify_playback_participant(request: Request, interview_id: uuid.UUID) -> None:
    authorization = request.headers.get("authorization", "").split()
    if len(authorization) != 2 or authorization[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="A participant token is required")
    try:
        claims = api.TokenVerifier(
            settings.livekit_api_key, settings.livekit_api_secret, leeway=timedelta(0)
        ).verify(authorization[1])
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid participant token") from exc
    if (
        claims.identity != "candidate"
        or not claims.video.room_join
        or claims.video.room != f"interview-{interview_id}"
    ):
        raise HTTPException(status_code=403, detail="Participant does not belong to this interview")


@router.post("/interviews/{interview_id}/closing/ack")
async def acknowledge_farewell(request: Request, interview_id: uuid.UUID, body: PlaybackAck):
    _verify_playback_participant(request, interview_id)
    try:
        async with asyncio.timeout(3):
            result, first = await acknowledge_playback(_sessionmaker(request), interview_id, body)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Interview not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=503, detail="Playback confirmation pending") from exc
    # Once per closing attempt: the browser repeats its ACK until one is accepted.
    if first and body.audio_output is not None:
        otel_metrics.record(
            "browser",
            "farewell_audio_output",
            1,
            {"audio_output": body.audio_output, "farewell_status": result["status"] or body.status},
        )
    return result


class ResponseOnset(BaseModel):
    """Browser-side heuristic: candidate speech end to interviewer audio onset."""

    model_config = ConfigDict(extra="forbid")
    # Fresh for every sample, which the browser never retries; only validated.
    sample_id: uuid.UUID
    seconds: float = Field(strict=True, ge=0, le=60, allow_inf_nan=False)


_MAX_ONSET_SAMPLES = 64


@router.post("/interviews/{interview_id}/metrics/response-onset", status_code=202)
async def record_response_onset(request: Request, interview_id: uuid.UUID, body: ResponseOnset):
    """Decoded-audio timing in the browser; not loopback or physical audibility."""
    _verify_playback_participant(request, interview_id)
    async with asyncio.timeout(3), _sessionmaker(request)() as session:
        # One statement takes a slot below the cap, so concurrent samples cannot
        # exceed it. Bookkeeping, not an interview change: updated_at stays.
        claimed = (
            await session.execute(
                update(db.Conversation)
                .where(
                    db.Conversation.id == interview_id,
                    db.Conversation.response_onset_samples < _MAX_ONSET_SAMPLES,
                )
                .values(
                    response_onset_samples=db.Conversation.response_onset_samples + 1,
                    updated_at=db.Conversation.updated_at,
                )
                .returning(db.Conversation.run_config)
            )
        ).first()
        if claimed is None:
            exists = await session.scalar(
                select(db.Conversation.id).where(db.Conversation.id == interview_id)
            )
            if exists is None:
                raise HTTPException(status_code=404, detail="Interview not found")
            return {"accepted": False}
        await session.commit()
    config = claimed.run_config or {}
    otel_metrics.record(
        "browser",
        "response_onset_seconds",
        body.seconds,
        {
            "source": "browser_decoded_audio_rms",
            "graph_version": config.get("graph_version", "unknown"),
            "config_version": config.get("config_version", "unknown"),
            "language": config.get("language", "unknown"),
            "seniority": config.get("seniority", "unknown"),
            "length": config.get("interview_length", "unknown"),
            "model": (config.get("models") or {}).get("interviewer", {}).get("model", "unknown"),
        },
    )
    return {"accepted": True}


@router.get("/interviews/{interview_id}/closing")
async def get_closing_state(request: Request, interview_id: uuid.UUID):
    _verify_playback_participant(request, interview_id)
    try:
        async with asyncio.timeout(3):
            return await closing_state(_sessionmaker(request), interview_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Interview not found") from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=503, detail="Closing recovery pending") from exc


# `resume.read()` buffers the upload in memory; cap it so a huge (or hostile)
# file cannot exhaust the process. Real resumes are well under this.
_MAX_RESUME_BYTES = 10 * 1024 * 1024  # 10 MB


def _validate_sources(resume_markdown: str, job_offer: str) -> None:
    try:
        validate_source_documents(resume_markdown, job_offer)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc


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
    return resolve_voice(voice)


def _default_voice_for(language: str, like: str | None = None) -> str:
    """The catalog voice for `language` when none was chosen: the one with
    the same gender as `like` (the voice being replaced) when there is one,
    else the first listed. `language` must be supported."""
    candidates = [key for key, cfg in VOICES.items() if cfg["language"] == language]
    gender = VOICES.get(like or "", {}).get("gender")
    same_gender = [key for key in candidates if VOICES[key]["gender"] == gender]
    return (same_gender or candidates)[0]


def _resolve_interviewer(
    app_settings: db.AppSettings | db.UserSettings, overrides: dict[str, Any] | None
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


class BudgetOverrides(BaseModel):
    question_limit: int | None = Field(default=None, ge=1, le=12, strict=True)
    followup_limit: int | None = Field(default=None, ge=0, le=2, strict=True)
    max_minutes: int | None = Field(default=None, ge=1, le=25, strict=True)


class InterviewerOverride(BudgetOverrides):
    """The `interviewer` JSON field of POST /interviews. Every key optional:
    absent inherits the global setting, a value (including "") wins."""

    agent_name: str | None = None
    language: str | None = None
    voice: str | None = None
    persona: str | None = None
    custom_instructions: str | None = None


def _parse_interviewer(raw: str | None) -> dict[str, Any] | None:
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
    """PUT /settings body: the user's agent configuration."""

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


def _serialize_settings(app_settings: db.AppSettings | db.UserSettings) -> dict[str, Any]:
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
async def get_settings(request: Request, user: CurrentUser):
    async with _sessionmaker(request)() as session:
        app_settings = await db.get_user_settings(session, user.id)
        return _serialize_settings(app_settings)


@router.put("/settings")
async def update_settings(request: Request, user: CurrentUser, body: SettingsUpdate):
    async with _sessionmaker(request)() as session:
        app_settings = await db.upsert_user_settings(session, user.id, body.model_dump())
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
    return reconnect_deadline(
        conversation,
        reconnect_seconds=settings.interview_reconnect_seconds,
        closing_seconds=settings.closing_timeout_seconds + 15,
    )


def _can_reconnect(conversation: db.Conversation, now: datetime) -> bool:
    return now < _reconnect_deadline(conversation)


def _lifecycle_fields(conversation: db.Conversation, now: datetime) -> dict[str, Any]:
    if conversation.status in ("interviewing", "closing"):
        deadline = _reconnect_deadline(conversation)
        return {"can_start": now < deadline, "reconnect_until": deadline.isoformat()}
    return {"can_start": conversation.status == "planned", "reconnect_until": None}


def _interview_language(conversation: db.Conversation) -> str:
    return (conversation.agent_settings or {}).get("language") or "en"


def _closing_remaining(conversation: db.Conversation, now: datetime) -> float | None:
    """Same server-clock bound as the closing-state endpoint, for a reloaded tab."""
    if conversation.status != "closing" or conversation.closing_ack_deadline_at is None:
        return None
    return max(0.0, (conversation.closing_ack_deadline_at - now).total_seconds() + 10)


def _serialize(
    conversation: db.Conversation, now: datetime, *, evaluation_invalid=False
) -> dict[str, Any]:
    evaluation = conversation.evaluation
    return {
        "id": str(conversation.id),
        "created_at": conversation.created_at.isoformat(),
        "updated_at": conversation.updated_at.isoformat(),
        "status": conversation.status,
        "elapsed_seconds": max(0.0, (now - conversation.started_at).total_seconds())
        if conversation.status == "interviewing" and conversation.started_at is not None
        else None,
        "ended_reason": conversation.ended_reason,
        **_lifecycle_fields(conversation, now),
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
        "question_limit": conversation.question_limit,
        "followup_limit": conversation.followup_limit,
        "run_config": conversation.run_config,
        "farewell_status": conversation.farewell_status,
        # Written fallback for a farewell that was not (or not provably) heard.
        "farewell_text": FAREWELLS.get(_interview_language(conversation), FAREWELLS["en"])
        if conversation.farewell_status in ("failed", "timeout", "not_possible")
        else None,
        "closing_remaining_seconds": _closing_remaining(conversation, now),
        "transcript_integrity": conversation._current_seal.integrity
        if getattr(conversation, "_current_seal", None)
        else conversation.transcript_integrity,
        "capture_integrity_pending": conversation.capture_integrity_pending,
        "transcript_sealed": conversation.transcript_sealed_at is not None,
        "closing_id": str(conversation.closing_id) if conversation.closing_id else None,
        # Who conducted it, as snapshotted at creation.
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
                "lifecycle": m.lifecycle,
                "close_reason": m.close_reason,
                "essential": m.essential,
                "competency": m.competency,
                "primary_questions": m.primary_questions,
                "followups": m.followups,
                "clarifications": m.clarifications,
            }
            for m in conversation.milestones
        ],
        "evaluation_invalidated": conversation.capture_integrity_pending or evaluation_invalid,
        "transcript_seal_id": str(conversation.transcript_seal_id)
        if conversation.transcript_seal_id
        else None,
        "evaluation_request_id": str(conversation.evaluation_request_id)
        if conversation.evaluation_request_id
        else None,
        "evaluation_is_previous": bool(
            (
                evaluation
                and conversation.evaluation_request_id
                and (evaluation.result or {}).get("request_id")
                != str(conversation.evaluation_request_id)
            )
            or (
                evaluation
                and conversation.transcript_seal_id
                and (evaluation.result or {}).get("seal_id") != str(conversation.transcript_seal_id)
            )
        ),
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
                **(evaluation.result or {}),
                **(
                    {"hired": None, "score": None, "evaluation_status": "partial"}
                    if conversation.capture_integrity_pending or evaluation_invalid
                    else {}
                ),
            }
            if evaluation
            else None
        ),
        "token_usage": conversation.token_usage,
    }


def _serialize_summary(
    conversation: db.Conversation, now: datetime, *, evaluation_invalid=False
) -> dict[str, Any]:
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
        **_lifecycle_fields(conversation, now),
        "title": _offer_title(conversation.job_offer),
        "resume_filename": conversation.resume_filename,
        "seniority": conversation.seniority,
        "seniority_source": conversation.seniority_source,
        "interview_length": conversation.interview_length,
        "max_minutes": conversation.max_minutes,
        "repeat_of_id": (str(conversation.repeat_of_id) if conversation.repeat_of_id else None),
        "milestones_total": len(milestones),
        "milestones_completed": sum(1 for m in milestones if m.completed),
        "evaluation_invalidated": conversation.capture_integrity_pending or evaluation_invalid,
        "transcript_seal_id": str(conversation.transcript_seal_id)
        if conversation.transcript_seal_id
        else None,
        "evaluation_request_id": str(conversation.evaluation_request_id)
        if conversation.evaluation_request_id
        else None,
        "evaluation_is_previous": bool(
            (
                evaluation
                and conversation.evaluation_request_id
                and (evaluation.result or {}).get("request_id")
                != str(conversation.evaluation_request_id)
            )
            or (
                evaluation
                and conversation.transcript_seal_id
                and (evaluation.result or {}).get("seal_id") != str(conversation.transcript_seal_id)
            )
        ),
        "evaluation": (
            {
                "hired": evaluation.hired,
                "score": evaluation.score,
                "evaluation_status": (evaluation.result or {}).get("evaluation_status"),
                **(
                    {"hired": None, "score": None, "evaluation_status": "partial"}
                    if conversation.capture_integrity_pending or evaluation_invalid
                    else {}
                ),
            }
            if evaluation
            else None
        ),
    }


async def _load_or_404(session: AsyncSession, interview_id: uuid.UUID) -> db.Conversation:
    conversation = await db.get_conversation(session, interview_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Interview not found")
    return conversation


async def _load_owned_or_404(
    session: AsyncSession, interview_id: uuid.UUID, user: User
) -> db.Conversation:
    """The interview if `user` owns it. Someone else's, or nobody's (from
    before accounts), is the same 404 as a missing one: no existence leak."""
    conversation = await db.get_conversation(session, interview_id)
    if conversation is None or conversation.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Interview not found")
    return conversation


class ResumeReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str

    @field_validator("text")
    @classmethod
    def _not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Reviewed resume text must not be empty")
        return value


async def _read_resume(resume: UploadFile) -> bytes:
    if not (resume.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="resume must be a PDF file")
    data = await resume.read(_MAX_RESUME_BYTES + 1)
    if len(data) > _MAX_RESUME_BYTES:
        raise HTTPException(status_code=413, detail="Resume PDF exceeds the 10 MB limit")
    if not data:
        raise HTTPException(status_code=400, detail="Resume PDF must not be empty")
    try:
        await anyio.to_thread.run_sync(resume_ingestion.validate_pdf, data)
    except Exception:
        raise HTTPException(
            status_code=400, detail="Resume must be a readable PDF document"
        ) from None
    return data


async def _extract_resume(data: bytes, filename: str | None) -> str:
    try:
        resume_markdown = await anyio.to_thread.run_sync(
            resume_ingestion.pdf_to_markdown, data, filename or "resume.pdf"
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read PDF: {exc}") from exc
    if not resume_markdown.strip():
        raise HTTPException(status_code=400, detail="PDF contained no extractable text")
    _validate_sources(resume_markdown, "")
    return resume_markdown


@router.post("/resumes/preview", dependencies=[Depends(current_user)])
async def preview_resume(resume: UploadFile):
    """Extract without planning or persisting; the user reviews the actual input."""
    data = await _read_resume(resume)
    content = await _extract_resume(data, resume.filename)
    return {
        "filename": resume.filename,
        "text": content,
        "characters": len(content),
        "pdf_sha256": sha256(data).hexdigest(),
    }


@router.post("/interviews")
async def create_interview(
    request: Request,
    user: CurrentUser,
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
    # JSON preserves an explicitly empty edit, which must be rejected rather
    # than silently falling back to fresh extraction. The hash binds the edit
    # to the selected PDF, including when preview requests finish out of order.
    resume_review: str | None = Form(None),
):
    requested_seniority = _parse_seniority(seniority)
    length = _parse_length(interview_length)
    interviewer_overrides = _parse_interviewer(interviewer)
    if not job_offer.strip():
        raise HTTPException(status_code=400, detail="job_offer must not be empty")
    _validate_sources("", job_offer)

    data = await _read_resume(resume)
    pdf_hash = sha256(data).hexdigest()
    logger.info("creating interview", extra={"resume": resume.filename, "bytes": len(data)})
    if resume_review is None:
        resume_markdown = await _extract_resume(data, resume.filename)
    else:
        try:
            review = ResumeReview.model_validate_json(resume_review)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Invalid reviewed resume text or PDF hash"
            ) from None
        if review.pdf_sha256 != pdf_hash:
            raise HTTPException(
                status_code=409, detail="The resume changed. Review the selected PDF again."
            )
        resume_markdown = review.text

    return await _plan_and_persist(
        request,
        user,
        job_offer=job_offer,
        resume_markdown=resume_markdown,
        resume_filename=resume.filename,
        requested_seniority=requested_seniority,
        length=length,
        interviewer_overrides=interviewer_overrides,
        resume_provenance={
            "kind": "reviewed_pdf_text" if resume_review is not None else "extracted_pdf_text",
            "pdf_sha256": pdf_hash,
            "text_sha256": sha256(resume_markdown.encode()).hexdigest(),
        },
    )


async def _plan_and_persist(
    request: Request,
    user: User,
    *,
    job_offer: str,
    resume_markdown: str,
    resume_filename: str | None,
    requested_seniority: Seniority | None,
    length: InterviewLength,
    repeat_of_id: uuid.UUID | None = None,
    pinned_source: str | None = None,
    pinned_evidence: str | None = None,
    interviewer_overrides: dict[str, Any] | None = None,
    resume_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the planner and store the resume and planned interview in Postgres.

    Shared by POST /interviews (fresh upload) and POST /interviews/{id}/repeat
    (same resume text and offer, replanned) — the only difference between the
    two is where the inputs come from, so everything after them lives here.

    `pinned_source` / `pinned_evidence` only apply when `requested_seniority`
    is given: a re-run carries the ORIGINAL provenance forward ("auto" stays
    "auto") instead of masquerading as a level the user just picked.

    `interviewer_overrides` follows the convention in `_resolve_interviewer`:
    None inherits the user's settings, a value wins for this interview only.

    The interview is the user's, and counts against their quota once every
    input is validated: a rejected request costs nothing, while a planner run
    that started counts even if it fails (it was spent).
    """
    _validate_sources(resume_markdown, job_offer)
    conversation_id = uuid.uuid4()
    # This interview's own time cap: the requested length's minutes, clamped
    # by the global setting. Stored on the row AND handed to the planner, so
    # a "deep" request under a 15-minute cap is planned for 15 minutes
    # instead of being cut off mid-plan (`interview_length` stays "deep").
    overrides = interviewer_overrides or {}
    max_minutes = min(
        overrides.get("max_minutes") or length_for(length)["minutes"],
        settings.interview_max_minutes,
    )
    question_limit = (
        overrides.get("question_limit")
        or length_for(fit_length(length, max_minutes))["max_milestones"]
    )
    async with _sessionmaker(request)() as session:
        # Snapshot the interviewer NOW: this interview keeps this
        # persona/language/voice even if the settings change later.
        app_settings = await db.get_user_settings(session, user.id)
        interviewer = _resolve_interviewer(app_settings, interviewer_overrides)
        await _reserve_interview(session, user)
        agent_settings = {
            key: interviewer[key]
            for key in ("agent_name", "language", "voice", "tts_model", "tts_voice")
        }
        run_config = execution_config(
            settings,
            language=interviewer["language"],
            voice=agent_settings,
            max_minutes=max_minutes,
            question_limit=question_limit,
            interview_length=length.value,
            seniority=requested_seniority.value if requested_seniority else "unknown",
        )
        run_config["resume_input"] = resume_provenance or {
            "kind": "stored_text",
            "text_sha256": sha256(resume_markdown.encode()).hexdigest(),
        }
        session.add(
            db.Conversation(
                id=conversation_id,
                owner_id=user.id,
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
                question_limit=question_limit,
                run_config=run_config,
            )
        )
        await session.commit()

        planner_usage = UsageMetadataCallbackHandler()
        telemetry = Telemetry(
            conversation_id,
            run_config,
            defer_dimensions=requested_seniority is None,
            process="planner",
            thread_of=repeat_of_id,
        )
        observer = LLMObserver(
            telemetry, "planner", settings.planner_model, settings.planner_reasoning_effort
        )
        try:
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
                question_limit=question_limit,
                telemetry_callback=observer,
                langsmith_extra={"metadata": telemetry.trace_metadata()},
            )
            if requested_seniority is None and (
                plan.detected_seniority is None or not plan.seniority_evidence
            ):
                raise ValueError("Automatic seniority requires classification and source evidence")
            telemetry.resolve_dimensions(
                {
                    "seniority": (requested_seniority or plan.detected_seniority).value,
                }
            )
        except Exception as exc:
            logger.exception("planning failed for %s", conversation_id)
            await db.set_status(session, conversation_id, "error")
            # The attempts inside with_retry were real spend even though no
            # plan came out of them.
            await evaluations.record_spent_usage(session, conversation_id, "planner", planner_usage)
            raise HTTPException(status_code=500, detail=f"Planning failed: {exc}") from exc
        finally:
            # Planning runs inside the HTTP request: keep the previous bound.
            await telemetry.drain(timeout_seconds=5)

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
        budget = followup_budget(conversation.seniority, fit_length(length, max_minutes))
        conversation.followup_limit = min(
            overrides.get("followup_limit")
            if overrides.get("followup_limit") is not None
            else budget,
            budget,
        )
        # One primary question per milestone: the effective limit is what the
        # plan can actually ask. Requested values stay visible beside it.
        conversation.question_limit = min(question_limit, len(plan.milestones))
        conversation.run_config = {
            **run_config,
            "seniority": conversation.seniority,
            "question_limit": conversation.question_limit,
            "requested_question_limit": overrides.get("question_limit"),
            "followup_limit": conversation.followup_limit,
            "requested_followup_limit": overrides.get("followup_limit"),
            "plan_hash": content_hash(plan.model_dump(mode="json")),
            "source_hash": content_hash([resume_markdown, job_offer]),
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
                    expected_evidence=spec.expected_evidence,
                    essential=spec.essential,
                    competency=spec.competency,
                )
            )
        await session.commit()

        await session.refresh(conversation)
        return _serialize(
            conversation,
            await session.scalar(select(func.clock_timestamp())),
            evaluation_invalid=await result_invalid(
                session,
                conversation,
                conversation.evaluation.result if conversation.evaluation else None,
            ),
        )


async def _reserve_interview(session: AsyncSession, user: User) -> None:
    """Spend one interview of the user's quota, or answer 429. Admins are not
    counted. The detail is a string, which the web client shows as the reason.

    The reservation ends the session's transaction (commit or rollback), so
    nothing loaded before it may be read after it."""
    if is_admin(user):
        otel_metrics.record("accounts", "interview_reserved", 1, {"role": "admin"})
        return
    outcome = await db.reserve_interview_slot(
        session,
        user.id,
        lifetime_limit=settings.lifetime_interviews_per_user,
        monthly_limit=settings.guest_interviews_per_month,
    )
    if outcome == "ok":
        otel_metrics.record("accounts", "interview_reserved", 1, {"role": "guest"})
        return
    logger.info("interview quota reached", extra={"limit": outcome})
    otel_metrics.record("accounts", "quota_rejected", 1, {"limit": outcome})
    if outcome == "lifetime":
        # Permanent: no retry time to offer.
        raise HTTPException(status_code=429, detail="lifetime_interview_limit_reached")
    raise HTTPException(
        status_code=429,
        detail="monthly_demo_capacity_reached",
        headers={"Retry-After": str(await db.seconds_until_next_month(session))},
    )


def _isoformat(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


@router.get("/me")
async def get_me(request: Request, user: CurrentUser):
    """The signed-in user and what is left of their interviews. Admins have no
    limit (null) and are never held back by the monthly capacity.

    The one place profiles are written: the web asks on every page, so this
    is when a user is seen (at most one write every few minutes)."""
    admin = is_admin(user)
    provider = auth_provider(user)
    async with _sessionmaker(request)() as session:
        profile, new = await db.touch_user_profile(
            session, user.id, auth_provider=provider, email=user.email, name=user.name
        )
        quota = await db.interview_quota_status(
            session, user.id, monthly_limit=settings.guest_interviews_per_month
        )
    if new:
        otel_metrics.record("accounts", "new_user", 1, {"auth_provider": provider})
    limit = None if admin else settings.lifetime_interviews_per_user
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "is_admin": admin,
        "auth_provider": profile.auth_provider,
        "created_at": profile.created_at.isoformat(),
        "last_seen_at": _isoformat(profile.last_seen_at),
        "interviews_used": quota.interviews_used,
        "interview_limit": limit,
        "interviews_remaining": None if limit is None else max(0, limit - quota.interviews_used),
        "demo_capacity_available": admin or quota.monthly_capacity_available,
    }


@router.get("/admin/users", dependencies=[Depends(require_admin)])
async def list_users(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """Everyone who signed in, most recently seen first: their profile, their
    quota, and how many of their interviews the retention purge left."""
    async with _sessionmaker(request)() as session:
        users, total = await db.list_user_profiles(session, limit=limit, offset=offset)
    items = []
    for summary in users:
        profile = summary.profile
        admin = profile.owner_id in settings.admin_user_ids
        items.append(
            {
                "id": profile.owner_id,
                "name": profile.name,
                "email": profile.email,
                "auth_provider": profile.auth_provider,
                "is_admin": admin,
                "created_at": profile.created_at.isoformat(),
                "last_seen_at": _isoformat(profile.last_seen_at),
                "interviews_used": summary.interviews_used,
                "interview_limit": None if admin else settings.lifetime_interviews_per_user,
                "interviews_stored": summary.interviews_stored,
                "last_interview_at": _isoformat(summary.last_interview_at),
            }
        )
    return {"total": total, "items": items}


@router.post("/internal/maintenance", dependencies=[Depends(internal_caller)])
async def run_maintenance(request: Request):
    """The retention purge and one lifecycle sweep, for a scheduler that wakes
    an otherwise idle (scaled to zero) API. Counts only."""
    sessionmaker = _sessionmaker(request)
    deleted = await purge_expired(sessionmaker, settings)
    reconciled, failures = await LifecycleSweeper(
        sessionmaker, settings, request.app.state.evaluations
    ).once()
    # Fresh counts for Grafana before an idle API scales back to zero.
    await publish_account_metrics(sessionmaker, settings)
    return {"deleted_interviews": deleted, "reconciled": reconciled, "failures": failures}


@router.get("/runtime", dependencies=[Depends(require_admin)])
async def get_runtime_manifest(request: Request):
    manifest = getattr(request.app.state, "runtime_manifest", None)
    if manifest is None:
        raise HTTPException(status_code=503, detail="Startup configuration has not been recorded")
    return manifest


_HISTORY_STATUSES = (
    "created",
    "planned",
    "interviewing",
    "closing",
    "completed",
    "evaluating",
    "evaluated",
    "evaluation_failed",
    "error",
)


@router.get("/interviews")
async def list_interviews(
    request: Request,
    user: CurrentUser,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None),
):
    """Paginated history of the user's interviews, newest first. `status`
    narrows it to one state; empty (a form's "all" option) is the same as
    absent."""
    status = status or None
    if status is not None and status not in _HISTORY_STATUSES:
        raise HTTPException(
            status_code=400, detail=f"status must be one of {list(_HISTORY_STATUSES)}"
        )
    async with _sessionmaker(request)() as session:
        conversations, total = await db.list_conversations(
            session, limit=limit, offset=offset, status=status, owner_id=user.id
        )
        now = await session.scalar(select(func.clock_timestamp()))
        return {
            "items": [
                _serialize_summary(
                    c,
                    now,
                    evaluation_invalid=await result_invalid(
                        session, c, c.evaluation.result if c.evaluation else None
                    ),
                )
                for c in conversations
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }


@router.get("/interviews/{interview_id}")
async def get_interview(request: Request, user: CurrentUser, interview_id: uuid.UUID):
    async with _sessionmaker(request)() as session:
        conversation = await _load_owned_or_404(session, interview_id, user)
        if conversation.status in ("interviewing", "closing"):
            await reconcile_interview(session, interview_id, settings)
            await session.refresh(conversation)
        return _serialize(
            conversation,
            await session.scalar(select(func.clock_timestamp())),
            evaluation_invalid=await result_invalid(
                session,
                conversation,
                conversation.evaluation.result if conversation.evaluation else None,
            ),
        )


@router.get("/interviews/{interview_id}/evaluations")
async def get_evaluation_history(request: Request, user: CurrentUser, interview_id: uuid.UUID):
    async with _sessionmaker(request)() as session:
        conversation = await _load_owned_or_404(session, interview_id, user)
        requests = list(
            await session.scalars(
                select(db.EvaluationRequest)
                .where(db.EvaluationRequest.conversation_id == interview_id)
                .order_by(db.EvaluationRequest.created_at)
            )
        )
        runs = list(
            await session.scalars(
                select(db.EvaluationRun)
                .where(db.EvaluationRun.conversation_id == interview_id)
                .order_by(db.EvaluationRun.created_at)
            )
        )
        request_map = {item.id: item for item in requests}
        seals = (
            await session.execute(
                select(db.TranscriptSeal.id, db.TranscriptSeal.version).where(
                    db.TranscriptSeal.conversation_id == interview_id
                )
            )
        ).all()
        seal_versions = {seal.id: seal.version for seal in seals}
        invalid_seals = {seal.id: await seal_invalid(session, seal.id) for seal in seals}
        invalid_runs = {}
        for run in runs:
            saved_request = request_map.get(run.request_id)
            invalid = saved_request is None or invalid_seals.get(saved_request.seal_id, True)
            invalid_runs[run.id] = conversation.capture_integrity_pending or invalid
        return {
            "current_request_id": str(conversation.evaluation_request_id)
            if conversation.evaluation_request_id
            else None,
            "requests": [
                {
                    "id": str(item.id),
                    "automatic": item.automatic,
                    "seal_id": str(item.seal_id) if item.seal_id else None,
                    "seal_version": seal_versions.get(item.seal_id),
                    "status": item.status,
                    "attempts": item.attempts,
                    "created_at": item.created_at.isoformat(),
                }
                for item in requests
            ],
            "attempts": [
                {
                    "id": str(run.id),
                    "request_id": str(run.request_id) if run.request_id else None,
                    "ordinal": run.ordinal,
                    "status": run.status,
                    "invalidated": invalid_runs.get(run.id, conversation.capture_integrity_pending),
                    "result": {
                        **run.result,
                        "score": None,
                        "hired": None,
                        "evaluation_status": "partial",
                    }
                    if run.result
                    and invalid_runs.get(run.id, conversation.capture_integrity_pending)
                    else run.result,
                    "error": run.error,
                    "created_at": run.created_at.isoformat(),
                }
                for run in runs
            ],
        }


@router.get("/interviews/{interview_id}/question")
async def get_question(request: Request, user: CurrentUser, interview_id: uuid.UUID):
    from interview_agent.interview.delivery import answered, latest_question

    async with _sessionmaker(request)() as session:
        conversation = await _load_owned_or_404(session, interview_id, user)
        question = await latest_question(session, interview_id)
        if (
            question is None
            or conversation.status != "interviewing"
            or conversation.capture_integrity_pending
            or await answered(session, question)
        ):
            return {"question": None}
        attempt = await session.scalar(
            select(db.QuestionAttempt)
            .where(db.QuestionAttempt.question_id == question.id)
            .order_by(db.QuestionAttempt.created_at.desc())
            .limit(1)
        )
        return {
            "question": {
                "id": str(question.id),
                "text": question.text,
                "status": attempt.status if attempt else "pending",
            }
        }


class QuestionReplay(BaseModel):
    question_id: uuid.UUID
    request_id: uuid.UUID


@router.post("/interviews/{interview_id}/question/replay", status_code=202)
async def replay_question(
    request: Request, user: CurrentUser, interview_id: uuid.UUID, body: QuestionReplay
):
    from interview_agent.interview.delivery import request_replay

    async with _sessionmaker(request)() as session:
        await _load_owned_or_404(session, interview_id, user)
        try:
            request_id = await request_replay(
                session, interview_id, body.question_id, body.request_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"request_id": str(request_id)}


class IncidentReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_id: uuid.UUID
    decision: str = Field(pattern="^(duplicate|post_cut|omission)$")
    rationale: str = Field(min_length=1, max_length=4000)
    reviewer: str = Field(min_length=1, max_length=120)


@router.post("/interviews/{interview_id}/incidents/{incident_id}/review")
async def review_capture_incident(
    request: Request,
    user: CurrentUser,
    interview_id: uuid.UUID,
    incident_id: uuid.UUID,
    body: IncidentReviewRequest,
):
    from interview_agent.interview.seals import review_incident

    async with _sessionmaker(request)() as session:
        await _load_owned_or_404(session, interview_id, user)
        try:
            reviewed = await review_incident(
                session, interview_id, incident_id, **body.model_dump()
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"review_id": str(reviewed.id), "decision": reviewed.decision}


class SuccessorSealRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seal_id: uuid.UUID
    parent_id: uuid.UUID
    incident_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    rationale: str = Field(min_length=1, max_length=4000)
    reviewer: str = Field(min_length=1, max_length=120)
    confirm_complete: bool = False


@router.post("/interviews/{interview_id}/seals", status_code=201)
async def create_reviewed_snapshot(
    request: Request, user: CurrentUser, interview_id: uuid.UUID, body: SuccessorSealRequest
):
    from interview_agent.interview.seals import create_successor

    async with _sessionmaker(request)() as session:
        await _load_owned_or_404(session, interview_id, user)
        try:
            seal = await create_successor(session, interview_id, **body.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"seal_id": str(seal.id), "version": seal.version, "evaluation_required": True}


@router.get("/interviews/{interview_id}/seals")
async def get_seal_history(request: Request, user: CurrentUser, interview_id: uuid.UUID):
    async with _sessionmaker(request)() as session:
        conversation = await _load_owned_or_404(session, interview_id, user)
        seals = list(
            await session.scalars(
                select(db.TranscriptSeal)
                .where(db.TranscriptSeal.conversation_id == interview_id)
                .order_by(db.TranscriptSeal.version)
            )
        )
        incidents = list(
            await session.scalars(
                select(db.CaptureIncident)
                .where(db.CaptureIncident.conversation_id == interview_id)
                .order_by(db.CaptureIncident.created_at)
            )
        )
        reviews = list(
            await session.scalars(
                select(db.IncidentResolution)
                .join(db.CaptureIncident)
                .where(db.CaptureIncident.conversation_id == interview_id)
            )
        )
        by_incident = {review.incident_id: review for review in reviews}
        return {
            "current_seal_id": str(conversation.transcript_seal_id)
            if conversation.transcript_seal_id
            else None,
            "seals": [
                {
                    "id": str(seal.id),
                    "version": seal.version,
                    "parent_id": str(seal.parent_id) if seal.parent_id else None,
                    "integrity": seal.integrity,
                    "invalidated": await seal_invalid(session, seal.id),
                    "records": seal.records,
                    "provenance": seal.provenance,
                    "created_at": seal.created_at.isoformat(),
                }
                for seal in seals
            ],
            "incidents": [
                {
                    "id": str(incident.id),
                    "content": incident.payload.get("content"),
                    "kind": incident.kind,
                    "seal_id": str(incident.seal_id) if incident.seal_id else None,
                    "review": {
                        "decision": by_incident[incident.id].decision,
                        "rationale": by_incident[incident.id].rationale,
                        "reviewer": by_incident[incident.id].reviewer,
                    }
                    if incident.id in by_incident
                    else None,
                }
                for incident in incidents
            ],
        }


@router.get("/interviews/{interview_id}/transcript")
async def get_transcript(request: Request, user: CurrentUser, interview_id: uuid.UUID):
    """The stored turns of a past interview, in turn order.

    Kept off /interviews/{id} on purpose: that one is polled every 2s while an
    interview runs, and the transcript grows without bound.
    """
    async with _sessionmaker(request)() as session:
        conversation = await _load_owned_or_404(session, interview_id, user)
        messages = await db.get_messages(session, interview_id)
        versions = list(
            await session.scalars(
                select(db.MessageVersion)
                .join(db.Message)
                .where(db.Message.conversation_id == interview_id)
                .order_by(db.MessageVersion.message_id, db.MessageVersion.version)
            )
        )
        incidents = list(
            await session.scalars(
                select(db.CaptureIncident)
                .where(db.CaptureIncident.conversation_id == interview_id)
                .order_by(db.CaptureIncident.created_at, db.CaptureIncident.id)
            )
        )
        return {
            "capture_integrity_pending": conversation.capture_integrity_pending,
            "seal_id": str(conversation.transcript_seal_id)
            if conversation.transcript_seal_id
            else None,
            "incidents": [
                {
                    "id": str(i.id),
                    "turn_id": i.turn_id,
                    "kind": i.kind,
                    "seal_id": str(i.seal_id) if i.seal_id else None,
                    "content": i.payload.get("content"),
                    "created_at": i.created_at.isoformat(),
                    "resolved_at": i.resolved_at.isoformat() if i.resolved_at else None,
                }
                for i in incidents
            ],
            "messages": [
                {
                    "id": str(m.id),
                    "source_id": m.source_id,
                    "turn_id": m.turn_id,
                    "version": m.version,
                    "versions": [
                        {"version": v.version, "content": v.content}
                        for v in versions
                        if v.message_id == m.id
                    ],
                    "role": m.role,
                    "content": m.content,
                    "created_at": m.created_at.isoformat(),
                    "interrupted": m.interrupted,
                    "metrics": m.metrics,
                }
                for m in messages
            ],
        }


class RepeatRequest(BudgetOverrides):
    """POST /interviews/{id}/repeat; level/length inherit when omitted.

    `seniority: "auto"` is not "inherit" — it asks the planner to classify the
    role again from scratch, exactly like it means on the upload form.
    """

    seniority: str | None = None
    interview_length: str | None = None


@router.post("/interviews/{interview_id}/repeat")
async def repeat_interview(
    request: Request, user: CurrentUser, interview_id: uuid.UUID, body: RepeatRequest | None = None
):
    """Run the same role again: a NEW interview off the stored resume and offer.

    The resume PDF is long gone; its markdown is kept in Postgres and reused
    to plan the new interview. Re-planning rather than cloning the old
    milestones is the point of a practice re-run: the same role and the same
    bar, different questions. The original is never touched.
    """
    body = body or RepeatRequest()
    async with _sessionmaker(request)() as session:
        source = await _load_owned_or_404(session, interview_id, user)
        job_offer = source.job_offer
        resume_markdown = source.resume_markdown
        resume_filename = source.resume_filename
        # Inherit unless the caller overrides. Inheriting carries the level's
        # provenance with it, so a re-run of an auto-detected interview still
        # reads as "auto" instead of claiming the user picked the level.
        if body.seniority is None and source.seniority_source == "fallback":
            # The provisional level of a failed auto classification was never
            # established: classify again instead of silently pinning it.
            requested_seniority = None
            pinned_source = None
            pinned_evidence = None
        elif body.seniority is None:
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
        # one does not inherit whatever the settings hold now.
        snapshot = source.agent_settings or {}
        interviewer_overrides = {
            "agent_name": snapshot.get("agent_name"),
            "language": snapshot.get("language"),
            "voice": snapshot.get("voice"),
            "persona": source.persona or "",
            "custom_instructions": source.custom_instructions or "",
        }
        # Same duration inherits effective limits. A new profile recalculates
        # them, then accepts fresh typed overrides; it does not resurrect the
        # original profile's 8-minute cap for a requested deep interview.
        for field in ("question_limit", "followup_limit", "max_minutes"):
            override = getattr(body, field)
            inherited = getattr(source, field) if length.value == source.interview_length else None
            if override is not None or inherited is not None:
                interviewer_overrides[field] = override if override is not None else inherited
        resume_provenance = (source.run_config or {}).get("resume_input")

    logger.info(
        "repeating interview",
        extra={"conversation": str(interview_id), "root": str(root_id)},
    )
    return await _plan_and_persist(
        request,
        user,
        job_offer=job_offer,
        resume_markdown=resume_markdown,
        resume_filename=resume_filename,
        requested_seniority=requested_seniority,
        length=length,
        repeat_of_id=root_id,
        pinned_source=pinned_source,
        pinned_evidence=pinned_evidence,
        interviewer_overrides=interviewer_overrides,
        resume_provenance=resume_provenance,
    )


@router.get("/interviews/{interview_id}/token")
async def get_token(request: Request, user: CurrentUser, interview_id: uuid.UUID):
    async with _sessionmaker(request)() as session:
        conversation = await _load_owned_or_404(session, interview_id, user)
        if conversation.status not in ("planned", "interviewing", "closing"):
            raise HTTPException(
                status_code=409,
                detail=f"Interview is '{conversation.status}', expected 'planned'",
            )
        if not (conversation.run_config or {}).get("models"):
            raise HTTPException(
                status_code=409,
                detail="This interview has no saved model configuration; create a new one.",
            )
        _validate_sources(conversation.resume_markdown, conversation.job_offer)
        # Reconnect window (see _reconnect_deadline): an "interviewing" row
        # past it is an orphan of a crashed worker, not a live interview.
        if conversation.status in ("interviewing", "closing") and not _can_reconnect(
            conversation, await session.scalar(select(func.clock_timestamp()))
        ):
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
        # The same participant credential confirms playback after RTC stops.
        # Cover the interview, reconnection and finalization with an explicit TTL.
        .with_ttl(timedelta(hours=6))
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
    return {
        "server_url": settings.livekit_url,
        "room": room,
        "token": token,
    }


@router.post("/interviews/{interview_id}/evaluate", status_code=202)
async def evaluate_interview(
    request: Request,
    caller: Annotated[User | None, Depends(internal_caller_or_user)],
    interview_id: uuid.UUID,
    automatic: bool = Query(default=False),
    request_id: uuid.UUID | None = None,
):
    """Start the evaluation in the background; 202 with the row as it stands.

    Idempotent: the row is CLAIMED with one atomic UPDATE into "evaluating"
    (db.claim_evaluation), so the worker's auto-trigger and a Retry from the
    browser racing here start exactly one run — the loser just gets the row
    back. A run whose process died (no heartbeat for evaluations.STALE_AFTER)
    is claimed again, which is what the UI's Retry does once its own clock,
    anchored on the same heartbeat, runs out. Poll GET /interviews/{id} for
    the outcome.

    Callable by the worker (X-Internal-Token, no user) or by the owner.
    """
    async with _sessionmaker(request)() as session:
        conversation = (
            await _load_or_404(session, interview_id)
            if caller is None
            else await _load_owned_or_404(session, interview_id, caller)
        )
        if conversation.status in ("interviewing", "closing"):
            # A live interview must never be evaluated: it would score half a
            # transcript. A row past its reconnect window is not live (the
            # sweeper may not have sealed it yet); its transcript is worth scoring.
            if _can_reconnect(conversation, await session.scalar(select(func.clock_timestamp()))):
                raise HTTPException(status_code=409, detail="Interview is still in progress")
            logger.info(
                "closing an orphaned interview for evaluation",
                extra={
                    "conversation": str(interview_id),
                    "updated_at": conversation.updated_at.isoformat(),
                },
            )
            await reconcile_interview(session, interview_id, settings)
            await session.refresh(conversation)
            if conversation.status in ("interviewing", "closing"):
                raise HTTPException(status_code=409, detail="Interview is still in progress")
        if conversation.transcript_sealed_at is None:
            raise HTTPException(status_code=409, detail="Transcript is not sealed")
        try:
            claim_id = await db.claim_evaluation(
                session,
                interview_id,
                evaluations.STALE_AFTER,
                automatic=automatic,
                request_id=request_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if claim_id:
            request.app.state.evaluations.start(interview_id, claim_id)
            logger.info("evaluation scheduled", extra={"conversation": str(interview_id)})
        else:
            # Not claimable: a run is already on it, and still heartbeating.
            logger.info("evaluation already running", extra={"conversation": str(interview_id)})
        await session.refresh(conversation)
        return _serialize(
            conversation,
            await session.scalar(select(func.clock_timestamp())),
            evaluation_invalid=await result_invalid(
                session,
                conversation,
                conversation.evaluation.result if conversation.evaluation else None,
            ),
        )
