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
import copy
import json
import logging
import math
import re
import threading
import time
import unicodedata
import uuid
from collections import deque
from collections.abc import AsyncIterable, Awaitable, Callable, Sequence
from contextlib import suppress

import httpx
from langchain_core.tracers.langchain import wait_for_all_tracers
from langsmith.integrations.livekit import LiveKitLangSmithSpanProcessor, set_thread_id
from livekit import api, rtc
from livekit.agents import (
    Agent,
    AgentSession,
    ConversationItemAddedEvent,
    JobContext,
    JobProcess,
    MetricsCollectedEvent,
    ModelSettings,
    TurnHandlingOptions,
    WorkerOptions,
    cli,
    inference,
    stt,
)
from livekit.agents import telemetry as livekit_telemetry
from livekit.agents.cli import cli as sdk_cli
from livekit.agents.llm import ChatContext, ChatMessage
from livekit.agents.voice import room_io
from livekit.plugins import silero
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from sqlalchemy import func, select

from interview_agent import error_reporting, otel_metrics
from interview_agent.closing import ClosingCoordinator
from interview_agent.config import settings
from interview_agent.interview import db
from interview_agent.interview.context import validate_source_documents
from interview_agent.interview.db import Message
from interview_agent.interview.dialogue import DialogueController, DialogueLLM
from interview_agent.interview.transcription import OrderedCaptureWriter
from interview_agent.interview.workers import WorkerCoordinator, WorkerOwnershipError
from interview_agent.logging_config import protect_log_handlers
from interview_agent.observability import TURN_HANDLING, Telemetry
from interview_agent.runtime import process_manifest, record_manifest, validate_database_revision
from interview_agent.stt_drain import DrainableInferenceSTT, SessionSTTBoundary
from interview_agent.user_transcript import UserTranscriptForwarder
from interview_agent.voice_metrics import record_voice_metrics

logger = logging.getLogger("interview_agent")

_WRAP_UP_SECONDS = 120  # warning-to-forced-close window inside the time cap

# Tuning for _make_duplicate_final_filter, calibrated against a real interview
# (see its docstring). Every threshold errs towards keeping speech: a missed
# duplicate is a visible, recoverable bug, a false positive silently deletes
# candidate speech from the transcript the evaluator scores.
_DEDUPE_MIN_WORDS = 8  # shortest real duplicate seen was 18 words
_DEDUPE_MAX_LAG_SECONDS = 5.0  # an aggregate lands 0-0.5s after the final it repeats
_DEDUPE_HISTORY_SECONDS = 60.0  # longest stretch one aggregate spanned was 11s
_DEDUPE_MAX_FINALS = 64  # memory guard; bounds the 60s window, not the interview
_DEDUPE_AUDIO_TOLERANCE = 0.001

_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)

# How much of an interrupted interview is replayed into the interviewer's
# context on resume. An interview runs ~2 exchanges (~4 messages) a minute, so
# a 25-minute "deep" one is ~100 messages: this never trims a legitimate
# resume — it only bounds the pathological case. It is not the cost control
# either: the replay is a one-off ~4% of an interview's tokens, whereas
# charging the elapsed time (see time_cap) is what stops a resume from
# doubling the interview and quadrupling LLM input.
_RESUME_MAX_MESSAGES = 160

# What the session start records with LangSmith: LiveKit's recorder writes the
# session audio (candidate and interviewer) and LangSmith's processor attaches
# it to the voice session's root ("session_report" mode). Traces, logs and
# transcript uploads to LiveKit Cloud stay off. A LiveKit Cloud project with
# agent observability enabled also receives this audio, under its retention.
RECORD_AUDIO = {"audio": True, "traces": False, "logs": False, "transcript": False}


class _FlushOnRootEnd(SpanProcessor):
    """Runs after LangSmith's processor. LiveKit ends the job's root span
    after the shutdown callbacks, as the process is about to exit: send it
    (with the recording) now rather than on the batch exporter's timer."""

    def __init__(self, processor: SpanProcessor) -> None:
        self.processor = processor

    def on_end(self, span: ReadableSpan) -> None:
        if span.parent is None:
            self.processor.force_flush(10_000)


def langsmith_voice_processor(settings) -> LiveKitLangSmithSpanProcessor | None:
    """LangSmith's LiveKit integration, only with a key: one trace per voice
    session, its turns (STT, reply, TTS) below a root with the recording.
    Without a key LiveKit's spans have no exporter and nothing leaves."""
    if not settings.langsmith_api_key:
        return None
    processor = LiveKitLangSmithSpanProcessor(
        api_key=settings.langsmith_api_key,
        project=settings.langsmith_project,
        # The SDK's own default: self-hosted endpoints keep their /api/v1.
        endpoint=settings.langsmith_endpoint.rstrip("/") + "/otel/v1/traces",
        recording_mode="session_report",
    )
    provider = TracerProvider()
    provider.add_span_processor(processor)
    provider.add_span_processor(_FlushOnRootEnd(processor))
    # LiveKit's own hook; the OTel global stays untouched so no other library
    # starts exporting through this provider.
    livekit_telemetry.set_tracer_provider(provider)
    return processor


