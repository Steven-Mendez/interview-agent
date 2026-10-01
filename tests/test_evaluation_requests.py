"""Crash recovery, request supersession and leases with PostgreSQL as authority."""

import asyncio
from datetime import timedelta

from sqlalchemy import func, select
from test_routes import _fake_evaluator, _seed_finished_interview

from interview_agent.interview import db
from interview_agent.server import evaluations


async def expire(sessions, attempt_id):
    async with sessions() as session:
        run = await session.get(db.EvaluationRun, attempt_id)
        run.lease_until = await session.scalar(select(func.clock_timestamp())) - timedelta(
            seconds=1
        )
        await session.commit()


async def test_dead_evaluator_recovers_same_request_and_preserves_abandoned_attempt(
    postgres_sessionmaker, monkeypatch
):
    interview_id = await _seed_finished_interview(postgres_sessionmaker)
    async with postgres_sessionmaker() as session:
        first = await db.claim_evaluation(
            session, interview_id, evaluations.STALE_AFTER, automatic=True
        )
        request_id = (await session.get(db.EvaluationRun, first)).request_id
    await expire(postgres_sessionmaker, first)
    monkeypatch.setattr(evaluations, "run_evaluator", _fake_evaluator)
    runner = evaluations.EvaluationRunner(postgres_sessionmaker)
    assert await runner.reconcile_pending() == 1
    await runner.wait_idle()
    async with postgres_sessionmaker() as session:
        row = await db.get_conversation(session, interview_id)
        assert row.status == "evaluated" and row.evaluation_request_id == request_id
        request = await session.get(db.EvaluationRequest, request_id)
        assert request.attempts == 2 and request.status == "completed"
        assert (await session.get(db.EvaluationRun, first)).status == "abandoned"
        assert row.evaluation.result["request_id"] == str(request_id)
    assert await runner.reconcile_pending() == 0


async def test_crash_after_seal_before_trigger_recovers_without_browser(
    postgres_sessionmaker, monkeypatch
):
    interview_id = await _seed_finished_interview(postgres_sessionmaker)
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, interview_id)
        row.transcript_sealed_at = await session.scalar(select(func.clock_timestamp()))
        await session.commit()
    monkeypatch.setattr(evaluations, "run_evaluator", _fake_evaluator)
    runner = evaluations.EvaluationRunner(postgres_sessionmaker)
    assert await runner.reconcile_pending() == 1
    await runner.wait_idle()
    assert await runner.reconcile_pending() == 0
    async with postgres_sessionmaker() as session:
        assert (await session.get(db.Conversation, interview_id)).status == "evaluated"
        assert await session.scalar(select(func.count()).select_from(db.EvaluationRequest)) == 1


async def test_three_crashed_attempts_exhaust_one_original_request(postgres_sessionmaker):
    interview_id = await _seed_finished_interview(postgres_sessionmaker)
    async with postgres_sessionmaker() as session:
        attempt = await db.claim_evaluation(session, interview_id, evaluations.STALE_AFTER)
        request_id = (await session.get(db.EvaluationRun, attempt)).request_id
        deadline = (await session.get(db.EvaluationRequest, request_id)).deadline_at
    for ordinal in (2, 3):
        await expire(postgres_sessionmaker, attempt)
        async with postgres_sessionmaker() as session:
            attempt = await db.claim_evaluation(
                session, interview_id, evaluations.STALE_AFTER, recover=True
            )
            run = await session.get(db.EvaluationRun, attempt)
            assert run.request_id == request_id and run.ordinal == ordinal
            assert (await session.get(db.EvaluationRequest, request_id)).deadline_at == deadline
    await expire(postgres_sessionmaker, attempt)
    async with postgres_sessionmaker() as session:
        assert (
            await db.claim_evaluation(session, interview_id, evaluations.STALE_AFTER, recover=True)
            is None
        )
        row = await session.get(db.Conversation, interview_id)
        assert row.status == "evaluation_failed"
        assert (await session.get(db.EvaluationRequest, request_id)).status == "failed"


async def test_provider_failure_is_final_and_not_retried_behind_the_ui(
    postgres_sessionmaker, monkeypatch
):
    interview_id = await _seed_finished_interview(postgres_sessionmaker)

    async def boom(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(evaluations, "run_evaluator", boom)
    runner = evaluations.EvaluationRunner(postgres_sessionmaker)
    async with postgres_sessionmaker() as session:
        attempt = await db.claim_evaluation(session, interview_id, evaluations.STALE_AFTER)
    runner.start(interview_id, attempt)
    await runner.wait_idle()
    # The UI shows a failure only when no background retry is coming.
    assert await runner.reconcile_pending() == 0
    async with postgres_sessionmaker() as session:
        row = await session.get(db.Conversation, interview_id)
        assert row.status == "evaluation_failed"
        request = await session.get(db.EvaluationRequest, row.evaluation_request_id)
        assert request.status == "failed" and request.attempts == 1


async def test_expired_evaluator_cannot_renew_or_publish(postgres_sessionmaker, monkeypatch):
    interview_id = await _seed_finished_interview(postgres_sessionmaker)
    async with postgres_sessionmaker() as session:
        attempt = await db.claim_evaluation(session, interview_id, evaluations.STALE_AFTER)

    async def evaluate(*args, **kwargs):
        await expire(postgres_sessionmaker, attempt)
        return await _fake_evaluator(*args, **kwargs)

    monkeypatch.setattr(evaluations, "run_evaluator", evaluate)
    runner = evaluations.EvaluationRunner(postgres_sessionmaker)
    runner.start(interview_id, attempt)
    await runner.wait_idle()
    async with postgres_sessionmaker() as session:
        assert not await db.heartbeat_evaluation(session, interview_id, attempt)
        row = await db.get_conversation(session, interview_id)
        assert row.evaluation is None


async def test_manual_request_supersedes_running_automatic_even_if_old_finishes_last(
    postgres_sessionmaker, monkeypatch
):
    interview_id = await _seed_finished_interview(postgres_sessionmaker)
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def evaluate(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
        return await _fake_evaluator(*args, **kwargs)

    monkeypatch.setattr(evaluations, "run_evaluator", evaluate)
    runner = evaluations.EvaluationRunner(postgres_sessionmaker)
    async with postgres_sessionmaker() as session:
        first = await db.claim_evaluation(
            session, interview_id, evaluations.STALE_AFTER, automatic=True
        )
    runner.start(interview_id, first)
    await asyncio.wait_for(started.wait(), 1)
    try:
        async with postgres_sessionmaker() as session:
            second = await db.claim_evaluation(session, interview_id, evaluations.STALE_AFTER)
            latest_request = (await session.get(db.EvaluationRun, second)).request_id
        assert first != second
        runner.start(interview_id, second)
        assert runner.running == 2
        async with asyncio.timeout(2):
            while runner.running == 2:
                await asyncio.sleep(0.01)
        release.set()
        await runner.wait_idle()
        async with postgres_sessionmaker() as session:
            row = await db.get_conversation(session, interview_id)
            assert row.evaluation.result["request_id"] == str(latest_request)
            assert (await session.get(db.EvaluationRun, first)).status == "superseded"
            assert (
                await db.claim_evaluation(
                    session, interview_id, evaluations.STALE_AFTER, automatic=True
                )
                is None
            )
    finally:
        release.set()
        await runner.shutdown()
