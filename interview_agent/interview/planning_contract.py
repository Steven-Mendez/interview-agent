"""Ground the rubric in the offer and personalize without changing its passing bar."""

from interview_agent.interview.models import (
    InterviewBudget,
    InterviewObjective,
    InterviewPlan,
    InterviewPreferences,
    InterviewType,
    MilestoneSpec,
    PersonalizedPlan,
    RoleRubric,
    Seniority,
)


def validate_rubric(rubric: RoleRubric, job_offer: str, seniority: Seniority | None) -> None:
    if seniority is not None and rubric.seniority != seniority:
        raise ValueError("The rubric must use the explicitly selected role level")
    if seniority is None and (
        not rubric.seniority_evidence.strip() or rubric.seniority_evidence not in job_offer
    ):
        raise ValueError("Automatic seniority must cite the job offer verbatim")
    ids = [r.id for r in rubric.requirements]
    if len(set(ids)) != len(ids):
        raise ValueError("Role requirements must have unique IDs")
    for requirement in rubric.requirements:
        if not requirement.job_quote.strip() or requirement.job_quote not in job_offer:
            raise ValueError("Each requirement must cite the job offer verbatim")
        if not requirement.expected_evidence.strip() or not requirement.competency.strip():
            raise ValueError("Each requirement needs a competency and passing evidence")


def assemble_plan(
    rubric: RoleRubric,
    personalized: PersonalizedPlan,
    resume: str,
    preferences: InterviewPreferences,
    budget: InterviewBudget,
    *,
    automatic_seniority: bool,
) -> InterviewPlan:
    requirements = {r.id: r for r in rubric.requirements}
    selected = [t.requirement_id for t in personalized.topics]
    omitted = [o.requirement_id for o in personalized.omitted_requirements]
    if len(selected) > budget.question_limit:
        raise ValueError("Plan exceeds the primary-question limit")
    if len(set(selected + omitted)) != len(selected + omitted) or set(selected + omitted) != set(
        requirements
    ):
        raise ValueError("Every supplied requirement must be selected or explicitly omitted once")
    for omission in personalized.omitted_requirements:
        requirement = requirements[omission.requirement_id]
        different_type = (
            preferences.interview_type != InterviewType.MIXED
            and requirement.kind != preferences.interview_type.value
        )
        if omission.reason == "interview_type" and not different_type:
            raise ValueError("An in-scope requirement cannot be omitted for interview type")
        if (
            omission.reason == "outside_focus"
            and preferences.objective != InterviewObjective.PRACTICE
        ):
            raise ValueError("Only focused practice can exclude requirements for outside_focus")
    milestones = []
    for topic in personalized.topics:
        requirement = requirements[topic.requirement_id]
        if (
            preferences.interview_type != InterviewType.MIXED
            and requirement.kind != preferences.interview_type.value
        ):
            raise ValueError("The topic does not match the selected interview type")
        if not topic.title.strip() or not topic.description.strip():
            raise ValueError("Each topic needs a title and a neutral question goal")
        if any(not quote.strip() or quote not in resume for quote in topic.resume_quotes):
            raise ValueError("Personalization must cite the resume verbatim")
        milestones.append(
            MilestoneSpec(
                title=topic.title,
                description=topic.description,
                expected_evidence=requirement.expected_evidence,
                essential=requirement.essential,
                competency=requirement.competency,
                requirement_id=requirement.id,
                resume_quotes=topic.resume_quotes,
            )
        )
    return InterviewPlan(
        persona=personalized.persona,
        summary=personalized.summary,
        focus_areas=personalized.focus_areas,
        milestones=milestones,
        detected_seniority=rubric.seniority if automatic_seniority else None,
        seniority_evidence=rubric.seniority_evidence if automatic_seniority else None,
        requirements=rubric.requirements,
        omitted_requirements=personalized.omitted_requirements,
        preferences=preferences,
        budget=budget.model_copy(update={"question_limit": len(milestones)}),
    )


def evaluation_scope(plan: dict | None) -> tuple[str, list[str]]:
    plan = plan or {}
    requirements = {r["id"]: r for r in plan.get("requirements", [])}
    uncovered = [
        requirements[item["requirement_id"]]["competency"]
        for item in plan.get("omitted_requirements", [])
        if item["requirement_id"] in requirements
        and requirements[item["requirement_id"]].get("essential", True)
    ]
    practice = plan.get("preferences", {}).get("objective") == "practice"
    return "practice" if practice else "limited" if uncovered else "interview", uncovered
