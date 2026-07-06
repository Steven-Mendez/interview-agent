"""Pure-logic tests for the interviewer graph's routing and prompt helpers —
no DB, no LLM."""

import uuid

from langchain_core.messages import AIMessage, ToolMessage

from interview_agent.interview.db import Milestone
from interview_agent.interview.interviewer_graph import _route_after_tools
from interview_agent.llm import summarize_usage
from interview_agent.prompts import build_milestone_status


def _milestone(position: int, title: str, completed: bool) -> Milestone:
    return Milestone(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        position=position,
        title=title,
        description="Probe it.",
        completed=completed,
    )


def _tool_message(name: str) -> ToolMessage:
    return ToolMessage(content="ok", name=name, tool_call_id=f"call-{name}")


def test_milestone_status_numbers_and_markers():
    out = build_milestone_status(
        [_milestone(0, "K8s", True), _milestone(1, "SQL", False)]
    )
    assert "1. [DONE] K8s" in out
    assert "2. [PENDING] SQL" in out
    assert "end_interview" in out  # the all-done instruction is present


def test_route_after_tools_ends_on_end_interview():
    state = {"messages": [AIMessage(content=""), _tool_message("end_interview")]}
    assert _route_after_tools(state) == "__end__"


def test_route_after_tools_scans_parallel_tool_batch():
    # complete_milestone + end_interview in one model turn: both ToolMessages
    # trail the AIMessage, end_interview not last — the run must still end.
    state = {
        "messages": [
            AIMessage(content=""),
            _tool_message("end_interview"),
            _tool_message("complete_milestone"),
        ]
    }
    assert _route_after_tools(state) == "__end__"


def test_route_after_tools_continues_otherwise():
    state = {"messages": [AIMessage(content=""), _tool_message("search_resume")]}
    assert _route_after_tools(state) == "chat"
    # Not a tool result at the tail (e.g. mid-graph inspection): keep chatting.
    assert _route_after_tools({"messages": [AIMessage(content="hi")]}) == "chat"


def test_summarize_usage_sums_across_models():
    out = summarize_usage(
        {
            "gpt-5.5": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
            "gpt-5.4-mini": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        }
    )
    assert out == {"input_tokens": 110, "output_tokens": 25, "total_tokens": 135}


def test_json_prefix_filter_drops_narrated_tool_args():
    from interview_agent.llm import make_json_prefix_filter

    spoken: list[str] = []
    write = make_json_prefix_filter(spoken.append)
    # Streamed in chunks, as the LLM emits it: JSON args first, speech after.
    for chunk in ['{"milestone_number":5,', '"notes":"foo {bar}"}', "Tell me", " more."]:
        write(chunk)
    assert "".join(spoken) == "Tell me more."


def test_json_prefix_filter_passes_normal_speech_through():
    from interview_agent.llm import make_json_prefix_filter

    spoken: list[str] = []
    write = make_json_prefix_filter(spoken.append)
    write("Hi, how are you?")
    write(" Tell me about your {important} project.")
    assert "".join(spoken) == "Hi, how are you? Tell me about your {important} project."


def test_json_prefix_filter_silences_json_only_turn():
    from interview_agent.llm import make_json_prefix_filter

    spoken: list[str] = []
    write = make_json_prefix_filter(spoken.append)
    write('{"milestone_number": 2, "notes": "ok"}')
    assert spoken == []


def test_summarize_usage_empty():
    assert summarize_usage({}) == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
