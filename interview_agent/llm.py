"""Shared LLM plumbing: model tuning, the ChatOpenAI factory and the
streaming chat node that both LangGraph brains (generic assistant and
interviewer) use. Previously duplicated in graph.py / interviewer_graph.py.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.messages import AIMessage, BaseMessageChunk, SystemMessage
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
from langgraph.config import get_stream_writer
from langgraph.graph import MessagesState
from pydantic import SecretStr

from interview_agent.config import Settings


def chat_model_tuning(
    model: str, *, reasoning_effort: str, temperature: float
) -> dict[str, Any]:
    """GPT-5-family models reject custom temperature and are tuned via
    reasoning effort instead; pre-GPT-5 models are the reverse. The
    `reasoning` dict routes the call to the Responses API — required, since
    Chat Completions rejects reasoning_effort + tools for these models."""
    if model.startswith("gpt-5"):
        return {"reasoning": {"effort": reasoning_effort}}
    return {"temperature": temperature}


def build_chat_model(
    settings: Settings,
    *,
    model: str,
    reasoning_effort: str,
    stream_usage: bool = False,
    max_retries: int = 3,
) -> ChatOpenAI:
    """ChatOpenAI with auth, tuning and transport retries in one place.

    `max_retries` retries transient failures (connection errors, 429/5xx)
    inside the OpenAI client, before the first streamed chunk — safe for the
    voice path: nothing already spoken is ever re-generated."""
    return ChatOpenAI(
        model=model,
        api_key=SecretStr(settings.openai_api_key),
        stream_usage=stream_usage,
        max_retries=max_retries,
        **chat_model_tuning(
            model,
            reasoning_effort=reasoning_effort,
            temperature=settings.interviewer_temperature,
        ),
    )


def make_streaming_chat_node(
    llm: Runnable, system_message: SystemMessage
) -> Callable[[MessagesState], Awaitable[dict]]:
    """Build the `chat` node shared by both graphs."""

    async def chat(state: MessagesState) -> dict:
        # LiveKit's LLMAdapter already injects the Agent instructions as a
        # leading SystemMessage, so only prepend ours when the graph runs
        # standalone (tests, LangGraph Studio) — never double the prompt.
        messages = state["messages"]
        if not any(isinstance(m, SystemMessage) for m in messages):
            messages = [system_message, *messages]
        # The adapter runs this graph with stream_mode="custom": only text
        # handed to the writer is spoken, so tool outputs never reach TTS.
        # No-op when run via ainvoke (tests, Studio).
        writer = get_stream_writer()
        full: AIMessage | BaseMessageChunk | None = None
        async for chunk in llm.astream(messages):
            # Summing chunks aggregates tool_call deltas into complete calls.
            # (chunk + chunk is always a chunk; the __add__ stubs over-widen.)
            full = chunk if full is None else full + chunk  # type: ignore[assignment,operator]
            if chunk.text:  # flattens Responses-API content blocks
                writer(chunk.text)
        return {"messages": [full]}

    return chat
