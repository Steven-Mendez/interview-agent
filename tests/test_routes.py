"""API route tests against a real (test) Postgres, with LLM calls and PDF conversion
mocked out.

The app is assembled by hand (router + app.state) instead of importing the
real `app`, whose lifespan needs validated API keys. The test
database is created on the fly in the session's Postgres (conftest: a throwaway
container locally, CI's service container via TEST_DATABASE_URL).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import jwt
import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import create_async_engine
from test_auth import bearer, token, use_neon_auth

from interview_agent.config import LocalAccount, settings
from interview_agent.interview import db
from interview_agent.interview import resume as resume_ingestion
from interview_agent.interview.context import MAX_JOB_OFFER_CHARS, MAX_RESUME_CHARS
from interview_agent.interview.dialogue import system_prompt
from interview_agent.interview.evaluation_contract import insufficient_evaluation
from interview_agent.interview.models import (
    Assessment,
    CriterionEvaluation,
    EvaluationResult,
    EvidenceRef,
    InterviewLength,
    InterviewPlan,
    MilestoneSpec,
    Seniority,
)
from interview_agent.interview.seals import ensure_seal
from interview_agent.prompts import length_for
from interview_agent.server import evaluations, routes
from interview_agent.server.auth import User, current_user, issue_local_token
from interview_agent.voices import VOICES

# The route tests' default caller: the local developer, listed as an admin (as
# in .env.example) so that tests about something else are never held back by
# a quota. Tests about accounts switch to other users through `acting_user`.
DEVELOPER = User("local-dev", None, "Local developer")


def _plan(detected: Seniority | None = Seniority.MID) -> InterviewPlan:
    return InterviewPlan(
        persona="Laura, engineering manager",
        summary="Solid candidate.",
        focus_areas=["Kubernetes"],
        detected_seniority=detected,
        seniority_evidence="The offer asks for 1-2 years." if detected else None,
        milestones=[
            MilestoneSpec(
                title=f"M{i}", description="Probe it.", expected_evidence="Names one index."
            )
            for i in range(4)
        ],
    )


def _evaluation() -> EvaluationResult:
    return EvaluationResult(
        hired=True,
        score=82,
        strengths=["clear communication"],
        weaknesses=["little SQL depth"],
        rationale="Convincing on most milestones.",
        score_gap="Little SQL depth kept it from the top of the band.",
        seniority_evaluated=Seniority.MID,
        calibration_notes=["Skipped trade-off depth: above this level."],
    )


async def _ensure_test_database(url) -> None:
    """CREATE DATABASE if missing — Postgres has no CREATE ... IF NOT EXISTS."""
    if not url.database or not url.database.endswith("_test"):
        raise ValueError("Route tests require a dedicated *_test database")
    admin = create_async_engine(
        url.set(database="postgres").render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
    )
    async with admin.connect() as conn:
        exists = await conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": url.database},
        )
        if not exists:
            await conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    await admin.dispose()


@pytest.fixture
def acting_user():
    """Who the route tests call as: set acting_user["user"] to switch, or to
    None to authenticate the request itself."""
    return {"user": DEVELOPER}


@pytest.fixture
async def client_and_sessionmaker(monkeypatch, integration_database_url, acting_user):
    await _ensure_test_database(integration_database_url)
    engine, sessionmaker = db.create_engine_and_sessionmaker(
        integration_database_url.render_as_string(hide_password=False)
    )
    async with engine.begin() as conn:
        # The test database persists across runs and create_all never ALTERs
        # an existing table — rebuild from scratch so schema changes (new
        # columns/tables) land, and every test starts from a clean slate.
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.run_sync(db.Base.metadata.create_all)

    monkeypatch.setattr(
        resume_ingestion, "pdf_to_markdown", lambda data, filename: "# Resume\nPython dev."
    )
    monkeypatch.setattr(routes, "run_planner", _fake_planner)
    monkeypatch.setattr(settings, "admin_user_ids", [DEVELOPER.id])

    async def acting(request: Request) -> User:
        # None: authenticate for real (the Authorization header, AUTH_MODE).
        if acting_user["user"] is None:
            return await current_user(request)
        return acting_user["user"]

    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    app.dependency_overrides[current_user] = acting
    app.state.sessionmaker = sessionmaker
    app.state.evaluations = evaluations.EvaluationRunner(sessionmaker)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, sessionmaker
    await engine.dispose()


async def _fake_planner(settings, resume_markdown, job_offer, **kwargs) -> InterviewPlan:
    return _plan()


async def _fake_evaluator(settings, **kwargs) -> EvaluationResult:
    records = [m for m in kwargs["transcript"] if m["role"] == "user"]
    milestones = kwargs["milestones"]
    level = Seniority(kwargs["seniority"])
    if not records or not milestones:
        return insufficient_evaluation(level, milestones, kwargs.get("language", "en"))
    reference = EvidenceRef(
        message_id=records[0]["id"],
        message_version=records[0].get("version"),
        quote=records[0]["content"],
    )
    complete = kwargs.get("transcript_complete", True)
    return _evaluation().model_copy(
        update={
            "criteria": [
                CriterionEvaluation(
                    milestone_id=m["id"],
                    assessment=Assessment.MEETS,
                    evidence=[reference],
                    rationale="Demonstrated the fixture criterion.",
                    practice="",
                )
                for m in milestones
            ],
            "strengths": [],
            "weaknesses": [],
            "seniority_evaluated": level,
            "evaluation_status": "complete" if complete else "partial",
            "score": 82 if complete else None,
            "hired": True if complete else None,
        }
    )


def _pdf_bytes():
    """A valid one-page synthetic PDF; extraction remains independently mocked."""
    stream = b"BT /F1 12 Tf 20 50 Td (Python developer) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    data = b"%PDF-1.4\n"
    offsets = []
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += str(index).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    xref = len(data)
    data += b"xref\n0 6\n0000000000 65535 f \n"
    for offset in offsets:
        data += f"{offset:010d} 00000 n \n".encode()
    return data + f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


def _upload(job_offer: str = "Backend engineer at ACME.", **extra: str):
    return {
        "files": {"resume": ("cv.pdf", _pdf_bytes(), "application/pdf")},
        "data": {"job_offer": job_offer, **extra},
    }


def _interviewer(**fields) -> str:
    """The `interviewer` form field. Only the keys passed are sent, so the
    test controls exactly what inherits and what overrides."""
    return json.dumps(fields)


async def test_preview_extracts_without_planning_or_persisting(
    client_and_sessionmaker, monkeypatch
):
    client, sessionmaker = client_and_sessionmaker

    async def unexpected_planner(*args, **kwargs):
        pytest.fail("Preview must not spend a planner request")

    monkeypatch.setattr(routes, "run_planner", unexpected_planner)
    response = await client.post("/api/resumes/preview", files=_upload()["files"])
    assert response.status_code == 200
    assert response.json() == {
        "filename": "cv.pdf",
        "text": "# Resume\nPython dev.",
        "characters": len("# Resume\nPython dev."),
        "pdf_sha256": sha256(_pdf_bytes()).hexdigest(),
    }
    async with sessionmaker() as session:
        assert (await db.list_conversations(session, limit=20, offset=0))[1] == 0


@pytest.mark.parametrize(
    "failure", ["extension", "bytes", "empty_file", "empty_text", "text_limit"]
)
async def test_preview_rejects_invalid_sources(client_and_sessionmaker, monkeypatch, failure):
    client, _ = client_and_sessionmaker
    name, data = "cv.pdf", _pdf_bytes()
    status = 400
    if failure == "extension":
        name = "cv.txt"
    elif failure == "bytes":
        monkeypatch.setattr(routes, "_MAX_RESUME_BYTES", len(data) - 1)
        status = 413
    elif failure == "empty_file":
        data = b""
    elif failure == "empty_text":
        monkeypatch.setattr(resume_ingestion, "pdf_to_markdown", lambda *args: " \n")
    else:
        monkeypatch.setattr(
            resume_ingestion, "pdf_to_markdown", lambda *args: "r" * (MAX_RESUME_CHARS + 1)
        )
        status = 413
    response = await client.post(
        "/api/resumes/preview", files={"resume": (name, data, "application/pdf")}
    )
    assert response.status_code == status


async def test_reviewed_resume_is_exact_planner_input_and_repeat_source(
    client_and_sessionmaker, monkeypatch
):
    client, sessionmaker = client_and_sessionmaker
    preview = (await client.post("/api/resumes/preview", files=_upload()["files"])).json()
    reviewed = "# Curriculum revisado\nPython y SQL; corregí la extracción.\n"
    seen = []

    async def planner(settings, resume_markdown, job_offer, **kwargs):
        seen.append(resume_markdown)
        return _plan()

    def unexpected_conversion(*args):
        pytest.fail("The reviewed input must not be replaced by another extraction")

    monkeypatch.setattr(routes, "run_planner", planner)
    monkeypatch.setattr(resume_ingestion, "pdf_to_markdown", unexpected_conversion)
    response = await client.post(
        "/api/interviews",
        **_upload(
            resume_review=json.dumps({"pdf_sha256": preview["pdf_sha256"], "text": reviewed})
        ),
    )
    assert response.status_code == 200
    source = response.json()
    assert source["run_config"]["resume_input"] == {
        "kind": "reviewed_pdf_text",
        "pdf_sha256": preview["pdf_sha256"],
        "text_sha256": sha256(reviewed.encode()).hexdigest(),
    }
    repeat = await client.post(f"/api/interviews/{source['id']}/repeat")
    assert repeat.status_code == 200
    assert seen == [reviewed, reviewed]
    assert repeat.json()["run_config"]["resume_input"] == source["run_config"]["resume_input"]
    async with sessionmaker() as session:
        stored = await db.get_conversation(session, uuid.UUID(source["id"]))
        assert stored.resume_markdown == reviewed


@pytest.mark.parametrize("failure", ["changed_pdf", "blank_text", "oversized_text", "invalid_json"])
async def test_invalid_review_never_reaches_planner_or_creates_row(
    client_and_sessionmaker, monkeypatch, failure
):
    client, sessionmaker = client_and_sessionmaker

    async def unexpected_planner(*args, **kwargs):
        pytest.fail("Invalid reviewed input must be rejected before planning")

    monkeypatch.setattr(routes, "run_planner", unexpected_planner)
    review = {"pdf_sha256": sha256(_pdf_bytes()).hexdigest(), "text": "Reviewed CV"}
    status = 400
    if failure == "changed_pdf":
        review["pdf_sha256"] = sha256(b"different PDF").hexdigest()
        status = 409
    elif failure == "blank_text":
        review["text"] = " \n"
    elif failure == "oversized_text":
        review["text"] = "x" * (MAX_RESUME_CHARS + 1)
        status = 413
    raw = "not JSON" if failure == "invalid_json" else json.dumps(review)
    response = await client.post("/api/interviews", **_upload(resume_review=raw))
    assert response.status_code == status
    async with sessionmaker() as session:
        assert (await db.list_conversations(session, limit=20, offset=0))[1] == 0


@pytest.mark.parametrize("reviewed", [False, True])
async def test_corrupt_pdf_rejected_even_with_matching_reviewed_text(
    client_and_sessionmaker, monkeypatch, reviewed
):
    client, sessionmaker = client_and_sessionmaker

    async def forbidden(*args, **kwargs):
        pytest.fail("A corrupt PDF must not reach the planner")

    monkeypatch.setattr(routes, "run_planner", forbidden)
    data = b"%PDF-1.4\ncorrupt document with no catalog"
    payload = _upload()
    payload["files"] = {"resume": ("cv.pdf", data, "application/pdf")}
    if reviewed:
        payload["data"]["resume_review"] = json.dumps(
            {"pdf_sha256": sha256(data).hexdigest(), "text": "Edited valid-looking text"}
        )
    response = await client.post("/api/interviews", **payload)
    assert response.status_code == 400 and "readable PDF" in response.json()["detail"]
    async with sessionmaker() as session:
        assert (await db.list_conversations(session, limit=20, offset=0))[1] == 0


async def test_repeat_inherits_limits_and_recalculates_changed_profile(
    client_and_sessionmaker, monkeypatch
):
    client, _ = client_and_sessionmaker
    seen = []

    async def planner(settings, resume_markdown, job_offer, **kwargs):
        seen.append(kwargs)
        spec = _plan().milestones[0]
        return _plan().model_copy(
            update={
                "milestones": [
                    spec.model_copy(update={"title": f"M{i}"})
                    for i in range(kwargs["question_limit"])
                ]
            }
        )

    monkeypatch.setattr(routes, "run_planner", planner)
    source = (
        await client.post(
            "/api/interviews",
            **_upload(
                seniority="senior",
                interviewer=_interviewer(question_limit=2, followup_limit=0, max_minutes=7),
            ),
        )
    ).json()
    inherited = (await client.post(f"/api/interviews/{source['id']}/repeat")).json()
    assert (inherited["question_limit"], inherited["followup_limit"], inherited["max_minutes"]) == (
        2,
        0,
        7,
    )
    assert seen[-1]["question_limit"] == 2
    assert seen[-1]["max_minutes"] == 7
    changed = (
        await client.post(
            f"/api/interviews/{source['id']}/repeat", json={"interview_length": "deep"}
        )
    ).json()
    assert seen[-1]["question_limit"] == 8
    assert changed["question_limit"] == 8
    assert changed["followup_limit"] == 2
    assert changed["max_minutes"] == min(25, settings.interview_max_minutes)
    overridden = (
        await client.post(
            f"/api/interviews/{source['id']}/repeat",
            json={
                "interview_length": "deep",
                "question_limit": 1,
                "followup_limit": 0,
                "max_minutes": 4,
            },
        )
    ).json()
    assert (
        overridden["question_limit"],
        overridden["followup_limit"],
        overridden["max_minutes"],
    ) == (1, 0, 4)
    assert len(overridden["milestones"]) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("question_limit", 0),
        ("question_limit", 13),
        ("question_limit", True),
        ("question_limit", "3"),
        ("question_limit", 2.5),
        ("followup_limit", -1),
        ("followup_limit", 3),
        ("max_minutes", 0),
        ("max_minutes", 26),
        ("max_minutes", False),
    ],
)
async def test_budget_contract_rejects_invalid_values_on_create_and_repeat(
    client_and_sessionmaker, field, value
):
    client, _ = client_and_sessionmaker
    created = await client.post(
        "/api/interviews", **_upload(interviewer=_interviewer(**{field: value}))
    )
    assert created.status_code == 400
    source = (await client.post("/api/interviews", **_upload())).json()
    repeated = await client.post(f"/api/interviews/{source['id']}/repeat", json={field: value})
    assert repeated.status_code == 422


@pytest.mark.parametrize("field", ["resume", "job_offer"])
async def test_rejects_oversized_source_before_planning(
    client_and_sessionmaker, monkeypatch, field
):
    client, sessionmaker = client_and_sessionmaker
    planner_called = False

    async def planner(*args, **kwargs):
        nonlocal planner_called
        planner_called = True
        return _plan()

    monkeypatch.setattr(routes, "run_planner", planner)
    offer = "offer"
    if field == "resume":
        monkeypatch.setattr(
            resume_ingestion, "pdf_to_markdown", lambda *args: "r" * (MAX_RESUME_CHARS + 1)
        )
    else:
        offer = "o" * (MAX_JOB_OFFER_CHARS + 1)
    response = await client.post("/api/interviews", **_upload(offer))
    assert response.status_code == 413
    assert "character limit" in response.json()["detail"]
    assert not planner_called
    async with sessionmaker() as session:
        assert (await db.list_conversations(session, limit=20, offset=0))[1] == 0


async def test_rejects_oversized_source_on_token_and_repeat(client_and_sessionmaker):
    client, sessionmaker = client_and_sessionmaker
    conversation_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            db.Conversation(
                id=conversation_id,
                owner_id=DEVELOPER.id,
                status="planned",
                job_offer="offer",
                resume_markdown="r" * (MAX_RESUME_CHARS + 1),
                run_config={
                    "models": {"interviewer": {"model": "gpt-6-astra", "reasoning_effort": "low"}}
                },
            )
        )
        await session.commit()
    assert (await client.get(f"/api/interviews/{conversation_id}/token")).status_code == 413
    assert (await client.post(f"/api/interviews/{conversation_id}/repeat")).status_code == 413
    async with sessionmaker() as session:
        assert (await db.get_conversation(session, conversation_id)).status == "planned"
        assert (await db.list_conversations(session, limit=20, offset=0))[1] == 1


async def _seed_finished_interview(sessionmaker, *, integrity="complete") -> uuid.UUID:
    """A completed interview with a transcript, ready to evaluate."""
    conversation_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            db.Conversation(
                id=conversation_id,
                owner_id=DEVELOPER.id,
                status="completed",
                ended_reason="plan_complete",
                job_offer="offer",
                resume_markdown="# Resume",
                plan={"language": "en", "summary": "s", "focus_areas": []},
            )
        )
        session.add(
            db.Milestone(
                id=uuid.uuid4(),
                conversation_id=conversation_id,
                position=0,
                title="M0",
                description="Probe it.",
                completed=True,
            )
        )
        await session.commit()
        for seq, (role, content) in enumerate(
            [("assistant", "Tell me about X."), ("user", "I built X.")]
        ):
            await db.insert_message(
                session,
                conversation_id,
                role,
                content,
                seq=seq,
                metrics={"stt_confirmed": True} if role == "user" else None,
            )
        # Every finished interview is sealed after its confirmed transcript.
        row = await db.get_conversation(session, conversation_id)
        row.transcript_sealed_at = datetime.now(UTC)
        row.transcript_integrity = integrity
        await ensure_seal(session, row)
        await session.commit()
    return conversation_id


async def _settled(client: AsyncClient, conversation_id: uuid.UUID, timeout: float = 5.0) -> dict:
    """Poll the row the way the UI does until the background run has landed."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        body = (await client.get(f"/api/interviews/{conversation_id}")).json()
        if body["status"] not in ("completed", "evaluating"):
            return body
        assert asyncio.get_running_loop().time() < deadline, f"still {body['status']}"
        await asyncio.sleep(0.01)


