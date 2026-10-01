"""Durable metadata-only telemetry; never export source documents or transcripts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import httpx
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult
from langsmith import Client
from urllib3.util.retry import Retry

from interview_agent.config import Settings
from interview_agent.interview import db
from interview_agent.metrics import record_metric, safe_dimensions
from interview_agent.privacy import guarded_export, register_trace

GRAPH_VERSION = "interview-v2"
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


class Telemetry:
    def __init__(
        self,
        sessionmaker,
        conversation_id: uuid.UUID,
        settings: Settings,
        config: dict | None = None,
        defer_dimensions: bool = False,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.conversation_id = conversation_id
        self.settings = settings
        self.config = config or {}
        self.pending: set[asyncio.Task] = set()
        self.failures = 0
        self.trace_id = uuid.uuid4()
        self.trace_started = datetime.now(UTC)
        self._trace_closed = False
        self.parent_span = ContextVar("interview_span", default=None)
        self._last_export = None
        self._registration = None
        self._export_pending = 0
        self._dimensions_ready = asyncio.Event()
        if not defer_dimensions:
            self._dimensions_ready.set()
        self.client = (
            Client(
                api_key=settings.langsmith_api_key,
                api_url=settings.langsmith_endpoint,
                timeout_ms=5000,
                retry_config=Retry(total=0),
                auto_batch_tracing=False,
                omit_traced_runtime_info=True,
            )
            if settings.langsmith_api_key
            else None
        )
        if self.client:
            self._enqueue_export(
                lambda: self.client.create_run(
                    name="interview-process",
                    id=self.trace_id,
                    trace_id=self.trace_id,
                    run_type="chain",
                    inputs={"redacted": True},
                    start_time=self.trace_started,
                    project_name=settings.langsmith_project,
                    extra={"metadata": self.dimensions()},
                )
            )

    def dimensions(self, extra: dict | None = None) -> dict:
        return safe_dimensions(
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
        await self._dimensions_ready.wait()
        model_component = (
            "interviewer" if component in ("graph", "dialogue", "voice", "closing") else component
        )
        configured = (self.config.get("models") or {}).get(model_component, {})
        dimensions = {
            **({"model": configured["model"]} if configured.get("model") else {}),
            **(dimensions or {}),
        }
        async with self.sessionmaker() as session:
            await record_metric(
                session,
                db.MetricEvent(
                    id=uuid.uuid4(),
                    conversation_id=self.conversation_id,
                    component=component,
                    name=name,
                    value=value,
                    turn_id=turn_id,
                    dimensions=self.dimensions(dimensions),
                ),
            )

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
                logger.exception("Metric persistence failed: %s/%s", component, name)

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
                if await asyncio.shield(self._registration):
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

    async def drain(self, timeout_seconds: float = 5) -> None:
        self._dimensions_ready.set()
        if self.client and not self._trace_closed:
            self._trace_closed = True
            self._enqueue_export(
                lambda: self.client.update_run(
                    self.trace_id,
                    end_time=datetime.now(UTC),
                    outputs={"redacted": True},
                    extra={"metadata": self.dimensions()},
                )
            )
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
        parent_id: uuid.UUID | None = None,
        run_type: str = "llm",
    ) -> None:
        if self.client is None:
            return

        def export():
            self.client.create_run(
                name=component,
                id=span_id,
                trace_id=self.trace_id,
                parent_run_id=parent_id or self.trace_id,
                run_type=run_type,
                inputs={"redacted": True},
                start_time=start,
                project_name=self.settings.langsmith_project,
                extra={"metadata": self.dimensions(metadata)},
            )
            self.client.update_run(
                span_id,
                end_time=end,
                outputs={"redacted": True},
                error="provider_error" if error else None,
            )

        self._enqueue_export(export)

    @asynccontextmanager
    async def span(self, name: str):
        span_id = uuid.uuid4()
        parent = self.parent_span.get()
        token = self.parent_span.set(span_id)
        start = datetime.now(UTC)
        if self.client:
            self._enqueue_export(
                lambda: self.client.create_run(
                    name=name,
                    id=span_id,
                    trace_id=self.trace_id,
                    parent_run_id=parent or self.trace_id,
                    run_type="chain",
                    inputs={"redacted": True},
                    start_time=start,
                    project_name=self.settings.langsmith_project,
                    extra={"metadata": self.dimensions()},
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
            if self.client:
                self._enqueue_export(
                    lambda: self.client.update_run(
                        span_id,
                        end_time=datetime.now(UTC),
                        outputs={"redacted": True},
                        error="stage_error" if failed else None,
                    )
                )

    def resolve_dimensions(self, dimensions: dict):
        self.config = {**self.config, **dimensions}
        self._dimensions_ready.set()


class LLMObserver(AsyncCallbackHandler):
    """LLM invocations and SDK HTTP attempts are measured separately, without bodies."""

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
        self.parents: dict[uuid.UUID, uuid.UUID | None] = {}

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
        self.parents[run_id] = self.telemetry.parent_span.get()
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
        for generations in response.generations:
            for generation in generations:
                message = getattr(generation, "message", None)
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
        self.telemetry.emit(
            self.component,
            "estimated_cost_usd",
            estimate_text_cost(self.model, usage) if usage else None,
            turn_id=self.turn_id,
            dimensions=dimensions,
        )
        await self._finish(run_id, dimensions, False)

    async def on_llm_error(self, error, *, run_id, **kwargs):
        self.telemetry.emit(
            self.component,
            "errors",
            1,
            turn_id=self.turn_id,
            dimensions={"model": self.model, "error_type": type(error).__name__},
        )
        await self._finish(run_id, {"model": self.model}, True)

    async def _finish(self, run_id, dimensions, error):
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
            parent_id=self.parents.pop(run_id, None),
        )