def prewarm(proc: JobProcess) -> None:
    """Load the Silero VAD once per worker process so every job reuses it,
    bind LiveKit's spans to LangSmith before the first job starts any, and give
    the process its own metrics provider (the parent running run() has none)
    and its own error reporting."""
    error_reporting.configure(settings, "worker")
    proc.userdata["vad"] = silero.VAD.load()
    proc.userdata["langsmith_processor"] = langsmith_voice_processor(settings)
    otel_metrics.configure(settings, "interview-agent-worker")


def _conversation_id_from_job(ctx: JobContext) -> uuid.UUID | None:
    """The conversation id travels as dispatch metadata; the room name
    (`interview-<uuid>`) is the fallback."""
    if ctx.job.metadata:
        try:
            metadata = json.loads(ctx.job.metadata)
            if isinstance(metadata, dict) and isinstance(metadata.get("conversation_id"), str):
                return uuid.UUID(metadata["conversation_id"])
        except (ValueError, json.JSONDecodeError):
            logger.warning("unparseable conversation id in job metadata")
    prefix = "interview-"
    room_name = ctx.job.room.name
    if room_name.startswith(prefix):
        try:
            return uuid.UUID(room_name[len(prefix) :])
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
    *, model: str, now: Callable[[], float] = time.monotonic
) -> Callable[[stt.SpeechEvent], bool]:
    """Drop AssemblyAI aggregates only when text AND audio boundaries match.

    The incident recorded in tests/test_stt_dedupe.py contained cumulative
    finals before and after LiveKit committed a turn. Text alone cannot tell
    these apart from a candidate repeating an answer. Missing or ambiguous
    timing evidence therefore keeps the speech. request_id scopes history;
    it is not a unique transcript identifier and never proves duplication.
    Create a new filter for every stt_node invocation, not every user turn.
    """
    history: deque[tuple[float, float, float, list[str]]] = deque(maxlen=_DEDUPE_MAX_FINALS)
    request_id: str | None = None
    last_end: float | None = None
    warned_missing_timing = False

    def keep(event: stt.SpeechEvent) -> bool:
        nonlocal request_id, last_end, warned_missing_timing
        if (
            not model.startswith("assemblyai/")
            or event.type is not stt.SpeechEventType.FINAL_TRANSCRIPT
        ):
            return True
        if not event.alternatives:
            history.clear()
            last_end = None
            return True
        if event.request_id and event.request_id != request_id:
            if history:
                logger.debug("reset stt dedupe history: request changed; preserving speech")
            history.clear()
            last_end = None
            request_id = event.request_id

        data = event.alternatives[0]
        tokens = _normalize_final(data.text)
        start, end = data.start_time, data.end_time
        valid_times = (
            getattr(data, "_interview_timing_explicit", True)
            and isinstance(start, (int, float))
            and isinstance(end, (int, float))
            and math.isfinite(start)
            and math.isfinite(end)
            and 0 <= start < end
        )
        if not tokens or not valid_times:
            history.clear()
            last_end = None
            if not warned_missing_timing:
                logger.warning(
                    "kept stt final: insufficient audio timing evidence; "
                    "deduplication requires provider audio boundaries"
                )
                warned_missing_timing = True
            return True
        # Aggregate start times go backwards by design; only a regressing END
        # is evidence of a restarted audio clock.
        if last_end is not None and end < last_end - _DEDUPE_AUDIO_TOLERANCE:
            history.clear()
            logger.debug("reset stt dedupe history: audio clock regressed")
        last_end = end
        stamp = now()
        cutoff = stamp - _DEDUPE_HISTORY_SECONDS
        while history and history[0][0] < cutoff:
            history.popleft()
        recent = bool(history) and stamp - history[-1][0] <= _DEDUPE_MAX_LAG_SECONDS
        if len(tokens) >= _DEDUPE_MIN_WORDS and recent:
            seen: list[str] = []
            for _, prior_start, _, prior_tokens in reversed(history):
                seen = prior_tokens + seen
                if len(seen) > len(tokens):
                    break
                if seen == tokens:
                    if math.isclose(
                        start, prior_start, abs_tol=_DEDUPE_AUDIO_TOLERANCE, rel_tol=0
                    ) and math.isclose(
                        end, history[-1][2], abs_tol=_DEDUPE_AUDIO_TOLERANCE, rel_tol=0
                    ):
                        logger.info(
                            "dropped duplicate stt final: matching text and audio boundaries"
                        )
                        return False
                    logger.debug("kept stt final: matching text but different audio boundaries")
                    break
        history.append((stamp, start, end, tokens))
        return True

    return keep


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
            item = ctx.add_message(
                role=m.role,
                content=m.content,
                id=m.source_id or f"stored-{m.id}",
                interrupted=bool(m.interrupted),
            )
            # LiveKit's typed MetricsReport discards our extension keys during
            # model construction; the pinned EOT hook likewise attaches them later.
            item.metrics.update(copy.deepcopy(m.metrics or {}))
    return ctx


