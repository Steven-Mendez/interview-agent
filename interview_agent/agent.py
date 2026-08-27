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
import re
import time
import unicodedata
import uuid
from collections import deque
from collections.abc import AsyncIterable, Callable, Sequence
from datetime import UTC, datetime

import httpx
from livekit import api, rtc
from livekit.agents import (
    Agent,
    AgentSession,
    ConversationItemAddedEvent,
    JobContext,
    JobProcess,
    ModelSettings,
    TurnHandlingOptions,
    WorkerOptions,
    cli,
    inference,
    stt,
)
from livekit.agents.llm import ChatContext, ChatMessage
from livekit.plugins import langchain, silero
from qdrant_client import AsyncQdrantClient

from interview_agent.config import settings
from interview_agent.interview import db, rag
from interview_agent.interview.db import Message
from interview_agent.interview.interviewer_graph import build_interviewer_graph
from interview_agent.prompts import build_interviewer_prompt
from interview_agent.voices import DEFAULT_LANGUAGE, DEFAULT_VOICE, VOICES

logger = logging.getLogger("interview_agent")

_WRAP_UP_SECONDS = 120  # warning-to-forced-close window inside the time cap

# Tuning for _make_duplicate_final_filter, calibrated against a real interview
# (see its docstring). Every threshold errs towards keeping speech: a missed
# duplicate is a visible, recoverable bug, a false positive silently deletes
# candidate speech from the transcript the evaluator scores.
_DEDUPE_MIN_WORDS = 8  # shortest real duplicate seen was 18 words
_DEDUPE_MAX_LAG_SECONDS = 5.0  # an aggregate lands 0-0.5s after the final it repeats
_DEDUPE_HISTORY_SECONDS = 60.0  # longest stretch one aggregate spanned was 11s
_DEDUPE_MAX_FINALS = 64  # memory guard for a 15-minute interview

_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)

# How much of an interrupted interview is replayed into the interviewer's
# context on resume. A 15-minute interview runs ~28 exchanges (~56 messages),
# so this never trims a legitimate resume — it only bounds the pathological
# case. It is not the cost control either: the replay is a one-off ~4% of an
# interview's tokens, whereas charging the elapsed time (see time_cap) is what
# stops a resume from doubling the interview and quadrupling LLM input.
_RESUME_MAX_MESSAGES = 80


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


