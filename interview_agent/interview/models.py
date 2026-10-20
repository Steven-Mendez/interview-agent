"""Pydantic schemas for the planner and evaluator structured outputs."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


class InterviewType(StrEnum):
    TECHNICAL = "technical"
    BEHAVIORAL = "behavioral"
    MIXED = "mixed"


class InterviewObjective(StrEnum):
    SIMULATION = "simulation"
    PRACTICE = "practice"


class InterviewPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interview_type: InterviewType = InterviewType.TECHNICAL
    objective: InterviewObjective = InterviewObjective.SIMULATION
    practice_focus: str = Field(default="", max_length=1000)

    @field_validator("practice_focus")
    @classmethod
    def strip_focus(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def focus_requires_practice(self) -> InterviewPreferences:
        if self.practice_focus and self.objective != InterviewObjective.PRACTICE:
            raise ValueError("A practice focus requires the practice objective")
        return self


class InterviewBudget(BaseModel):
    max_minutes: int = Field(ge=1, le=25)
    question_limit: int = Field(ge=1, le=12)
    followup_limit: int = Field(ge=0, le=2)


class RoleRequirement(BaseModel):
    id: str = Field(min_length=1)
    competency: str = Field(min_length=1)
    kind: str = Field(pattern="^(technical|behavioral)$")
    job_quote: str = Field(min_length=1, description="Verbatim supporting job-offer excerpt.")
    relevance: str = Field(
        min_length=1, description="How the quoted duty requires this competency."
    )
    expected_evidence: str = Field(
        min_length=1,
        description="Minimum observable passing evidence; accept equivalent valid approaches.",
    )
    essential: bool


class RoleRubric(BaseModel):
    seniority: Seniority
    seniority_evidence: str
    requirements: list[RoleRequirement] = Field(min_length=1, max_length=12)


class PlannedTopic(BaseModel):
    requirement_id: str
    title: str
    description: str = Field(
        description="Neutral scenario and question goal, without the solution."
    )
    resume_quotes: list[str] = Field(
        description="Verbatim CV references, or [] for a hypothetical."
    )


class OmittedRequirement(BaseModel):
    requirement_id: str
    reason: str = Field(pattern="^(time_budget|outside_focus|interview_type)$")
    explanation: str = Field(min_length=1)


class PersonalizedPlan(BaseModel):
    persona: str
    summary: str
    focus_areas: list[str]
    topics: list[PlannedTopic] = Field(min_length=1, max_length=12)
    omitted_requirements: list[OmittedRequirement]


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
    requirement_id: str | None = None
    resume_quotes: list[str] = Field(default_factory=list)


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
            "the job offer that justifies it."
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
    requirements: list[RoleRequirement] = Field(default_factory=list)
    omitted_requirements: list[OmittedRequirement] = Field(default_factory=list)
    preferences: InterviewPreferences = Field(default_factory=InterviewPreferences)
    budget: InterviewBudget | None = None


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


class AnswerRevision(BaseModel):
    milestone_id: str
    question_id: str = Field(description="ID of the question immediately before this answer.")
    question: str = Field(default="", description="Original question, filled from the transcript.")
    original: EvidenceRef
    suggested_answer: str = Field(
        min_length=1,
        max_length=1800,
        description="A concise answer the candidate could practise, grounded in known facts.",
    )
    explanation: str = Field(
        min_length=1,
        description="The specific change and why it addresses the criterion's actual gap.",
    )
    basis: str = Field(
        pattern="^(clarification|knowledge_example)$",
        description=(
            "clarification reorganizes facts already supplied by the candidate; "
            "knowledge_example teaches a missing or incorrect technical concept."
        ),
    )
    missing_details: list[str] = Field(
        description=(
            "Concrete facts the candidate must supply or verify before using this answer. "
            "Never fabricate achievements, numbers, experience or outcomes. Empty if none."
        ),
    )


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
    answer_revisions: list[AnswerRevision] = Field(default_factory=list)
    assessment_scope: str = Field(default="interview", pattern="^(interview|practice|limited)$")
    uncovered_requirements: list[str] = Field(default_factory=list)
    coverage: float = Field(
        default=0.0, ge=0, le=1, description="Fraction of criteria observed, not ability."
    )

    @model_validator(mode="after")
    def _no_verdict_without_complete_assessment(self) -> EvaluationResult:
        if (self.evaluation_status != "complete" or self.assessment_scope != "interview") and (
            self.score is not None or self.hired is not None
        ):
            raise ValueError(
                "Partial, insufficient or scoped practice cannot produce an overall score/verdict"
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