# ---- /settings ---------------------------------------------------------------


async def test_get_settings_returns_defaults_and_catalog(client_and_sessionmaker):
    client, _ = client_and_sessionmaker
    res = await client.get("/api/settings")
    assert res.status_code == 200
    body = res.json()
    assert body["agent_name"] == "Emma"
    assert body["language"] == "en"
    assert body["voice"] == "en_female"
    assert body["persona"] is None
    # Two curated voices per language: one feminine, one masculine.
    for language in ("en", "es"):
        genders = {v["gender"] for v in body["voices"][language]}
        assert genders == {"female", "male"}


async def test_put_settings_persists_and_echoes(client_and_sessionmaker):
    client, _ = client_and_sessionmaker
    payload = {
        "agent_name": "Sam",
        "language": "es",
        "voice": "es_male",
        "persona": "una manager exigente",
        "custom_instructions": "",
    }
    res = await client.put("/api/settings", json=payload)
    assert res.status_code == 200
    body = res.json()
    assert body["agent_name"] == "Sam"
    assert body["voice"] == "es_male"
    assert body["custom_instructions"] is None  # "" normalized to NULL

    # Persisted: a fresh GET returns the same values.
    body = (await client.get("/api/settings")).json()
    assert body["language"] == "es"
    assert body["persona"] == "una manager exigente"


async def test_put_settings_rejects_voice_language_mismatch(client_and_sessionmaker):
    client, _ = client_and_sessionmaker
    res = await client.put(
        "/api/settings", json={"agent_name": "Alex", "language": "en", "voice": "es_male"}
    )
    assert res.status_code == 422

    res = await client.put(
        "/api/settings", json={"agent_name": "Alex", "language": "fr", "voice": "en_female"}
    )
    assert res.status_code == 422


# ---- /interviews ------------------------------------------------------------


async def test_create_interview_happy_path(client_and_sessionmaker):
    client, sessionmaker = client_and_sessionmaker
    res = await client.post("/api/interviews", **_upload())
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "planned"
    assert len(body["milestones"]) == 4
    assert body["plan"]["language"] == "en"  # injected from the settings
    assert "milestones" not in body["plan"]  # stored separately
    assert "planner" in body["token_usage"]

    async with sessionmaker() as session:
        row = await db.get_conversation(session, uuid.UUID(body["id"]))
        assert row is not None
        prompt = system_prompt(row)
        assert "# Resume\nPython dev." in prompt
        assert body["job_offer"] in prompt


async def test_create_interview_snapshots_settings(client_and_sessionmaker):
    client, sessionmaker = client_and_sessionmaker
    res = await client.put(
        "/api/settings",
        json={
            "agent_name": "Sam",
            "language": "es",
            "voice": "es_female",
            "persona": "una manager exigente",
            "custom_instructions": "máximo 5 preguntas",
        },
    )
    assert res.status_code == 200

    job_offer = f"offer-{uuid.uuid4()}"  # unique marker to find the row
    res = await client.post("/api/interviews", **_upload(job_offer))
    assert res.status_code == 200
    assert res.json()["plan"]["language"] == "es"

    async with sessionmaker() as session:
        row = await session.scalar(
            select(db.Conversation).where(db.Conversation.job_offer == job_offer)
        )
    assert row.persona == "una manager exigente"
    assert row.custom_instructions == "máximo 5 preguntas"
    snapshot = row.agent_settings
    assert snapshot["agent_name"] == "Sam"
    assert snapshot["language"] == "es"
    assert snapshot["voice"] == "es_female"
    assert snapshot["tts_model"] == "cartesia/sonic-3.6-2026-08-27"
    assert snapshot["tts_voice"]  # resolved, not just the catalog key


async def test_create_interview_rejects_non_pdf(client_and_sessionmaker):
    client, _ = client_and_sessionmaker
    res = await client.post(
        "/api/interviews",
        files={"resume": ("cv.docx", b"bytes", "application/msword")},
        data={"job_offer": "offer"},
    )
    assert res.status_code == 400


async def test_create_interview_planning_failure_marks_error(client_and_sessionmaker, monkeypatch):
    client, sessionmaker = client_and_sessionmaker

    async def _boom(*args, **kwargs):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(routes, "run_planner", _boom)
    job_offer = f"offer-{uuid.uuid4()}"  # unique marker to find the row
    res = await client.post("/api/interviews", **_upload(job_offer))
    assert res.status_code == 500

    # The row survives with a visible error status (not stuck in "created").
    async with sessionmaker() as session:
        status = await session.scalar(
            text("SELECT status FROM conversations WHERE job_offer = :o"),
            {"o": job_offer},
        )
    assert status == "error"


async def test_a_failed_planner_run_still_books_its_tokens(client_and_sessionmaker, monkeypatch):
    client, sessionmaker = client_and_sessionmaker

    async def _spent_then_failed(*args, usage_callback, **kwargs):
        # The callback fires per retry attempt: the model was called (and
        # billed) before the structured output failed to validate.
        usage_callback.usage_metadata = {
            "gpt": {"input_tokens": 5, "output_tokens": 1, "total_tokens": 6}
        }
        raise RuntimeError("could not parse the plan")

    monkeypatch.setattr(routes, "run_planner", _spent_then_failed)
    job_offer = f"offer-{uuid.uuid4()}"
    assert (await client.post("/api/interviews", **_upload(job_offer))).status_code == 500
    async with sessionmaker() as session:
        row = await session.scalar(
            select(db.Conversation).where(db.Conversation.job_offer == job_offer)
        )
    assert row.status == "error"
    assert row.token_usage["planner"]["total_tokens"] == 6


async def test_get_interview_404(client_and_sessionmaker):
    client, _ = client_and_sessionmaker
    res = await client.get(f"/api/interviews/{uuid.uuid4()}")
    assert res.status_code == 404


# ---- /evaluate --------------------------------------------------------------
# The endpoint claims the row and answers 202; the run itself lands in the
# background (server.evaluations), so these poll the row afterwards the way
# the UI does.


async def test_evaluate_answers_202_and_the_result_lands_in_the_background(
    client_and_sessionmaker, monkeypatch
):
    client, sessionmaker = client_and_sessionmaker
    monkeypatch.setattr(evaluations, "run_evaluator", _fake_evaluator)
    conversation_id = await _seed_finished_interview(sessionmaker)

    res = await client.post(f"/api/interviews/{conversation_id}/evaluate")
    assert res.status_code == 202
    assert res.json()["status"] == "evaluating"
    assert res.json()["evaluation"] is None

    body = await _settled(client, conversation_id)
    assert body["status"] == "evaluated"
    assert body["evaluation"]["hired"] is True
    assert body["evaluation"]["score"] == 82
    assert body["evaluation"]["ended_by"] == "plan_complete"

    # Re-evaluation upserts instead of racing delete+insert into a 500.
    res2 = await client.post(f"/api/interviews/{conversation_id}/evaluate")
    assert res2.status_code == 202
    assert (await _settled(client, conversation_id))["evaluation"]["score"] == 82


async def test_a_second_evaluate_while_one_runs_starts_nothing(
    client_and_sessionmaker, monkeypatch
):
    # The worker's auto-trigger and a Retry from the browser can race here;
    # the atomic claim lets exactly one of them start a run.
    client, sessionmaker = client_and_sessionmaker
    release = asyncio.Event()
    calls = 0

    async def _slow_evaluator(settings, **kwargs):
        nonlocal calls
        calls += 1
        await release.wait()
        return await _fake_evaluator(settings, **kwargs)

    monkeypatch.setattr(evaluations, "run_evaluator", _slow_evaluator)
    conversation_id = await _seed_finished_interview(sessionmaker)

    first = await client.post(f"/api/interviews/{conversation_id}/evaluate")
    assert first.status_code == 202
    for _ in range(500):  # let the run reach the evaluator
        if calls:
            break
        await asyncio.sleep(0.01)
    assert calls == 1

    second = await client.post(f"/api/interviews/{conversation_id}/evaluate")
    assert second.status_code == 202
    assert second.json()["status"] == "evaluating"
    assert calls == 1

    release.set()
    assert (await _settled(client, conversation_id))["status"] == "evaluated"
    assert calls == 1


