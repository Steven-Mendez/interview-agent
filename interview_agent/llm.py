"""Shared LLM plumbing: model tuning, the ChatOpenAI factory and the
streaming chat node that both LangGraph brains (generic assistant and
interviewer) use. Previously duplicated in graph.py / interviewer_graph.py.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain_core.messages import AIMessage, BaseMessageChunk, SystemMessage
from langchain_core.messages.ai import UsageMetadata
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


def make_json_prefix_filter(emit: Callable[[str], None]) -> Callable[[str], None]:
    """Wrap a stream writer so a leading JSON object is swallowed.

    Reasoning-free mini models occasionally narrate their tool-call
    arguments ('{"milestone_number":5,...}') as text right before making
    the real call — spoken aloud, that is garbage. Legitimate speech never
    starts with '{', so while the turn's stream opens with one (or several)
    JSON objects, drop them; everything after flows through untouched.
    """
    speaking = False  # once real speech starts, pass everything through
    depth = 0  # brace depth of the JSON object currently being dropped

    def write(text: str) -> None:
        nonlocal speaking, depth
        if speaking:
            emit(text)
            return
        for i, char in enumerate(text):
            if depth:
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                continue
            if char.isspace():
                continue
            if char == "{":
                depth = 1
                continue
            speaking = True
            emit(text[i:])
            return

    return write


def make_streaming_chat_node(
    llm: Runnable,
    system_message: SystemMessage,
    *,
    on_usage: Callable[[UsageMetadata], None] | None = None,
) -> Callable[[MessagesState], Awaitable[dict]]:
    """Build the `chat` node shared by both graphs.

    `on_usage` is called with the usage_metadata of every completed LLM call
    (once per ReAct iteration) — the interviewer uses it to meter spend.
    """

    async def chat(state: MessagesState) -> dict:
        # LiveKit's LLMAdapter always injects the Agent instructions as the
        # LEADING SystemMessage, so only prepend ours when the graph runs
        # standalone (tests, LangGraph Studio) — never double the prompt.
        # Position 0 is the test, not "any system message": the interviewer
        # graph appends a per-turn milestone-status SystemMessage later in
        # the list, which must not suppress the static prompt.
        messages = state["messages"]
        if not messages or not isinstance(messages[0], SystemMessage):
            messages = [system_message, *messages]
        # The adapter runs this graph with stream_mode="custom": only text
        # handed to the writer is spoken, so tool outputs never reach TTS.
        # No-op when run via ainvoke (tests, Studio).
        speak = make_json_prefix_filter(get_stream_writer())
        full: AIMessage | BaseMessageChunk | None = None
        async for chunk in llm.astream(messages):
            # Summing chunks aggregates tool_call deltas into complete calls.
            # (chunk + chunk is always a chunk; the __add__ stubs over-widen.)
            full = chunk if full is None else full + chunk  # type: ignore[assignment,operator]
            if chunk.text:  # flattens Responses-API content blocks
                speak(str(chunk.text))
        # With stream_usage=True the summed chunk carries usage_metadata (on
        # both the Chat Completions and Responses API paths).
        if on_usage is not None and full is not None and full.usage_metadata:
            on_usage(full.usage_metadata)
        return {"messages": [full]}

    return chat


def summarize_usage(usage_by_model: Mapping[str, UsageMetadata]) -> dict[str, int]:
    """Collapse UsageMetadataCallbackHandler's per-model-name dict into the
    flat counters persisted on conversations.token_usage."""
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for usage in usage_by_model.values():
        for key in totals:
            totals[key] += usage.get(key, 0) or 0
    return totals
