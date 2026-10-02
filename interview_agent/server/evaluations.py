"""Background evaluations with an immutable input and an ownership token."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from datetime import timedelta

from langchain_core.callbacks import UsageMetadataCallbackHandler
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from interview_agent.config import settings
from interview_agent.interview import db
from interview_agent.interview.evaluation_contract import (
    canonical_message_records,
    validate_evaluation,
)
from interview_agent.interview.evaluation_requests import owned
from interview_agent.interview.evaluator import run_evaluator
from interview_agent.interview.models import Seniority
from interview_agent.interview.seals import seal_invalid
from interview_agent.llm import summarize_usage
from interview_agent.observability import LLMObserver, Telemetry, content_hash
from interview_agent.runtime import process_manifest, record_manifest

logger = logging.getLogger("interview_agent.server")
HEARTBEAT_SECONDS = 30
STALE_AFTER = timedelta(minutes=2)


async def record_spent_usage(session, conversation_id, component, handler):
    usage = summarize_usage(handler.usage_metadata)
    if any(usage.values()):
        await db.add_token_usage(session, conversation_id, component, usage)


class EvaluationRunner:
    def __init__(self, sessionmaker):
        self._sessionmaker = sessionmaker
        self._tasks = {}

    @property
    def running(self):
        return len(self._tasks)

    def start(self, interview_id: uuid.UUID, claim_id: uuid.UUID | None = None):
        key = (interview_id, claim_id)
        if key in self._tasks:
            return
        task = asyncio.create_task(
            self._run(interview_id, claim_id), name=f"evaluate-{interview_id}"
        )
        self._tasks[key] = task
        task.add_done_callback(lambda done: self._forget(key, done))

    def _forget(self, interview_id, task):
        if self._tasks.get(interview_id) is task:
            del self._tasks[interview_id]

    async def wait_idle(self):
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    async def reconcile_pending(self):
        recovered = 0
        cursor = None
        while True:
            async with self._sessionmaker() as session:
                query = (
                    select(db.Conversation.id)
                    .where(
                        db.Conversation.status.in_(
                            ("completed", "evaluating", "evaluation_failed")
                        ),
                        (db.Conversation.evaluation_request_id.is_not(None))
                        | (db.Conversation.transcript_sealed_at.is_not(None)),
                    )
                    .order_by(db.Conversation.id)
                    .limit(128)
                )
                if cursor is not None:
                    query = query.where(db.Conversation.id > cursor)
                ids = list(await session.scalars(query))
            for interview_id in ids:
                try:
                    async with asyncio.timeout(2), self._sessionmaker() as session:
                        conversation = await session.get(db.Conversation, interview_id)
                        if conversation is None:
                            continue
                        claim_id = await db.claim_evaluation(
                            session,
                            interview_id,
                            STALE_AFTER,
                            automatic=conversation.evaluation_request_id is None,
                            recover=conversation.evaluation_request_id is not None,
                        )
                    if claim_id is not None:
                        self.start(interview_id, claim_id)
                        recovered += 1
                except Exception:
                    logger.exception("Evaluation recovery failed")
            if len(ids) < 128:
                return recovered
            cursor = ids[-1]

    async def shutdown(self):
        for task in list(self._tasks.values()):
            task.cancel()
        await self.wait_idle()

    async def _run(self, interview_id, claim_id):
        if claim_id is None:
            async with self._sessionmaker() as session:
                conversation = await db.get_conversation(session, interview_id)
                claim_id = conversation.evaluation_claim_id if conversation else None
        if claim_id is None:
            return
        heartbeat = asyncio.create_task(self._heartbeat(interview_id, claim_id))
        try:
            await self._evaluate(interview_id, claim_id)
        except asyncio.CancelledError:
            # Shutdown: the row stays `evaluating` and the lease expires, so the
            # sweeper of the next process recovers the same request.
            raise
        except Exception:
            logger.exception("Evaluation crashed for %s", interview_id)
            await self._fail(interview_id, claim_id)
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def _heartbeat(self, interview_id, claim_id):
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                async with self._sessionmaker() as session:
                    alive = await db.heartbeat_evaluation(session, interview_id, claim_id)
            except Exception:
                logger.exception("Evaluation heartbeat failed")
                continue
            if not alive:
                return

    async def _fail(self, interview_id, claim_id, usage=None):
        try:
            async with self._sessionmaker() as session:
                conversation = await session.scalar(
                    select(db.Conversation)
                    .where(db.Conversation.id == interview_id)
                    .with_for_update()
                )
                run = await session.get(db.EvaluationRun, claim_id)
                if run is None:
                    return
                if run.status != "running":
                    if usage is not None and run.usage is None:
                        run.usage = summarize_usage(usage.usage_metadata)
                        await db.add_token_usage(
                            session, interview_id, "evaluator", run.usage, commit=False
                        )
                        await session.commit()
                    return
                current = await owned(session, conversation, run)
                run.status = "failed"
                run.error = "evaluation_contract_or_provider_error"
                run.finished_at = await session.scalar(select(func.clock_timestamp()))
                if usage is not None:
                    run.usage = summarize_usage(usage.usage_metadata)
                    await db.add_token_usage(
                        session, interview_id, "evaluator", run.usage, commit=False
                    )
                if current:
                    # A provider or contract failure is final for this request; the
                    # UI offers a manual retry, which creates a new one. Only an
                    # expired lease (crash) is recovered automatically.
                    request = await session.get(db.EvaluationRequest, run.request_id)
                    request.status = "failed"
                    conversation.status = "evaluation_failed"
                await session.commit()
        except Exception:
            logger.exception("Could not persist evaluation failure")

    async def _evaluate(self, interview_id, claim_id):
        async with self._sessionmaker() as session:
            conversation = await session.scalar(
                select(db.Conversation).where(db.Conversation.id == interview_id).with_for_update()
            )
            run = await session.get(db.EvaluationRun, claim_id)
            if not await owned(session, conversation, run):
                return
            messages = await db.get_messages(session, interview_id)
            milestones = await db.get_milestones(session, interview_id)
            request = await session.get(db.EvaluationRequest, run.request_id)
            seal = await session.get(db.TranscriptSeal, request.seal_id)
            records = seal.records
            if content_hash(canonical_message_records(messages)) != seal.provenance.get(
                "canonical_hash"
            ):
                raise ValueError("Canonical transcript changed after sealing")
            criteria = [
                {
                    "id": str(m.id),
                    "title": m.title,
                    "description": m.description,
                    "expected_evidence": m.expected_evidence,
                    "essential": m.essential,
                    "lifecycle": m.lifecycle,
                    "close_reason": m.close_reason,
                }
                for m in milestones
            ]
            resume = conversation.resume_markdown
            offer = conversation.job_offer
            plan = conversation.plan or {}
            reason = conversation.ended_reason or "unknown"
            custom = conversation.custom_instructions
            seniority = conversation.seniority
            thread_root = conversation.repeat_of_id
            transcript_complete = (
                seal.integrity not in ("partial", "failed")
                and not conversation.capture_integrity_pending
                and not await seal_invalid(session, request.seal_id)
            )
            capture_integrity_pending = conversation.capture_integrity_pending
            language = (conversation.agent_settings or {}).get(
                "language", plan.get("language", "en")
            )
            config = conversation.run_config or {}
            model_config = config.get("models", {}).get("evaluator", {})
            effective = settings.model_copy(
                update={
                    "evaluator_model": model_config.get("model", settings.evaluator_model),
                    "evaluator_reasoning_effort": model_config.get(
                        "reasoning_effort", settings.evaluator_reasoning_effort
                    ),
                }
            )
            request = await session.get(db.EvaluationRequest, run.request_id)
            now = await session.scalar(select(func.clock_timestamp()))
            remaining_seconds = max(0, (request.deadline_at - now).total_seconds())
            input_hash = content_hash(records)
            if input_hash != run.transcript_hash:
                raise ValueError("Evaluation request transcript changed before execution")
            run.config = {
                **config,
                "model": effective.evaluator_model,
                "reasoning_effort": effective.evaluator_reasoning_effort,
                "language": language,
                "seniority": seniority,
                "transcript_complete": transcript_complete,
            }
            manifest = await process_manifest(
                effective,
                "evaluator",
                config=config,
                functions=(self._evaluate.__func__, run_evaluator),
            )
            run.config["runtime_manifest_id"] = manifest["id"]
            await session.commit()

        await record_manifest(self._sessionmaker, manifest, conversation_id=interview_id)
        telemetry = Telemetry(interview_id, config, process="evaluator", thread_of=thread_root)
        observer = LLMObserver(
            telemetry, "evaluator", effective.evaluator_model, effective.evaluator_reasoning_effort
        )
        usage = UsageMetadataCallbackHandler()
        try:
            async with asyncio.timeout(remaining_seconds):
                result = await run_evaluator(
                    effective,
                    resume_markdown=resume,
                    job_offer=offer,
                    plan=plan,
                    milestones=criteria,
                    transcript=records,
                    ended_reason=reason,
                    seniority=seniority,
                    custom_instructions=custom,
                    language=language,
                    usage_callback=usage,
                    telemetry_callback=observer,
                    transcript_complete=transcript_complete,
                    langsmith_extra={"metadata": telemetry.trace_metadata()},
                )
            # Also validates mocked/custom evaluator integrations at the write boundary.
            result = validate_evaluation(
                result,
                seniority=Seniority(seniority),
                milestones=criteria,
                records=records,
                transcript_complete=transcript_complete,
            )
            values = {
                "hired": result.hired,
                "score": result.score,
                "strengths": result.strengths,
                "weaknesses": result.weaknesses,
                "rationale": result.rationale,
                "seniority_evaluated": result.seniority_evaluated.value,
                "calibration_notes": result.calibration_notes,
                "ended_by": reason,
                "result": result.model_dump(mode="json"),
            }
            async with self._sessionmaker() as session:
                # Lock once: ownership, spend, immutable history and the public result
                # all land in the same transaction.
                conversation = await session.scalar(
                    select(db.Conversation)
                    .where(db.Conversation.id == interview_id)
                    .with_for_update()
                )
                run = await session.get(db.EvaluationRun, claim_id)
                run.result = result.model_dump(mode="json")
                run.usage = summarize_usage(usage.usage_metadata)
                owns = await owned(session, conversation, run)
                request = await session.get(db.EvaluationRequest, run.request_id)
                sealed = await session.get(db.TranscriptSeal, request.seal_id)
                current_records = sealed.records
                if owns and content_hash(
                    canonical_message_records(await db.get_messages(session, interview_id))
                ) != sealed.provenance.get("canonical_hash"):
                    raise ValueError("Canonical transcript changed after sealing")
                if owns and conversation.capture_integrity_pending != capture_integrity_pending:
                    raise ValueError("Capture integrity changed during evaluation")
                if owns and content_hash(current_records) != input_hash:
                    raise ValueError("Transcript changed during evaluation")
                if run.status == "running":
                    run.status = "completed" if owns else "superseded"
                run.finished_at = await session.scalar(select(func.clock_timestamp()))
                await db.add_token_usage(
                    session, interview_id, "evaluator", run.usage, commit=False
                )
                if owns:
                    request = await session.get(db.EvaluationRequest, run.request_id)
                    request.status = "completed"
                    values["result"]["request_id"] = str(request.id)
                    values["result"]["seal_id"] = str(request.seal_id)
                    await session.execute(
                        pg_insert(db.Evaluation)
                        .values(conversation_id=interview_id, **values)
                        .on_conflict_do_update(index_elements=["conversation_id"], set_=values)
                    )
                    conversation.status = "evaluated"
                await session.commit()
            telemetry.emit("evaluation", "coverage", result.coverage)
            telemetry.emit("evaluation", "complete", int(result.evaluation_status == "complete"))
        except Exception:
            logger.exception("Evaluation failed for %s", interview_id)
            await self._fail(interview_id, claim_id, usage)
        finally:
            await telemetry.drain()
