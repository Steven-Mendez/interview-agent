"""Background evaluation runs.

POST /interviews/{id}/evaluate used to run the evaluator INSIDE the request:
an HTTP call that lasted as long as a high-reasoning LLM call, a worker that
had to wait on it with retries and timeouts, and a duplicate run whenever
anything in between gave up early. The endpoint now CLAIMS the row (one
atomic UPDATE into "evaluating", see db.claim_evaluation) and hands the id to
the runner below, which evaluates in the API process, heartbeats the row
while it does, and writes the outcome. Clients poll GET /interviews/{id}.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from datetime import timedelta

from langchain_core.callbacks import UsageMetadataCallbackHandler
from qdrant_client import AsyncQdrantClient
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from interview_agent.config import settings
from interview_agent.interview import db, rag
from interview_agent.interview.evaluator import run_evaluator
from interview_agent.llm import summarize_usage

logger = logging.getLogger("interview_agent.server")

# A live run bumps the row's updated_at this often. A row in "evaluating"
# that has not moved for STALE_AFTER belongs to a process that died (the API
# restarted mid-run) and can be claimed again; the UI's own "taking too long"
# clock is anchored on the same column, so it only ever fires for dead runs.
HEARTBEAT_SECONDS = 30
STALE_AFTER = timedelta(minutes=2)


async def record_spent_usage(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    component: str,
    handler: UsageMetadataCallbackHandler,
) -> None:
    """Book the tokens a FAILED planner/evaluator run consumed. The callback
    fires per retry attempt, so a run that never produced a usable result
    still spent whatever it reports; only a run that never reached the model
    (nothing to book) is skipped."""
    usage = summarize_usage(handler.usage_metadata)
    if any(usage.values()):
        await db.add_token_usage(session, conversation_id, component, usage)


class EvaluationRunner:
    """Owns the in-flight evaluations of one API process."""

    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], qdrant: AsyncQdrantClient
    ) -> None:
        self._sessionmaker = sessionmaker
        self._qdrant = qdrant
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}

    @property
    def running(self) -> int:
        return len(self._tasks)

    def start(self, interview_id: uuid.UUID) -> None:
        """Schedule the run for a row the caller has ALREADY claimed."""
        task = asyncio.create_task(self._run(interview_id), name=f"evaluate-{interview_id}")
        self._tasks[interview_id] = task
        task.add_done_callback(lambda done: self._forget(interview_id, done))

    def _forget(self, interview_id: uuid.UUID, task: asyncio.Task[None]) -> None:
        if self._tasks.get(interview_id) is task:
            del self._tasks[interview_id]

    async def wait_idle(self) -> None:
        """Block until every scheduled run has finished."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    async def shutdown(self) -> None:
        """Cancel what is still running. Each run marks its row
        evaluation_failed on the way out, so the UI offers a retry instead of
        a spinner over a row nobody is working on any more."""
        for task in list(self._tasks.values()):
            task.cancel()
        await self.wait_idle()

    async def _run(self, interview_id: uuid.UUID) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(interview_id))
        try:
            await self._evaluate(interview_id)
        except asyncio.CancelledError:
            await self._fail(interview_id)
            raise
        except Exception:
            logger.exception("evaluation run crashed for %s", interview_id)
            await self._fail(interview_id)
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def _heartbeat(self, interview_id: uuid.UUID) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                async with self._sessionmaker() as session:
                    alive = await db.heartbeat_evaluation(session, interview_id)
            except Exception:
                logger.exception("heartbeat failed for %s; retrying", interview_id)
                continue
            if not alive:
                # Finished, or reclaimed by another process: not ours any more.
                return

    async def _fail(
        self, interview_id: uuid.UUID, usage: UsageMetadataCallbackHandler | None = None
    ) -> None:
        try:
            async with self._sessionmaker() as session:
                # Spend first, status last: the status flip is what a poller
                # acts on, so everything it will read must already be there.
                if usage is not None:
                    await record_spent_usage(session, interview_id, "evaluator", usage)
                # Guarded: a run that lost its claim (reclaimed after going
                # stale) must not stamp its failure over the newer run.
                await db.set_status_if(session, interview_id, "evaluating", "evaluation_failed")
        except Exception:
            logger.exception("could not record the failed evaluation of %s", interview_id)

    async def _evaluate(self, interview_id: uuid.UUID) -> None:
        # Session 1 (short): load everything the evaluator needs, then release
        # the connection — the LLM call can take minutes, and holding a
        # transaction open across it is pure waste.
        async with self._sessionmaker() as session:
            conversation = await db.get_conversation(session, interview_id)
            if conversation is None or conversation.status != "evaluating":
                logger.warning(
                    "evaluation of %s skipped: row is %s",
                    interview_id,
                    conversation.status if conversation else "gone",
                )
                return
            messages = await db.get_messages(session, interview_id)
            milestones = await db.get_milestones(session, interview_id)
            resume_markdown = conversation.resume_markdown
            job_offer = conversation.job_offer
            plan = conversation.plan or {}
            ended_reason = conversation.ended_reason or "unknown"
            custom_instructions = conversation.custom_instructions
            seniority = conversation.seniority
        if not messages:
            logger.error("evaluation of %s has no transcript to score", interview_id)
            await self._fail(interview_id)
            return

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
                        "expected_evidence": m.expected_evidence,
                        "completed": m.completed,
                        "notes": m.notes,
                    }
                    for m in milestones
                ],
                transcript=[(m.role, m.content) for m in messages],
                ended_reason=ended_reason,
                seniority=seniority,
                custom_instructions=custom_instructions,
                usage_callback=evaluator_usage,
            )
        except Exception:
            # Surface the failure: the frontend polls status and offers a retry
            # instead of spinning forever.
            logger.exception("evaluation failed for %s", interview_id)
            await self._fail(interview_id, evaluator_usage)
            return

        if result.seniority_evaluated.value != seniority:
            # The level is pinned and handed to the evaluator; an echo of a
            # different one means the calibration did not hold on this run.
            # Stored as returned (it is what the score was judged against) but
            # made visible here.
            logger.warning(
                "evaluator judged %s against '%s' instead of the pinned '%s'",
                interview_id,
                result.seniority_evaluated.value,
                seniority,
            )

        # Session 2 (write): upsert instead of delete+insert, and the status
        # UNGUARDED — a result is never wrong, so even a run that lost its
        # claim (reclaimed after a heartbeat outage) gets to land it; the
        # newer run overwrites it when it finishes, last commit wins.
        values = {
            "hired": result.hired,
            "score": result.score,
            "strengths": result.strengths,
            "weaknesses": result.weaknesses,
            "rationale": result.rationale,
            "seniority_evaluated": result.seniority_evaluated.value,
            "calibration_notes": result.calibration_notes,
            "ended_by": ended_reason,
        }
        async with self._sessionmaker() as session:
            # Spend first, status last: the flip to "evaluated" is what a
            # poller acts on, so the result and the usage must land before it.
            # Re-evaluations accumulate on purpose: those tokens were spent.
            await db.add_token_usage(
                session,
                interview_id,
                "evaluator",
                summarize_usage(evaluator_usage.usage_metadata),
            )
            await session.execute(
                pg_insert(db.Evaluation)
                .values(conversation_id=interview_id, **values)
                .on_conflict_do_update(index_elements=["conversation_id"], set_=values)
            )
            await session.execute(
                update(db.Conversation)
                .where(db.Conversation.id == interview_id)
                .values(status="evaluated")
            )
            await session.commit()

        logger.info(
            "interview evaluated",
            extra={
                "conversation": str(interview_id),
                "hired": result.hired,
                "score": result.score,
                "seniority": result.seniority_evaluated.value,
            },
        )

        # The resume chunks only exist for the interviewer's search_resume; the
        # evaluation is done, so drop them (PII). Best-effort: the purge job
        # sweeps anything missed here.
        try:
            await rag.delete_resume_points(self._qdrant, settings, [interview_id])
        except Exception:
            logger.exception("failed to delete resume points for %s", interview_id)
