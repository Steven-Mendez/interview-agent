"""Resuming an interview after the worker died mid-conversation.

`get_token` (routes.py) deliberately re-issues a token while a conversation is
still `interviewing`, and a crash leaves it that way, so a browser reload
dispatches a SECOND job for the same conversation. These helpers are what make
that job continue the interview instead of starting a new one on top of it.
"""

from dataclasses import dataclass

from interview_agent.agent import _RESUME_MAX_MESSAGES, _chat_ctx_from_messages


@dataclass
class Row:
    """Stand-in for a db.Message row — only the fields the helpers read."""

    role: str
    content: str
    seq: int | None
    id: int = 1
    source_id: str | None = None
    metrics: dict | None = None
    interrupted: bool = False


def transcript(n: int, *, start: int = 0) -> list[Row]:
    return [
        Row(
            role="assistant" if i % 2 == 0 else "user",
            content=f"turn {i}",
            seq=i,
        )
        for i in range(start, start + n)
    ]


# --- _chat_ctx_from_messages -------------------------------------------------


def texts(ctx) -> list[tuple[str, str]]:
    return [(m.role, m.text_content) for m in ctx.messages()]


def test_first_job_gets_an_empty_context():
    assert texts(_chat_ctx_from_messages([])) == []


def test_roles_and_order_are_preserved():
    rows = [Row("assistant", "¿Me cuentas un proyecto?", 0), Row("user", "Claro.", 1)]
    assert texts(_chat_ctx_from_messages(rows)) == [
        ("assistant", "¿Me cuentas un proyecto?"),
        ("user", "Claro."),
    ]


def test_context_is_bounded_to_the_recent_tail():
    ctx = _chat_ctx_from_messages(transcript(_RESUME_MAX_MESSAGES + 10))
    assert len(ctx.messages()) == _RESUME_MAX_MESSAGES
    # The tail, not the head: the interviewer needs the latest exchange.
    assert texts(ctx)[-1][1] == f"turn {_RESUME_MAX_MESSAGES + 9}"


def test_blank_and_unknown_roles_are_dropped():
    rows = [
        Row("assistant", "  ", 0),
        Row("system", "internal note", 1),
        Row("user", "", 2),
        Row("user", "Sí.", 3),
    ]
    assert texts(_chat_ctx_from_messages(rows)) == [("user", "Sí.")]


def test_resume_preserves_capture_identity_version_and_confirmation():
    proof = {"stt_confirmed": True, "stt_turn_id": "turn", "stt_turn_version": 2}
    row = Row("user", "Corrected", 7, id=42, source_id="capture-turn-v1", metrics=proof)
    item = _chat_ctx_from_messages([row]).messages()[0]
    assert item.id == row.source_id and item.metrics == proof
    item.metrics["stt_turn_version"] = 3
    assert row.metrics["stt_turn_version"] == 2
