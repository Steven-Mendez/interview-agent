"""LiveKit worker: wires the speech pipeline (STT → LangGraph → TTS).

Each job is dispatched via RoomAgentDispatch with a conversation_id: the
worker loads the plan from Postgres, runs the per-session interviewer graph,
persists the transcript, enforces the time cap and auto-triggers the
evaluation on shutdown.

STT (AssemblyAI) and TTS (Cartesia/Inworld, per the voice chosen in the
Settings screen) run through LiveKit Inference, so only LiveKit credentials
are needed for them. LLM calls go to OpenAI directly.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
import uuid

import httpx
from livekit import api
from livekit.agents import (
    Agent,
    AgentSession,
    ConversationItemAddedEvent,
    JobContext,
    JobProcess,
    TurnHandlingOptions,
    WorkerOptions,
    cli,
    inference,
)
from livekit.agents.llm import ChatMessage
from livekit.plugins import langchain, silero
from qdrant_client import AsyncQdrantClient

from interview_agent.config import settings
from interview_agent.interview import db, rag
from interview_agent.interview.interviewer_graph import build_interviewer_graph
from interview_agent.prompts import build_interviewer_prompt
from interview_agent.voices import DEFAULT_LANGUAGE, DEFAULT_VOICE, VOICES

logger = logging.getLogger("interview_agent")

_WRAP_UP_SECONDS = 120  # warning-to-forced-close window inside the time cap


def prewarm(proc: JobProcess) -> None:
    """Load the Silero VAD once per worker process so every job reuses it."""
    proc.userdata["vad"] = silero.VAD.load()


def _conversation_id_from_job(ctx: JobContext) -> uuid.UUID | None:
    """The conversation id travels as dispatch metadata; the room name
    (`interview-<uuid>`) is the fallback."""
    if ctx.job.metadata:
        try:
            return uuid.UUID(json.loads(ctx.job.metadata)["conversation_id"])
        except (ValueError, KeyError, json.JSONDecodeError):
            logger.warning("unparseable job metadata: %r", ctx.job.metadata)
    prefix = "interview-"
    if ctx.room.name.startswith(prefix):
        try:
            return uuid.UUID(ctx.room.name[len(prefix) :])
        except ValueError:
            pass
    return None


def _build_session(ctx: JobContext, graph, agent_settings: dict | None) -> AgentSession:
    # The settings snapshot taken when the interview was created; legacy rows
    # (NULL) fall back to the catalog defaults.
    cfg = agent_settings or {}
    default_voice = VOICES[DEFAULT_VOICE]
    language = cfg.get("language", DEFAULT_LANGUAGE)
    return AgentSession(
        # STT is pinned to the configured interview language (no per-utterance
        # auto-detection): everything optimizes for that language.
        stt=inference.STT(model=settings.stt_model, language=language),
        # The "LLM" is a LangGraph workflow, adapted for LiveKit. "custom"
        # stream mode: only text the graph writes via get_stream_writer() is
        # spoken — tool outputs never leak into TTS (see graph chat nodes).
        llm=langchain.LLMAdapter(graph=graph, stream_mode="custom"),
        tts=inference.TTS(
            model=cfg.get("tts_model", default_voice["tts_model"]),
            voice=cfg.get("tts_voice", default_voice["tts_voice"]),
            language=language,
        ),
        vad=ctx.proc.userdata["vad"],
        # Audio end-of-turn detector: `v1` (cloud, via LiveKit Inference) in
        # dev/hosted mode, `v1-mini` (local, weights ship inside the
        # livekit-local-inference wheel) when self-hosted, with automatic
        # cloud→local fallback.
        turn_handling=TurnHandlingOptions(turn_detection=inference.TurnDetector()),
    )


async def _run_interview(ctx: JobContext, conversation_id: uuid.UUID) -> None:
    engine, sessionmaker = db.create_engine_and_sessionmaker(settings.database_url)
    qdrant = AsyncQdrantClient(url=settings.qdrant_url)
    embeddings = rag.build_embeddings(settings)

    async with sessionmaker() as s:
        conversation = await db.get_conversation(s, conversation_id)
        milestones = await db.get_milestones(s, conversation_id)
    if conversation is None or conversation.plan is None:
        logger.error("no planned conversation %s; aborting job", conversation_id)
        await engine.dispose()
        return

    # Fire-and-forget tasks need a strong reference until done, or the event
    # loop may garbage-collect them mid-flight (silently losing the work).
    background_tasks: set[asyncio.Task] = set()

    def _spawn(coro) -> None:
        task = asyncio.create_task(coro)
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    end_event = asyncio.Event()
    prompt = build_interviewer_prompt(
        conversation, milestones, settings.interview_max_minutes
    )

    # Interviewer token spend accumulates in memory and is flushed once at
    # shutdown: usage is telemetry, so losing it on a hard crash beats a DB
    # write per conversational turn.
    interviewer_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    def _track_usage(usage) -> None:
        for key in interviewer_usage:
            interviewer_usage[key] += usage.get(key, 0) or 0

    graph = build_interviewer_graph(
        settings,
        conversation_id,
        sessionmaker,
        qdrant,
        embeddings,
        end_event,
        prompt,
        usage_sink=_track_usage,
    )
    session = _build_session(ctx, graph, conversation.agent_settings)
    language = (conversation.plan or {}).get("language", "en")

    # --- Transcript persistence + activity tracking -------------------------
    last_activity = time.monotonic()
    # Turn order assigned here, synchronously on the event loop: the persist
    # tasks are fire-and-forget and their commits (which decide the
    # autoincrement id) can land out of order under DB latency.
    msg_seq = itertools.count()

    def _on_item(event: ConversationItemAddedEvent) -> None:
        # Any conversation item counts as activity for the idle watchdog.
        nonlocal last_activity
        last_activity = time.monotonic()
        item = event.item
        if not isinstance(item, ChatMessage):  # e.g. AgentHandoff items
            return
        text = item.text_content
        if item.role in ("user", "assistant") and text and text.strip():
            _spawn(_persist(item.role, text, next(msg_seq)))

    async def _persist(role: str, content: str, seq: int) -> None:
        try:
            async with sessionmaker() as s:
                await db.insert_message(s, conversation_id, role, content, seq=seq)
        except Exception:
            logger.exception("failed to persist transcript item")

    session.on("conversation_item_added", _on_item)

    # --- End-of-interview machinery ----------------------------------------
    closing = False

    async def finish(reason: str, say_goodbye: bool = True) -> None:
        nonlocal closing
        if closing:
            return
        closing = True
        logger.info("finishing interview %s (%s)", conversation_id, reason)
        if say_goodbye:
            try:
                farewell = session.generate_reply(
                    instructions=(
                        "The interview is over. Thank the candidate warmly and say "
                        f"goodbye in one or two short sentences, in language '{language}'. "
                        "Do not reveal any evaluation or decision."
                    )
                )
                await farewell
            except Exception:
                logger.exception("farewell failed; closing anyway")
        try:
            async with sessionmaker() as s:
                await db.set_status(s, conversation_id, "completed", reason)
        except Exception:
            logger.exception("failed to mark interview completed")
        await session.aclose()
        await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))

    wrap_up_issued = False

    async def watch_end_event() -> None:
        await end_event.wait()
        # The agent also calls end_interview when told to wrap up with
        # milestones still pending — that is a timeout, not a completed plan.
        reason = "plan_complete"
        if wrap_up_issued:
            async with sessionmaker() as s:
                milestones_now = await db.get_milestones(s, conversation_id)
            if any(not m.completed for m in milestones_now):
                reason = "timeout"
        await finish(reason)

    async def time_cap() -> None:
        nonlocal wrap_up_issued
        warn_after = max(0, settings.interview_max_minutes * 60 - _WRAP_UP_SECONDS)
        await asyncio.sleep(warn_after)
        if closing:
            return
        wrap_up_issued = True
        logger.info("time cap warning for %s", conversation_id)
        session.generate_reply(
            instructions=(
                "Time is almost up: about two minutes remain. Start wrapping up "
                "— cover what is essential, then call end_interview."
            )
        )
        await asyncio.sleep(_WRAP_UP_SECONDS)
        await finish("timeout")

    async def idle_watchdog() -> None:
        # A silent room still runs STT and holds a session slot; close it if
        # nobody has said anything for the configured window.
        idle_seconds = settings.interview_idle_minutes * 60
        while not closing:
            await asyncio.sleep(15)
            if time.monotonic() - last_activity > idle_seconds:
                logger.info("idle timeout for %s", conversation_id)
                await finish("idle_timeout")
                return

    # --- Shutdown: runs on every end path (tool, timeout, tab closed) ------
    async def _on_shutdown() -> None:
        for task in (watcher_task, timer_task, idle_task):
            task.cancel()
        # Drain in-flight transcript writes so the evaluation sees them all.
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        try:
            async with sessionmaker() as s:
                conv = await db.get_conversation(s, conversation_id)
                if conv is not None and conv.ended_reason is None:
                    await db.set_status(s, conversation_id, "completed", "candidate_left")
        except Exception:
            logger.exception("failed to record candidate_left")
        # Flush interviewer spend BEFORE triggering evaluation: token_usage
        # is read-modify-write, so the writers must be sequenced.
        if any(interviewer_usage.values()):
            try:
                async with sessionmaker() as s:
                    await db.add_token_usage(
                        s, conversation_id, "interviewer", interviewer_usage
                    )
            except Exception:
                logger.exception("failed to persist interviewer token usage")
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{settings.app_base_url}/api/interviews/{conversation_id}/evaluate",
                    timeout=300,
                )
                logger.info("auto-evaluation triggered: HTTP %s", response.status_code)
        except Exception:
            # The endpoint is re-invocable; a failed trigger is not fatal.
            logger.exception("failed to auto-trigger evaluation")
        await qdrant.close()
        await engine.dispose()

    ctx.add_shutdown_callback(_on_shutdown)

    async with sessionmaker() as s:
        await db.set_status(s, conversation_id, "interviewing")

    await session.start(room=ctx.room, agent=Agent(instructions=prompt))
    watcher_task = asyncio.create_task(watch_end_event())
    timer_task = asyncio.create_task(time_cap())
    idle_task = asyncio.create_task(idle_watchdog())

    # If the candidate closes the tab, end right away instead of waiting for
    # LiveKit's empty-room timeout — the evaluation then starts promptly.
    def _on_participant_disconnected(_participant) -> None:
        if not ctx.room.remote_participants and not closing:
            logger.info("candidate left room %s", ctx.room.name)
            _spawn(finish("candidate_left", say_goodbye=False))

    ctx.room.on("participant_disconnected", _on_participant_disconnected)

    await session.generate_reply(
        instructions=(
            f"Greet the candidate in language '{language}': introduce yourself "
            "per your persona in ONE short sentence, then ask ONE short first "
            "question. Under 40 words total. Do not use any tools yet."
        )
    )


async def entrypoint(ctx: JobContext) -> None:
    settings.require_keys()
    conversation_id = _conversation_id_from_job(ctx)
    if conversation_id is None:
        logger.error("job for room %r carries no conversation id; skipping", ctx.room.name)
        return
    await _run_interview(ctx, conversation_id)


def run() -> None:
    """Entry point for the LiveKit CLI."""
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            # Explicit dispatch: only join rooms whose token requests this
            # agent (see the /token endpoint).
            agent_name=settings.livekit_agent_name,
            # Evaluation runs in the shutdown callback and can take a while
            # (high-reasoning model over the whole transcript).
            shutdown_process_timeout=300.0,
        )
    )
