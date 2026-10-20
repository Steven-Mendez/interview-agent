"""Reasoning harness remains independent of the voice pipeline and database."""

import asyncio
import json
import os
import subprocess
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from interview_agent.config import Settings
from interview_agent.interview.models import TurnDecision
from scripts import interview_text as text
from scripts.interview_text import ROOT, apply_decision, interview, next_decision


def test_harness_does_not_import_livekit():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy, sys; "
            "runpy.run_path('scripts/interview_text.py', run_name='text_harness'); "
            "assert not any(m == 'livekit' or m.startswith('livekit.') for m in sys.modules)",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_text_state_applies_real_budgets_and_lifecycle():
    context = {
        "milestones": [
            {
                "id": "sql",
                "primary_questions": 0,
                "followups": 0,
                "clarifications": 0,
                "lifecycle": "pending",
            }
        ],
        "state_revision": 0,
        "previous_questions": [],
    }
    apply_decision(
        context,
        TurnDecision(
            action="question", spoken_text="How would you diagnose it?", target_milestone_id="sql"
        ),
    )
    apply_decision(
        context,
        TurnDecision(
            action="followup", spoken_text="What does the plan show?", target_milestone_id="sql"
        ),
    )
    assert context["primary_questions"] == 1
    assert context["milestones"][0]["followups"] == 1
    assert context["milestones"][0]["lifecycle"] == "active"
    assert context["state_revision"] == 2


async def test_harness_does_not_silently_evaluate_an_interview_that_never_closed(monkeypatch):
    fixture = json.loads((ROOT / "scripts/fixtures/text_interview.json").read_text())

    async def never_close(settings, context, usage):
        return TurnDecision(
            action="followup", spoken_text="What else?", target_milestone_id="sql"
        ), []

    monkeypatch.setattr("scripts.interview_text.next_decision", never_close)
    with pytest.raises(ValueError, match="did not close"):
        await interview(Settings(_env_file=None), fixture, "vague")


async def test_checkpoint_preserves_answers_when_a_later_provider_call_fails(monkeypatch, tmp_path):
    fixture = json.loads((ROOT / "scripts/fixtures/text_interview.json").read_text())
    provider = AsyncMock(
        side_effect=[
            (
                TurnDecision(
                    action="question",
                    spoken_text="How would you investigate?",
                    target_milestone_id="sql",
                ),
                [],
            ),
            RuntimeError("Provider unavailable"),
        ]
    )
    monkeypatch.setattr("scripts.interview_text.next_decision", provider)
    checkpoint = tmp_path / "vague.json"
    with pytest.raises(RuntimeError, match="Provider unavailable"):
        await interview(Settings(_env_file=None), fixture, "vague", checkpoint=checkpoint)
    saved = json.loads(checkpoint.read_text())
    assert saved["transcript"][-1]["content"] == fixture["scenarios"]["vague"]["answers"]["sql"]
    assert saved["ended_reason"] is None
    assert "evaluation" not in saved


async def test_text_decision_repairs_invalid_milestone_references(monkeypatch):
    from interview_agent.config import Settings

    invalid = TurnDecision(
        action="question", spoken_text="How would you inspect it?", target_milestone_id="invented"
    )
    valid = invalid.model_copy(update={"target_milestone_id": "sql"})
    structured = Mock(ainvoke=AsyncMock(side_effect=[invalid, valid]))
    monkeypatch.setattr(
        "scripts.interview_text.build_chat_model",
        Mock(return_value=Mock(with_structured_output=Mock(return_value=structured))),
    )
    monkeypatch.setattr("scripts.interview_text.close_chat_model", AsyncMock())
    context = {
        "system_prompt": "Interview at the pinned level.",
        "transcript": [],
        "milestones": [{"id": "sql", "lifecycle": "pending", "primary_questions": 0}],
        "remaining_seconds": 500,
        "primary_questions": 0,
        "question_limit": 3,
        "previous_questions": [],
    }
    decision, errors = await next_decision(Settings(_env_file=None), context, Mock())
    assert decision.target_milestone_id == "sql"
    assert len(errors) == 1
    payload = json.loads(structured.ainvoke.call_args.args[0][1].content)
    assert payload["repair"]["invalid_decision"]["target_milestone_id"] == "invented"


