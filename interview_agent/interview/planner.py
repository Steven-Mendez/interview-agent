"""Interview planner: one structured-output call, quality over latency.

Receives the FULL resume markdown (no retrieval — a resume fits in context
and planning needs the whole picture) plus the job offer.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from interview_agent.config import Settings
from interview_agent.interview.models import InterviewPlan
from interview_agent.llm import build_chat_model
from interview_agent.prompts import PLANNER_SYSTEM_PROMPT


async def run_planner(
    settings: Settings,
    resume_markdown: str,
    job_offer: str,
    persona: str | None = None,
    custom_instructions: str | None = None,
) -> InterviewPlan:
    llm = (
        build_chat_model(
            settings,
            model=settings.planner_model,
            reasoning_effort=settings.planner_reasoning_effort,
        )
        .with_structured_output(InterviewPlan, method="json_schema")
        # On top of the client's transport retries: re-run the whole call if
        # the structured output fails to parse/validate.
        .with_retry(stop_after_attempt=3)
    )

    content = (
        f"# Job offer\n\n{job_offer}\n\n"
        f"# Candidate resume (markdown)\n\n{resume_markdown}"
    )
    if persona:
        content += f"\n\n# Candidate's desired interviewer persona\n\n{persona}"
    if custom_instructions:
        content += f"\n\n# Candidate's custom instructions\n\n{custom_instructions}"

    result = await llm.ainvoke(
        [SystemMessage(content=PLANNER_SYSTEM_PROMPT), HumanMessage(content=content)]
    )
    if not isinstance(result, InterviewPlan):
        raise TypeError(f"Planner returned {type(result).__name__}, expected InterviewPlan")
    return result
