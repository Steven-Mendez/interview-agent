"""The seniority/length calibration: profile table, prompt assembly and the
follow-up budget.

These are the guards for the "implicit complexity bias" fix — with no explicit
level the model inferred difficulty from how advanced the tech stack sounded,
asked senior-depth questions of a junior, and then penalized correct, concise
answers for "lacking depth, metrics or trade-offs".
"""

from __future__ import annotations

import pytest

from interview_agent.interview.models import InterviewLength, Seniority
from interview_agent.prompts import (
    DEFAULT_SENIORITY,
    LENGTH_PROFILE,
    SENIORITY_CALIBRATION,
    build_calibration_block,
    build_evaluator_prompt,
    build_interviewer_prompt,
    build_planner_prompt,
    followup_budget,
    length_for,
    profile_for,
)


def _flat(text: str) -> str:
    """Collapse whitespace: assertions should track the wording of a prompt,
    not how it happens to be wrapped in the source."""
    return " ".join(text.split())


class _Conv:
    """Minimal stand-in for db.Conversation: the prompt builder only reads
    attributes, so a real row (and a database) is unnecessary here."""

    def __init__(self, seniority="mid", interview_length="standard", plan=None):
        self.seniority = seniority
        self.interview_length = interview_length
        self.plan = plan if plan is not None else {"language": "es", "persona": "Laura"}
        self.custom_instructions = None


class _MS:
    def __init__(self, position, title, description, expected_evidence=None):
        self.position = position
        self.title = title
        self.description = description
        self.expected_evidence = expected_evidence
        self.completed = False


# ---- The profile table ------------------------------------------------------


def test_every_level_has_a_complete_profile():
    assert set(SENIORITY_CALIBRATION) == set(Seniority)
    for level, p in SENIORITY_CALIBRATION.items():
        for field in (
            "label",
            "question_scope",
            "expected_evidence",
            "out_of_scope",
            "answer_shape",
            "pass_bar",
        ):
            value = getattr(p, field)
            # A stray trailing comma turns one of these into a tuple and the
            # prompt silently renders "('...',)" at the model.
            assert isinstance(value, str), f"{level}.{field} is {type(value).__name__}"
            assert value.strip(), f"{level}.{field} is empty"


@pytest.mark.parametrize("level", [Seniority.TRAINEE, Seniority.JUNIOR])
def test_junior_levels_put_metrics_and_tradeoffs_out_of_scope(level):
    """The exact expectations the evaluator used to punish juniors for."""
    out_of_scope = SENIORITY_CALIBRATION[level].out_of_scope
    assert "trade-off" in _flat(out_of_scope)
    assert "cost" in out_of_scope
    assert "scale" in out_of_scope or "scaling" in out_of_scope


def test_senior_expects_what_junior_does_not():
    senior = SENIORITY_CALIBRATION[Seniority.SENIOR].expected_evidence
    assert "two viable options" in _flat(senior)
    assert "trade-off" not in _flat(
        SENIORITY_CALIBRATION[Seniority.JUNIOR].expected_evidence
    )


def test_profile_and_length_lookups_fall_back_instead_of_raising():
    """Legacy rows carry NULL; a bad value must not 500 an interview."""
    assert profile_for(None) is SENIORITY_CALIBRATION[DEFAULT_SENIORITY]
    assert profile_for("archmage") is SENIORITY_CALIBRATION[DEFAULT_SENIORITY]
    assert profile_for("junior") is SENIORITY_CALIBRATION[Seniority.JUNIOR]
    assert length_for(None) == LENGTH_PROFILE[InterviewLength.STANDARD]
    assert length_for("epic") == LENGTH_PROFILE[InterviewLength.STANDARD]


# ---- Follow-up budget: the two axes compose, the stricter wins --------------


def test_followup_budget_takes_the_stricter_axis():
    # A deep interview covers more ground; it never turns a junior
    # conversation into a senior one.
    assert followup_budget(Seniority.JUNIOR, InterviewLength.DEEP) == 1
    assert followup_budget(Seniority.TRAINEE, InterviewLength.DEEP) == 0
    # A short senior screen stays short.
    assert followup_budget(Seniority.SENIOR, InterviewLength.SHORT) == 0
    assert followup_budget(Seniority.SENIOR, InterviewLength.DEEP) == 2


# ---- Planner prompt ---------------------------------------------------------


def test_planner_prompt_pins_an_explicit_level_and_forbids_stack_inference():
    prompt = _flat(build_planner_prompt(Seniority.JUNIOR, InterviewLength.STANDARD))
    assert SENIORITY_CALIBRATION[Seniority.JUNIOR].label in prompt
    assert "AUTHORITATIVE" in prompt
    assert "describes the team's stack" in prompt
    # Given a level, it must NOT be asked to classify one.
    assert "CLASSIFY IT FIRST" not in prompt


