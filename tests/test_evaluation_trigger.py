"""The worker's evaluation trigger: retried on connect failures only.

POST /evaluate runs the evaluator INLINE, so any request that reached the API
must never be re-sent — a re-POST after a read timeout would start a second,
concurrent evaluation of the same transcript and spend the tokens twice.
"""

from __future__ import annotations

import logging
import uuid

import httpx
import pytest

from interview_agent import agent

URL = "http://api.test/api/interviews/x/evaluate"


class _Server:
    """A MockTransport handler that replays a scripted sequence of outcomes:
    an httpx exception is raised, an int is returned as that status code."""

    def __init__(self, *script: int | Exception):
        self.script = list(script)
        self.posts = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.posts += 1
        outcome = self.script.pop(0) if self.script else 200
        if isinstance(outcome, Exception):
            outcome.request = request  # httpx attaches the request lazily
            raise outcome
        return httpx.Response(outcome)


@pytest.fixture
def slept() -> list[float]:
    return []


async def _trigger(server: _Server, slept: list[float]) -> None:
    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    await agent._trigger_evaluation(
        URL, uuid.uuid4(), transport=httpx.MockTransport(server), sleep=sleep
    )


async def test_connect_error_is_retried_until_the_api_answers(slept):
    # The one failure that leaves nothing behind: nothing reached the API.
    server = _Server(httpx.ConnectError("refused"), 200)
    await _trigger(server, slept)
    assert server.posts == 2
    assert slept == [2]


async def test_connect_timeout_is_retried_too(slept):
    server = _Server(httpx.ConnectTimeout("black hole"), 200)
    await _trigger(server, slept)
    assert server.posts == 2


async def test_read_timeout_is_never_retried(slept, caplog):
    # The request got through and the evaluation is still running server-side.
    server = _Server(httpx.ReadTimeout("still evaluating"))
    with caplog.at_level(logging.WARNING, logger="interview_agent"):
        await _trigger(server, slept)
    assert server.posts == 1
    assert slept == []
    assert "probably still running" in caplog.text


async def test_an_error_response_is_not_retried(slept):
    # A 502 means the endpoint ran and marked the row evaluation_failed; the
    # frontend offers the retry, re-POSTing would only spend the tokens twice.
    server = _Server(502)
    await _trigger(server, slept)
    assert server.posts == 1
    assert slept == []


async def test_gives_up_after_the_bounded_attempts_and_says_so(slept, caplog):
    server = _Server(*[httpx.ConnectError("down")] * 10)
    with caplog.at_level(logging.ERROR, logger="interview_agent"):
        await _trigger(server, slept)
    assert server.posts == len(agent._TRIGGER_BACKOFF_SECONDS)
    # No sleep after the final attempt.
    assert slept == [b for b in agent._TRIGGER_BACKOFF_SECONDS if b]
    assert "never triggered" in caplog.text


def test_connect_side_of_the_timeout_fails_fast():
    # The read side waits for a whole inline evaluation; the connect side
    # must not, or a black-holed host burns minutes per attempt.
    assert agent._TRIGGER_TIMEOUT.connect == 5.0
    assert agent._TRIGGER_TIMEOUT.read == 300.0