def _build_session(ctx: JobContext, controller_llm, voice: dict) -> AgentSession:
    """Voice pipeline from the interview's snapshot (language, STT, TTS)."""
    language = voice["language"]
    stt_model = voice["stt_model"]
    keyterms = voice.get("keyterms", [])
    stt_options = {}
    if keyterms:
        if stt_model.startswith("assemblyai/"):
            stt_options["keyterms_prompt"] = keyterms
        elif stt_model.startswith("deepgram/"):
            stt_options["keyterm"] = keyterms
    return AgentSession(
        # STT is pinned to the configured interview language (no per-utterance
        # auto-detection): everything optimizes for that language.
        stt=DrainableInferenceSTT(model=stt_model, language=language, extra_kwargs=stt_options),
        # The "LLM" is the dialogue controller: only a validated, persisted
        # decision's spoken text reaches TTS.
        llm=controller_llm,
        tts=inference.TTS(model=voice["tts_model"], voice=voice["tts_voice"], language=language),
        vad=ctx.proc.userdata["vad"],
        # Audio end-of-turn detector: `v1` (cloud, via LiveKit Inference) in
        # dev/hosted mode, `v1-mini` (local, weights ship inside the
        # livekit-local-inference wheel) when self-hosted, with automatic
        # cloud→local fallback.
        turn_handling=TurnHandlingOptions(
            turn_detection=inference.TurnDetector(),
            # A decision persists progress before it is spoken; it cannot be
            # rolled back when LiveKit discards a preemptive response.
            preemptive_generation={"enabled": False},
            # The 2026-09-30 voice test delivered a final 1.17s after audio
            # ended, after a 0.3s turn had already committed. Allow that tail
            # plus a margin; keep the detector and its longer hesitation wait.
            # Recorded in every new snapshot as TURN_HANDLING.
            endpointing=dict(TURN_HANDLING["endpointing"]),
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

    def __init__(
        self,
        *,
        instructions: str,
        chat_ctx: ChatContext | None = None,
        stt_model: str = settings.stt_model,
        worker: WorkerCoordinator | None = None,
        capture_writer: OrderedCaptureWriter | None = None,
        delivery_observer=None,
    ) -> None:
        # chat_ctx carries the earlier half of a resumed interview; livekit
        # copies it into the agent and never re-emits it as conversation items,
        # so seeding it does not re-persist the transcript.
        super().__init__(instructions=instructions, chat_ctx=chat_ctx)
        self._stt_model = stt_model
        self._worker = worker
        self._capture_writer = capture_writer
        self._delivery_observer = delivery_observer

    def _check_speech_owner(self):
        if self._worker is not None:
            self._worker.require_local()

    async def llm_node(self, chat_ctx, tools, model_settings):
        self._check_speech_owner()
        if self._capture_writer is not None:
            await self._capture_writer.drain()
        async for chunk in Agent.default.llm_node(self, chat_ctx, tools, model_settings):
            self._check_speech_owner()
            if self._delivery_observer and getattr(chunk, "id", "").startswith("question-"):
                # Pinned 1.8.3 pipeline discards ChatChunk IDs from message metrics.
                # Bind the attempt to this generation's handle, never current_speech
                # from a later concurrent turn and never by matching spoken text.
                from livekit.agents.voice.agent_activity import _SpeechHandleContextVar

                handle = _SpeechHandleContextVar.get(None)
                attempt_id = uuid.UUID(chunk.id.removeprefix("question-"))
                if handle is not None:
                    handle.add_done_callback(
                        lambda done, identity=attempt_id: self._delivery_observer(
                            identity,
                            interrupted=done.interrupted
                            or done.exception() is not None
                            or not any(
                                isinstance(item, ChatMessage) and item.role == "assistant"
                                for item in done.chat_items
                            ),
                        )
                    )
            yield chunk

    async def tts_node(self, text, model_settings):
        async def owned_text():
            async for chunk in text:
                self._check_speech_owner()
                yield chunk

        self._check_speech_owner()
        async for frame in Agent.default.tts_node(self, owned_text(), model_settings):
            self._check_speech_owner()
            yield frame

    async def stt_node(
        self, audio: AsyncIterable[rtc.AudioFrame], model_settings: ModelSettings
    ) -> AsyncIterable[stt.SpeechEvent]:
        keep_final = _make_duplicate_final_filter(model=self._stt_model)
        activity = self._get_activity_or_raise()
        backend = activity.stt
        if isinstance(backend, DrainableInferenceSTT):
            if backend.session_boundary is None:
                backend.session_boundary = SessionSTTBoundary(activity, backend)
            audio = backend.session_boundary.forward_audio(audio)
        async for event in Agent.default.stt_node(self, audio, model_settings):
            # Only finals are judged: interim/preflight transcripts drive the
            # live bubble, and RECOGNITION_USAGE carries STT billing metrics.
            if not keep_final(event):
                continue
            yield event

    async def on_user_turn_completed(self, turn_ctx, new_message):
        self._check_speech_owner()
        if isinstance(self.session.stt, DrainableInferenceSTT):
            # Explicit text inputs and SDK paths lacking an EOT provenance
            # snapshot remain diagnostic; never infer confirmation later.
            new_message.metrics.setdefault("stt_confirmed", False)


# Backoff between evaluation-trigger attempts; the trailing 0 is the last try.
_TRIGGER_BACKOFF_SECONDS = (2, 5, 10, 0)
# The endpoint only CLAIMS the row and schedules the evaluation in the API
# process (see server.evaluations), answering 202 at once, so the read side
# needs seconds, not the minutes a full LLM call takes; the connect side
# must fail fast — a black-holed host must not burn minutes per attempt.
_TRIGGER_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


async def _trigger_evaluation(
    url: str,
    conversation_id: uuid.UUID,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """POST the evaluation trigger, retrying ONLY when the API could not be
    reached at all.

    Being briefly unreachable (restart, boot ordering) is the one failure
    that leaves nothing behind: the row sits in "completed" forever and no
    one ever asks again. Everything else means the request got through and
    is not re-sent: the endpoint claims the row and schedules the run in
    the background, and a second POST would be a no-op at best (the claim
    is atomic) and noise at worst. A response of any status means the API
    handled it; a read timeout after the connect means it most likely did
    too. Only a connect failure is safe to retry, so that is the whole list.

    `transport` and `sleep` exist for the tests.

    The API takes the call as the worker's, not a user's, by its internal token.
    """
    headers = (
        {"X-Internal-Token": settings.internal_api_token} if settings.internal_api_token else {}
    )
    for attempt, backoff in enumerate(_TRIGGER_BACKOFF_SECONDS, start=1):
        try:
            async with httpx.AsyncClient(transport=transport, timeout=_TRIGGER_TIMEOUT) as client:
                response = await client.post(url, headers=headers)
            logger.info("auto-evaluation triggered: HTTP %s", response.status_code)
            break
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            logger.warning("evaluation trigger unreachable (attempt %s): %s", attempt, exc)
            if backoff:
                await sleep(backoff)
        except httpx.TimeoutException as exc:
            # Read/write timeout: the request reached the API and the
            # evaluation is most likely still running there. Not retried.
            logger.warning(
                "evaluation trigger for %s timed out waiting for the response "
                "(%s); the evaluation is probably still running — not re-sent",
                conversation_id,
                exc,
            )
            break
        except Exception:
            logger.exception("failed to auto-trigger evaluation")
            break
    else:
        # Out of attempts: say so loudly, the interview is now orphaned
        # until someone hits Retry in the UI.
        logger.error(
            "evaluation never triggered for %s; retry it from the UI",
            conversation_id,
        )


_METRICS_FLUSH_SECONDS = 4


async def _flush_metrics(telemetry: Telemetry) -> None:
    """The job's process exits after its interview: export what it recorded,
    off the event loop and bounded; a failure only logs. A displaced worker
    never drains its telemetry, so samples still in flight get a moment."""
    try:
        if telemetry.pending:
            await asyncio.wait(list(telemetry.pending), timeout=1)
        # The flush bounds itself: an abandoned to_thread would hold the exit.
        await asyncio.to_thread(otel_metrics.force_flush, _METRICS_FLUSH_SECONDS * 1000)
    except Exception:
        logger.warning("Metrics flush did not complete")


_ERRORS_FLUSH_SECONDS = 2


async def _flush_error_reports() -> None:
    """Errors logged while the interview ended are still queued when the job's
    process exits: send them, off the event loop and bounded."""
    await asyncio.to_thread(error_reporting.flush, _ERRORS_FLUSH_SECONDS)


_TRACES_FLUSH_SECONDS = 5


def _wait_for_tracers() -> None:
    try:
        wait_for_all_tracers()
    except Exception as exc:
        logger.warning("Trace flush failed: %s", type(exc).__name__)


async def _flush_traces() -> None:
    """LangChain's tracer sends the dialogue turns from a background thread:
    wait for it before the job process exits, bounded, on a daemon thread so
    an unresponsive LangSmith cannot hold the exit. Nothing to send without a
    key."""
    flush = threading.Thread(
        target=_wait_for_tracers, name="interview-agent-trace-flush", daemon=True
    )
    flush.start()
    await asyncio.to_thread(flush.join, _TRACES_FLUSH_SECONDS)
    if flush.is_alive():
        logger.warning("Trace flush did not complete")


async def _run_interview(ctx: JobContext, conversation_id: uuid.UUID) -> None:
    engine, sessionmaker = db.create_engine_and_sessionmaker(settings.database_url)
    startup_cleanup = []
    try:
        await validate_database_revision(sessionmaker)
        await _run_interview_job(ctx, conversation_id, engine, sessionmaker, startup_cleanup)
    except BaseException as exc:
        # Setup failures before the SDK registers its shutdown callback must
        # release the engine too. Once registered, use the same bounded,
        # idempotent cleanup as every other shutdown path.
        if startup_cleanup:
            await startup_cleanup[0]()
        else:
            try:
                async with asyncio.timeout(2):
                    await engine.dispose()
            except Exception:
                logger.warning("Failed startup engine disposal did not complete")
        if isinstance(exc, asyncio.CancelledError):
            raise
        logger.exception("Interview job initialization failed")
        await _flush_error_reports()
        ctx.shutdown(reason="worker_initialization_failed")


async def _run_interview_job(ctx, conversation_id, engine, sessionmaker, startup_cleanup):
    async with sessionmaker() as initial_session:
        saved = await db.get_conversation(initial_session, conversation_id)
        try:
            if saved is None:
                raise ValueError("Interview does not exist")
            # Everything the job runs with was fixed when the interview was
            # planned; a row without it cannot be started.
            if not (saved.run_config or {}).get("models") or not (saved.run_config or {}).get(
                "stt_model"
            ):
                raise ValueError("Interview has no saved model configuration")
            if not all(
                (saved.agent_settings or {}).get(key)
                for key in ("language", "tts_model", "tts_voice")
            ):
                raise ValueError("Interview has no saved voice")
            if None in (saved.max_minutes, saved.question_limit, saved.followup_limit):
                raise ValueError("Interview has no saved limits")
        except ValueError:
            await engine.dispose()
            ctx.shutdown(reason="invalid_interview_snapshot")
            return
    worker = WorkerCoordinator(
        conversation_id,
        sessionmaker,
        reconnect_seconds=settings.interview_reconnect_seconds,
        closing_seconds=settings.closing_timeout_seconds + 15,
        idle_minutes=settings.interview_idle_minutes,
    )
    try:
        lease = await worker.claim()
    except Exception:
        await engine.dispose()
        ctx.shutdown(reason="worker_claim_unavailable")
        return
    if lease is None:
        await engine.dispose()
        ctx.shutdown(reason="worker_not_acquired")
        return
    sessionmaker = worker.sessionmaker
    async with sessionmaker() as s:
        conversation = await db.get_conversation(s, conversation_id)
        prior_messages = await db.get_messages(s, conversation_id)

    # Fire-and-forget tasks need a strong reference until done, or the event
    # loop may garbage-collect them mid-flight (silently losing the work).
    background_tasks: set[asyncio.Task] = set()
    transcript_tasks: set[asyncio.Task] = set()
    persistence_errors: list[BaseException] = []

    def _spawn(coro, *, transcript=False) -> None:
        task = asyncio.create_task(coro)
        background_tasks.add(task)
        if transcript:
            transcript_tasks.add(task)

        def finished(done):
            background_tasks.discard(done)
            transcript_tasks.discard(done)
            if not done.cancelled():
                error = done.exception()
                if error is not None:
                    logger.error("Interview background task failed: %s", type(error).__name__)
                    if transcript:
                        persistence_errors.append(error)

        task.add_done_callback(finished)

    end_event = asyncio.Event()
    # This interview's own cap, fixed when it was created.
    max_minutes = conversation.max_minutes
    try:
        validate_source_documents(conversation.resume_markdown, conversation.job_offer)
    except ValueError:
        logger.exception("invalid interview source context for %s", conversation_id)
        try:
            async with sessionmaker() as s:
                await db.set_status_if(s, conversation_id, conversation.status, "error")
        except Exception:
            logger.exception("could not mark invalid source context as failed")
        try:
            # The job room name is available before ctx.connect/session.start.
            await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.job.room.name))
        except Exception:
            logger.exception("could not close room for invalid source context")
        finally:
            await engine.dispose()
            ctx.shutdown(reason="invalid_source_context")
        return

    # Interviewer token spend accumulates in memory and is flushed once at
    # shutdown: usage is telemetry, so losing it on a hard crash beats a DB
    # write per conversational turn.
    interviewer_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    def _track_usage(usage) -> None:
        for key in interviewer_usage:
            interviewer_usage[key] += usage.get(key, 0) or 0

    run_config = conversation.run_config
    telemetry = Telemetry(
        conversation_id, run_config, process="worker", thread_of=conversation.repeat_of_id
    )
    voice_processor = ctx.proc.userdata.get("langsmith_processor")
    # The voice session's spans start from this task (session.start): its
    # LangSmith trace joins the interview's Thread.
    set_thread_id(telemetry.trace_metadata()["thread_id"])
    model_config = run_config["models"]["interviewer"]
    effective = settings.model_copy(
        update={
            "interviewer_model": model_config["model"],
            "interviewer_reasoning_effort": model_config["reasoning_effort"],
        }
    )
    controller = DialogueController(
        effective, conversation_id, sessionmaker, end_event, telemetry, _track_usage
    )
    graph = DialogueLLM(controller)
    voice_config = {
        **conversation.agent_settings,
        "stt_model": run_config["stt_model"],
        "keyterms": list(
            dict.fromkeys(re.findall(r"\b[A-Z][A-Za-z0-9+#.\-]{1,30}\b", conversation.job_offer))
        )[:50],
    }
    session = _build_session(ctx, graph, voice_config)
    runtime_config = {
        **run_config,
        **{key: voice_config[key] for key in ("tts_model", "tts_voice", "language", "keyterms")},
    }
    manifest = await process_manifest(
        settings,
        "worker",
        config=runtime_config,
        functions=(_run_interview_job, _build_session, entrypoint),
    )
    await record_manifest(sessionmaker, manifest, conversation_id=conversation_id)
    capture_writer = OrderedCaptureWriter(sessionmaker, conversation_id)
    if isinstance(session.stt, DrainableInferenceSTT):
        session.stt.capture_sink = capture_writer.submit
    controller.capture_barrier = capture_writer.drain

    async def publish_notice(active: bool) -> None:
        await ctx.room.local_participant.set_attributes(
            {"interview.notice": "technical" if active else ""}
        )

    controller.notice_callback = publish_notice

    def _on_voice_metrics(event: MetricsCollectedEvent):
        record_voice_metrics(
            telemetry,
            event.metrics,
            stt_model=voice_config["stt_model"],
            tts_model=voice_config["tts_model"],
        )

    session.on("metrics_collected", _on_voice_metrics)
    language = (conversation.plan or {}).get("language", "en")

    # --- Transcript persistence + activity tracking -------------------------
    last_activity = time.monotonic()
    # Candidate order is committed by the EOT writer before model generation.
    if prior_messages:
        logger.info(
            "resuming interview %s: %d prior messages", conversation_id, len(prior_messages)
        )

    def _on_item(event: ConversationItemAddedEvent) -> None:
        # Any conversation item counts as activity for the idle watchdog.
        nonlocal last_activity
        if worker.lost:
            return
        worker.note_activity()
        last_activity = time.monotonic()
        item = event.item
        if not isinstance(item, ChatMessage):  # e.g. AgentHandoff items
            return
        text = item.text_content
        if item.role in ("user", "assistant") and text and text.strip():
            if item.role == "user" and isinstance(session.stt, DrainableInferenceSTT):
                item.metrics.setdefault("stt_confirmed", False)
            if item.role == "user":
                capture_writer.submit(
                    content=text,
                    source_id=item.id,
                    interrupted=item.interrupted,
                    metrics=dict(item.metrics),
                )
            else:
                _spawn(_persist(item), transcript=True)

    async def _persist(item: ChatMessage) -> None:
        metrics = dict(item.metrics)
        async with sessionmaker() as s:
            await db.insert_message(
                s,
                conversation_id,
                item.role,
                item.text_content,
                source_id=item.id,
                interrupted=item.interrupted,
                metrics=metrics,
            )
        for name, value in voice_metric_samples(metrics).items():
            telemetry.emit("voice", name, value, turn_id=item.id)
        if item.interrupted:
            telemetry.emit("voice", "interruptions", 1, turn_id=item.id)

    session.on("conversation_item_added", _on_item)
    session.on(
        "user_input_transcribed", lambda event: worker.note_activity() if event.transcript else None
    )
    user_transcript = UserTranscriptForwarder(session, ctx.room)

    async def drain_transcript() -> None:
        await capture_writer.drain()
        current = asyncio.current_task()
        tasks = [t for t in transcript_tasks if t is not current]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if persistence_errors:
            raise RuntimeError("Confirmed transcript persistence failed") from persistence_errors[0]

    async def seal_transcript() -> None:
        await session.aclose()
        await user_transcript.aclose()

    async def end_stt_input():
        session.input.set_audio_enabled(False)
        if not isinstance(session.stt, DrainableInferenceSTT):
            return None
        boundary = session.stt.session_boundary
        if boundary is None:
            return None  # Session admission and recognition were not observed.
        started = time.monotonic()
        report = await boundary.drain()
        telemetry.emit("stt", "final_drain_seconds", time.monotonic() - started)
        # All provider events have reached recognition before this commit.
        # Any promoted interim remains explicitly unconfirmed in persistence.
        await session.commit_user_turn(transcript_timeout=0, stt_flush_duration=0, skip_reply=True)
        return report

    coordinator = ClosingCoordinator(
        room=ctx.room,
        session=session,
        sessionmaker=sessionmaker,
        conversation_id=conversation_id,
        telemetry=telemetry,
        language=language,
        timeout_seconds=settings.closing_timeout_seconds,
        drain_transcript=drain_transcript,
        seal_transcript=seal_transcript,
        end_stt_input=end_stt_input,
        owner_id=worker.owner_id,
        on_claim=worker.heartbeat,
        speech_fence=worker.require_local,
    )

    # --- End-of-interview machinery ----------------------------------------
    closing = False

    async def finish(reason: str, say_goodbye: bool = True) -> None:
        nonlocal closing
        if closing:
            return
        closing = True
        controller.closing = True
        logger.info("finishing interview %s (%s)", conversation_id, reason)
        try:
            await coordinator.finish(reason, say_goodbye=say_goodbye)
        except Exception:
            if not coordinator.owns_closure:
                closing = False
                controller.closing = False
            raise
        finally:
            if coordinator.owns_closure:
                async with sessionmaker() as s:
                    final_state = await db.get_conversation(s, conversation_id)
                if final_state is not None and final_state.status not in (
                    "planned",
                    "interviewing",
                    "closing",
                ):
                    await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.job.room.name))

    async def watch_end_event() -> None:
        await end_event.wait()
        await finish(controller.end_reason or "unknown")

    # On a resume the clock does not restart: charge what the first half already
    # spent, or a reconnect would hand out a whole fresh budget. Computed here
    # rather than inside time_cap so a failure is loud — raised in the coroutine
    # it would kill timer_task silently and leave the interview with no hard
    # stop. min(created_at), not prior_messages[0]: get_messages sorts by seq,
    # and NULL seq sorts last in Postgres.
    async with sessionmaker() as s:
        clock_now = await s.scalar(select(func.clock_timestamp()))
    # A row without an origin can only recover its existing closure; no
    # interview duration is started or reset.
    elapsed_seconds = (
        max(0.0, (clock_now - conversation.started_at).total_seconds())
        if conversation.started_at is not None
        else max_minutes * 60
    )

    async def time_cap() -> None:
        budget = max_minutes * 60 - _WRAP_UP_SECONDS - elapsed_seconds
        # Already out of budget: warn immediately and let the usual wrap-up run,
        # so the candidate gets a goodbye instead of a dead screen.
        await asyncio.sleep(max(0, budget))
        if closing:
            return
        logger.info("time cap warning for %s", conversation_id)
        session.say(
            "Quedan aproximadamente dos minutos para terminar."
            if language == "es"
            else "We have about two minutes left to finish."
        )
        await asyncio.sleep(_WRAP_UP_SECONDS)
        await finish("timeout")

    async def recover_questions():
        while not closing:
            await asyncio.sleep(1)
            if worker.lost:
                return
            current = session.current_speech
            if current is not None and not current.done():
                continue
            try:
                await capture_writer.drain()
                speech = await controller.deliveries.claim()
                if speech is not None:
                    worker.require_local()
                    handle = session.say(speech.text)
                    await handle.wait_for_playout()
                    await controller.deliveries.observed(
                        speech.attempt_id,
                        interrupted=handle.interrupted or handle.exception() is not None,
                    )
            except WorkerOwnershipError:
                return
            except Exception:
                logger.exception("Question recovery failed; delivery remains uncertain")

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
    watcher_task = timer_task = idle_task = disconnect_task = heartbeat_task = delivery_task = None

    async def ownership_lost():
        session.input.set_audio_enabled(False)
        session.output.set_audio_enabled(False)
        with suppress(RuntimeError):
            session.interrupt(force=True)
        controller.closing = True
        telemetry.emit("worker", "ownership_lost", 1)
        ctx.shutdown(reason="worker_ownership_lost")

    async def _shutdown_owned() -> None:
        for task in (
            watcher_task,
            timer_task,
            idle_task,
            disconnect_task,
            heartbeat_task,
            delivery_task,
        ):
            if task is not None:
                task.cancel()
        if worker.lost:
            for cleanup in (coordinator.aclose, user_transcript.aclose, session.aclose):
                try:
                    await coordinator._bounded(cleanup(), 3)
                except Exception:
                    logger.warning("Displaced worker cleanup did not complete")
            return
        try:
            async with sessionmaker() as s:
                conv = await db.get_conversation(s, conversation_id)
                unfinished = conv is not None and conv.status in ("interviewing", "closing")
                shutdown_reason = conv.ended_reason if conv is not None else None
            if unfinished:
                await coordinator.finish(shutdown_reason or "candidate_left", say_goodbye=False)
        except Exception:
            logger.exception("failed to record candidate_left")
        for cleanup in (coordinator.aclose, user_transcript.aclose, telemetry.drain):
            try:
                await coordinator._bounded(cleanup(), 10)
            except Exception as exc:
                logger.warning("Shutdown cleanup failed: %s", type(exc).__name__)
        # Flush interviewer spend BEFORE triggering evaluation: token_usage
        # is read-modify-write, so the writers must be sequenced.
        if any(interviewer_usage.values()):
            try:
                async with sessionmaker() as s:
                    await db.add_token_usage(s, conversation_id, "interviewer", interviewer_usage)
            except Exception:
                logger.exception("failed to persist interviewer token usage")
        async with sessionmaker() as s:
            final_conversation = await db.get_conversation(s, conversation_id)
        if final_conversation is not None and final_conversation.status == "completed":
            await _trigger_evaluation(
                f"{settings.app_base_url}/api/interviews/{conversation_id}/evaluate?automatic=true",
                conversation_id,
            )

    shutdown_started = False

    async def _on_shutdown() -> None:
        nonlocal shutdown_started
        if shutdown_started:
            return
        shutdown_started = True
        task = asyncio.create_task(_shutdown_owned())
        try:
            done, _ = await asyncio.wait({task}, timeout=30)
            if task not in done:
                task.cancel()
                worker.lost = True
                await ownership_lost()
                logger.warning("Worker shutdown exceeded its total cleanup budget")
            else:
                task.result()
        except WorkerOwnershipError:
            worker.lost = True
            await ownership_lost()
            for cleanup in (coordinator.aclose, user_transcript.aclose, session.aclose):
                try:
                    await coordinator._bounded(cleanup(), 3)
                except Exception:
                    logger.warning("Displaced worker cleanup did not complete")
        except Exception:
            logger.exception("Worker shutdown failed")
        finally:
            if not task.done():
                task.cancel()
                task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            await _flush_metrics(telemetry)
            await _flush_traces()
            try:
                async with asyncio.timeout(2):
                    await engine.dispose()
            except Exception:
                logger.warning("Worker engine disposal did not complete")
            await _flush_error_reports()

    startup_cleanup.append(_on_shutdown)
    ctx.add_shutdown_callback(_on_shutdown)

    startup_status = worker.lease.phase
    if startup_status not in ("interviewing", "closing"):
        await engine.dispose()
        ctx.shutdown(reason="interview_already_terminal")
        return

    async def after_disconnect_grace():
        await asyncio.sleep(settings.interview_reconnect_seconds)
        if not any(p.identity == "candidate" for p in ctx.room.remote_participants.values()):
            await finish("candidate_left", say_goodbye=False)

    def _on_participant_disconnected(participant) -> None:
        nonlocal disconnect_task
        if participant.identity == "candidate" and not closing:
            _spawn(worker.set_connected(False))
            if disconnect_task is not None:
                disconnect_task.cancel()
            disconnect_task = asyncio.create_task(after_disconnect_grace())

    def _on_participant_connected(participant) -> None:
        if participant.identity == "candidate":
            _spawn(worker.set_connected(True))
            if disconnect_task is not None:
                disconnect_task.cancel()

    ctx.room.on("participant_disconnected", _on_participant_disconnected)
    ctx.room.on("participant_connected", _on_participant_connected)

    worker.require_local()
    heartbeat_task = asyncio.create_task(worker.monitor(ownership_lost, finish))
    try:
        await session.start(
            # Independent of LiveKit project defaults. With LangSmith, LiveKit
            # records the session audio for the trace; without it nothing.
            record=RECORD_AUDIO if voice_processor is not None else False,
            room=ctx.room,
            room_options=room_io.RoomOptions(close_on_disconnect=False),
            agent=InterviewAgent(
                # Spoken turns come only from the validated dialogue controller.
                instructions="Conduct the interview through validated decisions only.",
                chat_ctx=_chat_ctx_from_messages(prior_messages),
                stt_model=voice_config["stt_model"],
                worker=worker,
                capture_writer=capture_writer,
                delivery_observer=lambda attempt_id, interrupted: _spawn(
                    controller.deliveries.observed(attempt_id, interrupted=interrupted),
                    transcript=True,
                ),
            ),
        )
    except Exception:
        logger.exception("Interview session startup failed")
        await _on_shutdown()
        ctx.shutdown(reason="session_start_failed")
        return
    # A reconnecting candidate may already be present when session.start
    # returns. Events alone cannot clear a previous owner's disconnect mark.
    if any(p.identity == "candidate" for p in ctx.room.remote_participants.values()):
        _spawn(worker.set_connected(True))
    watcher_task = asyncio.create_task(watch_end_event())
    timer_task = asyncio.create_task(time_cap())
    idle_task = asyncio.create_task(idle_watchdog())
    delivery_task = asyncio.create_task(recover_questions())

    try:
        local_participant = ctx.room.local_participant
    except Exception:
        local_participant = None
    if local_participant is not None:

        @local_participant.register_rpc_method("interview.request_end")
        async def request_end(data):
            if data.caller_identity != "candidate":
                raise ValueError("Only the candidate can end this interview")
            await coordinator.request_close("candidate_requested")
            _spawn(finish("candidate_requested"))
            return json.dumps({"accepted": True})

        if startup_status != "closing":
            coordinator.prewarm()

    if startup_status == "closing":
        await finish(conversation.ended_reason or "connection_lost")
        return

    # A resumed interview acknowledges the cut and lets the dialogue
    # controller recover the saved question; a new one opens the plan.
    if prior_messages:
        session.say(
            "Retomemos la entrevista donde la dejamos."
            if language == "es"
            else "Let's continue the interview where we left off."
        )
    else:
        await session.generate_reply(instructions="Open the interview.")


