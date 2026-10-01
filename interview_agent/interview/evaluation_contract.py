"""Validate evaluation claims against immutable candidate evidence."""

from __future__ import annotations

from interview_agent.interview.models import (
    Assessment,
    CriterionEvaluation,
    EvaluationResult,
    Seniority,
)

# Highest score of each rubric band (90-100, 70-89, 40-69, 0-39).
BAND_TOPS = frozenset({100, 89, 69, 39})


def canonical_message_records(messages: list) -> list[dict]:
    records = []
    for message in messages:
        record = {"id": str(message.id), "role": message.role, "content": message.content}
        if getattr(message, "version", None) is not None:
            record["version"] = message.version
        metrics = getattr(message, "metrics", None) or {}
        # Candidate text counts only once the voice pipeline confirmed it.
        if metrics.get("stt_confirmed", message.role != "user") is not True:
            record["confirmed"] = False
        records.append(record)
    return records


def transcript_records(transcript: list[dict]) -> list[dict]:
    return [
        {
            "id": str(item["id"]),
            "role": item["role"],
            "content": item["content"],
            **({"version": item["version"]} if "version" in item else {}),
        }
        for item in transcript
        if not (item["role"] == "user" and item.get("confirmed") is False)
    ]


def verify_evidence(refs, records: list[dict]) -> None:
    candidate = {
        m["id"]: m for m in records if m["role"] == "user" and m.get("confirmed") is not False
    }
    for ref in refs:
        if (
            not ref.quote.strip()
            or ref.message_id not in candidate
            or ref.quote not in candidate[ref.message_id]["content"]
            or ref.message_version != candidate[ref.message_id].get("version")
        ):
            raise ValueError("Evidence must quote an existing candidate message verbatim")


def validate_evaluation(
    result: EvaluationResult,
    *,
    seniority: Seniority,
    milestones: list[dict],
    records: list[dict],
    transcript_complete: bool = True,
) -> EvaluationResult:
    if result.seniority_evaluated != seniority:
        raise ValueError("Evaluation seniority differs from the pinned role level")
    expected = {str(m["id"]): m for m in milestones}
    criteria = {c.milestone_id: c for c in result.criteria}
    if len(criteria) != len(result.criteria) or set(criteria) != set(expected):
        raise ValueError("Evaluate each supplied milestone exactly once")
    for criterion in result.criteria:
        verify_evidence(criterion.evidence, records)
        if criterion.assessment != Assessment.NOT_ASSESSABLE and not criterion.evidence:
            raise ValueError("An assessed criterion requires candidate evidence")
        if criterion.assessment == Assessment.NOT_ASSESSABLE and criterion.evidence:
            raise ValueError("Unobserved criteria cannot carry performance evidence")
    observed = sum(c.assessment != Assessment.NOT_ASSESSABLE for c in result.criteria)
    essential_observed = all(
        criteria[k].assessment != Assessment.NOT_ASSESSABLE
        for k, m in expected.items()
        if m.get("essential", True)
    )
    status = (
        "insufficient"
        if not observed
        else "complete"
        if essential_observed and transcript_complete
        else "partial"
    )
    if result.evaluation_status != status:
        raise ValueError(f"Evidence coverage requires evaluation_status={status}")
    if status == "complete" and (result.score is None or result.hired is None):
        raise ValueError("A complete evaluation requires score and verdict")
    if result.hired is True and (result.score is None or result.score < 70):
        raise ValueError("A positive hiring simulation requires a score of at least 70")
    if result.score is not None and result.score not in BAND_TOPS and not result.score_gap.strip():
        raise ValueError(
            "A score below the top of its band must explain the missing points in score_gap"
        )
    assessed = [c.assessment for c in result.criteria if c.assessment != Assessment.NOT_ASSESSABLE]
    if (
        result.score is not None
        and result.score >= 90
        and (
            any(a in (Assessment.PARTIAL, Assessment.BELOW) for a in assessed)
            or 2 * assessed.count(Assessment.EXCEEDS) < len(assessed)
        )
    ):
        # The rubric's top band is "above the level's bar", which only the
        # per-criterion evidence can establish.
        raise ValueError(
            "A score of 90 or more requires exceeds on at least half of the assessed "
            "criteria and none partial or below"
        )
    for finding in result.findings:
        if finding.milestone_id not in criteria or not finding.evidence:
            raise ValueError("Every finding requires a supplied criterion and evidence")
        verify_evidence(finding.evidence, records)
        assessment = criteria[finding.milestone_id].assessment
        if assessment == Assessment.NOT_ASSESSABLE:
            raise ValueError("Unobserved criteria cannot support strengths or weaknesses")
        if finding.kind == "weakness" and assessment in (Assessment.MEETS, Assessment.EXCEEDS):
            raise ValueError(
                "A criterion that meets its bar cannot support an unmet-expectation weakness"
            )
    for kind, texts in (("strength", result.strengths), ("weakness", result.weaknesses)):
        if set(texts) != {f.text for f in result.findings if f.kind == kind}:
            raise ValueError("Strengths and weaknesses must match evidence-backed findings")
    # Coverage is arithmetic, not an LLM estimate of ability or confidence.
    return result.model_copy(update={"coverage": observed / len(expected) if expected else 0.0})


def insufficient_evaluation(
    seniority: Seniority, milestones: list[dict], language: str
) -> EvaluationResult:
    reason = (
        "No hay respuestas evaluables del candidato."
        if language == "es"
        else "No assessable candidate answers were recorded."
    )
    return EvaluationResult(
        hired=None,
        score=None,
        strengths=[],
        weaknesses=[],
        rationale=reason,
        seniority_evaluated=seniority,
        evaluation_status="insufficient",
        criteria=[
            CriterionEvaluation(
                milestone_id=str(m["id"]),
                assessment=Assessment.NOT_ASSESSABLE,
                evidence=[],
                rationale=reason,
                practice="",
            )
            for m in milestones
        ],
    )