@pytest.mark.parametrize("ending", ["/end", None, asyncio.CancelledError(), TimeoutError()])
async def test_console_endings_keep_pending_question_without_inventing_answers(
    monkeypatch, tmp_path, ending
):
    fixture = json.loads((ROOT / "scripts/fixtures/text_interview.json").read_text())
    provider = AsyncMock(
        return_value=(
            TurnDecision(
                action="question",
                spoken_text="How would you investigate?",
                target_milestone_id="sql",
            ),
            [],
        )
    )
    monkeypatch.setattr(text, "next_decision", provider)
    checkpoint = tmp_path / "interactive.json"

    async def read_answer(remaining):
        saved = json.loads(checkpoint.read_text())
        assert len(saved["transcript"]) == 1  # Saved BEFORE waiting for an answer.
        assert saved["ended_reason"] is None
        if isinstance(ending, BaseException):
            raise ending
        return ending

    monkeypatch.setattr(text, "read_console_answer", read_answer)
    run = await interview(
        Settings(_env_file=None),
        fixture,
        "interactive",
        interactive=True,
        checkpoint=checkpoint,
    )
    assert run["transcript"] == [
        {
            "id": "1",
            "version": 1,
            "role": "assistant",
            "content": "How would you investigate?",
            "interrupted": False,
        }
    ]
    assert provider.await_count == 1
    assert run["ended_reason"] == (
        "candidate_left"
        if ending is None
        else "timeout"
        if isinstance(ending, TimeoutError)
        else "candidate_requested"
    )
    assert json.loads(checkpoint.read_text()) == run
    if isinstance(ending, TimeoutError):
        assert all(m["lifecycle"] == "skipped" for m in run["milestones"])


async def test_blank_console_input_keeps_question_and_real_answer_on_later_failure(
    monkeypatch, tmp_path
):
    fixture = json.loads((ROOT / "scripts/fixtures/text_interview.json").read_text())
    provider = AsyncMock(
        side_effect=[
            (TurnDecision(action="question", spoken_text="How?", target_milestone_id="sql"), []),
            RuntimeError("Provider unavailable"),
        ]
    )
    monkeypatch.setattr(text, "next_decision", provider)
    reader = AsyncMock(side_effect=["", "  ", "Usaría EXPLAIN."])
    monkeypatch.setattr(text, "read_console_answer", reader)
    checkpoint = tmp_path / "interactive.json"
    with pytest.raises(RuntimeError, match="Provider unavailable"):
        await interview(
            Settings(_env_file=None),
            fixture,
            "interactive",
            interactive=True,
            checkpoint=checkpoint,
        )
    saved = json.loads(checkpoint.read_text())
    assert [m["content"] for m in saved["transcript"]] == ["How?", "Usaría EXPLAIN."]
    assert saved["ended_reason"] is None
    assert reader.await_count == 3
    assert saved["milestones"][0]["primary_questions"] == 1


async def test_late_model_question_is_discarded_at_interview_deadline(monkeypatch):
    fixture = json.loads((ROOT / "scripts/fixtures/text_interview.json").read_text())
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(text, "time", SimpleNamespace(monotonic=lambda: clock.now))

    async def late(*_):
        clock.now = 2000
        return TurnDecision(
            action="question", spoken_text="Too late?", target_milestone_id="sql"
        ), []

    monkeypatch.setattr(text, "next_decision", late)
    run = await interview(Settings(_env_file=None), fixture, "interactive", interactive=True)
    assert run["ended_reason"] == "timeout"
    assert not run["transcript"]
    assert all(m["primary_questions"] == 0 for m in run["milestones"])
    assert run["decisions"][0]["discarded_decision"]["spoken_text"] == "Too late?"


async def test_console_reader_handles_piped_lines_partial_writes_and_eof(monkeypatch):
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "r", encoding="utf-8") as stream:
        monkeypatch.setattr(sys, "stdin", stream)
        try:
            os.write(write_fd, "Primera respuesta\nSegunda: sí\n".encode())
            assert await text.read_console_answer(1) == "Primera respuesta"
            assert await text.read_console_answer(1) == "Segunda: sí"
            os.write(write_fd, b"partial")
            with pytest.raises(TimeoutError):
                await text.read_console_answer(0.02)
        finally:
            os.close(write_fd)
        assert await text.read_console_answer(1) is None