def _normalize_final(text: str) -> list[str]:
    """Token list for comparing two renderings of the same speech.

    AssemblyAI's formatted and unformatted finals differ only in case,
    punctuation and digit grouping — "de los 5. 000 a 10. 000 productos." vs
    "de los 5 000 a 10 000 productos" — so fold all three away. Tokens, not a
    string: comparing word lists keeps "no" from matching the tail of "camino".
    """
    folded = unicodedata.normalize("NFD", text.lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return _NON_WORD.sub(" ", folded).split()


def _make_duplicate_final_filter(
    *, now: Callable[[], float] = time.monotonic
) -> Callable[[str], bool]:
    """Return `keep(final_text) -> bool`, dropping redundant STT finals.

    `assemblyai/universal-streaming-multilingual` emits, for one long answer,
    several FORMATTED per-phrase finals and then one UNFORMATTED final that
    re-states everything since its own turn started. That aggregate is
    cumulative rather than incremental (livekit/agents#3312), and it wrecks the
    transcript two different ways depending on whether it beats the turn flush:
    arriving before, it is concatenated into the same message; arriving after,
    it opens a whole second turn. Both come from the same event, so dropping it
    here — upstream of the browser stream, the end-of-turn detector and
    `conversation_item_added` — fixes both.

    The aggregate always covers up to and including the most recent phrase
    final, so it is structurally a *suffix* of what was already emitted. Match
    on suffix rather than containment: containment would also swallow a
    legitimate prefix of earlier speech.

    Stateful. Only kept finals are recorded, so the buffer stays a faithful
    record of what actually went downstream. It is a rolling time window and is
    deliberately NOT reset on turn commit: the aggregate lands ~0.06s AFTER the
    commit, so a commit-scoped buffer would be empty exactly when it is needed.
    """
    history: deque[tuple[float, list[str]]] = deque(maxlen=_DEDUPE_MAX_FINALS)

    def keep(text: str) -> bool:
        tokens = _normalize_final(text)
        if not tokens:
            return True
        stamp = now()
        cutoff = stamp - _DEDUPE_HISTORY_SECONDS
        while history and history[0][0] < cutoff:
            history.popleft()
        # A duplicate treads on the heels of what it repeats; anything slower is
        # the candidate genuinely saying the same thing again (e.g. after being
        # asked to repeat), which must be kept.
        recent = bool(history) and stamp - history[-1][0] <= _DEDUPE_MAX_LAG_SECONDS
        if len(tokens) >= _DEDUPE_MIN_WORDS and recent:
            seen = [tok for _, toks in history for tok in toks]
            if len(tokens) <= len(seen) and seen[-len(tokens) :] == tokens:
                return False
        history.append((stamp, tokens))
        return True

    return keep


def _next_seq(messages: Sequence[Message]) -> int:
    """Where this job should start numbering the transcript.

    `msg_seq` used to be a bare `itertools.count()`, which restarts at 0 when a
    second job runs for the same conversation (worker crash + browser reload —
    `get_token` deliberately allows that, see routes.py). `get_messages` orders
    by `(seq, id)`, so two runs of 0,1,2… interleave into gibberish. Continue
    past the highest seq instead.

    `seq` is nullable — the backfill covered every row that existed then, but
    nothing enforces it — so skip NULLs rather than crash comparing them.
    """
    return max((m.seq for m in messages if m.seq is not None), default=-1) + 1


def _chat_ctx_from_messages(messages: Sequence[Message]) -> ChatContext:
    """Rebuild the interviewer's memory of an interrupted interview.

    Bounded to the last `_RESUME_MAX_MESSAGES` turns: the LangChain adapter
    converts the WHOLE ChatContext into graph state on every turn
    (`livekit/plugins/langchain/langgraph.py:_chat_ctx_to_state`), so an
    unbounded replay would ride along on every LLM call for the rest of the
    interview. Milestone progress covers whatever gets trimmed — the graph
    re-reads it from Postgres each turn, it never lives in this context.
    """
    ctx = ChatContext.empty()
    for m in messages[-_RESUME_MAX_MESSAGES:]:
        if m.role in ("user", "assistant") and m.content and m.content.strip():
            ctx.add_message(role=m.role, content=m.content)
    return ctx


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
        turn_handling=TurnHandlingOptions(
            turn_detection=inference.TurnDetector(),
            # Pinned to livekit's streaming defaults. Do NOT raise min_delay to
            # chase the "late stt final" warning: AssemblyAI's aggregate final
            # would then land *before* the flush and get concatenated into the
            # same transcript instead of opening a second turn — the same
            # duplicate, but invisible. InterviewAgent.stt_node handles it.
            endpointing={"mode": "fixed", "min_delay": 0.3, "max_delay": 2.5},
        ),
    )


