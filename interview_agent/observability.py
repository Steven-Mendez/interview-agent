"""Anonymous metrics and, when LangSmith is configured, full interview traces.

Metrics are not rows in Postgres: they are OTLP metrics (see otel_metrics),
exported only when an endpoint is configured, with categories and numbers but
no interview, trace or turn ID and no content. Without a LangSmith key no
interview content leaves the process. With one, the key is the consent: each
interview's trace carries its content (CV, offer, plan, turns, prompts, model
answers and the evaluation) and is deleted from LangSmith once local detail
expires (see privacy.ExternalDeletionWorker). Metrics and logs stay
content-free either way."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import httpx
import langsmith
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import convert_to_openai_messages
from langchain_core.outputs import LLMResult
from langsmith import Client
from opentelemetry import context as otel_context
from opentelemetry import trace as otel_trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, Status, StatusCode, TraceFlags
from urllib3.util.retry import Retry

from interview_agent import otel_metrics
from interview_agent.config import Settings
from interview_agent.privacy import LangSmithDeletionAPI, guarded_export, register_trace

GRAPH_VERSION = "interview-v1"
SCHEMA_VERSION = 2
PRICE_VERSION = "2026-09-30"
# USD per million tokens; standard text requests below 272K input tokens.
TEXT_PRICES = {
    "gpt-6-astra": (10.0, 1.0, 50.0),
    "gpt-6.1-sol": (2.0, 0.1, 10.0),
    "gpt-5.5": (5.0, 0.5, 30.0),
    "gpt-5.4-mini": (0.75, 0.075, 4.5),
}
logger = logging.getLogger(__name__)
# Automatic LangChain/LangGraph tracing would start its own, unlinked traces
# (duplicating every LLM call and outliving our deletion bookkeeping). Every
# call site also disables it locally; this global fallback makes an env such as
# LANGSMITH_TRACING=true harmless for any future call that forgets to. Explicit
# exports below go through the Client or OpenTelemetry and are not affected.
langsmith.configure(enabled=False)
# One LangSmith trace per interview, shared by the API (planning), the worker
# and the evaluator: each process derives the same root from the interview ID.
TRACE_NAMESPACE = uuid.UUID("0d9c1f4e-5a7b-4c2e-9f3a-6b8d2e1c7a54")


def interview_trace_id(conversation_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(TRACE_NAMESPACE, str(conversation_id))


def dotted_order(start: datetime, run_id: uuid.UUID, parent: str | None = None) -> str:
    """LangSmith's position key, required with trace_id: one `<start>Z<id>`
    segment per ancestor, root first."""
    segment = f"{start.astimezone(UTC):%Y%m%dT%H%M%S%fZ}{run_id}"
    return f"{parent}.{segment}" if parent else segment


def otel_run_id() -> uuid.UUID:
    """A run id an OpenTelemetry span can name as its parent. LangSmith derives
    an OTel span's run id as 8 zero bytes followed by the 8-byte span id."""
    return uuid.UUID(bytes=bytes(8) + os.urandom(8))


def otel_span_run_id(span_id: int) -> uuid.UUID:
    return uuid.UUID(bytes=bytes(8) + span_id.to_bytes(8, "big"))


def _json_default(value: Any) -> Any:
    dump = getattr(value, "model_dump", None)
    return dump(mode="json") if callable(dump) else str(value)