async def test_a_run_whose_process_died_is_claimed_again(client_and_sessionmaker, monkeypatch):
    # "evaluating" with a heartbeat that stopped is the orphan of a restart:
    # a Retry must be allowed to take it over. One still beating is not.
    client, sessionmaker = client_and_sessionmaker
    calls = 0

    async def _counting_evaluator(settings, **kwargs):
        nonlocal calls
        calls += 1
        return await _fake_evaluator(settings, **kwargs)

    monkeypatch.setattr(evaluations, "run_evaluator", _counting_evaluator)
    conversation_id = await _seed_finished_interview(sessionmaker)
    async with sessionmaker() as session:
        await session.execute(
            update(db.Conversation)
            .where(db.Conversation.id == conversation_id)
            .values(status="evaluating")
        )
        await session.commit()

    # Fresh: somebody is on it.
    res = await client.post(f"/api/interviews/{conversation_id}/evaluate")
    assert res.status_code == 202
    assert res.json()["status"] == "evaluating"
    assert calls == 0

    stale = datetime.now(UTC) - evaluations.STALE_AFTER - timedelta(minutes=1)
    async with sessionmaker() as session:
        await session.execute(
            update(db.Conversation)
            .where(db.Conversation.id == conversation_id)
            .values(updated_at=stale)
        )
        await session.commit()

    res = await client.post(f"/api/interviews/{conversation_id}/evaluate")
    assert res.status_code == 202
    assert (await _settled(client, conversation_id))["status"] == "evaluated"
    assert calls == 1


async def test_a_live_run_keeps_its_heartbeat_moving(client_and_sessionmaker, monkeypatch):
    # updated_at is what tells a live run from a dead one — for the claim
    # above and for the UI's "taking too long" clock alike.
    client, sessionmaker = client_and_sessionmaker
    monkeypatch.setattr(evaluations, "HEARTBEAT_SECONDS", 0.02)
    release = asyncio.Event()

    async def _slow_evaluator(settings, **kwargs):
        await release.wait()
        return await _fake_evaluator(settings, **kwargs)

    monkeypatch.setattr(evaluations, "run_evaluator", _slow_evaluator)
    conversation_id = await _seed_finished_interview(sessionmaker)

    claimed_at = (await client.post(f"/api/interviews/{conversation_id}/evaluate")).json()
    # Bounded poll: the run's setup holds the row briefly before heartbeats.
    for _ in range(100):
        await asyncio.sleep(0.02)
        later = (await client.get(f"/api/interviews/{conversation_id}")).json()
        if later["updated_at"] != claimed_at["updated_at"]:
            break
    assert later["status"] == "evaluating"
    assert datetime.fromisoformat(later["updated_at"]) > datetime.fromisoformat(
        claimed_at["updated_at"]
    )

    release.set()
    assert (await _settled(client, conversation_id))["status"] == "evaluated"


async def test_shutdown_leaves_the_request_recoverable_after_its_lease(
    client_and_sessionmaker, monkeypatch
):
    _, sessionmaker = client_and_sessionmaker
    reached = asyncio.Event()

    async def _hanging_evaluator(settings, **kwargs):
        reached.set()
        await asyncio.Event().wait()  # never returns on its own

    monkeypatch.setattr(evaluations, "run_evaluator", _hanging_evaluator)
    conversation_id = await _seed_finished_interview(sessionmaker)
    runner = evaluations.EvaluationRunner(sessionmaker)
    async with sessionmaker() as session:
        assert await db.claim_evaluation(session, conversation_id, evaluations.STALE_AFTER)
    runner.start(conversation_id)
    await asyncio.wait_for(reached.wait(), 5)
    assert runner.running == 1

    await runner.shutdown()
    assert runner.running == 0
    async with sessionmaker() as session:
        conversation = await db.get_conversation(session, conversation_id)
        # The UI keeps polling instead of showing a failure that is not final.
        assert conversation.status == "evaluating"
        request_id = conversation.evaluation_request_id
        run = await session.get(db.EvaluationRun, conversation.evaluation_claim_id)
        run.lease_until = await session.scalar(select(func.clock_timestamp())) - timedelta(
            seconds=1
        )
        await session.commit()
    async with sessionmaker() as session:
        attempt = await db.claim_evaluation(
            session, conversation_id, evaluations.STALE_AFTER, recover=True
        )
        assert (await session.get(db.EvaluationRun, attempt)).request_id == request_id


async def test_evaluate_without_transcript_is_insufficient(client_and_sessionmaker, monkeypatch):
    client, sessionmaker = client_and_sessionmaker
    monkeypatch.setattr(evaluations, "run_evaluator", _fake_evaluator)
    conversation_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            db.Conversation(
                id=conversation_id,
                owner_id=DEVELOPER.id,
                status="completed",
                job_offer="o",
                resume_markdown="r",
                transcript_sealed_at=datetime.now(UTC),
            )
        )
        await session.commit()

    res = await client.post(f"/api/interviews/{conversation_id}/evaluate")
    assert res.status_code == 202
    result = (await _settled(client, conversation_id))["evaluation"]
    assert result["evaluation_status"] == "insufficient"
    assert result["score"] is None
    assert result["hired"] is None


async def test_interview_without_stt_confirmation_is_insufficient_without_model(
    client_and_sessionmaker, monkeypatch
):
    from interview_agent.interview import evaluator

    client, sessionmaker = client_and_sessionmaker
    monkeypatch.setattr(evaluator, "build_chat_model", pytest.fail)
    conversation_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            db.Conversation(
                id=conversation_id,
                owner_id=DEVELOPER.id,
                status="completed",
                job_offer="offer",
                resume_markdown="# Resume",
                plan={"language": "en"},
            )
        )
        await session.commit()
        await db.insert_message(session, conversation_id, "assistant", "Tell me about X.", seq=0)
        # An unconfirmed STT interim is never candidate evidence.
        await db.insert_message(
            session, conversation_id, "user", "I built X.", seq=1, metrics={"stt_confirmed": False}
        )
        row = await db.get_conversation(session, conversation_id)
        row.transcript_sealed_at = datetime.now(UTC)
        await session.commit()

    assert (await client.post(f"/api/interviews/{conversation_id}/evaluate")).status_code == 202
    result = (await _settled(client, conversation_id))["evaluation"]
    assert result["evaluation_status"] == "insufficient"
    assert result["score"] is None and result["hired"] is None


async def test_evaluate_refuses_a_live_interview(client_and_sessionmaker, monkeypatch):
    # Evaluating mid-interview would score half a transcript. The worker only
    # POSTs here after marking "completed".
    client, sessionmaker = client_and_sessionmaker
    monkeypatch.setattr(evaluations, "run_evaluator", _fake_evaluator)
    conversation_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            db.Conversation(
                id=conversation_id,
                owner_id=DEVELOPER.id,
                status="interviewing",
                job_offer="o",
                resume_markdown="r",
                max_minutes=8,
                started_at=datetime.now(UTC),
            )
        )
        await session.commit()
        await db.insert_message(session, conversation_id, "user", "mid-answer", seq=0)

    res = await client.post(f"/api/interviews/{conversation_id}/evaluate")
    assert res.status_code == 409
    assert "in progress" in res.json()["detail"]


async def test_an_interrupted_interview_is_evaluable_once_its_window_closes(
    client_and_sessionmaker, monkeypatch
):
    # A crashed worker leaves "interviewing" behind. /token stops offering a
    # rejoin after the reconnect window, and /evaluate used to refuse the row
    # forever — a full transcript nobody could score.
    client, sessionmaker = client_and_sessionmaker
    monkeypatch.setattr(evaluations, "run_evaluator", _fake_evaluator)
    conversation_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            db.Conversation(
                id=conversation_id,
                owner_id=DEVELOPER.id,
                status="interviewing",
                job_offer="o",
                resume_markdown="r",
                max_minutes=8,
                started_at=datetime.now(UTC),
            )
        )
        await session.commit()
        await db.insert_message(session, conversation_id, "assistant", "Tell me about X.", seq=0)
        await db.insert_message(
            session, conversation_id, "user", "I built X.", seq=1, metrics={"stt_confirmed": True}
        )

    # Inside the window it is a live interview: rejoinable, not evaluable.
    body = (await client.get(f"/api/interviews/{conversation_id}")).json()
    assert body["can_start"] is True
    assert datetime.fromisoformat(body["reconnect_until"]) > datetime.now(UTC)
    assert (await client.post(f"/api/interviews/{conversation_id}/evaluate")).status_code == 409

    async with sessionmaker() as session:
        await session.execute(
            update(db.Conversation)
            .where(db.Conversation.id == conversation_id)
            .values(
                started_at=datetime.now(UTC) - timedelta(minutes=9),
                worker_owner_id=uuid.uuid4(),
                worker_epoch=1,
                worker_lease_until=datetime.now(UTC) - timedelta(seconds=31),
            )
        )
        await session.commit()

    body = (await client.get(f"/api/interviews/{conversation_id}")).json()
    assert body["can_start"] is False
    assert body["reconnect_until"] is None and body["status"] == "completed"

    res = await client.post(f"/api/interviews/{conversation_id}/evaluate")
    assert res.status_code == 202
    assert res.json()["status"] == "evaluating"
    assert res.json()["ended_reason"] == "worker_lost"
    body = await _settled(client, conversation_id)
    assert body["status"] == "evaluated"
    assert body["evaluation"]["ended_by"] == "worker_lost"
    assert body["can_start"] is False
    assert body["reconnect_until"] is None


async def test_lifecycle_fields_follow_the_status(client_and_sessionmaker):
    client, sessionmaker = client_and_sessionmaker
    ids: dict[str, uuid.UUID] = {}
    async with sessionmaker() as session:
        for status in ("planned", "completed", "evaluating", "evaluated"):
            ids[status] = uuid.uuid4()
            session.add(
                db.Conversation(
                    id=ids[status],
                    owner_id=DEVELOPER.id,
                    status=status,
                    job_offer="o",
                    resume_markdown="r",
                )
            )
        await session.commit()

    for status, expected in (
        ("planned", True),
        ("completed", False),
        ("evaluating", False),
        ("evaluated", False),
    ):
        body = (await client.get(f"/api/interviews/{ids[status]}")).json()
        assert (body["can_start"], body["reconnect_until"]) == (expected, None), status

    # The history rows carry the same two fields, and the new status filters.
    page = (await client.get("/api/interviews?status=planned")).json()
    row = next(item for item in page["items"] if item["id"] == str(ids["planned"]))
    assert row["can_start"] is True
    assert row["reconnect_until"] is None
    page = (await client.get("/api/interviews?status=evaluating")).json()
    assert str(ids["evaluating"]) in {item["id"] for item in page["items"]}


async def test_evaluate_failure_sets_status_and_retry_recovers(
    client_and_sessionmaker, monkeypatch
):
    client, sessionmaker = client_and_sessionmaker
    conversation_id = await _seed_finished_interview(sessionmaker)

    async def _boom(*args, **kwargs):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(evaluations, "run_evaluator", _boom)
    assert (await client.post(f"/api/interviews/{conversation_id}/evaluate")).status_code == 202
    assert (await _settled(client, conversation_id))["status"] == "evaluation_failed"

    # The endpoint stays re-invocable: a later retry succeeds.
    monkeypatch.setattr(evaluations, "run_evaluator", _fake_evaluator)
    assert (await client.post(f"/api/interviews/{conversation_id}/evaluate")).status_code == 202
    assert (await _settled(client, conversation_id))["status"] == "evaluated"


async def test_a_failed_evaluation_still_books_its_tokens(client_and_sessionmaker, monkeypatch):
    client, sessionmaker = client_and_sessionmaker
    conversation_id = await _seed_finished_interview(sessionmaker)

    async def _spent_then_failed(settings, *, usage_callback, **kwargs):
        usage_callback.usage_metadata = {
            "gpt": {"input_tokens": 5, "output_tokens": 1, "total_tokens": 6}
        }
        raise RuntimeError("could not parse the verdict")

    monkeypatch.setattr(evaluations, "run_evaluator", _spent_then_failed)
    assert (await client.post(f"/api/interviews/{conversation_id}/evaluate")).status_code == 202
    body = await _settled(client, conversation_id)
    assert body["status"] == "evaluation_failed"
    assert body["token_usage"]["evaluator"]["total_tokens"] == 6

    # The successful retry accumulates on top: both runs were paid for.
    async def _spent_and_succeeded(settings, *, usage_callback, **kwargs):
        usage_callback.usage_metadata = {
            "gpt": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}
        }
        return await _fake_evaluator(settings, **kwargs)

    monkeypatch.setattr(evaluations, "run_evaluator", _spent_and_succeeded)
    assert (await client.post(f"/api/interviews/{conversation_id}/evaluate")).status_code == 202
    body = await _settled(client, conversation_id)
    assert body["status"] == "evaluated"
    assert body["token_usage"]["evaluator"]["total_tokens"] == 20


# ---- DB helpers -------------------------------------------------------------


