"""Pydantic schemas for the planner and evaluator structured outputs."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Seniority(StrEnum):
    """Expected level of the role, the axis that calibrates DEPTH.

    Pinned once per conversation (explicitly by the user or classified once by
    the planner) and read — never re-inferred — by the interviewer and the
    evaluator. Re-inferring it at each stage is exactly what produced the
    "advanced stack therefore senior" bias.
    """

    TRAINEE = "trainee"
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    LEAD = "lead"


class InterviewLength(StrEnum):
    """How much interview to run, the axis that calibrates VOLUME.

    Independent from seniority: a short senior screen and a long junior
    practice run are both legitimate.
    """

    SHORT = "short"
    STANDARD = "standard"
    DEEP = "deep"


class MilestoneSpec(BaseModel):
    """One interview milestone the interviewer must cover."""

    title: str = Field(description="Short milestone name, e.g. 'Kubernetes experience'.")
    description: str = Field(
        description="What the interviewer should probe and what counts as covered."
    )
    expected_evidence: str = Field(
        description=(
            "The BAR for this milestone at the role's seniority: one sentence "
            "stating the minimum a candidate must say for it to count as "
            "covered. Write the passing threshold, not the ideal answer."
        )
    )


class InterviewPlan(BaseModel):
    """Planner output: how the interview should be conducted."""

    persona: str = Field(
        description=(
            "The interviewer's persona: name, role and interviewing style, "
            "e.g. 'Laura, engineering manager, warm but rigorous'."
        )
    )
    summary: str = Field(
        description="2-3 sentence summary of the candidate/role fit to guide the interview."
    )
    focus_areas: list[str] = Field(
        description="Key areas to emphasize given gaps or strengths in the resume."
    )
    # Only filled when the caller asked for automatic classification; when the
    # level was given explicitly the planner is told the answer and these stay
    # empty. The server pins whichever value wins.
    detected_seniority: Seniority | None = Field(
        default=None,
        description=(
            "Only when the seniority was NOT given: the level you classified "
            "the role as, from the offer's stated level and responsibilities."
        ),
    )
    seniority_evidence: str | None = Field(
        default=None,
        description=(
            "Only when you classified the seniority: the concrete phrase from "
            "the job offer (or resume) that justifies it."
        ),
    )
    # Hard ceiling only: the exact range is imposed per interview_length by the
    # prompt, so the schema must not fight it.
    milestones: list[MilestoneSpec] = Field(
        description="Ordered milestones, as many as the prompt asks for.",
        min_length=3,
        max_length=8,
    )


class EvaluationResult(BaseModel):
    """Evaluator output: the hiring decision over the interview transcript."""

    hired: bool = Field(description="Final decision: would you hire this candidate?")
    score: int = Field(
        description="Overall score from 0 to 100, RELATIVE to the bar for the role's level.",
        ge=0,
        le=100,
    )
    strengths: list[str] = Field(description="The candidate's main strengths shown.")
    weaknesses: list[str] = Field(description="The candidate's main weaknesses shown.")
    rationale: str = Field(
        description="Concise reasoning behind the decision and score, in the interview language."
    )
    seniority_evaluated: Seniority = Field(
        description="The seniority level you judged this interview against."
    )
    calibration_notes: list[str] = Field(
        default_factory=list,
        description=(
            "Expectations you considered but DISCARDED for being above the "
            "role's level. Making the discard explicit here is what keeps it "
            "out of `weaknesses`."
        ),
    )
