"""Validate evaluation claims against immutable candidate evidence."""

from __future__ import annotations

from interview_agent.interview.models import (
    Assessment,
    CriterionEvaluation,
    EvaluationResult,
    Seniority,
)
from interview_agent.interview.planning_contract import evaluation_scope

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
    require_answer_revisions: bool = False,
    plan: dict | None = None,
) -> EvaluationResult:
    scope, uncovered = evaluation_scope(plan)
    if result.assessment_scope != scope or result.uncovered_requirements != uncovered:
        raise ValueError("Assessment scope and uncovered requirements must match the saved plan")
    if scope != "interview" and (result.score is not None or result.hired is not None):
        raise ValueError(
            "Focused practice or limited role coverage cannot produce a hiring verdict"
        )
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
        if scope == "practice" or m.get("essential", True)
    )
    status = (
        "insufficient"
        if not observed
        else "complete"
        if essential_observed and transcript_complete and scope != "limited"
        else "partial"
    )
    if result.evaluation_status != status:
        raise ValueError(f"Evidence coverage requires evaluation_status={status}")
    if (
        status == "complete"
        and scope == "interview"
        and (result.score is None or result.hired is None)
    ):
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
    revised = set()
    seen = set()
    for revision in result.answer_revisions:
        criterion = criteria.get(revision.milestone_id)
        if criterion is None or criterion.assessment not in (Assessment.PARTIAL, Assessment.BELOW):
            raise ValueError("Answer revisions require a criterion with a demonstrated gap")
        verify_evidence([revision.original], records)
        if not any(
            ref.message_id == revision.original.message_id
            and ref.message_version == revision.original.message_version
            for ref in criterion.evidence
        ):
            raise ValueError("The revised answer must be evidence for its linked criterion")
        index = next(i for i, m in enumerate(records) if m["id"] == revision.original.message_id)
        question = next((m for m in reversed(records[:index]) if m["role"] == "assistant"), None)
        if question is None or question["id"] != revision.question_id:
            raise ValueError("An answer revision must reference the question before its answer")
        revision.question = question["content"]
        if not revision.suggested_answer.strip() or not revision.explanation.strip():
            raise ValueError("An answer revision requires an alternative and an explanation")
        if revision.suggested_answer.strip() == revision.original.quote.strip():
            raise ValueError("An answer revision must make an actual change")
        key = (
            revision.milestone_id,
            revision.original.message_id,
            revision.original.message_version,
        )
        if key in seen:
            raise ValueError("Do not repeat an answer revision for the same criterion and answer")
        seen.add(key)
        revised.add(revision.milestone_id)
    if require_answer_revisions and any(
        c.assessment in (Assessment.PARTIAL, Assessment.BELOW) and c.milestone_id not in revised
        for c in result.criteria
    ):
        raise ValueError("Every demonstrated gap needs a concrete answer revision")
    # Coverage is arithmetic, not an LLM estimate of ability or confidence.
    return result.model_copy(update={"coverage": observed / len(expected) if expected else 0.0})


def insufficient_evaluation(
    seniority: Seniority, milestones: list[dict], language: str, plan: dict | None = None
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
        assessment_scope=evaluation_scope(plan)[0],
        uncovered_requirements=evaluation_scope(plan)[1],
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