async def test_add_token_usage_merges_components(client_and_sessionmaker):
    _, sessionmaker = client_and_sessionmaker
    conversation_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            db.Conversation(
                id=conversation_id,
                owner_id=DEVELOPER.id,
                status="created",
                job_offer="o",
                resume_markdown="r",
            )
        )
        await session.commit()
        await db.add_token_usage(
            session,
            conversation_id,
            "interviewer",
            {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
        )
        await db.add_token_usage(
            session,
            conversation_id,
            "interviewer",
            {"input_tokens": 50, "output_tokens": 5, "total_tokens": 55},
        )
        await db.add_token_usage(
            session,
            conversation_id,
            "evaluator",
            {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
        )
        conversation = await db.get_conversation(session, conversation_id)
    assert conversation.token_usage == {
        "interviewer": {"input_tokens": 150, "output_tokens": 15, "total_tokens": 165},
        "evaluator": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
    }


async def test_token_allows_a_fresh_reconnect(client_and_sessionmaker, monkeypatch):
    # A crash never marks the row completed, so "interviewing" is how a
    # resumable interview looks. Recent ones must still get a token.
    client, sessionmaker = client_and_sessionmaker
    # Minting the JWT needs real credentials; settings default to "" and CI
    # has no .env, so supply throwaway ones rather than depend on the
    # environment (which is what made this pass locally and fail in CI).
    monkeypatch.setattr(settings, "livekit_api_key", "devkey")
    monkeypatch.setattr(settings, "livekit_api_secret", "devsecret" * 4)
    monkeypatch.setattr(settings, "livekit_url", "wss://example.livekit.cloud")
    conversation_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            db.Conversation(
                id=conversation_id,
                owner_id=DEVELOPER.id,
                status="interviewing",
                job_offer="o",
                resume_markdown="r",
                max_minutes=8,
                started_at=datetime.now(UTC),
                run_config={
                    "models": {"interviewer": {"model": "gpt-6-astra", "reasoning_effort": "low"}},
                    "stt_model": "assemblyai/universal-3-6-pro",
                },
                agent_settings={
                    "language": "es",
                    "tts_model": "cartesia/sonic-3.6",
                    "tts_voice": "voice",
                },
            )
        )
        await session.commit()

    res = await client.get(f"/api/interviews/{conversation_id}/token")
    assert res.status_code == 200
    assert res.json()["room"] == f"interview-{conversation_id}"
    verified = routes.api.TokenVerifier(
        settings.livekit_api_key,
        settings.livekit_api_secret,
        leeway=timedelta(0),
    ).verify(res.json()["token"])
    assert verified.identity == "candidate"
    claims = jwt.decode(
        res.json()["token"],
        settings.livekit_api_secret,
        algorithms=["HS256"],
        issuer=settings.livekit_api_key,
        options={"require": ["exp", "nbf"]},
    )
    assert 6 * 60 * 60 <= claims["exp"] - claims["nbf"] <= 6 * 60 * 60 + 1
    assert claims["exp"] - claims["nbf"] > settings.interview_max_minutes * 60 + 45


async def test_token_refuses_a_stale_reconnect(client_and_sessionmaker):
    # An orphaned "interviewing" row lives until the retention purge. Without a
    # bound, a candidate could rejoin days later — and since the worker charges
    # the elapsed time against the cap, they would be greeted and wrapped up in
    # the same breath.
    client, sessionmaker = client_and_sessionmaker
    conversation_id = uuid.uuid4()
    stale = datetime.now(UTC) - timedelta(minutes=settings.interview_max_minutes + 30)
    async with sessionmaker() as session:
        session.add(
            db.Conversation(
                id=conversation_id,
                owner_id=DEVELOPER.id,
                status="interviewing",
                job_offer="o",
                resume_markdown="r",
            )
        )
        await session.commit()
        # onupdate=func.now() fires on any UPDATE, so age the row directly.
        await session.execute(
            update(db.Conversation)
            .where(db.Conversation.id == conversation_id)
            .values(updated_at=stale)
        )
        await session.commit()

    res = await client.get(f"/api/interviews/{conversation_id}/token")
    assert res.status_code == 409


async def test_get_messages_orders_by_seq_not_id(client_and_sessionmaker):
    _, sessionmaker = client_and_sessionmaker
    conversation_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            db.Conversation(
                id=conversation_id,
                owner_id=DEVELOPER.id,
                status="created",
                job_offer="o",
                resume_markdown="r",
            )
        )
        await session.commit()
        # Committed out of order (higher seq first), as racing persist tasks
        # can do — read order must follow seq regardless.
        await db.insert_message(session, conversation_id, "assistant", "second", seq=1)
        await db.insert_message(session, conversation_id, "user", "first", seq=0)
        messages = await db.get_messages(session, conversation_id)
    assert [m.content for m in messages] == ["first", "second"]


async def test_resumed_job_appends_instead_of_interleaving(client_and_sessionmaker):
    # A worker crash mid-interview leaves the row "interviewing", so a reload
    # dispatches a second job. It must continue the transcript, not renumber
    # from 0 — which order_by(seq, id) would shuffle into the first half.
    _, sessionmaker = client_and_sessionmaker
    conversation_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            db.Conversation(
                id=conversation_id,
                owner_id=DEVELOPER.id,
                status="interviewing",
                job_offer="o",
                resume_markdown="r",
            )
        )
        await session.commit()
        # Order is allocated under the conversation lock, so a second job
        # continues after the first one's messages instead of renumbering.
        for role, content in [
            ("assistant", "greeting"),
            ("user", "answer 1"),
            ("assistant", "question 2"),
            ("assistant", "welcome back"),
            ("user", "answer 2"),
        ]:
            await db.insert_message(session, conversation_id, role, content)
        messages = await db.get_messages(session, conversation_id)

    assert [m.content for m in messages] == [
        "greeting",
        "answer 1",
        "question 2",
        "welcome back",
        "answer 2",
    ]
    assert [m.seq for m in messages] == [0, 1, 2, 3, 4]


# ---- /healthz ---------------------------------------------------------------


# ---- per-interview interviewer ----------------------------------------------


async def test_create_interview_takes_a_per_interview_interviewer(client_and_sessionmaker):
    client, sessionmaker = client_and_sessionmaker
    # Global settings say English/Emma; this interview asks for Spanish/Sam.
    await client.put(
        "/api/settings",
        json={
            "agent_name": "Emma",
            "language": "en",
            "voice": "en_female",
            "persona": "a global persona",
        },
    )
    res = await client.post(
        "/api/interviews",
        **_upload(
            interviewer=_interviewer(
                agent_name="Sam",
                language="es",
                voice="es_male",
                persona="una manager exigente",
                custom_instructions="Pregunta por Kubernetes.",
            )
        ),
    )
    assert res.status_code == 200
    body = res.json()
    assert body["interviewer"] == {
        "agent_name": "Sam",
        "language": "es",
        "voice": "es_male",
    }
    assert body["plan"]["language"] == "es"

    async with sessionmaker() as session:
        row = await db.get_conversation(session, uuid.UUID(body["id"]))
        assert row is not None
        # The voice resolved to a concrete TTS pair for the worker.
        assert row.agent_settings["tts_voice"] == VOICES["es_male"]["tts_voice"]
        assert row.persona == "una manager exigente"
        assert row.custom_instructions == "Pregunta por Kubernetes."

    # The global settings are untouched by the per-interview choice.
    assert (await client.get("/api/settings")).json()["language"] == "en"


async def test_create_interview_without_an_interviewer_uses_the_settings(
    client_and_sessionmaker,
):
    client, sessionmaker = client_and_sessionmaker
    await client.put(
        "/api/settings",
        json={
            "agent_name": "Emma",
            "language": "es",
            "voice": "es_female",
            "persona": "una manager exigente",
        },
    )
    body = (await client.post("/api/interviews", **_upload())).json()
    assert body["interviewer"]["language"] == "es"
    assert body["interviewer"]["voice"] == "es_female"
    async with sessionmaker() as session:
        row = await db.get_conversation(session, uuid.UUID(body["id"]))
        assert row is not None and row.persona == "una manager exigente"


async def test_an_empty_persona_clears_it_for_this_interview_only(client_and_sessionmaker):
    client, sessionmaker = client_and_sessionmaker
    await client.put(
        "/api/settings",
        json={
            "agent_name": "Emma",
            "language": "en",
            "voice": "en_female",
            "persona": "a global persona",
        },
    )
    # "" is a real answer — run this one WITHOUT a persona — while omitting
    # the field inherits. Both must not touch the stored settings.
    body = (
        await client.post("/api/interviews", **_upload(interviewer=_interviewer(persona="")))
    ).json()
    async with sessionmaker() as session:
        row = await db.get_conversation(session, uuid.UUID(body["id"]))
        assert row is not None and row.persona is None
    assert (await client.get("/api/settings")).json()["persona"] == "a global persona"


async def test_overriding_the_language_alone_picks_a_voice_for_it(client_and_sessionmaker):
    client, sessionmaker = client_and_sessionmaker
    # Global voice speaks English; this interview only asks for Spanish.
    await client.put(
        "/api/settings", json={"agent_name": "Emma", "language": "en", "voice": "en_female"}
    )
    res = await client.post("/api/interviews", **_upload(interviewer=_interviewer(language="es")))
    assert res.status_code == 200, res.json()
    body = res.json()
    # A Spanish voice of the same gender as the one it replaces.
    assert body["interviewer"] == {"agent_name": "Emma", "language": "es", "voice": "es_female"}
    assert body["plan"]["language"] == "es"
    async with sessionmaker() as session:
        row = await db.get_conversation(session, uuid.UUID(body["id"]))
        assert row is not None
        assert row.agent_settings["tts_voice"] == VOICES["es_female"]["tts_voice"]

    # Same with a male global voice; and an explicit mismatch still fails.
    await client.put(
        "/api/settings", json={"agent_name": "Blake", "language": "en", "voice": "en_male"}
    )
    body = (
        await client.post("/api/interviews", **_upload(interviewer=_interviewer(language="es")))
    ).json()
    assert body["interviewer"]["voice"] == "es_male"
    res = await client.post(
        "/api/interviews", **_upload(interviewer=_interviewer(language="es", voice="en_male"))
    )
    assert res.status_code == 400


async def test_create_interview_rejects_an_impossible_voice(client_and_sessionmaker):
    client, _ = client_and_sessionmaker
    res = await client.post(
        "/api/interviews", **_upload(interviewer=_interviewer(language="en", voice="es_male"))
    )
    assert res.status_code == 400
    assert "not available" in res.json()["detail"]

    res = await client.post("/api/interviews", **_upload(interviewer=_interviewer(voice="klingon")))
    assert res.status_code == 400
    res = await client.post(
        "/api/interviews", **_upload(interviewer=_interviewer(language="fr", voice="en_female"))
    )
    assert res.status_code == 400
    # Malformed JSON is a 400 too, not a 500.
    res = await client.post("/api/interviews", **_upload(interviewer="{nope"))
    assert res.status_code == 400


async def test_repeat_keeps_the_original_interviewer(client_and_sessionmaker):
    client, sessionmaker = client_and_sessionmaker
    source = (
        await client.post(
            "/api/interviews",
            **_upload(
                interviewer=_interviewer(
                    agent_name="Sam", language="es", voice="es_male", persona="exigente"
                )
            ),
        )
    ).json()

    # The global settings move on AFTER the original ran.
    await client.put(
        "/api/settings",
        json={
            "agent_name": "Nova",
            "language": "en",
            "voice": "en_female",
            "persona": "a brand new persona",
        },
    )
    repeat = (await client.post(f"/api/interviews/{source['id']}/repeat")).json()
    assert repeat["interviewer"] == source["interviewer"]
    assert repeat["plan"]["language"] == "es"
    async with sessionmaker() as session:
        row = await db.get_conversation(session, uuid.UUID(repeat["id"]))
        assert row is not None and row.persona == "exigente"


async def test_repeat_of_a_personaless_interview_stays_personaless(client_and_sessionmaker):
    client, sessionmaker = client_and_sessionmaker
    source = (
        await client.post("/api/interviews", **_upload(interviewer=_interviewer(persona="")))
    ).json()
    await client.put(
        "/api/settings",
        json={
            "agent_name": "Emma",
            "language": "en",
            "voice": "en_female",
            "persona": "a persona added later",
        },
    )
    repeat = (await client.post(f"/api/interviews/{source['id']}/repeat")).json()
    async with sessionmaker() as session:
        row = await db.get_conversation(session, uuid.UUID(repeat["id"]))
        # Inheriting "none" must not pick up the persona the settings grew.
        assert row is not None and row.persona is None


# ---- history / repeat --------------------------------------------------------


async def test_history_lists_newest_first_with_a_summary(client_and_sessionmaker):
    client, sessionmaker = client_and_sessionmaker
    first = (await client.post("/api/interviews", **_upload("# Backend engineer\nACME."))).json()
    second = (await client.post("/api/interviews", **_upload("Data engineer at Beta."))).json()
    # Same-second creations: order the rows explicitly so "newest first" is
    # about created_at, not about which insert happened to land first.
    async with sessionmaker() as session:
        await session.execute(
            update(db.Conversation)
            .where(db.Conversation.id == uuid.UUID(first["id"]))
            .values(created_at=datetime.now(UTC) - timedelta(hours=1))
        )
        await session.commit()

    res = await client.get("/api/interviews")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 2
    assert [item["id"] for item in body["items"]] == [second["id"], first["id"]]

    row = body["items"][1]
    # The title is the offer's first line, with the markdown hash stripped.
    assert row["title"] == "Backend engineer"
    assert row["resume_filename"] == "cv.pdf"
    assert row["status"] == "planned"
    assert row["milestones_total"] == 4
    assert row["milestones_completed"] == 0
    assert row["evaluation"] is None
    assert row["repeat_of_id"] is None
    # The heavy fields stay out of the list.
    assert "plan" not in row and "job_offer" not in row