def voice_metric_samples(metrics: dict) -> dict[str, float]:
    """Durations only: epoch timestamps (*_at), flags and IDs are not samples."""
    return {
        name: value
        for name, value in metrics.items()
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and not name.endswith("_at")
        and math.isfinite(value)
    }


async def entrypoint(ctx: JobContext) -> None:
    # Do this before validation/source loading too: the SDK otherwise uploads
    # buffered crash logs when startup fails before AgentSession.start().
    ctx.init_recording({"audio": False, "transcript": False, "traces": False, "logs": False})
    settings.require_keys()
    try:
        metadata = json.loads(ctx.job.metadata or "{}")
    except (TypeError, json.JSONDecodeError):
        ctx.shutdown(reason="invalid_job_metadata")
        return
    if not isinstance(metadata, dict):
        ctx.shutdown(reason="invalid_job_metadata")
        return
    conversation_id = _conversation_id_from_job(ctx)
    if conversation_id is None:
        logger.error("job for room %r carries no conversation id; skipping", ctx.room.name)
        return
    await _run_interview(ctx, conversation_id)


def run() -> None:
    """Entry point for the LiveKit CLI."""
    error_reporting.configure(settings, "worker")
    original_logging_setup = sdk_cli.setup_logging

    def private_logging_setup(*args, **kwargs):
        original_logging_setup(*args, **kwargs)
        protect_log_handlers()

    # The CLI installs its own console handler after main.py configures files.
    # Scope this adapter to our CLI lifetime so provider content cannot bypass
    # the metadata formatter through that newly installed handler.
    sdk_cli.setup_logging = private_logging_setup
    try:
        cli.run_app(
            WorkerOptions(
                entrypoint_fnc=entrypoint,
                prewarm_fnc=prewarm,
                # Explicit dispatch: only join rooms whose token requests this
                # agent (see the /token endpoint).
                agent_name=settings.livekit_agent_name,
                drain_timeout=settings.worker_drain_minutes * 60,
                # Evaluation runs in the shutdown callback and can take a while
                # (high-reasoning model over the whole transcript).
                shutdown_process_timeout=300.0,
            )
        )
    finally:
        sdk_cli.setup_logging = original_logging_setup
