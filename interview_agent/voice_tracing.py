"""LiveKit's voice session in LangSmith, the integration's default shape.

LangSmith's official LiveKit integration turns LiveKit's OpenTelemetry spans
into one trace per voice session: the job's root span (LiveKit's
`job_entrypoint`, renamed `voice_session`) carries the session recording
(stereo: candidate and interviewer) and the chat history, with
`ls_modality: audio` so LangSmith shows it as a voice conversation; below
it every turn with the candidate's transcribed words (STT), the interviewer's
reply and its synthesis (TTS), with latencies. The
interviewer's decision graph and model calls nest inside each reply.

The interview's own trace (planner, worker, evaluator) stays separate. Both
share the interview's LangSmith thread, point at each other in metadata, and
both are registered for deletion (privacy.ExternalDeletionWorker).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from langsmith.integrations.livekit import LiveKitLangSmithSpanProcessor, set_thread_id
from langsmith.integrations.otel import langsmith_run_id_from_otel_span_id
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider

from interview_agent.config import Settings
from interview_agent.observability import Telemetry
from interview_agent.privacy import api_root

logger = logging.getLogger(__name__)

_LLM_SPAN = "llm_request"
SESSION_ROOT_NAME = "voice_session"
# Token counts LiveKit reports for an LLM; ours always reports zero (below).
_USAGE_ATTRIBUTES = (
    "langsmith.usage_metadata",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.total_tokens",
)
# What the session start records: LiveKit's recorder writes the session audio
# (candidate and interviewer) and the integration attaches it to the session's
# root ("session_report" mode: the local recorder, aligned with the trace; Egress
# would add a separate recording service and storage for the same audio).
# Traces, logs and transcript uploads to LiveKit Cloud stay off. A LiveKit Cloud
# project with agent observability enabled also receives this audio; that copy
# follows LiveKit's retention, not ours.
RECORD_AUDIO = {"audio": True, "traces": False, "logs": False, "transcript": False}


class InterviewSpanProcessor(LiveKitLangSmithSpanProcessor):
    """The integration's processor, limited to registered interview sessions."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # OTel trace id -> metadata for the session's root run
        self._interviews: dict[int, dict] = {}

    def follow(self, otel_trace_id: int, metadata: dict) -> None:
        self._interviews[otel_trace_id] = metadata

    def on_end(self, span) -> None:
        # Only sessions registered for deletion: a job that never reaches an
        # interview (invalid dispatch, startup failure) exports nothing.
        if span.context.trace_id not in self._interviews:
            return
        super().on_end(span)
        if span.parent is None:
            # The session's root ends last, at job teardown; send it (with
            # the recording) before the job process can exit.
            self.force_flush(10_000)

    def _dispatch(self, tspan) -> bool:
        span = tspan.span
        if span.parent is None:
            # LiveKit names its root after the job ("job_entrypoint"); in
            # LangSmith it is the interview's voice session.
            tspan.set_name(SESSION_ROOT_NAME)
            for key, value in self._interviews[span.context.trace_id].items():
                tspan.set_metadata(key, value)
        export = super()._dispatch(tspan)
        if span.name == _LLM_SPAN:
            # LiveKit's LLM here is the dialogue controller: the real model
            # calls are its child runs with their own tokens and cost. Left an
            # llm run with zero usage it would count as a second model call.
            tspan.set_kind("chain")
            for attribute in _USAGE_ATTRIBUTES:
                tspan.attributes.pop(attribute, None)
        return export


@dataclass
class VoiceTracing:
    processor: InterviewSpanProcessor
    tracer: otel_trace.Tracer

    def session_trace_id(self) -> uuid.UUID | None:
        """The LangSmith trace of this job's voice session. Run in the job's
        entrypoint task, where LiveKit's job span is current: a parentless
        OTel span becomes a LangSmith root whose run (and trace) id is 8 zero
        bytes plus its span id."""
        span = otel_trace.get_current_span().get_span_context()
        if not span.is_valid:
            return None
        return langsmith_run_id_from_otel_span_id(span.span_id)

    def follow(self, telemetry: Telemetry, session_trace_id: uuid.UUID) -> None:
        """After the session trace is registered for deletion, before
        session.start (the session's spans start from this task)."""
        current = otel_trace.get_current_span().get_span_context()
        self.processor.follow(
            current.trace_id,
            {
                "interview_trace_id": str(telemetry.trace_id),
                "process": "voice",
                **{k: v for k, v in telemetry.dimensions().items() if k != "trace_id"},
            },
        )
        telemetry.follow_otel_trace(current.trace_id)
        telemetry.tracer = self.tracer
        telemetry.annotate(voice_trace_id=str(session_trace_id))
        # Repeats of an interview, and its interview trace, share the thread.
        set_thread_id(telemetry.thread_id)


def configure_voice_tracing(settings: Settings) -> VoiceTracing | None:
    """Once per job process, before any job: without a LangSmith key LiveKit's
    spans have no exporter and nothing leaves the process."""
    if not settings.langsmith_api_key:
        return None
    from livekit.agents import telemetry

    processor = InterviewSpanProcessor(
        api_key=settings.langsmith_api_key,
        project=settings.langsmith_project,
        endpoint=api_root(settings.langsmith_endpoint) + "otel/v1/traces",
        recording_mode="session_report",
    )
    provider = TracerProvider()
    provider.add_span_processor(processor)
    # LiveKit's own hook; the OTel global stays untouched so no other library
    # starts exporting through this provider.
    telemetry.set_tracer_provider(provider)
    return VoiceTracing(processor, provider.get_tracer("interview_agent"))