async def test_history_paginates_and_filters_by_status(client_and_sessionmaker):
    client, sessionmaker = client_and_sessionmaker
    await client.post("/api/interviews", **_upload())
    await client.post("/api/interviews", **_upload())
    evaluated_id = await _seed_finished_interview(sessionmaker)
    async with sessionmaker() as session:
        await db.set_status(session, evaluated_id, "evaluated")

    body = (await client.get("/api/interviews", params={"limit": 1})).json()
    assert body["total"] == 3 and len(body["items"]) == 1
    page_two = (await client.get("/api/interviews", params={"limit": 1, "offset": 1})).json()
    assert page_two["items"][0]["id"] != body["items"][0]["id"]

    filtered = (await client.get("/api/interviews", params={"status": "evaluated"})).json()
    assert [item["id"] for item in filtered["items"]] == [str(evaluated_id)]
    assert filtered["total"] == 1

    assert (await client.get("/api/interviews", params={"status": "nope"})).status_code == 400
    # An empty filter (a form's "all" option) is not an unknown status.
    everything = (await client.get("/api/interviews", params={"status": ""})).json()
    assert everything["total"] == 3
    assert (await client.get("/api/interviews", params={"limit": 0})).status_code == 422


async def test_history_row_carries_the_score(client_and_sessionmaker, monkeypatch):
    client, sessionmaker = client_and_sessionmaker
    monkeypatch.setattr(evaluations, "run_evaluator", _fake_evaluator)
    conversation_id = await _seed_finished_interview(sessionmaker)
    await client.post(f"/api/interviews/{conversation_id}/evaluate")
    await _settled(client, conversation_id)

    body = (await client.get("/api/interviews")).json()
    row = next(item for item in body["items"] if item["id"] == str(conversation_id))
    assert row["status"] == "evaluated"
    assert row["evaluation"] == {
        "hired": True,
        "score": 82,
        "evaluation_status": "complete",
    }
    assert row["milestones_completed"] == 1


async def test_transcript_returns_the_turns_in_order(client_and_sessionmaker):
    client, sessionmaker = client_and_sessionmaker
    conversation_id = await _seed_finished_interview(sessionmaker)
    res = await client.get(f"/api/interviews/{conversation_id}/transcript")
    assert res.status_code == 200
    messages = res.json()["messages"]
    assert [(m["role"], m["content"]) for m in messages] == [
        ("assistant", "Tell me about X."),
        ("user", "I built X."),
    ]
    assert messages[0]["created_at"]

    assert (await client.get(f"/api/interviews/{uuid.uuid4()}/transcript")).status_code == 404


async def test_repeat_replans_the_same_role_into_a_new_interview(client_and_sessionmaker):
    client, sessionmaker = client_and_sessionmaker
    source = (
        await client.post(
            "/api/interviews",
            **_upload("Backend engineer at ACME.", seniority="senior", interview_length="deep"),
        )
    ).json()

    res = await client.post(f"/api/interviews/{source['id']}/repeat")
    assert res.status_code == 200
    repeat = res.json()
    assert repeat["id"] != source["id"]
    assert repeat["status"] == "planned"
    # Same inputs and same calibration, freshly planned milestones.
    assert repeat["job_offer"] == source["job_offer"]
    assert repeat["resume_filename"] == "cv.pdf"
    assert repeat["seniority"] == "senior"
    assert repeat["seniority_source"] == "explicit"
    assert repeat["interview_length"] == "deep"
    assert len(repeat["milestones"]) == 4
    assert repeat["repeat_of_id"] == source["id"]
    assert "planner" in repeat["token_usage"]

    # The original is untouched, and both show up in the history.
    assert (await client.get(f"/api/interviews/{source['id']}")).json()["status"] == "planned"
    assert (await client.get("/api/interviews")).json()["total"] == 2

    # The stored resume is reused by both the planner and the voice agent.
    async with sessionmaker() as session:
        row = await db.get_conversation(session, uuid.UUID(repeat["id"]))
        assert row is not None and row.resume_markdown == "# Resume\nPython dev."
        prompt = system_prompt(row)
        assert row.resume_markdown in prompt
        assert source["job_offer"] in prompt


async def test_repeat_of_a_repeat_points_back_at_the_original(client_and_sessionmaker):
    client, _ = client_and_sessionmaker
    source = (await client.post("/api/interviews", **_upload())).json()
    first = (await client.post(f"/api/interviews/{source['id']}/repeat")).json()
    second = (await client.post(f"/api/interviews/{first['id']}/repeat")).json()
    # The chain flattens: every attempt groups under the original's id.
    assert first["repeat_of_id"] == source["id"]
    assert second["repeat_of_id"] == source["id"]


async def test_repeat_keeps_an_auto_detected_level_marked_as_auto(
    client_and_sessionmaker, monkeypatch
):
    client, _ = client_and_sessionmaker

    async def _detecting_planner(settings, resume_markdown, job_offer, **kwargs):
        return _plan(detected=Seniority.JUNIOR)

    monkeypatch.setattr(routes, "run_planner", _detecting_planner)
    source = (await client.post("/api/interviews", **_upload())).json()
    assert source["seniority_source"] == "detected"

    # The planner is told the answer this time (seniority is pinned), so it
    # classifies nothing — the provenance has to be carried, not re-derived.
    repeat = (await client.post(f"/api/interviews/{source['id']}/repeat")).json()
    assert repeat["seniority"] == "junior"
    assert repeat["seniority_source"] == "detected"
    assert repeat["seniority_evidence"] == source["seniority_evidence"]


async def test_repeat_accepts_overrides_and_404s_on_an_unknown_id(client_and_sessionmaker):
    client, _ = client_and_sessionmaker
    source = (await client.post("/api/interviews", **_upload("Offer.", seniority="lead"))).json()

    repeat = (
        await client.post(
            f"/api/interviews/{source['id']}/repeat",
            json={"seniority": "junior", "interview_length": "short"},
        )
    ).json()
    assert repeat["seniority"] == "junior"
    assert repeat["seniority_source"] == "explicit"
    assert repeat["interview_length"] == "short"
    # The overridden length re-derives this interview's own time cap.
    assert repeat["max_minutes"] == min(
        length_for(InterviewLength.SHORT)["minutes"], settings.interview_max_minutes
    )

    bad = await client.post(f"/api/interviews/{source['id']}/repeat", json={"seniority": "wizard"})
    assert bad.status_code == 400
    assert (await client.post(f"/api/interviews/{uuid.uuid4()}/repeat")).status_code == 404


