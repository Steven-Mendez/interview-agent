"""run_planner with the LLM mocked out: exercises prompt assembly and the
structured-output type check, not OpenAI."""

import pytest

from interview_agent.config import Settings
from interview_agent.interview import planner
from interview_agent.interview.models import InterviewPlan, MilestoneSpec


def _plan() -> InterviewPlan:
    return InterviewPlan(
        persona="Laura, engineering manager, warm but rigorous",
        summary="Solid backend candidate.",
        focus_areas=["Kubernetes", "SQL"],
        milestones=[
            MilestoneSpec(title=f"M{i}", description="Probe it.") for i in range(4)
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
