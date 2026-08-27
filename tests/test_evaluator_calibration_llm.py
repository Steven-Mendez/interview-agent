"""Live regression for the seniority calibration. Costs real tokens, so it is
skipped unless OPENAI_API_KEY is set (CI has no key).

The unit tests prove the prompt is assembled correctly. Only this one proves
the behaviour actually changed: the SAME transcript of short-but-correct
answers must pass as a junior and fall short as a senior, and the junior run
must not list "no metrics / no trade-offs / lacked depth" as a weakness.
"""

from __future__ import annotations

import os
import re

import pytest

from interview_agent.config import Settings
from interview_agent.interview.evaluator import run_evaluator
from interview_agent.interview.models import Seniority

pytestmark = pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"), reason="needs a real OpenAI key"
)

# Short, correct, specific answers about database optimization — exactly the
# shape the old prompt punished as "lacking depth, metrics or trade-offs".
TRANSCRIPT = [
    (
        "assistant",
        "Hi, I'm Emma. A query listing a user's orders is slow. "
        "What would you look at first?",
    ),
    (
        "user",
        "I'd check whether user_id has an index, and run EXPLAIN to confirm "
        "the query is using it.",
    ),
    ("assistant", "Good. And how would you know your change actually helped?"),
    ("user", "I'd run EXPLAIN again and compare the query time before and after."),
    ("assistant", "Tell me about a bug you fixed in an API you worked on."),
    (
        "user",
        "We had an endpoint returning 500s on empty results. It assumed the "
        "query always returned a row, so I added a check and returned 404 instead.",
    ),
    ("assistant", "How do you test something like that?"),
    (
        "user",
        "A unit test for the empty case, plus one for the normal case so I "
        "don't break it.",
    ),
]

PLAN = {
    "summary": "Backend candidate with Python, FastAPI and PostgreSQL experience.",
    "focus_areas": ["SQL", "APIs"],
}

MILESTONES = [
    {
        "title": "Diagnosing a slow query",
        "description": "Probe how they approach a slow query.",
        "expected_evidence": (
            "Identifies a missing index on the filtered column and knows "
            "EXPLAIN shows it."
        ),
        "completed": True,
        "notes": "Named the index and EXPLAIN.",
    },
    {
        "title": "Debugging an API bug",
        "description": "Probe a concrete bug they fixed.",
        "expected_evidence": "Describes a concrete bug, its cause and the fix they applied.",
        "completed": True,
        "notes": "500 on empty result.",
    },
]

JOB_OFFER = (
    "Backend engineer at ACME. Our stack: Python, async FastAPI, PostgreSQL, "
    "Qdrant for our RAG pipeline, everything orchestrated on AWS."
)
RESUME = (
    "# Jane Doe\n\nBackend developer. Python, FastAPI, PostgreSQL. "
    "Worked on an internal RAG prototype using a vector database.\n"
)

# The exact vocabulary of the reported bug. Both languages: the evaluator
# writes in the interview language, so an English-only pattern would let a
# Spanish answer pass the offender check vacuously.
ABOVE_LEVEL_PATTERN = re.compile(
    r"trade[- ]?off|metric|m\u00e9trica|percentile|percentil|p95|depth|"
    r"profundidad|scalab|scaling|escalab|cost|coste|costo|brief|terse|"
    r"short answer|escueta|superficial|shallow",
    re.IGNORECASE,
)


async def _evaluate(seniority: Seniority):
    return await run_evaluator(
        Settings(),
        resume_markdown=RESUME,
        job_offer=JOB_OFFER,
        plan=PLAN,
        milestones=MILESTONES,
        transcript=TRANSCRIPT,
        ended_reason="plan_complete",
        seniority=seniority,
    )


async def test_same_answers_pass_as_junior_and_fall_short_as_senior():
    junior = await _evaluate(Seniority.JUNIOR)
    senior = await _evaluate(Seniority.SENIOR)

    # 1. The level moved the verdict in the right direction. Only the
    #    DIRECTION is asserted: across sampled runs the gap ranged 8-18 points,
    #    so any fixed magnitude here would be a flake, not a guard.
    assert junior.score > senior.score, f"junior={junior.score} senior={senior.score}"
    # 2. Answers that meet the junior bar clear the hire band at that level.
    assert junior.hired is True, junior.rationale
    assert junior.score >= 70, junior.rationale
    # 3. The level was read, not re-inferred from the stack.
    assert junior.seniority_evaluated is Seniority.JUNIOR
    assert senior.seniority_evaluated is Seniority.SENIOR
    # 4. THE bug: no above-level expectation survives as a junior weakness.
    offenders = [w for w in junior.weaknesses if ABOVE_LEVEL_PATTERN.search(w)]
    assert not offenders, f"above-level weaknesses leaked through: {offenders}"
    # 5. The discard was made explicit rather than happening silently — this is
    #    the field that keeps above-level expectations out of `weaknesses`.
    assert junior.calibration_notes, "the calibration filter left no trace"
    # 6. The filter is level-aware, not a blanket mute: the same answers still
    #    draw criticism at senior. Asserted on the COUNT rather than on the
    #    wording — the evaluator phrases senior gaps freely ("didn't say how
    #    they diagnosed it"), and matching vocabulary would test its word
    #    choice instead of its calibration.
    assert senior.weaknesses, "senior run produced no criticism at all"
    assert len(junior.weaknesses) < len(senior.weaknesses) or not junior.weaknesses
