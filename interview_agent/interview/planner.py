"""Interview planner: one structured-output call, quality over latency.

Receives the FULL resume markdown (no retrieval — a resume fits in context
and planning needs the whole picture) plus the job offer.
"""

from __future__ import annotations

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import HumanMessage, SystemMessage

from interview_agent.config import Settings
from interview_agent.interview.models import InterviewLength, InterviewPlan, Seniority
from interview_agent.llm import build_chat_model
from interview_agent.prompts import build_planner_prompt


async def run_planner(
    settings: Settings,
    resume_markdown: str,
    job_offer: str,
    language: str,
    agent_name: str,
    # None means "classify it yourself": the ONE explicit classification in the
    # whole pipeline. Anything else is authoritative and the planner is told so.
    seniority: Seniority | None = None,
    interview_length: InterviewLength = InterviewLength.STANDARD,
    persona: str | None = None,
    custom_instructions: str | None = None,
    usage_callback: UsageMetadataCallbackHandler | None = None,
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
        f"# Candidate resume (markdown)\n\n{resume_markdown}\n\n"
        f"# Interview language (mandatory, ISO 639-1)\n\n'{language}'\n\n"
        f"# Interviewer's name (mandatory)\n\n{agent_name}"
    )
    if persona:
        content += f"\n\n# Candidate's desired interviewer persona\n\n{persona}"
    if custom_instructions:
        content += f"\n\n# Candidate's custom instructions\n\n{custom_instructions}"

    # Explicit callback (not the get_usage_metadata_callback context manager,
    # which registers a fresh ContextVar per call and never unregisters it —
    # a slow leak in a long-running server). Fires per retry attempt: each
    # attempt is real spend.
    config = {"callbacks": [usage_callback]} if usage_callback else None
    result = await llm.ainvoke(
        [
            SystemMessage(content=build_planner_prompt(seniority, interview_length)),
            HumanMessage(content=content),
        ],
        config=config,
    )
    if not isinstance(result, InterviewPlan):
        raise TypeError(f"Planner returned {type(result).__name__}, expected InterviewPlan")
    return result