class InterviewAgent(Agent):
    """Agent that drops AssemblyAI's redundant aggregate STT finals.

    Everything that was duplicating — the browser transcription stream, the
    running transcript the end-of-turn detector accumulates, and
    `conversation_item_added` (hence the `messages` rows and the LLM's
    ChatContext) — consumes the single event stream `stt_node` yields, so one
    filter here covers all of them. See `_make_duplicate_final_filter`.
    """

    def __init__(self, *, instructions: str, chat_ctx: ChatContext | None = None) -> None:
        # chat_ctx carries the earlier half of a resumed interview; livekit
        # copies it into the agent and never re-emits it as conversation items,
        # so seeding it does not re-persist the transcript.
        super().__init__(instructions=instructions, chat_ctx=chat_ctx)
        # On the agent rather than inside stt_node, so the buffer survives a
        # reconnect of the STT stream.
        self._keep_final = _make_duplicate_final_filter()
        self._dropped_finals = 0

    async def stt_node(
        self, audio: AsyncIterable[rtc.AudioFrame], model_settings: ModelSettings
    ) -> AsyncIterable[stt.SpeechEvent]:
        async for event in Agent.default.stt_node(self, audio, model_settings):
            # Only finals are judged: interim/preflight transcripts drive the
            # live bubble, and RECOGNITION_USAGE carries STT billing metrics.
            if event.type is stt.SpeechEventType.FINAL_TRANSCRIPT and event.alternatives:
                text = event.alternatives[0].text
                if text.strip() and not self._keep_final(text):
                    self._dropped_finals += 1
                    logger.info(
                        "dropped duplicate stt final #%d (%d words): %.120s",
                        self._dropped_finals,
                        len(text.split()),
                        text,
                    )
                    continue
            yield event


async def _run_interview(ctx: JobContext, conversation_id: uuid.UUID) -> None:
    engine, sessionmaker = db.create_engine_and_sessionmaker(settings.database_url)
    qdrant = AsyncQdrantClient(url=settings.qdrant_url)
    embeddings = rag.build_embeddings(settings)

    async with sessionmaker() as s:
        conversation = await db.get_conversation(s, conversation_id)
        milestones = await db.get_milestones(s, conversation_id)
        # Non-empty only when a previous job already ran for this conversation
        # (worker crash + browser reload). Everything the resume needs — turn
        # numbering, the interviewer's memory, the real start time — comes off
        # these rows, so this is the only extra query, and it runs before
        # session.start so it costs the candidate no time to first word.
        prior_messages = await db.get_messages(s, conversation_id)
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
    msg_seq = itertools.count(_next_seq(prior_messages))
    if prior_messages:
        logger.info(
            "resuming interview %s: %d prior messages, transcript continues at seq %d",
            conversation_id,
            len(prior_messages),
            _next_seq(prior_messages),
        )

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

    # On a resume the clock does not restart: charge what the first half already
    # spent, or a reconnect would hand out a whole fresh budget. Computed here
    # rather than inside time_cap so a failure is loud — raised in the coroutine
    # it would kill timer_task silently and leave the interview with no hard
    # stop. min(created_at), not prior_messages[0]: get_messages sorts by seq,
    # and NULL seq sorts last in Postgres.
    elapsed_seconds = 0.0
    if prior_messages:
        started = min(m.created_at for m in prior_messages)
        elapsed_seconds = max(0.0, (datetime.now(UTC) - started).total_seconds())

    async def time_cap() -> None:
        nonlocal wrap_up_issued
        budget = settings.interview_max_minutes * 60 - _WRAP_UP_SECONDS - elapsed_seconds
        # Already out of budget: warn immediately and let the usual wrap-up run,
        # so the candidate gets a goodbye instead of a dead screen.
        await asyncio.sleep(max(0, budget))
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

    await session.start(
        room=ctx.room,
        agent=InterviewAgent(
            instructions=prompt, chat_ctx=_chat_ctx_from_messages(prior_messages)
        ),
    )
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

    # Only the worker writes to `messages`, so rows here mean this conversation
    # already had a job: the interview was cut short and is being resumed. The
    # transcript so far is in the agent's context, so pick the thread back up
    # instead of starting over.
    opening = (
        (
            f"The connection dropped and the candidate has just rejoined. In language "
            f"'{language}': acknowledge the interruption in ONE short sentence — do NOT "
            "introduce yourself again — then carry on from where the transcript left "
            "off, without repeating your last question word for word. Under 40 words "
            "total. Do not use any tools yet."
        )
        if prior_messages
        else (
            f"Greet the candidate in language '{language}': introduce yourself "
            "per your persona in ONE short sentence, then ask ONE short first "
            "question. Under 40 words total. Do not use any tools yet."
        )
    )
    await session.generate_reply(instructions=opening)


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
