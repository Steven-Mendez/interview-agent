"""API route tests against a real (test) Postgres, with the LLM/RAG calls
mocked out.

The app is assembled by hand (router + app.state) instead of importing the
real `app`, whose lifespan needs Qdrant and validated API keys. The test
database is created on the fly; locally it lands on the docker-compose
Postgres, in CI on the service container (TEST_DATABASE_URL overrides).
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from interview_agent.interview import db, rag
from interview_agent.interview.models import EvaluationResult, InterviewPlan, MilestoneSpec
from interview_agent.server import routes

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://interview:interview@localhost:5432/interview_test",
)


def _plan() -> InterviewPlan:
    return InterviewPlan(
        persona="Laura, engineering manager",
        summary="Solid candidate.",
        focus_areas=["Kubernetes"],
        milestones=[MilestoneSpec(title=f"M{i}", description="Probe it.") for i in range(4)],
    )


def _evaluation() -> EvaluationResult:
    return EvaluationResult(
        hired=True,
        score=82,
        strengths=["clear communication"],
        weaknesses=["little SQL depth"],
        rationale="Convincing on most milestones.",
    )


async def _ensure_test_database() -> None:
    """CREATE DATABASE if missing — Postgres has no CREATE ... IF NOT EXISTS."""
    url = make_url(TEST_DATABASE_URL)
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
async def client_and_sessionmaker(monkeypatch):
    await _ensure_test_database()
    engine, sessionmaker = db.create_engine_and_sessionmaker(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        # The test database persists across runs and create_all never ALTERs
        # an existing table — rebuild from scratch so schema changes (new
        # columns/tables) land, and every test starts from a clean slate.
        await conn.run_sync(db.Base.metadata.drop_all)
        await conn.run_sync(db.Base.metadata.create_all)

    # The routes only touch qdrant through rag helpers — stub those instead
    # of faking a client.
    async def _no_index(*args, **kwargs) -> int:
        return 3

    async def _no_delete(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(rag, "index_resume", _no_index)
    monkeypatch.setattr(rag, "delete_resume_points", _no_delete)
    monkeypatch.setattr(rag, "pdf_to_markdown", lambda data, filename: "# Resume\nPython dev.")
    monkeypatch.setattr(routes, "run_planner", _fake_planner)

    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    app.state.sessionmaker = sessionmaker
    app.state.qdrant = object()  # only ever handed to the stubbed rag helpers
    app.state.embeddings = object()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, sessionmaker
    await engine.dispose()


async def _fake_planner(settings, resume_markdown, job_offer, **kwargs) -> InterviewPlan:
    return _plan()


async def _fake_evaluator(settings, **kwargs) -> EvaluationResult:
    return _evaluation()


def _upload(job_offer: str = "Backend engineer at ACME."):
    return {
        "files": {"resume": ("cv.pdf", b"%PDF-fake", "application/pdf")},
        "data": {"job_offer": job_offer},
    }


async def _seed_finished_interview(sessionmaker) -> uuid.UUID:
    """A completed interview with a transcript, ready to evaluate."""
    conversation_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            db.Conversation(
                id=conversation_id,
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
            await db.insert_message(session, conversation_id, role, content, seq=seq)
    return conversation_id


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
    client, _ = client_and_sessionmaker
    res = await client.post("/api/interviews", **_upload())
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "planned"
    assert len(body["milestones"]) == 4
    assert body["plan"]["language"] == "en"  # injected from the settings
    assert "milestones" not in body["plan"]  # stored separately
    assert "planner" in body["token_usage"]


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
    assert snapshot["tts_model"] == "cartesia/sonic-3"
    assert snapshot["tts_voice"]  # resolved, not just the catalog key


async def test_create_interview_rejects_non_pdf(client_and_sessionmaker):
    client, _ = client_and_sessionmaker
    res = await client.post(
        "/api/interviews",
        files={"resume": ("cv.docx", b"bytes", "application/msword")},
        data={"job_offer": "offer"},
    )
    assert res.status_code == 400


async def test_create_interview_planning_failure_marks_error(
    client_and_sessionmaker, monkeypatch
):
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


async def test_get_interview_404(client_and_sessionmaker):
    client, _ = client_and_sessionmaker
    res = await client.get(f"/api/interviews/{uuid.uuid4()}")
    assert res.status_code == 404


# ---- /evaluate --------------------------------------------------------------


async def test_evaluate_happy_and_idempotent(client_and_sessionmaker, monkeypatch):
    client, sessionmaker = client_and_sessionmaker
    monkeypatch.setattr(routes, "run_evaluator", _fake_evaluator)
    conversation_id = await _seed_finished_interview(sessionmaker)

    res = await client.post(f"/api/interviews/{conversation_id}/evaluate")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "evaluated"
    assert body["evaluation"]["hired"] is True
    assert body["evaluation"]["score"] == 82
    assert body["evaluation"]["ended_by"] == "plan_complete"

    # Re-evaluation upserts instead of racing delete+insert into a 500.
    res2 = await client.post(f"/api/interviews/{conversation_id}/evaluate")
    assert res2.status_code == 200
    assert res2.json()["evaluation"]["score"] == 82


async def test_evaluate_without_transcript_409(client_and_sessionmaker, monkeypatch):
    client, sessionmaker = client_and_sessionmaker
    monkeypatch.setattr(routes, "run_evaluator", _fake_evaluator)
    conversation_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            db.Conversation(
                id=conversation_id, status="completed", job_offer="o", resume_markdown="r"
            )
        )
        await session.commit()

    res = await client.post(f"/api/interviews/{conversation_id}/evaluate")
    assert res.status_code == 409


async def test_evaluate_failure_sets_status_and_retry_recovers(
    client_and_sessionmaker, monkeypatch
):
    client, sessionmaker = client_and_sessionmaker
    conversation_id = await _seed_finished_interview(sessionmaker)

    async def _boom(*args, **kwargs):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(routes, "run_evaluator", _boom)
    res = await client.post(f"/api/interviews/{conversation_id}/evaluate")
    assert res.status_code == 502
    status = (await client.get(f"/api/interviews/{conversation_id}")).json()["status"]
    assert status == "evaluation_failed"

    # The endpoint stays re-invocable: a later retry succeeds.
    monkeypatch.setattr(routes, "run_evaluator", _fake_evaluator)
    res2 = await client.post(f"/api/interviews/{conversation_id}/evaluate")
    assert res2.status_code == 200
    assert res2.json()["status"] == "evaluated"


# ---- DB helpers -------------------------------------------------------------


async def test_add_token_usage_merges_components(client_and_sessionmaker):
    _, sessionmaker = client_and_sessionmaker
    conversation_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            db.Conversation(
                id=conversation_id, status="created", job_offer="o", resume_markdown="r"
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


async def test_get_messages_orders_by_seq_not_id(client_and_sessionmaker):
    _, sessionmaker = client_and_sessionmaker
    conversation_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            db.Conversation(
                id=conversation_id, status="created", job_offer="o", resume_markdown="r"
            )
        )
        await session.commit()
        # Committed out of order (higher seq first), as racing persist tasks
        # can do — read order must follow seq regardless.
        await db.insert_message(session, conversation_id, "assistant", "second", seq=1)
        await db.insert_message(session, conversation_id, "user", "first", seq=0)
        messages = await db.get_messages(session, conversation_id)
    assert [m.content for m in messages] == ["first", "second"]


# ---- /healthz ---------------------------------------------------------------


async def test_healthz_ok(client_and_sessionmaker):
    client, _ = client_and_sessionmaker
    res = await client.get("/api/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}
