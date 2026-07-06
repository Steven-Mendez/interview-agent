"""Per-session interviewer brain: the same ReAct shape as `graph.py`, but
built by a factory that closes over the session's context (conversation id,
DB, Qdrant, end signal) so the LangChain tools can reach it — LiveKit's
LLMAdapter gives tools no per-session context of its own.

`end_interview` must NOT touch the LiveKit session (we're mid-generation
inside the graph): it sets `end_event`, and a watcher task in the worker
entrypoint does the actual farewell/close.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable

from langchain_core.messages import SystemMessage, ToolMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.tools import tool
from langchain_openai import OpenAIEmbeddings
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from interview_agent.config import Settings
from interview_agent.interview import db, rag
from interview_agent.llm import build_chat_model, make_streaming_chat_node
from interview_agent.prompts import build_milestone_status

logger = logging.getLogger("interview_agent.interviewer")

# Tools must never raise: an exception here kills the turn mid-conversation.
# Returning the error as text lets the LLM acknowledge it and move on.
_TOOL_FAILED = "Tool failed: {exc}. Continue the interview without it."


def _route_after_tools(state: MessagesState) -> str:
    """End the run once end_interview has executed: re-entering chat would
    generate a spoken goodbye, and the worker's finish() already delivers
    the single farewell — the candidate would hear two otherwise.

    Scans all trailing ToolMessages (not just the last) so a parallel batch
    like complete_milestone + end_interview in one model turn still ends."""
    for message in reversed(state["messages"]):
        if not isinstance(message, ToolMessage):
            break
        if message.name == "end_interview":
            return END
    return "chat"


def build_interviewer_graph(
    settings: Settings,
    conversation_id: uuid.UUID,
    sessionmaker: async_sessionmaker[AsyncSession],
    qdrant: AsyncQdrantClient,
    embeddings: OpenAIEmbeddings,
    end_event: asyncio.Event,
    system_prompt: str,
    usage_sink: Callable[[UsageMetadata], None] | None = None,
):
    """Compile a per-session interviewer workflow for LiveKit's LLMAdapter."""

    @tool
    async def search_resume(query: str) -> str:
        """Semantically search the candidate's resume. Use before probing a
        specific claim (projects, dates, technologies) to ground your question.

        Args:
            query: What to look for, e.g. "Kubernetes migration project".
        """
        try:
            chunks = await rag.search_resume_chunks(
                qdrant, embeddings, settings, conversation_id, query
            )
        except Exception as exc:
            logger.exception("search_resume failed for %s", conversation_id)
            return _TOOL_FAILED.format(exc=exc)
        if not chunks:
            return "No matching section found in the resume."
        return "\n---\n".join(chunks)

    @tool
    async def complete_milestone(milestone_number: int, notes: str) -> str:
        """Mark one interview milestone as covered.

        Args:
            milestone_number: The milestone's number (1, 2, ...) as shown in
                your instructions and the live status message.
            notes: One line on what the candidate showed for this milestone.
        """
        try:
            async with sessionmaker() as session:
                # Resolve the spoken-friendly 1-based number to the row;
                # positions are 0-based. Conversation-scoped by construction.
                milestones = await db.get_milestones(session, conversation_id)
                target = next(
                    (m for m in milestones if m.position == milestone_number - 1),
                    None,
                )
                if target is None:
                    return f"Unknown milestone number: {milestone_number}."
                await db.complete_milestone(session, target.id, notes)
                remaining = [
                    m.title
                    for m in milestones
                    if not m.completed and m.id != target.id
                ]
        except Exception as exc:
            logger.exception("complete_milestone failed for %s", conversation_id)
            return _TOOL_FAILED.format(exc=exc)
        if remaining:
            return f"Milestone marked complete. Still pending: {', '.join(remaining)}."
        return (
            "Milestone marked complete. All milestones are done — call "
            "end_interview now."
        )

    @tool
    async def end_interview(reason: str) -> str:
        """End the interview. Call when all milestones are complete, or when
        wrapping up with nothing left to ask.

        Args:
            reason: One line on why the interview is ending.
        """
        end_event.set()
        # The graph routes to END after this tool, so the model never reads
        # this — kept truthful for standalone runs and logs.
        return "The interview is ending. The farewell is handled outside this graph."

    tools = [search_resume, complete_milestone, end_interview]

    llm = build_chat_model(
        settings,
        model=settings.interviewer_model,
        reasoning_effort=settings.interviewer_reasoning_effort,
        stream_usage=True,
    ).bind_tools(tools)

    system_message = SystemMessage(content=system_prompt)

    async def milestone_status(state: MessagesState) -> dict:
        """Append a fresh DONE/PENDING snapshot for this turn.

        Runs once per turn: the tools→chat inner loop re-enters chat
        directly, so the DB is queried once per user turn, not once per
        ReAct iteration."""
        try:
            async with sessionmaker() as session:
                milestones = await db.get_milestones(session, conversation_id)
        except Exception:
            # Never kill the turn over a status refresh; the static prompt
            # still describes the milestones.
            logger.exception("milestone status refresh failed for %s", conversation_id)
            return {"messages": []}
        return {"messages": [SystemMessage(content=build_milestone_status(milestones))]}

    builder = StateGraph(MessagesState)
    builder.add_node("milestone_status", milestone_status)
    builder.add_node("chat", make_streaming_chat_node(llm, system_message, on_usage=usage_sink))
    builder.add_node("tools", ToolNode(tools))
    builder.add_edge(START, "milestone_status")
    builder.add_edge("milestone_status", "chat")
    builder.add_conditional_edges("chat", tools_condition)
    builder.add_conditional_edges("tools", _route_after_tools)
    return builder.compile()
