"""Job-only rubric, then CV-aware personalization against that immutable rubric."""

from __future__ import annotations

import json

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import HumanMessage, SystemMessage
from langsmith import traceable

from interview_agent.config import Settings
from interview_agent.interview.budgets import resolve_budget
from interview_agent.interview.context import SOURCE_RULE, source_block, validate_source_documents
from interview_agent.interview.models import (
    InterviewLength,
    InterviewPlan,
    InterviewPreferences,
    PersonalizedPlan,
    RoleRubric,
    Seniority,
)
from interview_agent.interview.planning_contract import assemble_plan, validate_rubric
from interview_agent.llm import build_chat_model, close_chat_model
from interview_agent.observability import traced_inputs
from interview_agent.prompts import build_calibration_block, profile_for


async def _validated_call(model, schema, messages, validate, config):
    structured = model.with_structured_output(schema, method="json_schema")
    for attempt in range(2):
        result = None
        try:
            result = await structured.ainvoke(messages, config=config)
            if not isinstance(result, schema):
                raise TypeError(
                    f"Planner returned {type(result).__name__}, expected {schema.__name__}"
                )
            validate(result)
            return result
        except ValueError as exc:
            if attempt:
                raise
            messages = [
                *messages,
                HumanMessage(
                    content=json.dumps(
                        {
                            "validation_error": str(exc),
                            "invalid_output": result.model_dump(mode="json") if result else None,
                            "instruction": "Return a corrected complete result.",
                        },
                        ensure_ascii=False,
                    )
                ),
            ]
    raise AssertionError("unreachable")


