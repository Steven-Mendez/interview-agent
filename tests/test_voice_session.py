"""LiveKit/LangGraph regression checks without provider calls or real audio."""

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langchain_core.messages import AIMessageChunk
from livekit.agents import Agent

from interview_agent import agent
from interview_agent.config import settings
from interview_agent.interview import interviewer_graph
from interview_agent.interview.db import Milestone


class LocalToolModel:
    def bind_tools(self, tools):
        return self

    async def astream(self, messages):
        yield AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": "complete_milestone",
                    "args": json.dumps({"milestone_number": 1, "notes": "Explained SQL"}),
                    "id": "milestone",
                    "index": 0,
                },
                {"name": "end_interview", "args": '{"reason":"Done"}', "id": "end", "index": 1},
            ],
        )


class LocalSessionMaker:
    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


async def test_configured_session_disables_speculation_and_keeps_tools_working(monkeypatch):
    end_event = asyncio.Event()
    milestone = Milestone(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        position=0,
        title="SQL",
        description="Explain",
        completed=False,
    )
    write = AsyncMock()
    monkeypatch.setattr(interviewer_graph, "build_chat_model", lambda *a, **k: LocalToolModel())
    monkeypatch.setattr(interviewer_graph.db, "get_milestones", AsyncMock(return_value=[milestone]))
    monkeypatch.setattr(interviewer_graph.db, "complete_milestone", write)
    graph = interviewer_graph.build_interviewer_graph(
        settings, milestone.conversation_id, LocalSessionMaker(), end_event, "Interview"
    )
    # Construct the actual AgentSession and LLMAdapter, replacing only audio
    # providers. No connection or model credentials are needed for this test.
    ctx = SimpleNamespace(proc=SimpleNamespace(userdata={"vad": None}))
    with monkeypatch.context() as providers:
        providers.setattr(agent.inference, "STT", lambda **kwargs: None)
        providers.setattr(agent.inference, "TTS", lambda **kwargs: None)
        providers.setattr(agent.inference, "TurnDetector", lambda: None)
        session = agent._build_session(ctx, graph, None)
    assert session.options.preemptive_generation["enabled"] is False
    assert not end_event.is_set()
    write.assert_not_called()

    try:
        await session.start(Agent(instructions="Interview"), record=False)
        # A committed text input follows the normal generation path. Tool JSON
        # never needs to become spoken output for the internal tools to run.
        await asyncio.wait_for(session.run(user_input="I explained SQL"), timeout=5)
        write.assert_awaited_once()
        assert end_event.is_set()
    finally:
        await session.aclose()
