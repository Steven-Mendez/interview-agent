"""Pure-logic tests for the evaluator's prompt formatting helpers."""

from interview_agent.interview.evaluator import _format_milestones, _format_transcript


def test_format_transcript_maps_roles_to_labels():
    out = _format_transcript(
        [("assistant", "Tell me about X."), ("user", "I built X at my last job.")]
    )
    assert out == ("Interviewer: Tell me about X.\nCandidate: I built X at my last job.")


def test_format_transcript_passes_unknown_roles_through():
    assert _format_transcript([("system", "hi")]) == "system: hi"


def test_format_milestones_checkboxes_and_notes():
    out = _format_milestones(
        [
            {"title": "K8s", "description": "Probe it.", "completed": True, "notes": "solid"},
            {"title": "SQL", "description": "Ask joins.", "completed": False, "notes": None},
        ]
    )
    lines = out.splitlines()
    assert lines[0] == "- [x] K8s: Probe it. (notes: solid)"
    assert lines[1] == "- [ ] SQL: Ask joins."  # no notes suffix when empty


def test_format_milestones_carries_the_expected_evidence_bar():
    """The bar travels to the evaluator so it judges against a written
    criterion instead of re-deriving how deep the topic should go."""
    out = _format_milestones(
        [
            {
                "title": "Indexes",
                "description": "Probe it.",
                "expected_evidence": "Names the missing index",
                "completed": True,
                "notes": "solid",
            },
            # Legacy row planned before the column existed.
            {"title": "SQL", "description": "Ask joins.", "completed": False, "notes": None},
        ]
    )
    lines = out.splitlines()
    assert lines[0] == (
        "- [x] Indexes: Probe it. (passes when: Names the missing index) (notes: solid)"
    )
    assert lines[1] == "- [ ] SQL: Ask joins."