async def test_healthz_ok(client_and_sessionmaker):
    client, _ = client_and_sessionmaker
    res = await client.get("/api/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


# ---- Seniority / length calibration ------------------------------------------


async def test_create_interview_defaults_to_auto_and_takes_the_detected_level(
    client_and_sessionmaker, monkeypatch
):
    """No level in the form: the planner classifies it ONCE and the server
    pins the result. Every later stage reads that pinned value."""

    async def _detecting_planner(settings, resume_markdown, job_offer, **kwargs):
        assert kwargs["seniority"] is None  # i.e. "classify it yourself"
        return _plan(detected=Seniority.JUNIOR)

    monkeypatch.setattr(routes, "run_planner", _detecting_planner)
    client, _ = client_and_sessionmaker
    res = await client.post("/api/interviews", **_upload())
    assert res.status_code == 200
    body = res.json()
    assert body["seniority"] == "junior"
    assert body["seniority_source"] == "detected"
    assert body["seniority_evidence"] == "The offer asks for 1-2 years."
    assert body["interview_length"] == "standard"
    # Pinned in columns, deliberately not duplicated into the plan JSON.
    assert "detected_seniority" not in body["plan"]
    assert "seniority_evidence" not in body["plan"]


async def test_explicit_seniority_wins_and_the_planner_never_classifies(
    client_and_sessionmaker, monkeypatch
):
    seen = {}

    async def _planner(settings, resume_markdown, job_offer, **kwargs):
        seen["seniority"] = kwargs["seniority"]
        seen["length"] = kwargs["interview_length"]
        # Even if the planner does classify, the user's choice must win.
        return _plan(detected=Seniority.SENIOR)

    monkeypatch.setattr(routes, "run_planner", _planner)
    client, _ = client_and_sessionmaker
    res = await client.post(
        "/api/interviews", **_upload(seniority="junior", interview_length="short")
    )
    assert res.status_code == 200
    body = res.json()
    assert seen["seniority"] is Seniority.JUNIOR
    assert seen["length"] is InterviewLength.SHORT
    assert body["seniority"] == "junior"
    assert body["seniority_source"] == "explicit"
    assert body["seniority_evidence"] is None


async def test_interview_length_sets_this_interviews_own_time_cap(
    client_and_sessionmaker,
):
    client, _ = client_and_sessionmaker
    res = await client.post("/api/interviews", **_upload(interview_length="short"))
    assert res.status_code == 200
    body = res.json()
    assert body["interview_length"] == "short"
    # Clamped by the global setting, so it can only ever be shorter.
    assert body["max_minutes"] == min(8, settings.interview_max_minutes)


async def test_deep_under_a_lower_cap_is_planned_for_the_cap(client_and_sessionmaker, monkeypatch):
    """`max_minutes` clamps the row; the planner must be sized for the SAME
    clamped value, or the interview is cut off mid-plan. The requested length
    is still stored as requested."""
    monkeypatch.setattr(settings, "interview_max_minutes", 15)
    seen: list[dict] = []

    async def _planner(settings, resume_markdown, job_offer, **kwargs):
        seen.append(kwargs)
        return _plan()

    monkeypatch.setattr(routes, "run_planner", _planner)
    client, _ = client_and_sessionmaker
    res = await client.post("/api/interviews", **_upload(interview_length="deep"))
    assert res.status_code == 200
    body = res.json()
    assert body["interview_length"] == "deep"
    assert body["max_minutes"] == 15
    assert seen[-1]["interview_length"] is InterviewLength.DEEP
    assert seen[-1]["max_minutes"] == 15

    # The repeat replans under the same rule.
    repeat = await client.post(f"/api/interviews/{body['id']}/repeat")
    assert repeat.status_code == 200
    assert repeat.json()["max_minutes"] == 15
    assert seen[-1]["interview_length"] is InterviewLength.DEEP
    assert seen[-1]["max_minutes"] == 15

    # With the shipped default the deep profile fits untouched.
    monkeypatch.setattr(settings, "interview_max_minutes", 25)
    body = (await client.post("/api/interviews", **_upload(interview_length="deep"))).json()
    assert body["max_minutes"] == 25
    assert seen[-1]["max_minutes"] == 25


@pytest.mark.parametrize(
    ("field", "value"),
    [("seniority", "archmage"), ("interview_length", "epic")],
)
async def test_create_interview_rejects_unknown_axis_values(client_and_sessionmaker, field, value):
    client, _ = client_and_sessionmaker
    res = await client.post("/api/interviews", **_upload(**{field: value}))
    assert res.status_code == 400


async def test_milestones_carry_their_bar_to_the_api(client_and_sessionmaker):
    client, _ = client_and_sessionmaker
    res = await client.post("/api/interviews", **_upload())
    assert res.status_code == 200
    assert all(m["expected_evidence"] == "Names one index." for m in res.json()["milestones"])


async def test_evaluate_judges_against_the_pinned_level(client_and_sessionmaker, monkeypatch):
    """The evaluator is HANDED the level and the per-milestone bars; it never
    re-infers seniority from how advanced the stack sounds."""
    client, sessionmaker = client_and_sessionmaker
    interview_id = await _seed_finished_interview(sessionmaker)
    async with sessionmaker() as session:
        await session.execute(
            update(db.Conversation)
            .where(db.Conversation.id == interview_id)
            .values(seniority="junior")
        )
        await session.commit()

    seen = {}

    async def _capturing_evaluator(settings, **kwargs):
        seen.update(kwargs)
        return _evaluation()

    monkeypatch.setattr(evaluations, "run_evaluator", _capturing_evaluator)
    res = await client.post(f"/api/interviews/{interview_id}/evaluate")
    assert res.status_code == 202
    body = await _settled(client, interview_id)
    assert seen["seniority"] == "junior"
    assert all("expected_evidence" in m for m in seen["milestones"])

    assert body["status"] == "evaluation_failed"
    assert body["evaluation"] is None  # A mismatched level is never persisted as valid.


async def test_direct_evaluation_seals_expired_closing_snapshot(
    client_and_sessionmaker, monkeypatch
):
    client, sessionmaker = client_and_sessionmaker
    monkeypatch.setattr(evaluations, "run_evaluator", _fake_evaluator)
    conversation_id = await _seed_finished_interview(sessionmaker)
    async with sessionmaker() as session:
        for message in await db.get_messages(session, conversation_id):
            if message.role == "user":
                message.metrics = {"stt_confirmed": True}
        await session.execute(
            update(db.Conversation)
            .where(db.Conversation.id == conversation_id)
            .values(
                status="closing",
                closing_id=uuid.uuid4(),
                closing_owner_id=uuid.uuid4(),
                closing_deadline_at=datetime.now(UTC) - timedelta(seconds=1),
                farewell_status="pending",
                # An expired close has not sealed yet.
                transcript_sealed_at=None,
                transcript_integrity=None,
                transcript_seal_id=None,
            )
        )
        await session.execute(
            delete(db.TranscriptSeal).where(db.TranscriptSeal.conversation_id == conversation_id)
        )
        await session.commit()
    response = await client.post(f"/api/interviews/{conversation_id}/evaluate")
    assert response.status_code == 202
    assert response.json()["transcript_integrity"] == "partial"
    async with sessionmaker() as session:
        row = await db.get_conversation(session, conversation_id)
        assert row.transcript_sealed_at is not None
        assert row.farewell_status == "timeout"
        with pytest.raises(ValueError, match="sealed"):
            await db.insert_message(session, conversation_id, "user", "Late answer")
    body = await _settled(client, conversation_id)
    assert body["evaluation"]["evaluation_status"] == "partial"
    assert body["evaluation"]["score"] is None
    assert body["evaluation"]["hired"] is None


async def test_delayed_automatic_trigger_does_not_reevaluate(client_and_sessionmaker, monkeypatch):
    client, sessionmaker = client_and_sessionmaker
    calls = 0

    async def evaluator(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await _fake_evaluator(*args, **kwargs)

    monkeypatch.setattr(evaluations, "run_evaluator", evaluator)
    conversation_id = await _seed_finished_interview(sessionmaker)
    url = f"/api/interviews/{conversation_id}/evaluate?automatic=true"
    assert (await client.post(url)).status_code == 202
    assert (await _settled(client, conversation_id))["status"] == "evaluated"
    assert (await client.post(url)).json()["status"] == "evaluated"
    await asyncio.sleep(0.05)
    assert calls == 1
    assert (await client.post(f"/api/interviews/{conversation_id}/evaluate")).status_code == 202
    await _settled(client, conversation_id)
    assert calls == 2


async def test_integrity_incident_hides_published_global_without_rewriting_history(
    client_and_sessionmaker, monkeypatch
):
    from interview_agent.interview.transcription import admit_candidate

    client, sessionmaker = client_and_sessionmaker
    monkeypatch.setattr(evaluations, "run_evaluator", _fake_evaluator)
    interview_id = await _seed_finished_interview(sessionmaker)
    await client.post(f"/api/interviews/{interview_id}/evaluate")
    before = await _settled(client, interview_id)
    assert before["evaluation"]["score"] is not None
    async with sessionmaker() as session:
        await admit_candidate(
            session,
            interview_id,
            content="Late omitted content",
            source_id="late",
            metrics={"stt_confirmed": True},
        )
    after = (await client.get(f"/api/interviews/{interview_id}")).json()
    assert after["capture_integrity_pending"]
    assert after["evaluation"]["score"] is None and after["evaluation"]["hired"] is None
    transcript = (await client.get(f"/api/interviews/{interview_id}/transcript")).json()
    assert transcript["incidents"][0]["content"] == "Late omitted content"
    async with sessionmaker() as session:
        row = await db.get_conversation(session, interview_id)
        assert row.evaluation.score == before["evaluation"]["score"]


async def test_capture_incident_during_evaluation_prevents_publication(
    client_and_sessionmaker, monkeypatch
):
    client, sessionmaker = client_and_sessionmaker
    interview_id = await _seed_finished_interview(sessionmaker)

    async def evaluate(settings, **kwargs):
        async with sessionmaker() as session:
            row = await db.get_conversation(session, interview_id)
            row.capture_integrity_pending = True
            await session.commit()
        return await _fake_evaluator(settings, **kwargs)

    monkeypatch.setattr(evaluations, "run_evaluator", evaluate)
    await client.post(f"/api/interviews/{interview_id}/evaluate")
    result = await _settled(client, interview_id)
    assert result["status"] == "evaluation_failed" and result["evaluation"] is None


async def test_evaluation_requires_a_seal(client_and_sessionmaker):
    client, sessions = client_and_sessionmaker
    interview_id = uuid.uuid4()
    async with sessions() as transaction:
        transaction.add(
            db.Conversation(
                id=interview_id,
                owner_id=DEVELOPER.id,
                status="completed",
                job_offer="o",
                resume_markdown="r",
            )
        )
        await transaction.commit()
    result = await client.post(f"/api/interviews/{interview_id}/evaluate")
    assert result.status_code == 409 and result.json()["detail"] == "Transcript is not sealed"


async def test_question_replay_api_is_idempotent_and_rejects_answered_question(
    client_and_sessionmaker,
):
    from interview_agent.interview.delivery import save_question
    from interview_agent.interview.transcription import admit_candidate

    client, sessions = client_and_sessionmaker
    interview_id = await _seed_finished_interview(sessions)
    question_id = uuid.uuid4()
    async with sessions() as transaction:
        row = await transaction.get(db.Conversation, interview_id)
        row.status = "interviewing"
        transaction.add(
            db.TurnRun(
                id=question_id,
                conversation_id=interview_id,
                turn_id="saved",
                decision={"spoken_text": "Saved question?"},
            )
        )
        await transaction.flush()
        await save_question(transaction, interview_id, question_id, "Saved question?")
        await transaction.commit()
    result = (await client.get(f"/api/interviews/{interview_id}/question")).json()
    assert result["question"]["id"] == str(question_id)
    payload = {"question_id": str(question_id), "request_id": str(uuid.uuid4())}
    url = f"/api/interviews/{interview_id}/question/replay"
    first = await client.post(url, json=payload)
    second = await client.post(url, json=payload)
    assert first.status_code == second.status_code == 202 and first.json() == second.json()
    async with sessions() as transaction:
        await admit_candidate(
            transaction,
            interview_id,
            content="An answer",
            source_id="answer",
            metrics={"stt_confirmed": True},
        )
    assert (await client.post(url, json=payload)).status_code == 409
    assert (await client.get(f"/api/interviews/{interview_id}/question")).json()["question"] is None


async def test_join_dispatches_the_configured_worker_with_only_the_interview_id(
    client_and_sessionmaker, monkeypatch
):
    client, sessionmaker = client_and_sessionmaker
    monkeypatch.setattr(settings, "livekit_api_key", "devkey")
    monkeypatch.setattr(settings, "livekit_api_secret", "devsecret" * 4)
    ready, unconfigured = uuid.uuid4(), uuid.uuid4()
    async with sessionmaker() as session:
        session.add_all(
            [
                db.Conversation(
                    id=ready,
                    owner_id=DEVELOPER.id,
                    status="planned",
                    job_offer="Synthetic",
                    resume_markdown="Synthetic",
                    run_config={
                        "models": {
                            "interviewer": {"model": "gpt-6-astra", "reasoning_effort": "low"}
                        }
                    },
                ),
                db.Conversation(
                    id=unconfigured,
                    owner_id=DEVELOPER.id,
                    status="planned",
                    job_offer="Synthetic",
                    resume_markdown="Synthetic",
                ),
            ]
        )
        await session.commit()
    response = await client.get(f"/api/interviews/{ready}/token")
    assert response.status_code == 200 and "protocol_version" not in response.json()
    claims = jwt.decode(
        response.json()["token"],
        settings.livekit_api_secret,
        algorithms=["HS256"],
        issuer=settings.livekit_api_key,
    )
    agent = claims["roomConfig"]["agents"][0]
    assert agent["agentName"] == settings.livekit_agent_name
    assert json.loads(agent["metadata"]) == {"conversation_id": str(ready)}
    # A row without a saved model snapshot cannot start with silent defaults.
    assert (await client.get(f"/api/interviews/{unconfigured}/token")).status_code == 409


@pytest.mark.parametrize("clock_offset", [-3650, 3650])
async def test_live_elapsed_uses_database_time_and_preserves_start_on_refresh(
    client_and_sessionmaker, monkeypatch, clock_offset
):
    client, sessions = client_and_sessionmaker
    cid, owner = uuid.uuid4(), uuid.uuid4()
    async with sessions() as session:
        now = await session.scalar(select(func.clock_timestamp()))
        start = now - timedelta(seconds=123)
        session.add(
            db.Conversation(
                id=cid,
                owner_id=DEVELOPER.id,
                status="interviewing",
                job_offer="Synthetic offer",
                resume_markdown="Synthetic resume",
                started_at=start,
                max_minutes=8,
                worker_owner_id=owner,
                worker_epoch=1,
                worker_lease_until=now + timedelta(seconds=15),
            )
        )
        await session.commit()

    class SkewedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=clock_offset)

    monkeypatch.setattr(routes, "datetime", SkewedClock)
    for _ in range(2):
        response = await client.get(f"/api/interviews/{cid}")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "interviewing"
        assert 123 <= body["elapsed_seconds"] < 128
    async with sessions() as session:
        assert (await session.get(db.Conversation, cid)).started_at == start


async def test_detail_exposes_written_farewell_and_closing_bound_from_database_clock(
    client_and_sessionmaker,
):
    client, sessions = client_and_sessionmaker
    lost, closing = uuid.uuid4(), uuid.uuid4()
    async with sessions() as session:
        now = await session.scalar(select(func.clock_timestamp()))
        session.add_all(
            [
                db.Conversation(
                    id=lost,
                    owner_id=DEVELOPER.id,
                    status="completed",
                    job_offer="Synthetic offer",
                    resume_markdown="Synthetic resume",
                    agent_settings={"language": "es"},
                    ended_reason="worker_lost",
                    farewell_status="not_possible",
                    transcript_sealed_at=now,
                    transcript_integrity="partial",
                ),
                db.Conversation(
                    id=closing,
                    owner_id=DEVELOPER.id,
                    status="closing",
                    job_offer="Synthetic offer",
                    resume_markdown="Synthetic resume",
                    closing_id=uuid.uuid4(),
                    closing_started_at=now,
                    closing_deadline_at=now + timedelta(seconds=35),
                    closing_ack_deadline_at=now + timedelta(seconds=30),
                    farewell_status="pending",
                ),
            ]
        )
        await session.commit()
    body = (await client.get(f"/api/interviews/{lost}")).json()
    assert body["ended_reason"] == "worker_lost"
    assert body["farewell_text"].startswith("Gracias por compartir")
    assert body["closing_remaining_seconds"] is None
    body = (await client.get(f"/api/interviews/{closing}")).json()
    assert body["farewell_text"] is None
    assert 35 < body["closing_remaining_seconds"] <= 40


async def test_repeat_of_failed_auto_classification_reclassifies_instead_of_pinning_mid(
    client_and_sessionmaker, monkeypatch
):
    client, _ = client_and_sessionmaker
    requested = []

    async def _boom(*args, **kwargs):
        requested.append(kwargs["seniority"])
        raise RuntimeError("classification failed")

    async def _detecting(settings, resume_markdown, job_offer, **kwargs):
        requested.append(kwargs["seniority"])
        return _plan(detected=Seniority.SENIOR)

    monkeypatch.setattr(routes, "run_planner", _boom)
    job_offer = f"offer-{uuid.uuid4()}"
    assert (await client.post("/api/interviews", **_upload(job_offer))).status_code == 500
    source = next(
        row
        for row in (await client.get("/api/interviews")).json()["items"]
        if row["status"] == "error"
    )
    assert source["seniority_source"] == "fallback"

    monkeypatch.setattr(routes, "run_planner", _detecting)
    repeat = (await client.post(f"/api/interviews/{source['id']}/repeat")).json()
    assert requested == [None, None]
    assert repeat["seniority"] == "senior"
    assert repeat["seniority_source"] == "detected"


async def test_question_limit_above_eight_is_reachable_and_effective_limit_is_honest(
    client_and_sessionmaker, monkeypatch
):
    client, _ = client_and_sessionmaker
    planned = {"count": 12}

    async def planner(settings, resume_markdown, job_offer, **kwargs):
        spec = _plan().milestones[0]
        return _plan().model_copy(
            update={
                "milestones": [
                    spec.model_copy(update={"title": f"M{i}"}) for i in range(planned["count"])
                ]
            }
        )

    monkeypatch.setattr(routes, "run_planner", planner)
    full = (
        await client.post(
            "/api/interviews",
            **_upload(seniority="senior", interviewer=_interviewer(question_limit=12)),
        )
    ).json()
    assert full["question_limit"] == 12 and len(full["milestones"]) == 12
    planned["count"] = 9
    fewer = (
        await client.post(
            "/api/interviews",
            **_upload(seniority="senior", interviewer=_interviewer(question_limit=12)),
        )
    ).json()
    # One primary question per planned topic: never display an unreachable 12.
    assert fewer["question_limit"] == 9
    assert fewer["run_config"]["requested_question_limit"] == 12


# ---- accounts: ownership, quotas, internal calls ------------------------------

GUEST = User("user-guest", "guest@example.com", "Guest")
OTHER = User("user-other", "other@example.com", "Other")


async def _me(client: AsyncClient) -> dict:
    response = await client.get("/api/me")
    assert response.status_code == 200
    return response.json()


async def test_another_users_interview_is_a_404_everywhere(client_and_sessionmaker, acting_user):
    client, sessionmaker = client_and_sessionmaker
    mine = (await client.post("/api/interviews", **_upload())).json()["id"]
    finished = await _seed_finished_interview(sessionmaker)
    nobodys = uuid.uuid4()
    async with sessionmaker() as session:
        # From before accounts: nobody's until claimed, so nobody sees it.
        session.add(
            db.Conversation(id=nobodys, status="planned", job_offer="o", resume_markdown="r")
        )
        await session.commit()
    assert (await client.get(f"/api/interviews/{nobodys}")).status_code == 404
    assert (await client.get("/api/interviews")).json()["total"] == 2

    acting_user["user"] = OTHER
    review = {"rationale": "r", "reviewer": "someone else"}
    for interview_id in (mine, finished, nobodys):
        for method, path, body in (
            ("GET", "", None),
            ("GET", "/transcript", None),
            ("GET", "/evaluations", None),
            ("GET", "/seals", None),
            ("GET", "/question", None),
            ("GET", "/token", None),
            ("POST", "/repeat", None),
            ("POST", "/evaluate", None),
            (
                "POST",
                "/question/replay",
                {"question_id": str(uuid.uuid4()), "request_id": str(uuid.uuid4())},
            ),
            (
                "POST",
                f"/incidents/{uuid.uuid4()}/review",
                {"review_id": str(uuid.uuid4()), "decision": "duplicate", **review},
            ),
            (
                "POST",
                "/seals",
                {
                    "seal_id": str(uuid.uuid4()),
                    "parent_id": str(uuid.uuid4()),
                    "incident_ids": [str(uuid.uuid4())],
                    **review,
                },
            ),
        ):
            response = await client.request(
                method, f"/api/interviews/{interview_id}{path}", json=body
            )
            assert response.status_code == 404, (method, path)
            assert response.json()["detail"] == "Interview not found"
    listing = (await client.get("/api/interviews")).json()
    assert listing["items"] == [] and listing["total"] == 0
    # Nothing was planned or spent on someone else's interview.
    assert (await _me(client))["interviews_used"] == 0

    # Admins get no cross-user access either.
    theirs = (await client.post("/api/interviews", **_upload())).json()["id"]
    acting_user["user"] = DEVELOPER
    assert (await client.get(f"/api/interviews/{theirs}")).status_code == 404
    assert (await client.get(f"/api/interviews/{mine}")).status_code == 200


async def test_the_lifetime_quota_survives_deletion_and_time(
    client_and_sessionmaker, acting_user, monkeypatch
):
    client, sessionmaker = client_and_sessionmaker
    # This month's shared capacity runs out with the user's lifetime quota.
    monkeypatch.setattr(
        settings, "guest_interviews_per_month", settings.lifetime_interviews_per_user
    )
    acting_user["user"] = GUEST
    for _ in range(settings.lifetime_interviews_per_user):
        assert (await client.post("/api/interviews", **_upload())).status_code == 200
    fourth = await client.post("/api/interviews", **_upload())
    assert fourth.status_code == 429
    assert fourth.json()["detail"] == "lifetime_interview_limit_reached"
    assert "retry-after" not in fourth.headers

    # The retention purge deletes the interviews, never the count of them.
    async with sessionmaker() as session:
        await session.execute(
            update(db.Conversation).values(created_at=func.now() - timedelta(days=60))
        )
        await session.commit()
    monkeypatch.setattr(settings, "retention_days", 30)
    from interview_agent.server.retention import purge_expired

    assert await purge_expired(sessionmaker, settings) == 3
    assert (await client.get("/api/interviews")).json()["total"] == 0
    assert (await client.post("/api/interviews", **_upload())).status_code == 429

    assert (await _me(client))["demo_capacity_available"] is False

    # Months later, as the quota logic reads the clock: the shared monthly
    # capacity is fresh, the lifetime is not.
    later_month = db._current_month()
    monkeypatch.setattr(
        db,
        "_current_month",
        lambda: func.date_trunc(
            "month", func.timezone("UTC", func.now()) + timedelta(days=400)
        ).cast(later_month.type),
    )
    assert (await _me(client))["demo_capacity_available"] is True
    later = await client.post("/api/interviews", **_upload())
    assert later.status_code == 429
    assert later.json()["detail"] == "lifetime_interview_limit_reached"
    me = await _me(client)
    assert (me["interviews_used"], me["interviews_remaining"]) == (3, 0)
    assert me["demo_capacity_available"] is True


async def test_repeat_and_failed_planning_spend_the_quota(
    client_and_sessionmaker, acting_user, monkeypatch
):
    client, _ = client_and_sessionmaker
    monkeypatch.setattr(settings, "guest_interviews_per_month", 100)
    acting_user["user"] = GUEST
    source = (await client.post("/api/interviews", **_upload())).json()
    assert (await client.post(f"/api/interviews/{source['id']}/repeat")).status_code == 200
    assert (await _me(client))["interviews_used"] == 2

    # The planner ran (and was paid for) before it failed: it counts.
    async def _boom(*args, **kwargs):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(routes, "run_planner", _boom)
    assert (await client.post(f"/api/interviews/{source['id']}/repeat")).status_code == 500
    assert (await _me(client))["interviews_used"] == 3
    monkeypatch.setattr(routes, "run_planner", _fake_planner)
    response = await client.post(f"/api/interviews/{source['id']}/repeat")
    assert response.status_code == 429
    assert response.json()["detail"] == "lifetime_interview_limit_reached"


async def test_rejected_requests_spend_no_quota(client_and_sessionmaker, acting_user, monkeypatch):
    client, sessionmaker = client_and_sessionmaker
    acting_user["user"] = GUEST

    async def unexpected_planner(*args, **kwargs):
        pytest.fail("A rejected request must not reach the planner")

    monkeypatch.setattr(routes, "run_planner", unexpected_planner)
    review = json.dumps({"pdf_sha256": sha256(b"another PDF").hexdigest(), "text": "CV"})
    for payload, status in (
        (_upload(interviewer=_interviewer(language="en", voice="es_male")), 400),
        (_upload(seniority="archmage"), 400),
        (_upload("o" * (MAX_JOB_OFFER_CHARS + 1)), 413),
        (_upload(resume_review=review), 409),
    ):
        assert (await client.post("/api/interviews", **payload)).status_code == status
    assert (await _me(client))["interviews_used"] == 0
    async with sessionmaker() as session:
        assert (await db.list_conversations(session, limit=20, offset=0))[1] == 0


async def test_concurrent_creates_never_exceed_either_limit(
    client_and_sessionmaker, acting_user, monkeypatch
):
    client, sessionmaker = client_and_sessionmaker
    monkeypatch.setattr(settings, "guest_interviews_per_month", 100)
    planned = 0

    async def planner(*args, **kwargs):
        nonlocal planned
        planned += 1
        await asyncio.sleep(0.05)  # keep the requests overlapping
        return _plan()

    monkeypatch.setattr(routes, "run_planner", planner)
    acting_user["user"] = GUEST
    responses = await asyncio.gather(
        *(client.post("/api/interviews", **_upload()) for _ in range(6))
    )
    statuses = sorted(response.status_code for response in responses)
    assert statuses == [200] * 3 + [429] * 3
    assert planned == 3

    # The month's shared capacity, raced by different users.
    monkeypatch.setattr(settings, "guest_interviews_per_month", 5)
    users = [User(f"user-{index}", None, None) for index in range(5)]

    async def create_as(user: User):
        async with sessionmaker() as session:
            return await db.reserve_interview_slot(
                session, user.id, lifetime_limit=3, monthly_limit=5
            )

    outcomes = await asyncio.gather(*(create_as(user) for user in users))
    assert sorted(outcomes) == ["monthly"] * 3 + ["ok"] * 2
    async with sessionmaker() as session:
        assert await session.scalar(select(db.GuestInterviewMonth.interviews_started)) == 5
        # A monthly refusal took nothing from the user's own quota.
        assert await session.scalar(select(func.sum(db.UserInterviewQuota.interviews_used))) == 5


async def test_the_monthly_capacity_answers_when_it_comes_back(
    client_and_sessionmaker, acting_user
):
    client, sessionmaker = client_and_sessionmaker
    for user in (GUEST, OTHER):
        acting_user["user"] = user
        assert (await client.post("/api/interviews", **_upload())).status_code == 200
    acting_user["user"] = User("user-third", None, None)
    assert (await _me(client))["demo_capacity_available"] is False
    response = await client.post("/api/interviews", **_upload())
    assert response.status_code == 429
    assert response.json()["detail"] == "monthly_demo_capacity_reached"
    retry_after = response.headers["retry-after"]
    assert retry_after.isdigit() and 0 < int(retry_after) <= 31 * 24 * 60 * 60
    async with sessionmaker() as session:
        # 00:00 UTC on the 1st of next month, on the database clock.
        now = await session.scalar(select(func.timezone("UTC", func.now())))
    next_month = (now.replace(day=28) + timedelta(days=4)).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    assert abs(int(retry_after) - (next_month - now).total_seconds()) <= 5
    # The lifetime quota is untouched.
    me = await _me(client)
    assert (me["interviews_used"], me["interviews_remaining"]) == (0, 3)


async def test_admins_are_unlimited(client_and_sessionmaker, monkeypatch):
    client, sessionmaker = client_and_sessionmaker
    monkeypatch.setattr(settings, "guest_interviews_per_month", 0)
    for _ in range(settings.lifetime_interviews_per_user + 2):
        assert (await client.post("/api/interviews", **_upload())).status_code == 200
    me = await _me(client)
    # First seen now: the profile was created by this very request.
    assert me.pop("created_at") == me.pop("last_seen_at")
    assert me == {
        "id": "local-dev",
        "email": None,
        "name": "Local developer",
        "is_admin": True,
        "auth_provider": "local",
        "interviews_used": 0,
        "interview_limit": None,
        "interviews_remaining": None,
        "demo_capacity_available": True,
    }
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(db.UserInterviewQuota)) == 0