def test_planner_prompt_asks_for_classification_only_on_auto():
    prompt = _flat(build_planner_prompt(None, InterviewLength.STANDARD))
    assert "CLASSIFY IT FIRST" in prompt
    assert "detected_seniority" in prompt
    # The two instructions that attack the bias at its source.
    assert "Do NOT classify by the technologies mentioned" in prompt
    assert "ALWAYS pick the lower one" in prompt


@pytest.mark.parametrize(
    ("length", "lo", "hi", "minutes"),
    [("short", 3, 4, 8), ("standard", 4, 6, 15), ("deep", 6, 8, 25)],
)
def test_planner_prompt_carries_the_length_axis(length, lo, hi, minutes):
    prompt = _flat(build_planner_prompt(Seniority.MID, length))
    assert f"between {lo} and {hi} milestones" in prompt
    assert f"about {minutes} minutes" in prompt


def test_planner_prompt_always_demands_a_per_milestone_bar():
    for seniority in (None, Seniority.SENIOR):
        assert "expected_evidence" in build_planner_prompt(seniority, "standard")


# ---- Interviewer prompt -----------------------------------------------------


def test_interviewer_prompt_injects_the_level_ceiling():
    junior = _flat(build_interviewer_prompt(_Conv(seniority="junior"), [], 15))
    assert _flat(SENIORITY_CALIBRATION[Seniority.JUNIOR].out_of_scope) in junior
    assert "NEVER ask about" in junior
    # The rewritten probing rule: brevity must stop reading as vagueness.
    assert "Brevity is not vagueness" in junior
    # And the anti-ratchet rule.
    assert "do NOT raise the difficulty of later questions" in junior

    senior = _flat(build_interviewer_prompt(_Conv(seniority="senior"), [], 15))
    assert _flat(SENIORITY_CALIBRATION[Seniority.JUNIOR].out_of_scope) not in senior


def test_interviewer_prompt_states_the_follow_up_budget():
    trainee = _flat(
        build_interviewer_prompt(_Conv(seniority="trainee", interview_length="deep"), [], 15)
    )
    assert "NO follow-up budget" in trainee

    senior = _flat(
        build_interviewer_prompt(_Conv(seniority="senior", interview_length="deep"), [], 25)
    )
    assert "budget of 2 follow-up(s)" in senior


def test_interviewer_prompt_carries_each_milestone_bar():
    milestones = [
        _MS(0, "Indexes", "Probe it.", "Names the missing index on the filter column"),
        _MS(1, "Legacy", "Probe it.", None),
    ]
    prompt = build_interviewer_prompt(_Conv(seniority="junior"), milestones, 15)
    assert "1. Indexes: Probe it. (passes when: Names the missing index" in prompt
    # A legacy milestone with no bar renders cleanly, with no dangling suffix.
    assert "2. Legacy: Probe it.\n" in prompt


def test_interviewer_prompt_tolerates_a_legacy_row_without_a_level():
    prompt = _flat(
        build_interviewer_prompt(_Conv(seniority=None, interview_length=None), [], 15)
    )
    assert SENIORITY_CALIBRATION[DEFAULT_SENIORITY].label in prompt


# ---- Evaluator prompt -------------------------------------------------------


def test_evaluator_prompt_scores_relative_to_the_level():
    prompt = _flat(build_evaluator_prompt(Seniority.JUNIOR))
    label = SENIORITY_CALIBRATION[Seniority.JUNIOR].label
    assert f"how well did they do FOR A {label}?" in prompt
    assert "not exceptional in absolute terms" in prompt
    assert "it never raises the bar" in prompt


def test_evaluator_prompt_carries_the_weakness_filter():
    prompt = _flat(build_evaluator_prompt(Seniority.JUNIOR))
    # The four steps that stop above-level expectations becoming weaknesses.
    assert "Run every weakness through this filter" in prompt
    assert "DISCARD it" in prompt
    assert "Length is NOT evidence" in prompt
    assert "calibration_notes" in prompt
    # And the anchor to the written bar rather than the model's own idea.
    assert "Judge each milestone against ITS OWN `expected_evidence`" in prompt


def test_evaluator_prompt_lists_what_not_to_penalize():
    prompt = _flat(build_evaluator_prompt(Seniority.JUNIOR))
    assert _flat(SENIORITY_CALIBRATION[Seniority.JUNIOR].out_of_scope) in prompt
    assert _flat(SENIORITY_CALIBRATION[Seniority.JUNIOR].pass_bar) in prompt


def test_calibration_block_renders_every_level_in_every_framing():
    for level in Seniority:
        for audience in ("planner", "interviewer", "evaluator"):
            block = _flat(build_calibration_block(level, audience))
            assert SENIORITY_CALIBRATION[level].label in block
            assert "{" not in block  # no unsubstituted placeholders
