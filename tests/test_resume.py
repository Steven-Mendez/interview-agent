"""Resuming an interview after the worker died mid-conversation.

`get_token` (routes.py) deliberately re-issues a token while a conversation is
still `interviewing`, and a crash leaves it that way, so a browser reload
dispatches a SECOND job for the same conversation. These helpers are what make
that job continue the interview instead of starting a new one on top of it.
"""

from dataclasses import dataclass

from interview_agent.agent import (
    _RESUME_MAX_MESSAGES,
    _chat_ctx_from_messages,
    _next_seq,
)


@dataclass
class Row:
    """Stand-in for a db.Message row — only the fields the helpers read."""

    role: str
    content: str
    seq: int | None


def transcript(n: int, *, start: int = 0) -> list[Row]:
    return [
        Row(
            role="assistant" if i % 2 == 0 else "user",
            content=f"turn {i}",
            seq=i,
        )
        for i in range(start, start + n)
    ]


# --- _next_seq ---------------------------------------------------------------


def test_first_job_starts_at_zero():
    assert _next_seq([]) == 0


def test_resumed_job_continues_past_the_highest_seq():
    assert _next_seq(transcript(6)) == 6


def test_continues_from_the_max_not_the_count():
    # A gap (a persist that failed) must not push the next job onto a used seq.
    rows = [Row("assistant", "a", 0), Row("user", "b", 1), Row("assistant", "c", 7)]
    assert _next_seq(rows) == 8


def test_legacy_rows_without_seq_are_skipped():
    rows = [Row("assistant", "a", None), Row("user", "b", None), Row("assistant", "c", 2)]
    assert _next_seq(rows) == 3


def test_all_rows_without_seq_falls_back_to_zero():
    # max() over an empty generator would raise; default=-1 keeps it total.
    assert _next_seq([Row("assistant", "a", None)]) == 0


def test_second_job_transcript_does_not_interleave():
    # The bug: two runs of 0,1,2… sort as 0,0,1,1,2,2 under order_by(seq, id).
    first = transcript(3)
    second = transcript(3, start=_next_seq(first))
    assert [r.seq for r in first + second] == [0, 1, 2, 3, 4, 5]


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
