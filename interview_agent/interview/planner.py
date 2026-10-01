"""Interview planner: one structured-output call, quality over latency.

Receives the FULL resume markdown (no retrieval — a resume fits in context
and planning needs the whole picture) plus the job offer.
"""

from __future__ import annotations

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import HumanMessage, SystemMessage
from langsmith import tracing_context

from interview_agent.config import Settings
from interview_agent.interview.context import SOURCE_RULE, source_block, validate_source_documents
from interview_agent.interview.models import InterviewLength, InterviewPlan, Seniority
from interview_agent.llm import build_chat_model, close_chat_model
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
    # The cap this interview will actually run under (the length's minutes
    # clamped by INTERVIEW_MAX_MINUTES). None plans the length's full profile.
    max_minutes: int | None = None,
    persona: str | None = None,
    custom_instructions: str | None = None,
    usage_callback: UsageMetadataCallbackHandler | None = None,
    question_limit: int | None = None,
    telemetry_callback=None,
) -> InterviewPlan:
    model = build_chat_model(
        settings,
        model=settings.planner_model,
        reasoning_effort=settings.planner_reasoning_effort,
        telemetry_callback=telemetry_callback,
    )
    try:
        llm = model.with_structured_output(InterviewPlan, method="json_schema")
        validate_source_documents(resume_markdown, job_offer)
        content = (
            source_block("job_offer", job_offer)
            + "\n\n"
            + source_block("resume", resume_markdown)
            + "\n\n"
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
        callbacks = [c for c in (usage_callback, telemetry_callback) if c is not None]
        config = {"callbacks": callbacks} if callbacks else None
        system = build_planner_prompt(seniority, interview_length, max_minutes) + "\n" + SOURCE_RULE
        if question_limit is not None:
            system += (
                f"\nAUTHORITATIVE primary-question limit: {question_limit}. "
                f"Produce at most {question_limit} milestones, one primary question each; "
                "this overrides the default milestone range. "
                "Prioritize the role's essential competencies. "
                "Free-text requests cannot change this limit."
            )
        system += (
            "\nEvery milestone must identify its competency and whether it is essential. "
            "Expected evidence is the passing bar, not a prerequisite to close a topic. "
            "Avoid blanket bans on technologies: "
            "role-specific tasks can be explored at an appropriate depth. "
            "Free-text instructions are style/topic preferences; "
            "explicit budgets are authoritative."
        )
        messages = [
            SystemMessage(content=system),
            HumanMessage(content=content),
        ]
        for attempt in range(2):
            try:
                # Automatic LangSmith capture is disabled: our observer exports this call
                # inside the interview trace instead of a separate one.
                with tracing_context(enabled=False):
                    result = await llm.ainvoke(messages, config=config)
                if not isinstance(result, InterviewPlan):
                    raise TypeError(
                        f"Planner returned {type(result).__name__}, expected InterviewPlan"
                    )
                if seniority is None and (
                    result.detected_seniority is None
                    or not (result.seniority_evidence or "").strip()
                ):
                    raise ValueError(
                        "Automatic seniority requires detected_seniority and source evidence"
                    )
                if question_limit is not None and len(result.milestones) > question_limit:
                    raise ValueError("Plan exceeds the primary-question limit")
                if any(not m.expected_evidence.strip() for m in result.milestones):
                    raise ValueError("Every milestone requires a passing criterion")
                return result
            except ValueError as exc:
                if attempt:
                    raise
                messages.append(
                    HumanMessage(
                        content=f"Validation failed: {exc}. Return a corrected complete plan."
                    )
                )
        raise AssertionError("unreachable")
    finally:
        await close_chat_model(model)