async def test_schema_repair_reuses_deadline_and_production_request(monkeypatch):
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(text, "time", SimpleNamespace(monotonic=lambda: clock.now))

    async def respond(messages, config):
        assert config["callbacks"]
        payload = json.loads(messages[1].content)
        if "repair" not in payload:
            clock.now += 4
            raise ValueError("Schema parse failure")
        assert "Schema parse failure" in payload["repair"]["error"]
        assert payload["repair"]["invalid_decision"] == {}
        return TurnDecision(action="question", spoken_text="How?", target_milestone_id="sql")

    structured = Mock(ainvoke=AsyncMock(side_effect=respond))
    model = Mock(with_structured_output=Mock(return_value=structured))
    factory = Mock(return_value=model)
    monkeypatch.setattr(text, "build_chat_model", factory)
    monkeypatch.setattr(text, "close_chat_model", AsyncMock())
    context = {
        "system_prompt": "Interview at the pinned level.",
        "transcript": [],
        "milestones": [{"id": "sql", "lifecycle": "pending", "primary_questions": 0}],
        "remaining_seconds": 500,
        "primary_questions": 0,
        "question_limit": 3,
        "previous_questions": [],
    }
    await next_decision(Settings(_env_file=None), context, Mock())
    assert [c.kwargs["timeout_seconds"] for c in factory.call_args_list] == [15, 11]
    assert [c.kwargs["max_retries"] for c in factory.call_args_list] == [1, 1]
    model.with_structured_output.assert_called_with(TurnDecision, method="json_schema")


async def test_decision_deadline_stops_hung_model_and_expired_interview_needs_no_model(monkeypatch):
    structured = Mock(ainvoke=AsyncMock(side_effect=lambda *_args, **_kw: None))

    async def hung(*_args, **_kwargs):
        await asyncio.Event().wait()

    structured.ainvoke.side_effect = hung
    factory = Mock(return_value=Mock(with_structured_output=Mock(return_value=structured)))
    monkeypatch.setattr(text, "build_chat_model", factory)
    monkeypatch.setattr(text, "close_chat_model", AsyncMock())
    monkeypatch.setattr(text, "DECISION_SECONDS", 0.02)
    with pytest.raises(TimeoutError):
        await next_decision(
            Settings(_env_file=None),
            {
                "system_prompt": "Interview.",
                "remaining_seconds": 50,
            },
            Mock(),
        )
    factory.assert_called_once()
    decision, _ = await next_decision(
        Settings(_env_file=None),
        {
            "remaining_seconds": 0,
            "milestones": [{"id": "sql", "lifecycle": "pending"}],
        },
        Mock(),
    )
    assert decision.close_reason == "timeout"
    factory.assert_called_once()


async def test_console_planning_resolves_auto_seniority_and_preserves_configuration(monkeypatch):
    from interview_agent.interview.models import InterviewPlan

    fixture = json.loads((ROOT / "scripts/fixtures/text_interview.json").read_text())
    planner = AsyncMock(
        return_value=InterviewPlan(
            persona="Lucía, entrevistadora",
            summary="Fit",
            focus_areas=["SQL"],
            detected_seniority="junior",
            seniority_evidence="Backend junior",
            budget={"max_minutes": 8, "question_limit": 3, "followup_limit": 1},
            preferences={
                "objective": "practice",
                "interview_type": "technical",
                "practice_focus": "SQL",
            },
            milestones=[
                {
                    "title": "SQL",
                    "description": "Investigate a query",
                    "expected_evidence": "Explain plan",
                    "requirement_id": "sql",
                    "resume_quotes": ["Sé usar EXPLAIN"],
                }
            ],
        )
    )
    monkeypatch.setattr(text, "run_planner", planner)
    args = text.parse_args(
        [
            "--demo",
            "--seniority",
            "auto",
            "--agent-name",
            "Lucía",
            "--persona",
            "Directa",
            "--custom-instructions",
            "Preguntas breves",
            "--objective",
            "practice",
            "--practice-focus",
            "SQL",
        ],
        interactive_default=True,
    )
    planned = await text.plan_console_interview(Settings(_env_file=None), args, fixture)
    assert planner.call_args.kwargs["seniority"] is None
    assert planned["seniority"] == "junior"
    assert planned["seniority_source"] == "detected"
    assert planned["plan"]["budget"]["question_limit"] == 1
    assert planned["plan"]["preferences"]["practice_focus"] == "SQL"
    assert planned["milestones"][0]["planning_context"]["resume_quotes"] == ["Sé usar EXPLAIN"]
    assert planned["agent_settings"]["agent_name"] == "Lucía"
    assert planned["custom_instructions"] == "Preguntas breves"


