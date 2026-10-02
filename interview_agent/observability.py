"""Anonymous metrics and, when LangSmith is configured, full interview traces.

Metrics are not rows in Postgres: they are OTLP metrics (see otel_metrics),
exported only when an endpoint is configured, with categories and numbers but
no interview, trace or turn ID and no content. Without a LangSmith key nothing
is traced (see config). With one, the key is the consent: LangChain/LangGraph
and @traceable send each process's runs with their content (CV, offer, plan,
turns, prompts, model answers and the evaluation), grouped per interview in a
LangSmith Thread by trace_metadata(). The app never deletes them; LangSmith's
retention applies. Metrics and logs stay content-free either way."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import httpx
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult

from interview_agent import otel_metrics
from interview_agent.config import Settings

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


# Our own plumbing, never a traced input: Settings carries the API keys.
_UNTRACED_INPUTS = frozenset({"settings", "usage_callback", "telemetry_callback"})


def traced_inputs(inputs: dict) -> dict:
    """process_inputs for the @traceable planner and evaluator."""
    return {key: value for key, value in inputs.items() if key not in _UNTRACED_INPUTS}


class Telemetry:
    """One process's part of an interview: anonymous metrics with the
    interview's categories, and the metadata of its LangSmith traces."""

    def __init__(
        self,
        conversation_id: uuid.UUID,
        config: dict | None = None,
        *,
        defer_dimensions: bool = False,
        process: str = "process",
        thread_of: uuid.UUID | None = None,
    ) -> None:
        self.conversation_id = conversation_id
        self.config = config or {}
        self.process = process
        # Repeats of an interview share the original's LangSmith Thread.
        self.thread_of = thread_of
        self.pending: set[asyncio.Task] = set()
        self.failures = 0
        self._dimensions_ready = asyncio.Event()
        if not defer_dimensions:
            self._dimensions_ready.set()

    def trace_metadata(self, **extra) -> dict:
        """Metadata for this process's LangSmith root runs. `thread_id` groups
        them into the interview's Thread; never named `conversation_id`, which
        LangSmith may also read as a thread key."""
        return {
            "thread_id": str(self.thread_of or self.conversation_id),
            "interview_id": str(self.conversation_id),
            "process": self.process,
            **self.dimensions(),
            **extra,
        }

    def dimensions(self, extra: dict | None = None) -> dict:
        return otel_metrics.safe_dimensions(
            {
                "graph_version": self.config.get("graph_version", GRAPH_VERSION),
                "language": self.config.get("language", "unknown"),
                "seniority": self.config.get("seniority", "unknown"),
                "length": self.config.get("interview_length", "unknown"),
                "config_version": self.config.get("config_version", "unknown"),
                **(extra or {}),
            }
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

    async def drain(self, timeout_seconds: float = 8) -> None:
        """Releases samples still waiting for deferred dimensions (they would
        wait forever otherwise) and waits, bounded, for every pending one."""
        self._dimensions_ready.set()
        if self.pending:
            done, pending = await asyncio.wait(list(self.pending), timeout=timeout_seconds)
            for task in done:
                task.result()
            for task in pending:
                task.cancel()
                self.failures += 1
            if pending:
                logger.warning("Telemetry drain deadline expired for %d tasks", len(pending))

    def resolve_dimensions(self, dimensions: dict):
        self.config = {**self.config, **dimensions}
        self._dimensions_ready.set()


class LLMObserver(AsyncCallbackHandler):
    """LLM invocations and SDK HTTP attempts are measured separately. Metrics
    never read bodies; LangSmith's own tracer records prompts and answers."""

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
        self.starts: dict[uuid.UUID, float] = {}
        self.first_tokens: set[uuid.UUID] = set()

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
        self.starts[run_id] = time.monotonic()
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
                time.monotonic() - self.starts[run_id],
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
        self._finish(run_id, dimensions)

    async def on_llm_error(self, error, *, run_id, **kwargs):
        self.telemetry.emit(
            self.component,
            "errors",
            1,
            turn_id=self.turn_id,
            dimensions={"model": self.model, "error_type": type(error).__name__},
        )
        self._finish(run_id, {"model": self.model})

    def _finish(self, run_id, dimensions):
        # Feeds Grafana's "LLM call duration" panels.
        started = self.starts.pop(run_id, time.monotonic())
        self.telemetry.emit(
            self.component,
            "duration_seconds",
            time.monotonic() - started,
            turn_id=self.turn_id,
            dimensions=dimensions,
        )