def jsonable(value: Any) -> Any:
    """Run inputs/outputs as plain JSON; unknown objects become their text."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=_json_default))


def chat_messages(messages: list) -> list[dict]:
    """LangChain messages in the OpenAI shape LangSmith renders as a chat."""
    try:
        return jsonable(convert_to_openai_messages(messages))
    except Exception:
        return jsonable([{"role": getattr(m, "type", "unknown"), "content": m} for m in messages])


def practice_thread_id(root_conversation_id: uuid.UUID) -> str:
    """Repeats of an interview share a LangSmith thread (opaque, not the ID)."""
    return str(uuid.uuid5(TRACE_NAMESPACE, f"thread:{root_conversation_id}"))


# Trace metadata (filters and grouping in LangSmith): categories and numbers
# produced by our own code. Content goes in run inputs/outputs instead.
# Metrics keep their own list (otel_metrics.DIMENSIONS).
TRACE_FIELDS = frozenset(
    {
        "process",
        "thread_id",
        "voice_trace_id",
        "ls_provider",
        "ls_model_name",
        "action",
        "close_reason",
        "validation_failed",
        "replayed",
        "attempts",
        "outcome",
        "ended_reason",
        "farewell_status",
        "transcript_integrity",
        "milestones",
        "question_limit",
        "followup_limit",
        "seniority_source",
        "evaluation_status",
        "coverage",
        "score",
        "hired",
        "automatic",
    }
)
# Numeric scores attached to the interview trace, for dashboards and alerts.
FEEDBACK_KEYS = frozenset(
    {
        "evaluation_score",
        "evaluation_coverage",
        "evaluation_complete",
        "hired",
        "farewell_played",
        "transcript_complete",
        "response_onset_seconds",
    }
)


def trace_fields(values: dict) -> dict:
    return {
        key: value
        for key, value in values.items()
        if key in TRACE_FIELDS
        and isinstance(value, (str, int, float, bool))
        and len(str(value)) <= 128
    }


def usage_metadata(usage: dict | None, cost: float | None) -> dict:
    """LangSmith's native token/cost fields, numbers only."""
    usage = usage or {}
    result = {
        key: usage[key]
        for key in ("input_tokens", "output_tokens", "total_tokens")
        if isinstance(usage.get(key), int)
    }
    for details, key in (
        ("input_token_details", "cache_read"),
        ("output_token_details", "reasoning"),
    ):
        value = (usage.get(details) or {}).get(key)
        if isinstance(value, int):
            result[details] = {key: value}
    if cost is not None:
        result["total_cost"] = cost
    return result


def content_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


# Who decides that an answer ended, and what each layer may add. The STT
# provider's own end-of-turn options are left at its defaults (no options are
# sent), so they are recorded as such rather than as copied values.
TURN_HANDLING = {
    "decided_by": "livekit_turn_detector_over_stt_finals",
    "turn_detector": "livekit.inference.TurnDetector",
    "endpointing": {"mode": "fixed", "min_delay": 1.5, "max_delay": 2.5},
    "preemptive_generation": False,
    "vad": "silero_default",
    "stt_end_of_turn_options": "provider_default",
    "per_turn_metrics": ["end_of_turn_delay", "transcription_delay"],
}


def execution_config(settings: Settings, *, language: str, voice: dict, **budgets) -> dict:
    packages = {}
    for package in ("livekit-agents", "langgraph", "langchain-openai", "openai", "langsmith"):
        try:
            packages[package] = version(package)
        except PackageNotFoundError:
            packages[package] = "unknown"
    config = {
        "graph_version": GRAPH_VERSION,
        "schema_version": SCHEMA_VERSION,
        "models": {
            component: {
                "model": getattr(settings, f"{component}_model"),
                "reasoning_effort": getattr(settings, f"{component}_reasoning_effort"),
            }
            for component in ("planner", "interviewer", "evaluator")
        },
        "transport": "responses",
        "stt_model": settings.stt_model,
        "turn_handling": TURN_HANDLING,
        "tts_model": voice["tts_model"],
        "tts_voice": voice["tts_voice"],
        "language": language,
        "dependencies": packages,
        "price_version": PRICE_VERSION,
        **budgets,
    }
    root = Path(__file__).parent
    config["artifact_hashes"] = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in (
            "prompts.py",
            "llm.py",
            "interview/models.py",
            "interview/dialogue.py",
            "interview/evaluation_contract.py",
            "closing.py",
        )
    }
    config["config_version"] = content_hash(config)
    return config


def estimate_text_cost(model: str, usage: dict) -> float | None:
    price = next(
        (v for k, v in TEXT_PRICES.items() if model == k or model.startswith(k + "-")), None
    )
    if price is None:
        return None
    if usage.get("input_tokens") is None or usage.get("output_tokens") is None:
        return None
    incoming = usage.get("input_tokens", 0) or 0
    outgoing = usage.get("output_tokens", 0) or 0
    cached = (usage.get("input_token_details") or {}).get("cache_read", 0) or 0
    input_rate, cache_rate, output_rate = price
    if model.startswith("gpt-6-astra") and incoming > 272_000:
        input_rate, cache_rate, output_rate = input_rate * 2, cache_rate * 2, output_rate * 1.5
    return (
        (incoming - min(cached, incoming)) * input_rate
        + min(cached, incoming) * cache_rate
        + outgoing * output_rate
    ) / 1_000_000


