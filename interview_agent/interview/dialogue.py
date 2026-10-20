"""Typed per-turn LangGraph controller. PostgreSQL owns durable conversation state."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, TypedDict

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_openai.chat_models.base import OpenAIRefusalError
from langgraph.graph import END, START, StateGraph
from livekit.agents import llm
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS
from openai import APIError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

from interview_agent.interview import db, turns
from interview_agent.interview.decision import (
    request_decision,
    system_prompt,
    timeout_decision,
    validate_decision,
)
from interview_agent.interview.delivery import QuestionDeliveries, save_question
from interview_agent.interview.evaluation_contract import canonical_message_records
from interview_agent.interview.models import TurnDecision
from interview_agent.interview.transcription import admit_candidate
from interview_agent.interview.turns import (
    DecisionLimitError,
    InvocationReservation,
    TurnContextChangedError,
    TurnCoordinator,
    TurnDecisionError,
    TurnLease,
    TurnOwnershipError,
    TurnQueueTimeoutError,
)
from interview_agent.llm import (
    build_chat_model,
    chat_model_tuning,
    close_chat_model,
    summarize_usage,
)
from interview_agent.observability import LLMObserver, Telemetry

FINALIZE_SECONDS = 1

logger = logging.getLogger(__name__)


class DialogueState(TypedDict, total=False):
    turn_id: str
    context: dict[str, Any]
    decision: dict[str, Any]
    replayed: bool
    error: str
    schema_error: str
    attempts: int
    stale: bool
    synthetic: bool
    reload_required: bool
    lease: TurnLease
    initial_reservation: InvocationReservation | None


# Named, not lambdas: LangSmith shows each edge run under its function name.
def after_load(state: DialogueState) -> str:
    return END if state.get("replayed") else "decide"


def after_decide(state: DialogueState) -> str:
    return "load" if state.get("reload_required") else "validate"


def after_validate(state: DialogueState) -> str:
    return "decide" if state.get("error") else "persist"


def after_persist(state: DialogueState) -> str:
    return "load" if state.get("stale") else END


class DialogueController:
    def __init__(
        self,
        settings,
        conversation_id,
        sessionmaker,
        end_event,
        telemetry: Telemetry,
        usage_sink=None,
    ):
        self.settings = settings
        chat_model_tuning(
            settings.interviewer_model,
            reasoning_effort=settings.interviewer_reasoning_effort,
            temperature=settings.interviewer_temperature,
        )
        self.conversation_id = conversation_id
        self.sessionmaker = sessionmaker
        self.end_event = end_event
        self.telemetry = telemetry
        self.usage_sink = usage_sink
        self.end_reason = None
        self.closing = False
        self.turns = TurnCoordinator(conversation_id, sessionmaker)
        self.deliveries = QuestionDeliveries(sessionmaker, conversation_id)
        builder = StateGraph(DialogueState)
        for name, function in (
            ("load", self.load),
            ("decide", self.decide),
            ("validate", self.validate),
            ("persist", self.persist),
        ):
            builder.add_node(name, self._timed(name, function))
        builder.add_edge(START, "load")
        builder.add_conditional_edges("load", after_load)
        builder.add_conditional_edges("decide", after_decide)
        builder.add_conditional_edges("validate", after_validate)
        builder.add_conditional_edges("persist", after_persist)
        self.graph = builder.compile()

    def _timed(self, name, function):
        async def run(state):
            start = time.monotonic()
            try:
                return await function(state)
            finally:
                self.telemetry.emit(
                    "graph", name + "_seconds", time.monotonic() - start, turn_id=state["turn_id"]
                )

        return run

    async def load(self, state):
        async with self.sessionmaker() as session:
            conversation = await db.get_conversation(session, self.conversation_id)
            if conversation is None:
                raise ValueError("Conversation no longer exists")
            if conversation.capture_integrity_pending:
                raise ValueError("Capture integrity requires explicit resolution")
            previous = await session.scalar(
                select(db.TurnRun).where(
                    db.TurnRun.conversation_id == self.conversation_id,
                    db.TurnRun.turn_id == state["turn_id"],
                )
            )
            if previous is not None or conversation.status in (
                "closing",
                "completed",
                "evaluating",
                "evaluated",
            ):
                self.telemetry.emit("dialogue", "replayed_turns", 1, turn_id=state["turn_id"])
                return {"replayed": True}
            milestones = await db.get_milestones(session, self.conversation_id)
            transcript = await db.get_messages(session, self.conversation_id)
            prior = list(
                await session.scalars(
                    select(db.TurnRun)
                    .where(db.TurnRun.conversation_id == self.conversation_id)
                    .order_by(db.TurnRun.created_at)
                )
            )
            now = await session.scalar(select(func.clock_timestamp()))
            context = self.context(conversation, milestones, transcript, now)
            self.add_history(context, prior)
            context["system_prompt"] = system_prompt(conversation)
            execution = (
                await session.get(db.TurnExecution, state["lease"].execution_id)
                if state.get("lease")
                else None
            )
            return {
                "context": context,
                "attempts": execution.invocations_reserved
                if execution is not None
                else state.get("attempts", 0),
                "error": "",
                "schema_error": "",
                "replayed": False,
                "stale": False,
                "synthetic": False,
                "reload_required": False,
            }

    @staticmethod
    def context(conversation, milestones, transcript, now):
        started = conversation.started_at
        remaining = conversation.max_minutes * 60 - max(0, (now - started).total_seconds())
        return {
            "state_revision": conversation.state_revision,
            "milestones": [
                {
                    "id": str(m.id),
                    "title": m.title,
                    "description": m.description,
                    "expected_evidence": m.expected_evidence,
                    "essential": m.essential,
                    "competency": m.competency,
                    "planning_context": m.planning_context,
                    "lifecycle": m.lifecycle,
                    "close_reason": m.close_reason,
                    "primary_questions": m.primary_questions,
                    "followups": m.followups,
                    "clarifications": m.clarifications,
                }
                for m in milestones
            ],
            "transcript": [
                {
                    **record,
                    "interrupted": m.interrupted,
                }
                for record, m in zip(
                    canonical_message_records(transcript),
                    transcript,
                    strict=True,
                )
            ],
            "remaining_seconds": remaining,
            "question_limit": conversation.question_limit,
            "followup_limit": conversation.followup_limit,
            "primary_questions": sum(m.primary_questions for m in milestones),
            "previous_questions": [],
            "language": conversation.agent_settings["language"],
        }

    @staticmethod
    def add_history(context, prior):
        """Previously used evidence prevents reopening on the same candidate answer."""
        milestones = {item["id"]: item for item in context["milestones"]}
        for run in prior:
            if run.decision.get("spoken_text"):
                context["previous_questions"].append(run.decision["spoken_text"])
            for update in run.decision.get("updates", []):
                if update["milestone_id"] in milestones:
                    milestones[update["milestone_id"]].setdefault("evidence", []).extend(
                        update.get("evidence", [])
                    )

    async def decide(self, state):
        context = state["context"]
        if context["remaining_seconds"] <= 0:
            decision = self._timeout_decision(context)
            return {
                "decision": decision.model_dump(mode="json"),
                "attempts": state["attempts"],
                "synthetic": True,
            }
        reservation = state.get("initial_reservation")
        try:
            if reservation is not None:
                reservation = await self.turns.prepare(
                    state["lease"], reservation, context["state_revision"]
                )
            else:
                if state["attempts"] >= 2:
                    raise DecisionLimitError(
                        "Interview decision failed validation after one repair"
                    )
                reservation = await self.turns.reserve(state["lease"], context["state_revision"])
                self.telemetry.emit("dialogue", "invocations_reserved", 1, turn_id=state["turn_id"])
        except TurnContextChangedError:
            self.telemetry.emit("dialogue", "stale_before_invocation", 1, turn_id=state["turn_id"])
            return {"reload_required": True}
        observer = LLMObserver(
            self.telemetry,
            "interviewer",
            self.settings.interviewer_model,
            self.settings.interviewer_reasoning_effort,
            state["turn_id"],
        )
        model = None
        usage = UsageMetadataCallbackHandler()
        repair = None
        if state.get("error"):
            repair = {"invalid_decision": state.get("decision"), "error": state["error"]}
        outcome = "provider_error"
        try:
            async with asyncio.timeout(reservation.remaining_seconds):
                model = build_chat_model(
                    self.settings,
                    model=self.settings.interviewer_model,
                    reasoning_effort=self.settings.interviewer_reasoning_effort,
                    max_retries=1,
                    timeout_seconds=reservation.remaining_seconds,
                    telemetry_callback=observer,
                )
                result = await request_decision(
                    model,
                    context,
                    callbacks=[usage, observer],
                    repair=repair,
                )
            outcome = "returned"
        except OpenAIRefusalError:
            outcome = "refusal"
            raise
        except ValueError as exc:
            outcome = "schema_error"
            # The repair must see why the output was rejected, not a later
            # "field required" from validating an empty decision.
            return {
                "decision": {},
                "schema_error": f"Output did not match the decision schema: {exc}"[:1000],
                "attempts": reservation.ordinal,
                "initial_reservation": None,
            }
        except TimeoutError:
            outcome = "deadline"
            raise DecisionLimitError("Interview decision deadline exhausted") from None
        except asyncio.CancelledError:
            outcome = "deadline" if state["lease"].remaining_seconds <= 0 else "cancelled"
            raise
        finally:
            try:
                await self._finalize(
                    "outcome", self.turns.record_outcome(reservation, outcome), state["turn_id"]
                )
            finally:
                if model is not None:
                    try:
                        async with asyncio.timeout(1):
                            await close_chat_model(model)
                    except TimeoutError:
                        self.telemetry.emit(
                            "dialogue", "client_cleanup_timeouts", 1, turn_id=state["turn_id"]
                        )
                if self.usage_sink is not None:
                    self.usage_sink(summarize_usage(usage.usage_metadata))
        return {
            "decision": result.model_dump(mode="json"),
            "schema_error": "",
            "attempts": reservation.ordinal,
            "initial_reservation": None,
        }

    _timeout_decision = staticmethod(timeout_decision)

    async def validate(self, state):
        try:
            if state.get("schema_error"):
                raise ValueError(state["schema_error"])
            validate_decision(TurnDecision.model_validate(state["decision"]), state["context"])
            return {"error": ""}
        except ValueError as exc:
            self.telemetry.emit("dialogue", "validation_failures", 1, turn_id=state["turn_id"])
            if state.get("synthetic") or state["attempts"] >= 2:
                raise
            return {"error": str(exc)}

    async def persist(self, state):
        barrier = getattr(self, "capture_barrier", None)
        if barrier is not None:
            await barrier()
        decision = TurnDecision.model_validate(state["decision"])
        async with self.sessionmaker() as session:
            conversation = await session.scalar(
                select(db.Conversation)
                .where(db.Conversation.id == self.conversation_id)
                .with_for_update()
            )
            previous = await session.scalar(
                select(db.TurnRun).where(
                    db.TurnRun.conversation_id == self.conversation_id,
                    db.TurnRun.turn_id == state["turn_id"],
                )
            )
            if (
                previous is not None
                or conversation is None
                or conversation.status != "interviewing"
            ):
                return {"replayed": True}
            if conversation.capture_integrity_pending:
                raise ValueError("Capture integrity requires explicit resolution")
            execution, now = await self.turns.owned(session, state["lease"])
            milestones = await db.get_milestones(session, self.conversation_id)
            transcript = await db.get_messages(session, self.conversation_id)
            current = self.context(conversation, milestones, transcript, now)
            if current["remaining_seconds"] <= 0:
                # The wall clock can expire without any revision change while
                # the model runs. Close from locked state, never speak its question.
                decision = self._timeout_decision(current)
            elif conversation.state_revision != state["context"]["state_revision"]:
                self.telemetry.emit("dialogue", "stale_decisions", 1, turn_id=state["turn_id"])
                if state["attempts"] >= 2:
                    raise ValueError("Interview state changed; the bounded retry was exhausted")
                return {"stale": True}
            prior = await session.scalars(
                select(db.TurnRun).where(db.TurnRun.conversation_id == self.conversation_id)
            )
            self.add_history(current, prior)
            validate_decision(decision, current)
            targets = {str(m.id): m for m in milestones}
            for change in decision.updates:
                target = targets[change.milestone_id]
                target.lifecycle = change.status
                target.close_reason = change.close_reason
                target.completed = change.status in ("closed", "skipped")
                if target.completed and target.completed_at is None:
                    target.completed_at = now
                elif not target.completed:
                    target.completed_at = None
                for ref in change.evidence:
                    await session.execute(
                        pg_insert(db.Evidence)
                        .values(
                            id=uuid.uuid4(),
                            conversation_id=self.conversation_id,
                            milestone_id=target.id,
                            message_id=int(ref.message_id),
                            message_version=ref.message_version,
                            quote=ref.quote,
                        )
                        .on_conflict_do_nothing(constraint="evidence_unique")
                    )
            if decision.action != "close":
                target = targets[decision.target_milestone_id]
                target.lifecycle = "active"
                if decision.action in ("question", "advance"):
                    target.primary_questions += 1
                elif decision.action == "followup":
                    target.followups += 1
                else:
                    target.clarifications += 1
            else:
                conversation.status = "closing"
                conversation.closing_started_at = conversation.closing_started_at or now
                conversation.ended_reason = decision.close_reason
                conversation.closing_id = conversation.closing_id or uuid.uuid4()
                conversation.farewell_status = "pending"
            run_id = uuid.uuid4()
            session.add(
                db.TurnRun(
                    id=run_id,
                    conversation_id=self.conversation_id,
                    turn_id=state["turn_id"],
                    decision=decision.model_dump(mode="json"),
                )
            )
            await session.flush()
            if decision.spoken_text:
                await save_question(session, self.conversation_id, run_id, decision.spoken_text)
            execution.status = "applied"
            execution.owner_id = None
            execution.lease_until = None
            conversation.state_revision += 1
            await session.commit()
        self.telemetry.emit(
            "dialogue",
            "actions",
            1,
            turn_id=state["turn_id"],
            dimensions={"action": decision.action},
        )
        if decision.action == "close":
            self.end_reason = decision.close_reason
            self.closing = True
            self.end_event.set()
        return {"decision": decision.model_dump(mode="json")}

    async def run_turn(self, turn_id: str, *, queue_deadline: float | None = None):
        lease = await self.turns.claim(turn_id, queue_deadline=queue_deadline)
        if lease is None:
            self.telemetry.emit("dialogue", "replayed_turns", 1, turn_id=turn_id)
            return {"replayed": True}
        if lease.initial_reservation is not None:
            self.telemetry.emit("dialogue", "invocations_reserved", 1, turn_id=turn_id)
        try:
            async with asyncio.timeout(lease.remaining_seconds):
                return await self.graph.ainvoke(
                    {
                        "turn_id": turn_id,
                        "lease": lease,
                        "initial_reservation": lease.initial_reservation,
                    },
                    config={
                        "recursion_limit": 64,
                        # Each turn is a root in the interview's LangSmith Thread.
                        "run_name": "dialogue_turn",
                        "metadata": self.telemetry.trace_metadata(turn_id=turn_id),
                    },
                )
        except TimeoutError as exc:
            raise TurnDecisionError(
                DecisionLimitError("Interview decision deadline exhausted"), lease
            ) from exc
        except TurnOwnershipError:
            raise
        except Exception as exc:
            raise TurnDecisionError(exc, lease) from exc
        finally:
            await self._finalize("release", self.turns.release(lease), turn_id)

    async def _finalize(self, operation, coroutine, turn_id):
        async def bounded():
            try:
                async with asyncio.timeout(FINALIZE_SECONDS):
                    await coroutine
            except (TimeoutError, SQLAlchemyError, TurnOwnershipError) as exc:
                self.telemetry.emit(
                    "dialogue",
                    operation + "_cleanup_failures",
                    1,
                    turn_id=turn_id,
                    dimensions={"error_type": type(exc).__name__},
                )

        task = asyncio.create_task(bounded())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # An inner model timeout and the outer graph timeout can coincide.
            # Preserve cancellation after the independently bounded diagnostic
            # commit rather than cancelling it a second time mid-transaction.
            await asyncio.shield(task)
            raise

    async def _technical_notice(self, authorization=None, *, unconfirmed_stt=False):
        try:
            async with asyncio.timeout(FINALIZE_SECONDS):
                if authorization is not None and not await authorization:
                    return ""
                async with self.sessionmaker() as session:
                    conversation = await db.get_conversation(session, self.conversation_id)
        except (TimeoutError, SQLAlchemyError, TurnOwnershipError):
            return ""
        if conversation is None or conversation.status != "interviewing":
            return ""
        language = (conversation.agent_settings or {}).get(
            "language", (conversation.plan or {}).get("language", "en")
        )
        if unconfirmed_stt:
            return (
                "No pude confirmar la transcripción de tu respuesta. "
                "Puedes repetirla o terminar la entrevista."
                if language == "es"
                else "I couldn't confirm the transcription of your answer. "
                "You can repeat it or end the interview."
            )
        return (
            "Estoy teniendo una dificultad técnica. "
            "Puedes volver a responder o terminar la entrevista."
            if language == "es"
            else "I'm having a technical difficulty. You can answer again or end the interview."
        )

    notice_callback = None

    async def signal_notice(self, active: bool) -> None:
        """Best effort UI hint; never delays or blocks speech for long."""
        if self.notice_callback is None:
            return
        try:
            async with asyncio.timeout(1):
                await self.notice_callback(active)
        except Exception:
            logger.warning("Technical notice attribute could not be published")

    async def respond(self, chat_ctx):
        if self.closing:
            return ""
        queue_deadline = time.monotonic() + turns.QUEUE_SECONDS
        barrier = getattr(self, "capture_barrier", None)
        if barrier is not None:
            try:
                async with asyncio.timeout_at(queue_deadline):
                    await barrier()
            except (TimeoutError, RuntimeError, TurnOwnershipError):
                return await self._technical_notice(unconfirmed_stt=True)
        user_messages = [m for m in chat_ctx.messages() if m.role == "user" and m.text_content]
        if user_messages:
            incoming = user_messages[-1]
            turn_id = incoming.metrics.get("stt_turn_id") or incoming.id
            try:
                async with asyncio.timeout_at(queue_deadline), self.sessionmaker() as session:
                    admitted = await admit_candidate(
                        session,
                        self.conversation_id,
                        content=incoming.text_content,
                        source_id=incoming.id,
                        metrics=dict(incoming.metrics),
                        interrupted=incoming.interrupted,
                    )
                    if not admitted.accepted or incoming.metrics.get("stt_confirmed") is not True:
                        self.telemetry.emit("stt", "unconfirmed_turns", 1, turn_id=turn_id)
                        return await self._technical_notice(unconfirmed_stt=True)
            except TimeoutError:
                # The answer was not admitted durably, so no other owner can
                # be deciding it: tell the candidate instead of going silent.
                self.telemetry.emit("dialogue", "queue_timeouts", 1, turn_id=turn_id)
                return await self._technical_notice()
        else:
            turn_id = "opening"
        # The durable coordinator serializes both local and replacement workers.
        # A second local lock would add an unbounded wait before its queue budget.
        try:
            result = await self.run_turn(turn_id, queue_deadline=queue_deadline)
        except TurnOwnershipError:
            return ""
        except TurnDecisionError as exc:
            self.telemetry.emit(
                "dialogue",
                "decision_errors",
                1,
                turn_id=turn_id,
                dimensions={"error_type": exc.error_type},
            )
            return await self._technical_notice(self.turns.claim_notice(exc.lease))
        except TurnQueueTimeoutError:
            self.telemetry.emit("dialogue", "queue_timeouts", 1, turn_id=turn_id)
            return await self._technical_notice(self.turns.claim_queue_notice(turn_id))
        except (DecisionLimitError, TimeoutError, APIError):
            # No new owner/notice claim exists for an exhausted replay.
            return ""
        return "" if result.get("replayed") else result["decision"]["spoken_text"]


class DialogueLLM(llm.LLM):
    """Explicit LiveKit integration: only validated, committed speech reaches TTS."""

    def __init__(self, controller):
        super().__init__()
        self.controller = controller

    @property
    def model(self):
        return self.controller.settings.interviewer_model

    @property
    def provider(self):
        return "OpenAI/Responses/LangGraph"

    def chat(self, *, chat_ctx, tools=None, conn_options=DEFAULT_API_CONNECT_OPTIONS, **kwargs):
        return DialogueStream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)


class DialogueStream(llm.LLMStream):
    async def _run(self):
        text = await self._llm.controller.respond(self._chat_ctx)
        if text:
            users = [m for m in self._chat_ctx.messages() if m.role == "user" and m.text_content]
            turn_id = (users[-1].metrics.get("stt_turn_id") or users[-1].id) if users else "opening"
            speech = await self._llm.controller.deliveries.claim(turn_id=turn_id)
            # A fixed technical notice has no validated question to deliver.
            async with self._llm.controller.sessionmaker() as session:
                applied = await session.scalar(
                    select(db.TurnRun.id).where(
                        db.TurnRun.conversation_id == self._llm.controller.conversation_id,
                        db.TurnRun.turn_id == turn_id,
                    )
                )
            if applied and speech is None:
                return
            # Without a validated question this text is the fixed notice; the
            # browser offers to answer again or end, cleared by a question.
            await self._llm.controller.signal_notice(speech is None)
            self._event_ch.send_nowait(
                llm.ChatChunk(
                    id="question-" + str(speech.attempt_id)
                    if speech
                    else "dialogue-" + uuid.uuid4().hex,
                    delta=llm.ChoiceDelta(
                        role="assistant", content=speech.text if speech else text
                    ),
                )
            )
