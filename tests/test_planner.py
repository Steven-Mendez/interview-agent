"""run_planner with the LLM mocked out: exercises prompt assembly and the
structured-output type check, not OpenAI."""

import pytest

from interview_agent.config import Settings
from interview_agent.interview import planner
from interview_agent.interview.models import (
    InterviewLength,
    InterviewPlan,
    MilestoneSpec,
    Seniority,
)


def _plan() -> InterviewPlan:
    return InterviewPlan(
        persona="Laura, engineering manager, warm but rigorous",
        summary="Solid backend candidate.",
        focus_areas=["Kubernetes", "SQL"],
        milestones=[
            MilestoneSpec(
                title=f"M{i}", description="Probe it.", expected_evidence="Names one index."
            )
            for i in range(4)
        ],
    )


class _FakeChain:
    """Stands in for build_chat_model(...): records the messages it gets and
    returns a canned result, mimicking the with_structured_output/with_retry
    chaining that run_planner performs."""

    def __init__(self, result):
        self.result = result
        self.messages = None

    def with_structured_output(self, schema, method=None):
        return self

    def with_retry(self, **kwargs):
        return self

    async def ainvoke(self, messages, config=None):
        self.messages = messages
        return self.result


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


async def test_run_planner_returns_plan_and_includes_optional_inputs(
    settings, monkeypatch
):
    fake = _FakeChain(_plan())
    monkeypatch.setattr(planner, "build_chat_model", lambda *a, **k: fake)

    result = await planner.run_planner(
        settings,
        resume_markdown="# Resume\nPython dev.",
        job_offer="Backend engineer at ACME.",
        language="es",
        agent_name="Alex",
        persona="a strict FAANG manager",
        custom_instructions="focus on system design",
    )

    assert isinstance(result, InterviewPlan)
    # System + human message, with every input woven into the human content.
    system, human = fake.messages
    assert "recruiter" in system.content
    for needle in (
        "Backend engineer at ACME.",
        "Python dev.",
        "'es'",
        "Alex",
        "a strict FAANG manager",
        "focus on system design",
    ):
        assert needle in human.content


async def test_run_planner_omits_optional_sections_when_absent(settings, monkeypatch):
    fake = _FakeChain(_plan())
    monkeypatch.setattr(planner, "build_chat_model", lambda *a, **k: fake)

    await planner.run_planner(
        settings, resume_markdown="cv", job_offer="offer", language="en", agent_name="Alex"
    )

    _, human = fake.messages
    assert "desired interviewer persona" not in human.content
    assert "custom instructions" not in human.content


async def test_run_planner_rejects_non_plan_output(settings, monkeypatch):
    fake = _FakeChain({"persona": "not a model"})
    monkeypatch.setattr(planner, "build_chat_model", lambda *a, **k: fake)

    with pytest.raises(TypeError, match="expected InterviewPlan"):
        await planner.run_planner(
            settings, resume_markdown="cv", job_offer="offer", language="en", agent_name="Alex"
        )


async def test_run_planner_pins_an_explicit_level(settings, monkeypatch):
    """Given a level, the planner is told it and must not classify one."""
    fake = _FakeChain(_plan())
    monkeypatch.setattr(planner, "build_chat_model", lambda *a, **k: fake)

    await planner.run_planner(
        settings,
        resume_markdown="# Resume",
        job_offer="Backend engineer, RAG and AWS.",
        language="en",
        agent_name="Alex",
        seniority=Seniority.JUNIOR,
        interview_length=InterviewLength.SHORT,
    )

    system = fake.messages[0].content
    assert "Junior (roughly 0-2 years of experience)" in system
    assert "CLASSIFY IT FIRST" not in system
    # The length axis drives milestone count and minutes, not the level.
    assert "between 3 and 4 milestones" in system
    assert "about\n8 minutes" in system


async def test_run_planner_asks_for_classification_when_level_is_auto(
    settings, monkeypatch
):
    fake = _FakeChain(_plan())
    monkeypatch.setattr(planner, "build_chat_model", lambda *a, **k: fake)

    await planner.run_planner(
        settings,
        resume_markdown="# Resume",
        job_offer="Backend engineer, RAG and AWS.",
        language="en",
        agent_name="Alex",
        seniority=None,
    )

    system = fake.messages[0].content
    assert "CLASSIFY IT FIRST" in system
    assert "Do NOT classify by the technologies mentioned" in system


async def test_run_planner_defaults_to_a_standard_length(settings, monkeypatch):
    fake = _FakeChain(_plan())
    monkeypatch.setattr(planner, "build_chat_model", lambda *a, **k: fake)

    await planner.run_planner(
        settings,
        resume_markdown="# Resume",
        job_offer="Backend engineer.",
        language="en",
        agent_name="Alex",
    )
    assert "between 4 and 6 milestones" in fake.messages[0].content
