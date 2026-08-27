"""Interview evaluator: one structured-output call over the full transcript.

Like the planner, it gets the FULL resume + job offer + plan + transcript —
quality matters, latency does not.
"""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import HumanMessage, SystemMessage

from interview_agent.config import Settings
from interview_agent.interview.models import EvaluationResult, Seniority
from interview_agent.llm import build_chat_model
from interview_agent.prompts import build_evaluator_prompt


def _format_transcript(messages: list[tuple[str, str]]) -> str:
    labels = {"user": "Candidate", "assistant": "Interviewer"}
    return "\n".join(f"{labels.get(role, role)}: {text}" for role, text in messages)


def _format_milestones(milestones: list[dict[str, Any]]) -> str:
    """Each milestone carries the bar set for it at planning time, so the
    evaluator judges against a written criterion instead of re-deriving how
    deep the topic "should" go. Legacy rows have no bar; the line just omits it.
    """
    return "\n".join(
        f"- [{'x' if m['completed'] else ' '}] {m['title']}: {m['description']}"
        + (f" (passes when: {m['expected_evidence']})" if m.get("expected_evidence") else "")
        + (f" (notes: {m['notes']})" if m.get("notes") else "")
        for m in milestones
    )


async def run_evaluator(
    settings: Settings,
    resume_markdown: str,
    job_offer: str,
    plan: dict[str, Any],
    milestones: list[dict[str, Any]],
    transcript: list[tuple[str, str]],
    ended_reason: str,
    # The level pinned at creation. Never re-inferred here: re-inferring is
    # what let an advanced-looking stack drag the bar up to senior.
    seniority: Seniority | str | None = None,
    custom_instructions: str | None = None,
    usage_callback: UsageMetadataCallbackHandler | None = None,
) -> EvaluationResult:
    llm = (
        build_chat_model(
            settings,
            model=settings.evaluator_model,
            reasoning_effort=settings.evaluator_reasoning_effort,
        )
        .with_structured_output(EvaluationResult, method="json_schema")
        # On top of the client's transport retries: re-run the whole call if
        # the structured output fails to parse/validate.
        .with_retry(stop_after_attempt=3)
    )

    content = (
        f"# Job offer\n\n{job_offer}\n\n"
        f"# Candidate resume (markdown)\n\n{resume_markdown}\n\n"
        f"# Interview plan\n\nSummary: {plan.get('summary', '')}\n"
        f"Focus areas: {', '.join(plan.get('focus_areas', []))}\n\n"
        f"# Milestones (checked = covered)\n\n{_format_milestones(milestones)}\n\n"
        f"# How the interview ended\n\n{ended_reason}\n\n"
        f"# Transcript\n\n{_format_transcript(transcript)}"
    )
    if custom_instructions:
        content += f"\n\n# Candidate's custom instructions\n\n{custom_instructions}"

    # Explicit callback, same rationale as the planner: no context-manager
    # ContextVar leak, and per-retry-attempt accumulation is real spend.
    config = {"callbacks": [usage_callback]} if usage_callback else None
    result = await llm.ainvoke(
        [
            SystemMessage(content=build_evaluator_prompt(seniority)),
            HumanMessage(content=content),
        ],
        config=config,
    )
    if not isinstance(result, EvaluationResult):
        raise TypeError(
            f"Evaluator returned {type(result).__name__}, expected EvaluationResult"
        )
    return result