def trace_client(settings: Settings) -> Client:
    """Production export settings: bounded, no retries, no batching."""
    return Client(
        api_key=settings.langsmith_api_key,
        api_url=settings.langsmith_endpoint,
        timeout_ms=5000,
        retry_config=Retry(total=0),
        auto_batch_tracing=False,
        omit_traced_runtime_info=True,
    )


class TraceLinks:
    """Resolves the LangSmith organization and project once, then builds trace
    URLs for the UI. The project appears with its first trace, so it retries."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base: str | None = None

    async def resolve(self) -> bool:
        api = LangSmithDeletionAPI(self.settings)
        try:
            project = await api.project(self.settings.langsmith_project)
        finally:
            await api.close()
        host = langsmith.utils.get_host_url(None, self.settings.langsmith_endpoint)
        self.base = f"{host}/o/{project['tenant_id']}/projects/p/{project['id']}/r/"
        return True

    async def run(self, interval_seconds: float = 60) -> None:
        while self.base is None:
            try:
                await self.resolve()
            except LookupError:
                await asyncio.sleep(interval_seconds)  # no trace exported yet
            except Exception as exc:
                logger.warning("LangSmith metadata export failed: %s", type(exc).__name__)
                await asyncio.sleep(interval_seconds)

    def url(self, trace_id: uuid.UUID) -> str | None:
        return f"{self.base}{trace_id}?poll=true" if self.base else None


_feedback_tasks: set[asyncio.Task] = set()
_project_ids: dict[tuple[str, str], uuid.UUID] = {}


def feedback_project(client: Client, settings: Settings) -> dict:
    """Run-level feedback without session_id (the project ID) is deprecated in
    LangSmith. Resolved once per process; a project not visible yet (its first
    trace is still ingesting) falls back to the deprecated form, not a lost score."""
    key = (settings.langsmith_endpoint, settings.langsmith_project)
    if key not in _project_ids:
        try:
            _project_ids[key] = client.read_project(project_name=settings.langsmith_project).id
        except langsmith.utils.LangSmithNotFoundError:
            return {}
    return {"session_id": _project_ids[key]}


def send_trace_feedback(sessionmaker, settings: Settings, conversation_id, key, score) -> None:
    """Fire-and-forget feedback from a process without the interview's Telemetry
    (e.g. the API receiving a browser measurement). Never delays the caller and
    only exports while the trace is registered and not retired."""
    if not settings.langsmith_api_key or key not in FEEDBACK_KEYS or score is None:
        return
    trace_id = interview_trace_id(conversation_id)

    async def send():
        try:
            client = trace_client(settings)
            await guarded_export(
                sessionmaker,
                trace_id,
                lambda: client.create_feedback(
                    trace_id=trace_id,
                    key=key,
                    score=float(score),
                    extend_trace_retention=False,
                    stop_after_attempt=1,
                    **feedback_project(client, settings),
                ),
            )
        except Exception as exc:
            logger.warning("LangSmith metadata export failed: %s", type(exc).__name__)

    task = asyncio.create_task(send())
    _feedback_tasks.add(task)
    task.add_done_callback(_feedback_tasks.discard)


def _nanoseconds(moment: datetime) -> int:
    return int(moment.timestamp() * 1_000_000) * 1000


class Telemetry:
    def __init__(
        self,
        sessionmaker,
        conversation_id: uuid.UUID,
        settings: Settings,
        config: dict | None = None,
        defer_dimensions: bool = False,
        process: str = "process",
        thread_of: uuid.UUID | None = None,
        closes_trace: bool = False,
        inputs: dict | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.conversation_id = conversation_id
        self.settings = settings
        self.config = config or {}
        self.pending: set[asyncio.Task] = set()
        self.failures = 0
        # The root is the interview; this process is one child span under it.
        self.trace_id = interview_trace_id(conversation_id)
        self.process = process
        # LangSmith accepts a single update per run, so exactly one stage ends
        # the interview's root: the evaluator, the last one. Planner and worker
        # only close their own spans.
        self.closes_trace = closes_trace
        # OTel-compatible, so the worker's voice spans can hang under it.
        self.process_id = otel_run_id()
        self.thread_id = practice_thread_id(thread_of or conversation_id)
        # What this stage received and produced. The first stage to register
        # the interview (the planner) also gives the root its inputs; the
        # closing stage (the evaluator) gives it its outputs.
        self.inputs = jsonable(inputs) if inputs is not None else None
        self.outputs: dict | None = None
        # Set by the worker when LiveKit's spans are traced: spans then become
        # OpenTelemetry spans nested in the voice turn that caused them.
        self.tracer: otel_trace.Tracer | None = None
        # OTel traces this process's spans may join: its own interview trace
        # and, in the worker, the voice session's trace.
        self._otel_traces = {self.trace_id.int}
        self._annotations: dict = {}
        self._span_notes: dict[uuid.UUID, dict] = {}
        self._span_outputs: dict[uuid.UUID, Any] = {}
        self.trace_started = datetime.now(UTC)
        self._trace_closed = False
        self._root_posted = False
        # run id -> (dotted_order, start); the root is placed on registration.
        self._placement: dict[uuid.UUID, tuple[str, datetime]] = {}
        self.parent_span = ContextVar("interview_span", default=None)
        self._last_export = None
        self._registration = None
        self._export_pending = 0
        self._dimensions_ready = asyncio.Event()
        if not defer_dimensions:
            self._dimensions_ready.set()
        self.client = trace_client(settings) if settings.langsmith_api_key else None
        if self.client:
            self._enqueue_export(
                lambda: self.client.create_run(
                    name=process,
                    id=self.process_id,
                    parent_run_id=self.trace_id,
                    run_type="chain",
                    inputs=self.inputs or {},
                    **self._place(self.process_id, self.trace_id, self.trace_started),
                    project_name=settings.langsmith_project,
                    tags=[process, *self.tags()],
                    extra={"metadata": self.trace_metadata()},
                )
            )

    def _place(self, run_id: uuid.UUID, parent_id: uuid.UUID, start: datetime) -> dict:
        """Runs from export threads, which are serialized, so a parent is always
        placed before its children. A child never starts before its parent: the
        root starts at registration, which can follow this process's start."""
        parent_order, parent_start = self._placement[parent_id]
        start = max(start, parent_start)
        order = dotted_order(start, run_id, parent_order)
        self._placement[run_id] = (order, start)
        return {"trace_id": self.trace_id, "dotted_order": order, "start_time": start}

    def _end(self, run_id: uuid.UUID, ended: datetime) -> datetime:
        """An end never precedes the (possibly clamped) start sent for the run."""
        placed = self._placement.get(run_id)
        return max(ended, placed[1]) if placed else ended

    def _create_root(self) -> None:
        order, started = self._placement[self.trace_id]
        self.client.create_run(
            name="interview",
            id=self.trace_id,
            trace_id=self.trace_id,
            dotted_order=order,
            run_type="chain",
            inputs=self.inputs or {},
            start_time=started,
            project_name=self.settings.langsmith_project,
            tags=self.tags(),
            extra={"metadata": self.trace_metadata()},
        )

    def trace_metadata(self, extra: dict | None = None) -> dict:
        return {
            **self.dimensions(),
            "process": self.process,
            "thread_id": self.thread_id,
            **trace_fields(extra or {}),
        }

    def tags(self) -> list[str]:
        dimensions = self.dimensions()
        return [
            f"{key}:{dimensions[key]}"
            for key in ("language", "seniority", "length")
            if dimensions.get(key, "unknown") != "unknown"
        ]

    def annotate(self, **fields) -> None:
        """Filterable facts about this process, sent when its span closes."""
        self._annotations.update(trace_fields(fields))

    def annotate_span(self, span_id, **fields) -> None:
        if span_id in self._span_notes:
            self._span_notes[span_id].update(trace_fields(fields))

    def set_outputs(self, outputs: Any) -> None:
        """What this stage produced (and, for the closing stage, the interview)."""
        self.outputs = jsonable(outputs)

    def span_outputs(self, span_id, outputs: Any) -> None:
        if span_id in self._span_notes:
            self._span_outputs[span_id] = jsonable(outputs)

    def span_context(self) -> SpanContext:
        """This process's run as an OpenTelemetry parent in the interview's trace."""
        return SpanContext(
            trace_id=self.trace_id.int,
            span_id=int.from_bytes(self.process_id.bytes[8:], "big"),
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )

    def follow_otel_trace(self, otel_trace_id: int) -> None:
        self._otel_traces.add(otel_trace_id)

    def _otel_parent(self, context: otel_context.Context | None = None):
        """The given (or current) OTel context while it belongs to a followed
        trace (a voice turn), else this process's run in the interview trace."""
        current = otel_trace.get_current_span(context).get_span_context()
        if current.is_valid and current.trace_id in self._otel_traces:
            return context if context is not None else otel_context.get_current()
        return otel_trace.set_span_in_context(NonRecordingSpan(self.span_context()))

    def current_parent(self):
        """Where a span starting now belongs; passed back to export_span."""
        return otel_context.get_current() if self.tracer else self.parent_span.get()

    def _otel_attributes(self, kind: str, metadata: dict, inputs=None, outputs=None) -> dict:
        attributes = {
            "langsmith.span.kind": kind,
            **{
                f"langsmith.metadata.{key}": value
                for key, value in metadata.items()
                if isinstance(value, (str, int, float, bool))
            },
        }
        if inputs is not None:
            attributes["gen_ai.prompt"] = json.dumps(inputs, ensure_ascii=False)
        if outputs is not None:
            attributes["gen_ai.completion"] = json.dumps(outputs, ensure_ascii=False)
        return attributes

    def feedback(self, key: str, score: float | int | bool | None) -> None:
        """A numeric score on the interview trace (dashboards, filters, alerts)."""
        if self.client is None or key not in FEEDBACK_KEYS or score is None:
            return
        self._enqueue_export(
            lambda: self.client.create_feedback(
                trace_id=self.trace_id,
                key=key,
                score=float(score),
                # The SDK default would upgrade the trace to 400-day retention
                # (10x price) and outlive our deletion policy.
                extend_trace_retention=False,
                stop_after_attempt=1,
                start_time=self._placement[self.trace_id][1],
                **feedback_project(self.client, self.settings),
            )
        )

    def dimensions(self, extra: dict | None = None) -> dict:
        return otel_metrics.safe_dimensions(
            {
                "graph_version": self.config.get("graph_version", GRAPH_VERSION),
                "language": self.config.get("language", "unknown"),
                "seniority": self.config.get("seniority", "unknown"),
                "length": self.config.get("interview_length", "unknown"),
                "config_version": self.config.get("config_version", "unknown"),
                "trace_id": str(self.trace_id),
                **(extra or {}),
            },
            detail=True,
        )

    async def record(
        self,
        component: str,
        name: str,
        value: float | None,
        *,
        turn_id: str | None = None,
        dimensions: dict | None = None,
    ) -> None:
        """An anonymous sample: the interview's categories and the component's
        configured model, never the trace or turn it came from."""
        await self._dimensions_ready.wait()
        model_component = (
            "interviewer" if component in ("graph", "dialogue", "voice", "closing") else component
        )
        configured = (self.config.get("models") or {}).get(model_component, {})
        dimensions = {
            **({"model": configured["model"]} if configured.get("model") else {}),
            **(dimensions or {}),
        }
        otel_metrics.record(component, name, value, self.dimensions(dimensions))

    def emit(
        self,
        component: str,
        name: str,
        value: float | None,
        *,
        turn_id: str | None = None,
        dimensions: dict | None = None,
    ) -> None:
        async def write():
            try:
                await self.record(component, name, value, turn_id=turn_id, dimensions=dimensions)
            except Exception:
                self.failures += 1
                logger.exception("Metric recording failed: %s/%s", component, name)

        task = asyncio.create_task(write())
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    def _enqueue_export(self, operation) -> None:
        if self._export_pending >= 256:
            self.failures += 1
            self.emit("telemetry", "export_queue_dropped", 1)
            return
        self._export_pending += 1
        predecessor = self._last_export

        async def export():
            try:
                if predecessor is not None:
                    await asyncio.shield(predecessor)
                if self._registration is None:
                    self._registration = asyncio.create_task(
                        register_trace(
                            self.sessionmaker, self.trace_id, self.conversation_id, self.settings
                        )
                    )
                registered = await asyncio.shield(self._registration)
                if not registered:
                    return
                status, root_started = registered
                self._placement.setdefault(
                    self.trace_id, (dotted_order(root_started, self.trace_id), root_started)
                )
                # Only the process that registered the interview creates the
                # root; exports are serialized, so it precedes every child.
                if status == "created" and not self._root_posted:
                    self._root_posted = True
                    await guarded_export(self.sessionmaker, self.trace_id, self._create_root)
                await guarded_export(self.sessionmaker, self.trace_id, operation)
            except Exception as exc:
                self.failures += 1
                logger.warning("LangSmith metadata export failed: %s", type(exc).__name__)
                self.emit(
                    "telemetry", "export_errors", 1, dimensions={"error_type": type(exc).__name__}
                )

        def completed(task):
            self.pending.discard(task)
            self._export_pending -= 1

        task = asyncio.create_task(export())
        self._last_export = task
        self.pending.add(task)
        task.add_done_callback(completed)

    async def drain(self, timeout_seconds: float = 8) -> None:
        self._dimensions_ready.set()
        if self.client and not self._trace_closed:
            self._trace_closed = True
            ended = datetime.now(UTC)

            def close():
                self.client.update_run(
                    self.process_id,
                    end_time=self._end(self.process_id, ended),
                    outputs=self.outputs or {},
                    extra={"metadata": self.trace_metadata(self._annotations)},
                )
                if not self.closes_trace:
                    return
                # A re-evaluation conflicts: the first evaluation already ended it.
                with contextlib.suppress(langsmith.utils.LangSmithConflictError):
                    self.client.update_run(
                        self.trace_id,
                        end_time=self._end(self.trace_id, ended),
                        outputs=self.outputs or {},
                    )

            self._enqueue_export(close)
        if self.pending:
            done, pending = await asyncio.wait(list(self.pending), timeout=timeout_seconds)
            for task in done:
                task.result()
            for task in pending:
                task.cancel()
                self.failures += 1
            if pending:
                logger.warning("Telemetry drain deadline expired for %d tasks", len(pending))

    async def export_span(
        self,
        span_id: uuid.UUID,
        component: str,
        start: datetime,
        end: datetime,
        metadata: dict,
        error: bool = False,
        parent=None,
        run_type: str = "llm",
        usage: dict | None = None,
        cost: float | None = None,
        inputs: dict | None = None,
        outputs: dict | None = None,
    ) -> None:
        """A finished leaf (an LLM call). `parent` comes from current_parent()."""
        if self.client is None:
            return
        model = metadata.get("resolved_model") or metadata.get("model", "unknown")
        trace_extra = {
            **self.trace_metadata(metadata),
            # LangSmith shows tokens and cost natively for llm runs; the cost is
            # ours, so models missing from its price table are still priced.
            "ls_provider": "openai",
            "ls_model_name": model,
        }
        if self.tracer is not None:
            attributes = self._otel_attributes(run_type, trace_extra, inputs, outputs)
            attributes.update(
                {
                    "gen_ai.request.model": model,
                    "gen_ai.provider.name": "openai",
                    "gen_ai.system": "openai",
                    "langsmith.usage_metadata": json.dumps(usage_metadata(usage, cost)),
                }
            )
            span = self.tracer.start_span(
                component,
                context=self._otel_parent(parent),
                start_time=_nanoseconds(start),
                attributes=attributes,
            )
            if error:
                span.set_status(Status(StatusCode.ERROR, "provider_error"))
            span.end(end_time=_nanoseconds(max(end, start)))
            return
        trace_extra["usage_metadata"] = usage_metadata(usage, cost)

        # A finished leaf span: one request instead of create + update.
        def create():
            parent_id = parent or self.process_id
            placed = self._place(span_id, parent_id, start)
            self.client.create_run(
                name=component,
                id=span_id,
                parent_run_id=parent_id,
                run_type=run_type,
                inputs=inputs or {},
                outputs=outputs or {},
                **placed,
                end_time=max(end, placed["start_time"]),
                error="provider_error" if error else None,
                project_name=self.settings.langsmith_project,
                extra={"metadata": trace_extra},
            )

        self._enqueue_export(create)

    @asynccontextmanager
    async def span(self, name: str, inputs: dict | None = None):
        """A stage of this process; set its result with span_outputs()."""
        if self.client is not None and self.tracer is not None:
            async with self._otel_span(name, inputs) as span_id:
                yield span_id
            return
        span_id = uuid.uuid4()
        parent = self.parent_span.get()
        token = self.parent_span.set(span_id)
        start = datetime.now(UTC)
        self._span_notes[span_id] = {}
        sent_inputs = jsonable(inputs) if inputs is not None else {}
        if self.client:
            self._enqueue_export(
                lambda: self.client.create_run(
                    name=name,
                    id=span_id,
                    parent_run_id=parent or self.process_id,
                    run_type="chain",
                    inputs=sent_inputs,
                    **self._place(span_id, parent or self.process_id, start),
                    project_name=self.settings.langsmith_project,
                    extra={"metadata": self.trace_metadata()},
                )
            )
        failed = False
        try:
            yield span_id
        except BaseException:
            failed = True
            raise
        finally:
            self.parent_span.reset(token)
            notes = self._span_notes.pop(span_id, {})
            outputs = self._span_outputs.pop(span_id, None)
            if self.client:
                ended = datetime.now(UTC)
                self._enqueue_export(
                    lambda: self.client.update_run(
                        span_id,
                        end_time=self._end(span_id, ended),
                        outputs=outputs or {},
                        error="stage_error" if failed else None,
                        extra={"metadata": self.trace_metadata(notes)},
                    )
                )

    @asynccontextmanager
    async def _otel_span(self, name: str, inputs: dict | None):
        with self.tracer.start_as_current_span(
            name,
            context=self._otel_parent(),
            attributes=self._otel_attributes(
                "chain", {}, jsonable(inputs) if inputs is not None else None
            ),
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            span_id = otel_span_run_id(span.get_span_context().span_id)
            self._span_notes[span_id] = {}
            try:
                yield span_id
            except BaseException:
                span.set_status(Status(StatusCode.ERROR, "stage_error"))
                raise
            finally:
                notes = self._span_notes.pop(span_id, {})
                outputs = self._span_outputs.pop(span_id, None)
                span.set_attributes(
                    self._otel_attributes("chain", self.trace_metadata(notes), outputs=outputs)
                )

    def resolve_dimensions(self, dimensions: dict):
        self.config = {**self.config, **dimensions}
        self._dimensions_ready.set()


class LLMObserver(AsyncCallbackHandler):
    """LLM invocations and SDK HTTP attempts are measured separately. Metrics
    never read bodies; the exported LLM run carries prompt and answer."""

    def __init__(
        self,
        telemetry: Telemetry,
        component: str,
        model: str,
        effort: str,
        turn_id: str | None = None,
    ) -> None:
        self.telemetry = telemetry
        self.component = component
        self.model = model
        self.effort = effort
        self.turn_id = turn_id
        self.starts: dict[uuid.UUID, tuple[float, datetime]] = {}
        self.first_tokens: set[uuid.UUID] = set()
        self.parents: dict[uuid.UUID, Any] = {}
        self.prompts: dict[uuid.UUID, dict] = {}

    async def http_request(self, request: httpx.Request):
        request.extensions["interview_started"] = time.monotonic()
        try:
            retry = int(request.headers.get("x-stainless-retry-count", "0"))
        except ValueError:
            retry = 0
        self.telemetry.emit(
            self.component,
            "http_requests",
            1,
            turn_id=self.turn_id,
            dimensions={"model": self.model},
        )
        if retry > 0:
            self.telemetry.emit(
                self.component,
                "http_retries",
                1,
                turn_id=self.turn_id,
                dimensions={"model": self.model},
            )

    async def http_response(self, response: httpx.Response):
        dimensions = {"model": self.model, "status_code": response.status_code}
        started = response.request.extensions.get("interview_started")
        self.telemetry.emit(
            self.component,
            "http_headers_seconds",
            time.monotonic() - started if started is not None else None,
            turn_id=self.turn_id,
            dimensions=dimensions,
        )
        if response.status_code >= 400:
            self.telemetry.emit(
                self.component, "http_errors", 1, turn_id=self.turn_id, dimensions=dimensions
            )

    async def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        self.starts[run_id] = (time.monotonic(), datetime.now(UTC))
        self.parents[run_id] = self.telemetry.current_parent()
        self.prompts[run_id] = {
            "messages": chat_messages(messages[0])
            if len(messages) == 1
            else [chat_messages(batch) for batch in messages]
        }
        self.telemetry.emit(
            self.component,
            "llm_invocations",
            1,
            turn_id=self.turn_id,
            dimensions={"model": self.model, "reasoning_effort": self.effort},
        )

    async def on_llm_new_token(self, token, *, run_id, **kwargs):
        if token and run_id not in self.first_tokens and run_id in self.starts:
            self.first_tokens.add(run_id)
            self.telemetry.emit(
                self.component,
                "ttft_seconds",
                time.monotonic() - self.starts[run_id][0],
                turn_id=self.turn_id,
                dimensions={"model": self.model},
            )

    async def on_llm_end(self, response: LLMResult, *, run_id, **kwargs):
        usage = {}
        resolved_model = None
        answers = []
        for generations in response.generations:
            for generation in generations:
                message = getattr(generation, "message", None)
                answers.append(message if message is not None else ("ai", generation.text))
                usage = getattr(message, "usage_metadata", None) or usage
                resolved_model = (getattr(message, "response_metadata", {}) or {}).get(
                    "model_name", resolved_model
                )
        dimensions = {
            "model": self.model,
            **({"resolved_model": resolved_model} if resolved_model else {}),
            "reasoning_effort": self.effort,
            "price_version": PRICE_VERSION,
        }
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            self.telemetry.emit(
                self.component, key, usage.get(key), turn_id=self.turn_id, dimensions=dimensions
            )
        for name, details, key in (
            ("cached_tokens", "input_token_details", "cache_read"),
            ("reasoning_tokens", "output_token_details", "reasoning"),
        ):
            self.telemetry.emit(
                self.component,
                name,
                (usage.get(details) or {}).get(key),
                turn_id=self.turn_id,
                dimensions=dimensions,
            )
        cost = estimate_text_cost(self.model, usage) if usage else None
        self.telemetry.emit(
            self.component,
            "estimated_cost_usd",
            cost,
            turn_id=self.turn_id,
            dimensions=dimensions,
        )
        outputs = {"messages": chat_messages(answers)}
        await self._finish(run_id, dimensions, False, usage=usage, cost=cost, outputs=outputs)

    async def on_llm_error(self, error, *, run_id, **kwargs):
        self.telemetry.emit(
            self.component,
            "errors",
            1,
            turn_id=self.turn_id,
            dimensions={"model": self.model, "error_type": type(error).__name__},
        )
        await self._finish(
            run_id, {"model": self.model}, True, outputs={"error": type(error).__name__}
        )

    async def _finish(self, run_id, dimensions, error, *, usage=None, cost=None, outputs=None):
        started, stamp = self.starts.pop(run_id, (time.monotonic(), datetime.now(UTC)))
        self.telemetry.emit(
            self.component,
            "duration_seconds",
            time.monotonic() - started,
            turn_id=self.turn_id,
            dimensions=dimensions,
        )
        await self.telemetry.export_span(
            run_id,
            self.component,
            stamp,
            datetime.now(UTC),
            dimensions,
            error,
            parent=self.parents.pop(run_id, None),
            usage=usage,
            cost=cost,
            inputs=self.prompts.pop(run_id, None),
            outputs=outputs,
        )
