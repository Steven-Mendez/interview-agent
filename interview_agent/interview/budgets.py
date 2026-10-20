"""One effective budget for planning, API creation and text interviews."""

from interview_agent.interview.models import InterviewBudget, InterviewLength, Seniority
from interview_agent.prompts import fit_length, followup_budget, length_for


def resolve_budget(
    length: InterviewLength | str = InterviewLength.STANDARD,
    seniority: Seniority | str | None = None,
    *,
    server_max_minutes: int = 25,
    max_minutes: int | None = None,
    question_limit: int | None = None,
    followup_limit: int | None = None,
) -> InterviewBudget:
    minutes = min(max_minutes or length_for(length)["minutes"], server_max_minutes)
    effective_length = fit_length(length, minutes)
    # Before classification, retain the upper bound; resolve again with the pinned level.
    cap = followup_budget(seniority or Seniority.LEAD, effective_length)
    return InterviewBudget(
        max_minutes=minutes,
        question_limit=question_limit or length_for(effective_length)["max_milestones"],
        followup_limit=min(cap, cap if followup_limit is None else followup_limit),
    )
