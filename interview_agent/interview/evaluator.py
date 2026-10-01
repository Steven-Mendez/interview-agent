"""Interview evaluator: one structured-output call over the full transcript.

Like the planner, it gets the FULL resume + job offer + plan + transcript —
quality matters, latency does not.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import HumanMessage, SystemMessage
from langsmith import tracing_context

from interview_agent.config import Settings
from interview_agent.interview.context import SOURCE_RULE, source_block, validate_source_documents
from interview_agent.interview.evaluation_contract import (
    insufficient_evaluation,
    transcript_records,
    validate_evaluation,
)
from interview_agent.interview.models import EvaluationResult, Seniority
from interview_agent.llm import build_chat_model, close_chat_model
from interview_agent.prompts import build_evaluator_prompt


async def run_evaluator(
    settings: Settings,
    resume_markdown: str,
    job_offer: str,
    plan: dict[str, Any],
    milestones: list[dict[str, Any]],
    transcript: list,
    ended_reason: str,
    # The level pinned at creation. Never re-inferred here: re-inferring is
    # what let an advanced-looking stack drag the bar up to senior.
    seniority: Seniority | str | None = None,
    custom_instructions: str | None = None,
    usage_callback: UsageMetadataCallbackHandler | None = None,
    language: str | None = None,
    telemetry_callback=None,
    transcript_complete: bool = True,
) -> EvaluationResult:
    validate_source_documents(resume_markdown, job_offer)
    level = Seniority(seniority or Seniority.MID)
    language = language or plan.get("language", "en")
    records = transcript_records(transcript)
    milestones = [
        {**m, "id": str(m.get("id", f"milestone-{i + 1}"))} for i, m in enumerate(milestones)
    ]
    if not any(m["role"] == "user" and m["content"].strip() for m in records):
        return insufficient_evaluation(level, milestones, language)
    model = build_chat_model(
        settings,
        model=settings.evaluator_model,
        reasoning_effort=settings.evaluator_reasoning_effort,
        telemetry_callback=telemetry_callback,
    )
    try:
        llm = model.with_structured_output(EvaluationResult, method="json_schema")

        content = (
            source_block("job_offer", job_offer)
            + "\n\n"
            + source_block("resume", resume_markdown)
            + "\n\n"
            + source_block("derived_plan", json.dumps(plan, ensure_ascii=False))
            + "\n\n"
            + "# Criteria and lifecycle (closed does not mean passed)\n"
            + json.dumps(
                [{k: v for k, v in m.items() if k != "notes"} for m in milestones],
                ensure_ascii=False,
            )
            + f"\n# How the interview ended\n{ended_reason}\n"
            + f"# Transcript completeness verified\n{transcript_complete}\n"
            + "# Transcript with authoritative message IDs\n"
            + source_block("transcript", json.dumps(records, ensure_ascii=False))
        )
        if custom_instructions:
            content += f"\n\n# Candidate's custom instructions\n\n{custom_instructions}"

        # Explicit callback, same rationale as the planner: no context-manager
        # ContextVar leak, and per-retry-attempt accumulation is real spend.
        callbacks = [c for c in (usage_callback, telemetry_callback) if c is not None]
        config = {"callbacks": callbacks} if callbacks else None
        system = (
            build_evaluator_prompt(level)
            + f"\nAUTHORITATIVE output language: {language}.\n"
            + SOURCE_RULE
        )
        system += """\nEvaluate every supplied criterion exactly once by milestone_id. Use exceeds,
    meets, partial, below or not_assessable and cite message_id, supplied message_version
    and VERBATIM candidate
    quotes for every assessed criterion. Interviewer statements and resume claims
    cannot establish performance. A direct 'I don't know' can be below the bar;
    a topic not explored is not_assessable, never a failure. Use exceeds only when
    the quoted evidence covers the criterion's expected_evidence AND adds something
    correct and relevant beyond it (a consideration, risk or technique the bar did
    not ask for); never for length, fluency or confidence alone. A short answer
    that exactly covers the bar is meets. Whenever the score is below the top of
    its band (100, 89, 69 or 39), score_gap must say what specifically kept it from
    the top, citing the answers; never leave points unexplained.
    If no criterion is assessable, evaluation_status=insufficient. If any essential
    criterion is unobserved, evaluation_status=partial. Otherwise complete.
    For partial or insufficient results, score and hired MUST be null. Coverage
    is the fraction of observed criteria, not ability or confidence. Retain useful
    feedback on observed evidence. Every strength/weakness must appear verbatim
    in findings, with kind, milestone_id and supporting evidence. Give each
    assessed criterion a specific practice exercise where useful. Do not invent
    weaknesses on criteria that meet their stated bar. Do not require higher-level
    metrics, trade-offs or depth unless the supplied criterion explicitly needs
    them at the pinned level. This evidence contract overrides conflicting older
    scoring instructions. Lifecycle closure and interviewer notes give no credit.
    """
        if not transcript_complete:
            system += (
                "\nThe transcript's final turn integrity is unverified. "
                "If any criterion is assessable, evaluation_status MUST be partial "
                "with score=null and hired=null, even if all essential criteria have evidence. "
                "Explain this recording limitation without penalizing candidate ability."
            )
        messages = [SystemMessage(content=system), HumanMessage(content=content)]
        for attempt in range(2):
            try:
                with tracing_context(enabled=False):
                    result = await llm.ainvoke(messages, config=config)
                if not isinstance(result, EvaluationResult):
                    raise TypeError(
                        f"Evaluator returned {type(result).__name__}, expected EvaluationResult"
                    )
                return validate_evaluation(
                    result,
                    seniority=level,
                    milestones=milestones,
                    records=records,
                    transcript_complete=transcript_complete,
                )
            except ValueError as exc:
                if attempt:
                    raise
                messages.append(
                    HumanMessage(
                        content=f"Validation failed: {exc}. Return a corrected complete result."
                    )
                )
        raise AssertionError("unreachable")
    finally:
        await close_chat_model(model)