async def test_me_for_a_guest(client_and_sessionmaker, acting_user):
    client, _ = client_and_sessionmaker
    acting_user["user"] = GUEST
    me = await _me(client)
    created_at = datetime.fromisoformat(me.pop("created_at"))
    assert created_at.tzinfo is not None
    assert datetime.fromisoformat(me.pop("last_seen_at")) == created_at
    assert me == {
        "id": "user-guest",
        "email": "guest@example.com",
        "name": "Guest",
        "is_admin": False,
        "auth_provider": "neon",
        "interviews_used": 0,
        "interview_limit": 3,
        "interviews_remaining": 3,
        "demo_capacity_available": True,
    }
    await client.post("/api/interviews", **_upload())
    me = await _me(client)
    assert (me["interviews_used"], me["interviews_remaining"]) == (1, 2)


async def _profile_version(sessionmaker, owner_id: str) -> str:
    """The transaction that last wrote the profile: unchanged, nothing was written."""
    async with sessionmaker() as session:
        return await session.scalar(
            text("SELECT xmin::text FROM user_profiles WHERE owner_id=:id"), {"id": owner_id}
        )


async def _last_seen(sessionmaker, owner_id: str, minutes_ago: int) -> None:
    async with sessionmaker() as session:
        await session.execute(
            text(
                "UPDATE user_profiles SET last_seen_at = now() - make_interval(mins => :minutes) "
                "WHERE owner_id=:id"
            ),
            {"id": owner_id, "minutes": minutes_ago},
        )
        await session.commit()


def _counted(points) -> dict[tuple, int]:
    return {tuple(sorted(dict(point.attributes).items())): point.count for point in points}


async def test_me_records_the_profile_without_a_write_per_page(
    client_and_sessionmaker, acting_user, recorded_metrics
):
    client, sessionmaker = client_and_sessionmaker
    acting_user["user"] = GUEST
    first = await _me(client)
    assert _counted(recorded_metrics("interview_agent.accounts.new_user")) == {
        (("auth_provider", "neon"),): 1
    }
    async with sessionmaker() as session:
        profile = await session.get(db.UserProfile, GUEST.id)
        assert (profile.auth_provider, profile.email, profile.name) == (
            "neon",
            "guest@example.com",
            "Guest",
        )
        assert profile.last_seen_at.isoformat() == first["last_seen_at"]

    # The next pages within five minutes write nothing.
    version = await _profile_version(sessionmaker, GUEST.id)
    assert (await _me(client))["last_seen_at"] == first["last_seen_at"]
    assert await _profile_version(sessionmaker, GUEST.id) == version
    await _last_seen(sessionmaker, GUEST.id, 4)
    version = await _profile_version(sessionmaker, GUEST.id)
    four_minutes_ago = datetime.fromisoformat((await _me(client))["last_seen_at"])
    assert four_minutes_ago < datetime.fromisoformat(first["last_seen_at"])
    assert await _profile_version(sessionmaker, GUEST.id) == version

    # Five minutes on, the visit is recorded again.
    await _last_seen(sessionmaker, GUEST.id, 6)
    version = await _profile_version(sessionmaker, GUEST.id)
    seen_again = await _me(client)
    assert await _profile_version(sessionmaker, GUEST.id) != version
    assert datetime.fromisoformat(seen_again["last_seen_at"]) > four_minutes_ago
    assert seen_again["created_at"] == first["created_at"]

    # A new name or email from the sign-in is kept at once.
    for renamed in (
        User(GUEST.id, GUEST.email, "Guest Renamed"),
        User(GUEST.id, "renamed@example.com", "Guest Renamed"),
        User(GUEST.id, None, "Guest Renamed"),
    ):
        acting_user["user"] = renamed
        version = await _profile_version(sessionmaker, GUEST.id)
        await _me(client)
        assert await _profile_version(sessionmaker, GUEST.id) != version
        async with sessionmaker() as session:
            profile = await session.get(db.UserProfile, GUEST.id)
            assert (profile.email, profile.name) == (renamed.email, renamed.name)

    # A new user once, however often they come back; local ids are local.
    acting_user["user"] = User("local:guest", None, "guest")
    assert (await _me(client))["auth_provider"] == "local"
    assert _counted(recorded_metrics("interview_agent.accounts.new_user")) == {
        (("auth_provider", "neon"),): 1,
        (("auth_provider", "local"),): 1,
    }


