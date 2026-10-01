"""Unconfirmed STT text is retained as diagnostics, never candidate evidence."""

from types import SimpleNamespace

import pytest

from interview_agent.interview.evaluation_contract import (
    canonical_message_records,
    transcript_records,
    verify_evidence,
)
from interview_agent.interview.models import EvidenceRef


@pytest.mark.parametrize("confirmation", [False, None, "unknown", "true", 1, {"reason": "pending"}])
def test_unconfirmed_interim_is_absent_from_evaluation_input_and_cannot_be_cited(confirmation):
    records = canonical_message_records(
        [
            SimpleNamespace(
                id=1, role="user", content="Confirmed answer", metrics={"stt_confirmed": True}
            ),
            SimpleNamespace(
                id=2,
                role="user",
                content="Unconfirmed tail",
                metrics={"stt_confirmed": confirmation},
            ),
            SimpleNamespace(id=3, role="assistant", content="Question", metrics=None),
        ]
    )
    assert records[1]["content"] == "Unconfirmed tail" and records[1]["confirmed"] is False
    assert [r["id"] for r in transcript_records(records)] == ["1", "3"]
    verify_evidence([EvidenceRef(message_id="1", quote="Confirmed")], records)
    with pytest.raises(ValueError, match="existing candidate"):
        verify_evidence([EvidenceRef(message_id="2", quote="Unconfirmed")], records)


def test_strict_records_require_explicit_provider_confirmation_for_candidate_evidence():
    records = canonical_message_records(
        [
            SimpleNamespace(id=1, role="user", content="Unknown origin", metrics=None),
            SimpleNamespace(
                id=2, role="user", content="Provider final", metrics={"stt_confirmed": True}
            ),
            SimpleNamespace(id=3, role="assistant", content="Question", metrics=None),
        ]
    )
    assert records[0]["confirmed"] is False
    assert [record["id"] for record in transcript_records(records)] == ["2", "3"]
    verify_evidence([EvidenceRef(message_id="2", quote="Provider final")], records)
    with pytest.raises(ValueError, match="existing candidate"):
        verify_evidence([EvidenceRef(message_id="1", quote="Unknown origin")], records)


@pytest.mark.parametrize("quote", [" ", "\n", "\t"])
def test_whitespace_is_not_candidate_evidence(quote):
    records = [{"id": "1", "role": "user", "content": "I built an API.\n\t"}]
    with pytest.raises(ValueError, match="existing candidate"):
        verify_evidence([EvidenceRef(message_id="1", quote=quote)], records)
