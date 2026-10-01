"""Shared LLM plumbing: model tuning, the ChatOpenAI factory and usage totals."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

import httpx
from langchain_core.messages.ai import UsageMetadata
from langchain_openai import ChatOpenAI
from openai import AsyncOpenAI
from pydantic import SecretStr

from interview_agent.config import Settings


class ObservedTransport(httpx.AsyncBaseTransport):
    """Include failed connection attempts, which HTTP response hooks never see."""

    def __init__(self, observer, transport=None):
        self.observer = observer
        self.transport = transport or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        started = time.monotonic()
        try:
            return await self.transport.handle_async_request(request)
        except httpx.TransportError as exc:
            self.observer.telemetry.emit(
                self.observer.component,
                "http_transport_errors",
                1,
                turn_id=self.observer.turn_id,
                dimensions={"model": self.observer.model, "error_type": type(exc).__name__},
            )
            self.observer.telemetry.emit(
                self.observer.component,
                "http_failed_attempt_seconds",
                time.monotonic() - started,
                turn_id=self.observer.turn_id,
                dimensions={"model": self.observer.model},
            )
            raise

    async def aclose(self):
        await self.transport.aclose()


def chat_model_tuning(model: str, *, reasoning_effort: str, temperature: float) -> dict[str, Any]:
    """GPT-5-family models reject custom temperature and are tuned via
    reasoning effort instead; pre-GPT-5 models are the reverse. The
    `reasoning` dict routes the call to the Responses API — required, since
    Chat Completions rejects reasoning_effort + tools for these models."""
    if model.startswith(("gpt-5", "gpt-6")):
        allowed = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
        if model.startswith(("gpt-6-astra", "gpt-6.1-sol")):
            allowed -= {"none", "minimal"}
        if reasoning_effort not in allowed:
            raise ValueError(f"Unsupported reasoning effort {reasoning_effort!r} for {model}")
        return {"reasoning": {"effort": reasoning_effort}}
    return {"temperature": temperature}


def build_chat_model(
    settings: Settings,
    *,
    model: str,
    reasoning_effort: str,
    stream_usage: bool = False,
    max_retries: int = 3,
    timeout_seconds: float = 120,
    telemetry_callback=None,
) -> ChatOpenAI:
    """ChatOpenAI with auth, tuning and transport retries in one place.

    `max_retries` retries transient failures (connection errors, 429/5xx)
    inside the OpenAI client, before the first streamed chunk — safe for the
    voice path: nothing already spoken is ever re-generated."""
    tuning = chat_model_tuning(
        model, reasoning_effort=reasoning_effort, temperature=settings.interviewer_temperature
    )
    http_options = {}
    if telemetry_callback is not None:
        http_options.update(
            transport=ObservedTransport(telemetry_callback),
            event_hooks={
                "request": [telemetry_callback.http_request],
                "response": [telemetry_callback.http_response],
            },
        )
    # LangChain caches its default transport. Closing an isolated model must
    # not poison the client used by the next planner/evaluator in this process.
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds, connect=min(10, timeout_seconds)),
        **http_options,
    )
    return ChatOpenAI(
        model=model,
        api_key=SecretStr(settings.openai_api_key),
        stream_usage=stream_usage,
        streaming=True,
        max_retries=max_retries,
        timeout=timeout_seconds,
        use_responses_api=True,
        store=False,
        http_async_client=http_client,
        **tuning,
    )


async def close_chat_model(model) -> None:
    """Release the owned SDK client after each isolated planner/decision/evaluation."""
    client = getattr(model, "root_async_client", None)
    if isinstance(client, AsyncOpenAI):
        await client.close()


def summarize_usage(usage_by_model: Mapping[str, UsageMetadata]) -> dict[str, int]:
    """Collapse UsageMetadataCallbackHandler's per-model-name dict into the
    flat counters persisted on conversations.token_usage."""
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for usage in usage_by_model.values():
        for key in totals:
            totals[key] += usage.get(key, 0) or 0
    return totals
