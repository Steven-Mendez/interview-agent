"""Pydantic schemas for the planner and evaluator structured outputs."""

from __future__ import annotations

from pydantic import BaseModel, Field


class MilestoneSpec(BaseModel):
    """One interview milestone the interviewer must cover."""

    title: str = Field(description="Short milestone name, e.g. 'Kubernetes experience'.")
    description: str = Field(
        description="What the interviewer should probe and what counts as covered."
    )


class InterviewPlan(BaseModel):
    """Planner output: how the interview should be conducted."""

    persona: str = Field(
        description=(
            "The interviewer's persona: name, role and interviewing style, "
            "e.g. 'Laura, engineering manager, warm but rigorous'."
        )
    )
    language: str = Field(
        description=(
            "ISO 639-1 code of the language the interview must be conducted "
            "in, inferred from the job offer (e.g. 'es', 'en')."
        )
    )
    summary: str = Field(
        description="2-3 sentence summary of the candidate/role fit to guide the interview."
    )
    focus_areas: list[str] = Field(
        description="Key areas to emphasize given gaps or strengths in the resume."
    )
    milestones: list[MilestoneSpec] = Field(
        description="4 to 6 milestones, ordered as the interview should flow.",
        min_length=4,
        max_length=6,
    )


class EvaluationResult(BaseModel):
    """Evaluator output: the hiring decision over the interview transcript."""

    hired: bool = Field(description="Final decision: would you hire this candidate?")
    score: int = Field(description="Overall score from 0 to 100.", ge=0, le=100)
    strengths: list[str] = Field(description="The candidate's main strengths shown.")
    weaknesses: list[str] = Field(description="The candidate's main weaknesses shown.")
    rationale: str = Field(
        description="Concise reasoning behind the decision and score, in the interview language."
    )
