"""Pydantic schemas for the planner and evaluator structured outputs."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


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
    essential: bool = Field(default=True, description="Required to make an overall assessment.")
    competency: str = Field(
        default="", description="Competency assessed, independent of topic count."
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
        min_length=1,
        # 12 = the typed primary-question ceiling; length profiles stay lower.
        max_length=12,
    )


class Assessment(StrEnum):
    # Above the criterion's own bar, with evidence beyond its expected_evidence.
    EXCEEDS = "exceeds"
    MEETS = "meets"
    PARTIAL = "partial"
    BELOW = "below"
    NOT_ASSESSABLE = "not_assessable"


class EvidenceRef(BaseModel):
    message_id: str = Field(description="Exact candidate message ID from the supplied transcript.")
    message_version: int | None = Field(
        default=None,
        ge=1,
        description="Exact supplied version of the cited candidate message.",
    )
    quote: str = Field(min_length=1, max_length=1200, description="Verbatim candidate evidence.")


class CriterionEvaluation(BaseModel):
    milestone_id: str
    assessment: Assessment
    evidence: list[EvidenceRef]
    rationale: str
    practice: str = Field(
        description="One specific next practice exercise, or empty if unnecessary."
    )


class FeedbackFinding(BaseModel):
    kind: str = Field(pattern="^(strength|weakness)$")
    text: str
    milestone_id: str
    evidence: list[EvidenceRef]


class EvaluationResult(BaseModel):
    """Evaluator output: the hiring decision over the interview transcript."""

    hired: bool | None = Field(description="Hiring simulation; null unless assessment is complete.")
    score: int | None = Field(
        description="Overall score from 0 to 100, RELATIVE to the bar for the role's level.",
        ge=0,
        le=100,
    )
    strengths: list[str] = Field(description="The candidate's main strengths shown.")
    weaknesses: list[str] = Field(description="The candidate's main weaknesses shown.")
    rationale: str = Field(
        description="Concise reasoning behind the decision and score, in the interview language."
    )
    score_gap: str = Field(
        default="",
        description=(
            "When the score is below the top of its band (100, 89, 69 or 39): what "
            "specifically kept it from the top, citing the answers, in the interview "
            "language. Empty only at the top of a band or without a score."
        ),
    )
    # Keep enum references bare: the provider rejects description beside $ref.
    seniority_evaluated: Seniority
    calibration_notes: list[str] = Field(
        default_factory=list,
        description=(
            "Expectations you considered but DISCARDED for being above the "
            "role's level. Making the discard explicit here is what keeps it "
            "out of `weaknesses`."
        ),
    )
    evaluation_status: str = Field(default="complete", pattern="^(complete|partial|insufficient)$")
    criteria: list[CriterionEvaluation] = Field(default_factory=list)
    findings: list[FeedbackFinding] = Field(default_factory=list)
    coverage: float = Field(
        default=0.0, ge=0, le=1, description="Fraction of criteria observed, not ability."
    )

    @model_validator(mode="after")
    def _no_verdict_without_complete_assessment(self) -> EvaluationResult:
        if self.evaluation_status != "complete" and (
            self.score is not None or self.hired is not None
        ):
            raise ValueError(
                "Partial or insufficient evidence cannot produce an overall score/verdict"
            )
        return self


class MilestoneUpdate(BaseModel):
    milestone_id: str
    status: str = Field(pattern="^(active|closed|skipped)$")
    close_reason: str | None = Field(
        default=None,
        description=(
            "covered, budget_exhausted, candidate_declined, time_limit; closed is not passed."
        ),
    )
    evidence: list[EvidenceRef] = Field(default_factory=list)


class TurnDecision(BaseModel):
    action: str = Field(pattern="^(question|clarification|followup|advance|close)$")
    spoken_text: str = Field(
        max_length=700, description="One natural question, at most 50 words; empty for close."
    )
    target_milestone_id: str | None = None
    updates: list[MilestoneUpdate] = Field(default_factory=list)
    close_reason: str | None = Field(
        default=None,
        description="plan_complete, plan_exhausted, timeout, candidate_requested, question_limit",
    )
    closure_evidence: list[EvidenceRef] = Field(default_factory=list)