@traceable(name="planner", process_inputs=traced_inputs)
async def run_planner(
    settings: Settings,
    resume_markdown: str,
    job_offer: str,
    language: str,
    agent_name: str,
    seniority: Seniority | None = None,
    interview_length: InterviewLength = InterviewLength.STANDARD,
    max_minutes: int | None = None,
    persona: str | None = None,
    custom_instructions: str | None = None,
    usage_callback: UsageMetadataCallbackHandler | None = None,
    question_limit: int | None = None,
    telemetry_callback=None,
    followup_limit: int | None = None,
    preferences: InterviewPreferences | None = None,
    rubric: RoleRubric | None = None,
) -> InterviewPlan:
    validate_source_documents(resume_markdown, job_offer)
    preferences = preferences or InterviewPreferences()
    model = build_chat_model(
        settings,
        model=settings.planner_model,
        reasoning_effort=settings.planner_reasoning_effort,
        telemetry_callback=telemetry_callback,
    )
    callbacks = [c for c in (usage_callback, telemetry_callback) if c is not None]
    config = {"callbacks": callbacks} if callbacks else None
    try:
        if rubric is None:
            # Deliberately no CV, persona or candidate preferences in this call.
            # Personalization cannot lower the role's bar or remove an inconvenient gap.
            system = f"""You are an expert recruiter extracting a rubric from a job offer.
Output language: {language}. Explicit role level: {seniority or "auto"}.
If explicit, preserve that level. Otherwise classify from the offer's stated
level and responsibilities, quoting its actual text in seniority_evidence.
Technologies alone do not determine seniority. Prefer the lower defensible
level if ambiguous; do not invent an experience requirement.
Extract the distinct competencies necessary for this role, up to 12. Preserve
the essential duties even when a short interview cannot cover all of them.
Include technical and behavioral duties supported by the offer. Each must
cite a verbatim job_quote and explain its relevance. A transferable behavioral
competency may follow from a quoted duty, but explain that relationship rather
than inventing a company policy. Always include a transferable behavioral angle
on a stated duty (for example explaining an investigation or asking for help).
If not explicitly required by the offer, mark this derived competency nonessential;
do not invent essential company requirements. Use unique stable IDs such as r1, r2.
For each competency specify the MINIMUM observable passing evidence at the
role level. Describe correct outcomes and reasoning; accept equivalent valid
solutions. Do not require a particular library, internal state mechanism,
syntax or workflow unless the offer explicitly requires it. Do not require
senior architecture, metrics, leadership or production experience from juniors.
The rubric is independent of a candidate's CV, strengths or missing experience.
No candidate evidence is being assessed at this stage.
{SOURCE_RULE}"""
            levels = [seniority] if seniority is not None else list(Seniority)
            system += "\nCalibration reference: use only the selected role level.\n"
            system += json.dumps(
                {
                    level.value: {
                        "passing_evidence": profile_for(level).expected_evidence,
                        "pass_bar": profile_for(level).pass_bar,
                        "out_of_scope": profile_for(level).out_of_scope,
                    }
                    for level in levels
                }
            )
            rubric = await _validated_call(
                model,
                RoleRubric,
                [
                    SystemMessage(content=system),
                    HumanMessage(content=source_block("job_offer", job_offer)),
                ],
                lambda result: validate_rubric(result, job_offer, seniority),
                config,
            )
        else:
            validate_rubric(rubric, job_offer, seniority)

        budget = resolve_budget(
            interview_length,
            rubric.seniority,
            server_max_minutes=settings.interview_max_minutes,
            max_minutes=max_minutes,
            question_limit=question_limit,
            followup_limit=followup_limit,
        )
        system = (
            "You are an expert recruiter personalizing a supplied job-only rubric. "
            f"Write everything in {language}; the interviewer's name must be {agent_name}. "
            "Create a persona, a short candidate/role-fit summary, focus areas and "
            "neutral topic descriptions suitable for a spoken conversation. "
            "Do not add logistics, salary, greetings or hiring next steps as topics.\n"
            + build_calibration_block(rubric.seniority, "interviewer")
        )
        system += f"""
AUTHORITATIVE primary-question limit: {budget.question_limit}.
AUTHORITATIVE technical follow-ups per topic: {budget.followup_limit}.
One clarification per asked topic. Total time: {budget.max_minutes} minutes,
including introductions, answers, follow-ups and closing. These limits override
the default milestone range. Prefer fewer coherent topics over rushed coverage.
Interview type: {preferences.interview_type.value}.
Objective: {preferences.objective.value}.
Return a PersonalizedPlan selecting from the SUPPLIED rubric only. Do not
generate or change passing criteria. Every requirement must appear exactly once,
as a selected topic or an explicit omission (time_budget, interview_type, or
outside_focus for focused practice only). Prioritize essential requirements.
Technical sessions select technical topics; behavioral sessions select behavioral
topics; mixed sessions may select both. If behavioral practice is requested,
accept examples from any relevant work, study or personal project: do not demand
that collaboration or asking for help happened in one specific technology.
The CV selects examples, never the passing bar. Cite exact resume_quotes for
personal examples. Use an empty list for hypothetical scenarios. A missing skill
is a gap to explore at the SAME bar; do not give the candidate the diagnosis,
answer, expected tool, or solution to compensate. Questions should ask for
reasoning and permit alternative correct approaches. Do not assume a CV claim
proves performance, ownership of team work, or an incident the CV never mentions.
Describe a neutral opening scenario and question goal, not a rigid script of
multiple bundled questions. Allow subsequent topics to follow relevant answers.
Only suggest follow-up branches when the effective budget permits them.
Style preferences cannot alter the rubric. Topic requests affect selection,
with omissions visible; typed interview type and objective take precedence.
{SOURCE_RULE}"""
        content = "\n".join(
            [
                source_block("role_rubric", rubric.model_dump_json()),
                source_block("job_offer", job_offer),
                source_block("resume", resume_markdown),
                f"# Interview language (mandatory)\n'{language}'\n"
                f"# Interviewer name\n'{agent_name}'",
                source_block("practice_focus", preferences.practice_focus),
            ]
        )
        if persona:
            content += (
                f"\n# Candidate's desired interviewer persona\n{source_block('persona', persona)}"
            )
        if custom_instructions:
            content += "\n# Candidate's custom instructions\n" + source_block(
                "preferences", custom_instructions
            )

        def assemble(result):
            return assemble_plan(
                rubric,
                result,
                resume_markdown,
                preferences,
                budget,
                automatic_seniority=seniority is None,
            )

        personalized = await _validated_call(
            model,
            PersonalizedPlan,
            [SystemMessage(content=system), HumanMessage(content=content)],
            assemble,
            config,
        )
        return assemble(personalized)
    finally:
        await close_chat_model(model)