async def test_frontend_snapshot_uses_saved_setup_and_never_updates_source(
    postgres_sessionmaker, monkeypatch
):
    from sqlalchemy import text as sql_text

    from interview_agent.interview import db

    interview_id, milestone_id = uuid.uuid4(), uuid.uuid4()
    config = {
        "models": {
            component: {"model": f"saved-{component}", "reasoning_effort": "low"}
            for component in ("planner", "interviewer", "evaluator")
        }
    }
    async with postgres_sessionmaker() as session:
        session.add(
            db.Conversation(
                id=interview_id,
                status="evaluated",
                job_offer="Offer",
                resume_markdown="CV",
                plan={"persona": "Alex", "summary": "Fit"},
                agent_settings={"agent_name": "Alex", "language": "en"},
                persona="Direct",
                custom_instructions="Concise questions",
                interview_preferences={"interview_type": "behavioral", "objective": "practice"},
                seniority="senior",
                seniority_source="explicit",
                interview_length="deep",
                max_minutes=20,
                question_limit=1,
                followup_limit=2,
                run_config=config,
                milestones=[
                    db.Milestone(
                        id=milestone_id,
                        position=0,
                        title="Topic",
                        description="Scenario",
                        expected_evidence="Bar",
                        planning_context={"requirement_id": "r1", "resume_quotes": ["CV"]},
                        lifecycle="closed",
                        close_reason="covered",
                        primary_questions=1,
                        followups=2,
                        clarifications=1,
                        completed=True,
                    )
                ],
            )
        )
        await session.commit()
    settings = Settings(_env_file=None).model_copy(
        update={
            "database_url": postgres_sessionmaker.kw["bind"].url.render_as_string(
                hide_password=False
            ),
            "interview_max_minutes": 1,  # Must not rewrite the saved 20-minute budget.
        }
    )
    original_get = db.get_conversation

    async def check_read_only(session, identifier):
        assert await session.scalar(sql_text("SHOW transaction_read_only")) == "on"
        return await original_get(session, identifier)

    monkeypatch.setattr(db, "get_conversation", check_read_only)
    fixture = await text.load_frontend_interview(settings, interview_id)
    monkeypatch.setattr(db, "get_conversation", original_get)

    async def decide(effective, context, usage):
        assert effective.interviewer_model == "saved-interviewer"
        assert context["remaining_seconds"] > 1100
        assert context["followup_limit"] == 2
        assert context["primary_questions"] == 0
        assert context["milestones"][0]["lifecycle"] == "pending"
        assert "Concise questions" in context["system_prompt"]
        assert "Direct" in context["system_prompt"]
        return TurnDecision(
            action="question", spoken_text="Your example?", target_milestone_id=str(milestone_id)
        ), []

    monkeypatch.setattr(text, "next_decision", decide)
    monkeypatch.setattr(text, "read_console_answer", AsyncMock(return_value="/end"))
    run = await interview(settings, fixture, "interactive", interactive=True)
    assert run["source_interview_id"] == str(interview_id)
    assert run["plan"]["preferences"]["objective"] == "practice"
    assert run["agent_settings"] == {"agent_name": "Alex", "language": "en"}
    evaluator = AsyncMock(return_value=SimpleNamespace(model_dump=lambda **_: {}))
    monkeypatch.setattr(text, "run_evaluator", evaluator)
    await text.evaluate(settings, run)
    assert evaluator.call_args.args[0].evaluator_model == "saved-evaluator"
    assert evaluator.call_args.kwargs["seniority"] == "senior"
    assert evaluator.call_args.kwargs["language"] == "en"
    assert evaluator.call_args.kwargs["custom_instructions"] == "Concise questions"
    async with postgres_sessionmaker() as session:
        saved = await db.get_conversation(session, interview_id)
        assert saved.status == "evaluated"
        assert saved.milestones[0].lifecycle == "closed"
        assert saved.milestones[0].followups == 2
        assert saved.plan == {"persona": "Alex", "summary": "Fit"}


def test_console_cli_rejects_ambiguous_sources_and_conflicting_snapshot_overrides():
    for arguments in (
        [],
        ["--resume", "cv.md"],
        ["--demo", "--job-offer", "role.txt"],
        ["--from-interview", str(uuid.uuid4()), "--language", "es"],
    ):
        with pytest.raises(SystemExit) as error:
            text.parse_args(arguments, interactive_default=True)
        assert error.value.code == 2
    first = text.parse_args(["--demo"], interactive_default=True)
    second = text.parse_args(["--demo"], interactive_default=True)
    assert first.output != second.output
