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

from langchain_core.messages import SystemMessage
from langchain_core.tools import tool
from langchain_openai import OpenAIEmbeddings
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from interview_agent.config import Settings
from interview_agent.interview import db, rag
from interview_agent.llm import build_chat_model, make_streaming_chat_node

logger = logging.getLogger("interview_agent.interviewer")

# Tools must never raise: an exception here kills the turn mid-conversation.
# Returning the error as text lets the LLM acknowledge it and move on.
_TOOL_FAILED = "Tool failed: {exc}. Continue the interview without it."


def build_interviewer_graph(
    settings: Settings,
    conversation_id: uuid.UUID,
    sessionmaker: async_sessionmaker[AsyncSession],
    qdrant: AsyncQdrantClient,
    embeddings: OpenAIEmbeddings,
    end_event: asyncio.Event,
    system_prompt: str,
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
    async def complete_milestone(milestone_id: str, notes: str) -> str:
        """Mark one interview milestone as covered.

        Args:
            milestone_id: The id of the milestone, as listed in your instructions.
            notes: One line on what the candidate showed for this milestone.
        """
        try:
            mid = uuid.UUID(milestone_id)
        except ValueError:
            return f"Invalid milestone id: {milestone_id}"
        try:
            async with sessionmaker() as session:
                milestone = await db.complete_milestone(session, mid, notes)
                if milestone is None or milestone.conversation_id != conversation_id:
                    return f"Unknown milestone id: {milestone_id}"
                remaining = [
                    m.title
                    for m in await db.get_milestones(session, conversation_id)
                    if not m.completed
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
        return "The interview is ending. Say a brief, warm goodbye now."

    tools = [search_resume, complete_milestone, end_interview]

    llm = build_chat_model(
        settings,
        model=settings.interviewer_model,
        reasoning_effort=settings.interviewer_reasoning_effort,
        stream_usage=True,
    ).bind_tools(tools)

    system_message = SystemMessage(content=system_prompt)

    builder = StateGraph(MessagesState)
    builder.add_node("chat", make_streaming_chat_node(llm, system_message))
    builder.add_node("tools", ToolNode(tools))
    builder.add_edge(START, "chat")
    builder.add_conditional_edges("chat", tools_condition)
    builder.add_edge("tools", "chat")
    return builder.compile()
