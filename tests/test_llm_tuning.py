"""Tests for the GPT-5-vs-legacy model tuning switch."""

from unittest.mock import Mock

import httpx
import pytest
from openai import APIStatusError

from interview_agent.config import Settings
from interview_agent.llm import build_chat_model, chat_model_tuning, close_chat_model
from interview_agent.observability import LLMObserver


def test_gpt5_family_gets_reasoning_not_temperature():
    tuning = chat_model_tuning("gpt-5.4-mini", reasoning_effort="none", temperature=0.7)
    assert tuning == {"reasoning": {"effort": "none"}}


def test_pre_gpt5_gets_temperature_not_reasoning():
    tuning = chat_model_tuning("gpt-4o", reasoning_effort="high", temperature=0.3)
    assert tuning == {"temperature": 0.3}


def test_never_both_keys():
    for model in ("gpt-5.5", "gpt-5.4-mini", "gpt-4o", "gpt-4.1-mini"):
        tuning = chat_model_tuning(model, reasoning_effort="low", temperature=1.0)
        assert not ({"reasoning", "temperature"} <= tuning.keys())
        assert len(tuning) == 1


async def test_interviewer_sdk_performs_only_two_http_attempts(monkeypatch):
    requests = []

    async def unavailable(request):
        requests.append(request)
        return httpx.Response(
            503, json={"error": {"message": "Synthetic outage", "type": "server_error"}}
        )

    monkeypatch.setattr(
        "interview_agent.llm.httpx.AsyncHTTPTransport", lambda: httpx.MockTransport(unavailable)
    )
    telemetry = Mock()
    observer = LLMObserver(telemetry, "interviewer", "gpt-6-astra", "low", "turn")
    model = build_chat_model(
        Settings(_env_file=None, OPENAI_API_KEY="synthetic-test-key"),
        model="gpt-6-astra",
        reasoning_effort="low",
        max_retries=1,
        timeout_seconds=15,
        telemetry_callback=observer,
    )
    client = model.root_async_client
    monkeypatch.setattr(client, "_calculate_retry_timeout", lambda *args: 0)
    try:
        with pytest.raises(APIStatusError):
            await client.responses.create(model="gpt-6-astra", input="Synthetic interview question")
        assert len(requests) == 2
        assert [request.headers["x-stainless-retry-count"] for request in requests] == ["0", "1"]
        assert (
            len([call for call in telemetry.emit.call_args_list if call.args[1] == "http_requests"])
            == 2
        )
        assert (
            len([call for call in telemetry.emit.call_args_list if call.args[1] == "http_retries"])
            == 1
        )
    finally:
        await close_chat_model(model)


def test_structured_output_references_have_no_unsupported_siblings():
    from openai.lib._pydantic import to_strict_json_schema

    from interview_agent.interview.models import EvaluationResult, InterviewPlan, TurnDecision

    def check(node):
        if isinstance(node, dict):
            if "$ref" in node:
                assert set(node) == {"$ref"}, node
            for child in node.values():
                check(child)
        elif isinstance(node, list):
            for child in node:
                check(child)

    for schema in (EvaluationResult, InterviewPlan, TurnDecision):
        check(to_strict_json_schema(schema))


async def test_closing_one_model_does_not_poison_the_next_transport():
    settings = Settings(_env_file=None, OPENAI_API_KEY="synthetic-test-key")
    first = build_chat_model(settings, model="gpt-6-astra", reasoning_effort="low")
    await close_chat_model(first)
    second = build_chat_model(settings, model="gpt-6-astra", reasoning_effort="low")
    try:
        assert first.http_async_client.is_closed
        assert second.http_async_client is not first.http_async_client
        assert not second.http_async_client.is_closed
    finally:
        await close_chat_model(second)
    assert second.http_async_client.is_closed