async def test_only_me_writes_profiles(client_and_sessionmaker, acting_user):
    client, sessionmaker = client_and_sessionmaker
    acting_user["user"] = GUEST
    assert (await client.post("/api/interviews", **_upload())).status_code == 200
    assert (await client.get("/api/interviews")).status_code == 200
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(db.UserProfile)) == 0


async def test_admins_list_users_by_last_visit(client_and_sessionmaker, acting_user, monkeypatch):
    client, sessionmaker = client_and_sessionmaker
    monkeypatch.setattr(settings, "guest_interviews_per_month", 100)
    for user in (DEVELOPER, GUEST, OTHER):
        acting_user["user"] = user
        await _me(client)
    acting_user["user"] = GUEST
    kept, purged = [
        (await client.post("/api/interviews", **_upload())).json()["id"] for _ in range(2)
    ]
    async with sessionmaker() as session:
        # Profiles from the migration's backfill: never seen since.
        session.add_all(
            db.UserProfile(owner_id=owner_id, auth_provider="neon")
            for owner_id in ("user-unseen-b", "user-unseen-a")
        )
        await session.execute(
            update(db.Conversation)
            .where(db.Conversation.id == uuid.UUID(kept))
            .values(created_at=func.now() - timedelta(days=3))
        )
        await session.execute(
            delete(db.Conversation).where(db.Conversation.id == uuid.UUID(purged))
        )
        await session.commit()
        kept_at = await session.scalar(
            select(db.Conversation.created_at).where(db.Conversation.id == uuid.UUID(kept))
        )
    for owner_id, minutes in (("local-dev", 3), ("user-guest", 1), ("user-other", 2)):
        await _last_seen(sessionmaker, owner_id, minutes)

    assert (await client.get("/api/admin/users")).status_code == 403
    acting_user["user"] = DEVELOPER
    body = (await client.get("/api/admin/users")).json()
    assert body["total"] == 5
    assert [item["id"] for item in body["items"]] == [
        "user-guest",
        "user-other",
        "local-dev",
        "user-unseen-a",
        "user-unseen-b",
    ]
    guest, other, developer, unseen, _ = body["items"]
    assert datetime.fromisoformat(guest.pop("last_seen_at")) > datetime.fromisoformat(
        other["last_seen_at"]
    )
    assert datetime.fromisoformat(guest.pop("created_at")).tzinfo is not None
    assert guest == {
        "id": "user-guest",
        "name": "Guest",
        "email": "guest@example.com",
        "auth_provider": "neon",
        "is_admin": False,
        # Both count against the quota; only one is still stored.
        "interviews_used": 2,
        "interview_limit": 3,
        "interviews_stored": 1,
        "last_interview_at": kept_at.isoformat(),
    }
    assert (other["interviews_used"], other["interviews_stored"]) == (0, 0)
    assert other["last_interview_at"] is None
    assert (developer["is_admin"], developer["interview_limit"]) == (True, None)
    assert developer["auth_provider"] == "local"
    assert unseen["last_seen_at"] is None and unseen["interview_limit"] == 3
    assert (unseen["name"], unseen["email"], unseen["interviews_used"]) == (None, None, 0)

    page = (await client.get("/api/admin/users", params={"limit": 2, "offset": 1})).json()
    assert page["total"] == 5
    assert [item["id"] for item in page["items"]] == ["user-other", "local-dev"]
    beyond = (await client.get("/api/admin/users", params={"offset": 10})).json()
    assert beyond == {"total": 5, "items": []}
    assert (await client.get("/api/admin/users", params={"limit": 100})).status_code == 200
    for params in ({"limit": 101}, {"limit": 0}, {"offset": -1}):
        assert (await client.get("/api/admin/users", params=params)).status_code == 422


async def test_reservations_and_quota_refusals_are_counted_by_category(
    client_and_sessionmaker, acting_user, monkeypatch, recorded_metrics
):
    client, _ = client_and_sessionmaker
    assert (await client.post("/api/interviews", **_upload())).status_code == 200
    acting_user["user"] = GUEST
    monkeypatch.setattr(settings, "guest_interviews_per_month", 100)
    for _ in range(settings.lifetime_interviews_per_user):
        assert (await client.post("/api/interviews", **_upload())).status_code == 200
    assert (await client.post("/api/interviews", **_upload())).status_code == 429
    acting_user["user"] = OTHER
    monkeypatch.setattr(settings, "guest_interviews_per_month", 0)
    assert (await client.post("/api/interviews", **_upload())).status_code == 429
    assert _counted(recorded_metrics("interview_agent.accounts.interview_reserved")) == {
        (("role", "admin"),): 1,
        (("role", "guest"),): 3,
    }
    assert _counted(recorded_metrics("interview_agent.accounts.quota_rejected")) == {
        (("limit", "lifetime"),): 1,
        (("limit", "monthly"),): 1,
    }


async def test_neon_mode_requires_a_valid_jwt_and_ignores_its_role(
    client_and_sessionmaker, acting_user, monkeypatch
):
    use_neon_auth(monkeypatch)
    client, _ = client_and_sessionmaker
    acting_user["user"] = None
    files = _upload()["files"]
    response = await client.post("/api/resumes/preview", files=files)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert (await client.get("/api/me")).status_code == 401
    assert (await client.get("/api/healthz")).status_code == 200

    elevated = bearer(token(sub="user-guest", role="admin"))
    assert (
        await client.post("/api/resumes/preview", files=files, headers=elevated)
    ).status_code == 200
    me = (await client.get("/api/me", headers=elevated)).json()
    assert me["id"] == "user-guest" and me["is_admin"] is False
    assert (await client.get("/api/runtime", headers=elevated)).status_code == 403


async def test_runtime_is_for_admins(client_and_sessionmaker, acting_user):
    client, _ = client_and_sessionmaker
    # Listed admin: past the gate (no manifest recorded in this bare app).
    assert (await client.get("/api/runtime")).status_code == 503
    acting_user["user"] = GUEST
    assert (await client.get("/api/runtime")).status_code == 403


async def test_maintenance_takes_only_the_internal_token(
    client_and_sessionmaker, monkeypatch, recorded_metrics
):
    client, sessionmaker = client_and_sessionmaker
    old = await _seed_finished_interview(sessionmaker)
    async with sessionmaker() as session:
        await session.execute(
            update(db.Conversation)
            .where(db.Conversation.id == old)
            .values(created_at=func.now() - timedelta(days=60))
        )
        await session.commit()
    monkeypatch.setattr(settings, "retention_days", 30)
    assert (await client.post("/api/internal/maintenance")).status_code == 401
    monkeypatch.setattr(settings, "internal_api_token", "synthetic-internal-token")
    for headers in ({}, {"X-Internal-Token": "wrong"}):
        response = await client.post("/api/internal/maintenance", headers=headers)
        assert response.status_code == 401
    response = await client.post(
        "/api/internal/maintenance", headers={"X-Internal-Token": "synthetic-internal-token"}
    )
    assert response.status_code == 200
    assert response.json() == {"deleted_interviews": 1, "reconciled": 0, "failures": 0}
    async with sessionmaker() as session:
        assert await db.get_conversation(session, old) is None
    # The user counts are fresh for Grafana too.
    (limit,) = recorded_metrics("interview_agent.accounts.guest_interviews_monthly_limit")
    assert limit.value == settings.guest_interviews_per_month


async def test_maintenance_survives_a_failed_user_recount(
    client_and_sessionmaker, monkeypatch, caplog
):
    from interview_agent.server import account_metrics

    client, _ = client_and_sessionmaker

    async def broken(sessionmaker, settings):
        raise RuntimeError("SENSITIVE_DATABASE_ERROR")

    # The purge and the sweep have committed by then: their counts still
    # reach the scheduler, and the failure is logged by its template alone.
    monkeypatch.setattr(account_metrics, "account_snapshot", broken)
    monkeypatch.setattr(settings, "internal_api_token", "synthetic-internal-token")
    with caplog.at_level("ERROR", logger="interview_agent"):
        response = await client.post(
            "/api/internal/maintenance", headers={"X-Internal-Token": "synthetic-internal-token"}
        )
    assert response.status_code == 200
    assert response.json() == {"deleted_interviews": 0, "reconciled": 0, "failures": 0}
    assert [record.msg for record in caplog.records] == [
        "account metrics failed; retrying next cycle"
    ]


async def test_the_worker_evaluates_with_the_internal_token_alone(
    client_and_sessionmaker, acting_user, monkeypatch
):
    client, sessionmaker = client_and_sessionmaker
    monkeypatch.setattr(evaluations, "run_evaluator", _fake_evaluator)
    monkeypatch.setattr(settings, "internal_api_token", "synthetic-internal-token")
    interview_id = await _seed_finished_interview(sessionmaker)
    url = f"/api/interviews/{interview_id}/evaluate?automatic=true"
    # A wrong token falls back to the signed-in user, who must own it.
    acting_user["user"] = OTHER
    assert (await client.post(url, headers={"X-Internal-Token": "wrong"})).status_code == 404
    acting_user["user"] = None
    use_neon_auth(monkeypatch)
    assert (await client.post(url, headers={"X-Internal-Token": "wrong"})).status_code == 401
    # The worker: no user at all.
    response = await client.post(url, headers={"X-Internal-Token": "synthetic-internal-token"})
    assert response.status_code == 202
    acting_user["user"] = DEVELOPER
    assert (await _settled(client, interview_id))["status"] == "evaluated"


async def test_the_worker_evaluates_a_local_accounts_interview_with_the_internal_token(
    client_and_sessionmaker, acting_user, monkeypatch
):
    # The dev login: every user route needs a local token, but the worker's
    # trigger carries only the internal one.
    client, sessionmaker = client_and_sessionmaker
    monkeypatch.setattr(evaluations, "run_evaluator", _fake_evaluator)
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(
        settings, "local_accounts", (LocalAccount("guest", "synthetic-guest-password"),)
    )
    monkeypatch.setattr(settings, "internal_api_token", "synthetic-internal-token")
    interview_id = await _seed_finished_interview(sessionmaker)
    async with sessionmaker() as session:
        await session.execute(
            update(db.Conversation)
            .where(db.Conversation.id == interview_id)
            .values(owner_id="local:guest")
        )
        await session.commit()
    url = f"/api/interviews/{interview_id}/evaluate?automatic=true"
    acting_user["user"] = None
    assert (await client.post(url)).status_code == 401
    assert (await client.post(url, headers={"X-Internal-Token": "wrong"})).status_code == 401
    response = await client.post(url, headers={"X-Internal-Token": "synthetic-internal-token"})
    assert response.status_code == 202
    guest, _ = issue_local_token(User("local:guest", None, "guest"))
    deadline = asyncio.get_running_loop().time() + 5
    while True:
        body = (await client.get(f"/api/interviews/{interview_id}", headers=bearer(guest))).json()
        if body["status"] not in ("completed", "evaluating"):
            break
        assert asyncio.get_running_loop().time() < deadline, f"still {body['status']}"
        await asyncio.sleep(0.01)
    assert body["status"] == "evaluated"


async def test_settings_belong_to_each_user(client_and_sessionmaker, acting_user):
    client, sessionmaker = client_and_sessionmaker
    mine = {"agent_name": "Sam", "language": "es", "voice": "es_male"}
    assert (await client.put("/api/settings", json=mine)).status_code == 200

    acting_user["user"] = GUEST
    # A user who never saved any reads the app-wide defaults.
    assert (await client.get("/api/settings")).json()["language"] == "en"
    theirs = {"agent_name": "Nova", "language": "en", "voice": "en_male", "persona": "calm"}
    assert (await client.put("/api/settings", json=theirs)).json()["agent_name"] == "Nova"
    planned = (await client.post("/api/interviews", **_upload())).json()
    assert planned["interviewer"] == {"agent_name": "Nova", "language": "en", "voice": "en_male"}

    acting_user["user"] = DEVELOPER
    body = (await client.get("/api/settings")).json()
    assert (body["agent_name"], body["language"], body["persona"]) == ("Sam", "es", None)
    async with sessionmaker() as session:
        assert await session.get(db.AppSettings, 1) is None


@pytest.mark.parametrize(
    ("origin", "allowed"),
    [("https://app.example.com", True), ("https://attacker.example", False)],
)
async def test_cors_preflight_allows_only_the_listed_origins(origin, allowed):
    from interview_agent.server import app as server_app

    app = FastAPI()
    server_app.add_cors(app, ["https://app.example.com"])
    app.include_router(routes.router, prefix="/api")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.options(
            "/api/interviews",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Authorization, Content-Type",
            },
        )
    if allowed:
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin
        assert "authorization" in response.headers["access-control-allow-headers"].lower()
        assert "access-control-allow-credentials" not in response.headers
    else:
        assert response.status_code == 400
        assert "access-control-allow-origin" not in response.headers

    bare = FastAPI()
    server_app.add_cors(bare, [])
    assert bare.user_middleware == []


@pytest.mark.parametrize("origins", ["*", "https://app.example.com, *"])
def test_cors_origins_never_include_the_wildcard(origins):
    from pydantic import ValidationError

    from interview_agent.config import Settings

    with pytest.raises(ValidationError, match="never \\*"):
        Settings(_env_file=None, CORS_ALLOWED_ORIGINS=origins)
    configured = Settings(_env_file=None, CORS_ALLOWED_ORIGINS=" https://app.example.com, ")
    assert configured.cors_allowed_origins == ["https://app.example.com"]
